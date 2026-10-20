from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from .conftest import OCT1, PLAN_START, ingest, sample

D = Decimal


@pytest.fixture
def october_usage(client, tenant):
    """m1.small 10h (25.00) + 100 GB volume 10h (0.20) + 1 FIP 10h (free) + 150 GB egress (400)."""
    ingest(
        client,
        [
            sample("c", "compute.instance.m1.small", 1, OCT1, hours=10),
            sample("v", "storage.volume.gb", 100, OCT1, hours=10, resource_id="vol"),
            sample("f", "network.floating_ip", 1, OCT1, hours=10, resource_id="fip"),
            sample("e", "network.bandwidth.egress.gb", 150, OCT1, hours=10, resource_id="router"),
        ],
    )
    return tenant


def invoice(client, period="2026-10"):
    r = client.post("/v1/tenants/acme/invoices", params={"period": period})
    assert r.status_code == 201, r.text
    return r.json()


def test_invoice_totals_equal_sum_of_lines(client, october_usage):
    inv = invoice(client)
    assert inv["status"] == "draft"
    assert inv["currency"] == "BDT"
    assert inv["number"].startswith("INV-202610-")
    amounts = {ln["metric"]: D(ln["amount"]) for ln in inv["lines"]}
    assert amounts == {
        "compute.instance.m1.small": D("25.00"),
        "storage.volume.gb": D("0.20"),
        "network.floating_ip": D("0.00"),
        "network.bandwidth.egress.gb": D("400.00"),
    }
    assert D(inv["subtotal"]) == sum(amounts.values()) == D("425.20")
    assert D(inv["total"]) == D("425.20")
    egress = next(ln for ln in inv["lines"] if ln["metric"] == "network.bandwidth.egress.gb")
    assert D(egress["free_quantity"]) == 100 and D(egress["unit_price"]) == D("8")
    assert "100.00 free" in egress["description"]


def test_percentage_discount_whole_invoice_and_per_metric(client, october_usage):
    client.post(
        "/v1/tenants/acme/discounts",
        json={"percent": "10", "description": "Launch promo", "valid_from": PLAN_START.isoformat()},
    )
    client.post(
        "/v1/tenants/acme/discounts",
        json={
            "percent": "50",
            "metric": "network.bandwidth.*",
            "valid_from": PLAN_START.isoformat(),
        },
    )
    inv = invoice(client)
    # 10% of 425.20 = 42.52 ; 50% of 400.00 = 200.00
    assert D(inv["discount_total"]) == D("242.52")
    assert D(inv["total"]) == D("182.68")
    assert {d["description"]: d["amount"] for d in inv["discounts_applied"]} == {
        "Launch promo": "42.52",
        "50% discount": "200.00",
    }


def test_expired_discount_is_ignored(client, october_usage):
    client.post(
        "/v1/tenants/acme/discounts",
        json={
            "percent": "10",
            "valid_from": PLAN_START.isoformat(),
            "valid_to": (OCT1 - timedelta(days=1)).isoformat(),
        },
    )
    assert D(invoice(client)["discount_total"]) == 0


def test_credits_are_projected_on_draft_and_consumed_on_finalize(client, october_usage):
    client.post("/v1/tenants/acme/credits", json={"amount": "100", "reason": "SLA breach"})
    inv = invoice(client)
    assert D(inv["credit_total"]) == D("100.00")
    assert D(inv["total"]) == D("325.20")
    # Draft does not consume anything; regenerating gives the same result.
    assert D(client.get("/v1/tenants/acme/credits").json()[0]["remaining"]) == 100
    again = invoice(client)
    assert again["id"] == inv["id"] and D(again["total"]) == D("325.20")

    final = client.post(f"/v1/invoices/{inv['id']}/finalize").json()
    assert final["status"] == "finalized" and final["finalized_at"] is not None
    assert D(client.get("/v1/tenants/acme/credits").json()[0]["remaining"]) == 0
    assert final["credits_applied"][0]["reason"] == "SLA breach"


def test_credit_larger_than_invoice_leaves_remainder(client, october_usage):
    client.post("/v1/tenants/acme/credits", json={"amount": "1000", "reason": "Prepaid"})
    inv = invoice(client)
    assert D(inv["total"]) == 0 and D(inv["credit_total"]) == D("425.20")
    client.post(f"/v1/invoices/{inv['id']}/finalize")
    assert D(client.get("/v1/tenants/acme/credits").json()[0]["remaining"]) == D("574.80")


def test_expired_credit_not_used(client, october_usage):
    client.post(
        "/v1/tenants/acme/credits",
        json={"amount": "100", "expires_at": (OCT1 + timedelta(days=5)).isoformat()},
    )
    assert D(invoice(client)["credit_total"]) == 0


def test_finalized_invoice_cannot_be_regenerated(client, october_usage):
    inv = invoice(client)
    client.post(f"/v1/invoices/{inv['id']}/finalize")
    r = client.post("/v1/tenants/acme/invoices", params={"period": "2026-10"})
    assert r.status_code == 409
    listed = client.get("/v1/tenants/acme/invoices").json()
    assert len(listed) == 1 and listed[0]["status"] == "finalized"


def test_csv_export(client, october_usage):
    inv = invoice(client)
    r = client.get(f"/v1/invoices/{inv['id']}/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("invoice,tenant_id,period,position,metric")
    assert len([ln for ln in lines if ln.startswith(inv["number"])]) == 4
    assert lines[-1] == "total,425.20,BDT"


def test_invoice_without_plan_or_bad_period(client):
    client.post("/v1/tenants", json={"id": "acme", "name": "Acme"})
    assert client.post("/v1/tenants/acme/invoices", params={"period": "2026-10"}).status_code == 409
    assert client.post("/v1/tenants/acme/invoices", params={"period": "October"}).status_code == 422
    assert client.get("/v1/invoices/nope").status_code == 404


def test_empty_period_produces_zero_invoice(client, tenant):
    inv = invoice(client, period="2026-09")
    assert inv["lines"] == [] and D(inv["total"]) == 0
