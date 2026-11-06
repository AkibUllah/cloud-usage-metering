from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from metering.main import create_app

from .conftest import OCT1, ingest, make_settings, sample

D = Decimal
METRIC = "compute.instance.m1.small"
NOV1 = OCT1 + timedelta(days=31)
DEC1 = NOV1 + timedelta(days=30)
URL = "/v1/tenants/acme/commitments"


def body(**overrides) -> dict:
    return {
        "metric": METRIC,
        "quantity": "100",
        "unit_price": "1.50",
        "starts_at": OCT1.isoformat(),
        **overrides,
    }


def create(client, **overrides) -> dict:
    r = client.post(URL, json=body(**overrides))
    assert r.status_code == 201, r.text
    return r.json()


def finalize(client, period: str = "2026-10") -> dict:
    r = client.post("/v1/tenants/acme/invoices", params={"period": period})
    assert r.status_code == 201, r.text
    return client.post(f"/v1/invoices/{r.json()['id']}/finalize").json()


def test_full_flow_from_commitment_to_invoice(client, tenant):
    created = create(client, id="reserved-oct", quantity="310", starts_at="2026-10-22T00:00:00Z")
    assert created["id"] == "reserved-oct" and created["currency"] == "BDT"
    ingest(
        client,
        [
            sample("early", METRIC, 1, OCT1, hours=10),
            sample("late", METRIC, 1, OCT1 + timedelta(days=21), hours=80),
        ],
    )

    summary = client.get("/v1/tenants/acme/usage/summary", params={"period": "2026-10"}).json()
    r = client.post("/v1/tenants/acme/invoices", params={"period": "2026-10"})
    assert r.status_code == 201, r.text
    inv = r.json()

    assert [ln["line_type"] for ln in inv["lines"]] == [
        "committed",
        "usage",
        "unused_commitment",
    ]
    assert D(inv["committed_total"]) == D("120.00")
    assert D(inv["overage_total"]) == D("25.00")
    assert D(inv["unused_commitment_total"]) == D("30.00")
    assert D(inv["total"]) == D("175.00")

    # The summary is what the invoice would say.
    for field in ("committed_total", "unused_commitment_total", "overage_total", "subtotal"):
        assert D(summary[field]) == D(inv[field])
    usage = summary["commitments"][0]
    assert usage["commitment_id"] == "reserved-oct"
    assert (D(usage["entitlement"]), D(usage["used"]), D(usage["unused"])) == (
        D("100"),
        D("80"),
        D("20"),
    )
    line = summary["lines"][0]
    assert (D(line["quantity"]), D(line["committed_quantity"])) == (D("90"), D("80"))


def test_summary_without_commitments_is_unchanged(client, tenant):
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=3)])
    summary = client.get("/v1/tenants/acme/usage/summary", params={"period": "2026-10"}).json()
    assert summary["commitments"] == [] and D(summary["committed_total"]) == 0
    assert D(summary["overage_total"]) == D(summary["subtotal"]) == D("7.50")


def test_summary_refuses_a_commitment_in_another_currency(client, tenant):
    create(client, starts_at="2026-10-01T00:00:00Z")
    client.post(
        "/v1/plans",
        json={
            "id": "usd",
            "name": "USD",
            "currency": "USD",
            "prices": [{"metric": METRIC, "unit_price": "0.10"}],
        },
    )
    client.post(
        "/v1/tenants/acme/plan", json={"plan_id": "usd", "effective_from": OCT1.isoformat()}
    )
    r = client.get("/v1/tenants/acme/usage/summary", params={"period": "2026-10"})
    assert r.status_code == 409


def test_list_get_update_delete(client, tenant):
    a = create(client, description="Reserved block")
    b = create(client, starts_at=NOV1.isoformat())
    assert [c["id"] for c in client.get(URL).json()] == [a["id"], b["id"]]
    assert client.get(f"{URL}/{a['id']}").json()["description"] == "Reserved block"

    r = client.patch(
        f"{URL}/{a['id']}",
        json={"quantity": "200", "ends_at": DEC1.isoformat(), "description": "Bigger"},
    )
    assert r.status_code == 200, r.text
    assert D(r.json()["quantity"]) == 200 and r.json()["description"] == "Bigger"
    assert D(r.json()["unit_price"]) == D("1.50")

    r = client.patch(f"{URL}/{a['id']}", json={"ends_at": None})
    assert r.status_code == 200 and r.json()["ends_at"] is None

    assert client.delete(f"{URL}/{a['id']}").status_code == 204
    assert client.get(f"{URL}/{a['id']}").status_code == 404
    assert [c["id"] for c in client.get(URL).json()] == [b["id"]]


def test_commitment_is_scoped_to_its_tenant(client, tenant):
    client.post("/v1/tenants", json={"id": "other", "name": "Other"})
    created = create(client)
    path = f"/v1/tenants/other/commitments/{created['id']}"
    assert client.get(path).status_code == 404
    assert client.patch(path, json={"quantity": "1"}).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.get("/v1/tenants/other/commitments").json() == []
    assert client.get("/v1/tenants/nobody/commitments").status_code == 404


def test_retried_create_with_the_same_id_conflicts(client, tenant):
    create(client, id="once")
    assert client.post(URL, json=body(id="once")).status_code == 409
    assert len(client.get(URL).json()) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"metric": "compute.instance.*"},
        {"metric": "gpu.hours"},
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "1.1234567"},
        {"quantity": "NaN"},
        {"quantity": "12345678901"},
        {"unit_price": "-0.01"},
        {"unit_price": "0.123456789"},
        {"unit_price": "Infinity"},
        {"ends_at": OCT1.isoformat()},
        {"ends_at": (OCT1 - timedelta(days=1)).isoformat()},
    ],
)
def test_invalid_commitments_are_rejected(client, tenant, overrides):
    assert client.post(URL, json=body(**overrides)).status_code == 422


def test_float_literal_is_accepted_as_its_shortest_decimal(client, tenant):
    created = create(client, unit_price=1.1)
    assert D(created["unit_price"]) == D("1.1")


def test_naive_and_aware_timestamps_can_be_compared(client, tenant):
    r = client.post(URL, json=body(starts_at="2026-10-05T00:00:00", ends_at="2026-10-06T00:00:00Z"))
    assert r.status_code == 201, r.text


def test_currency_defaults_to_the_plan_and_must_match_it(client, tenant):
    assert create(client)["currency"] == "BDT"
    assert client.post(URL, json=body(currency="USD")).status_code == 409


def test_commitment_before_any_plan_needs_a_currency(client):
    client.post("/v1/tenants", json={"id": "acme", "name": "Acme"})
    assert client.post(URL, json=body()).status_code == 409
    assert client.post(URL, json=body(currency="BDT")).status_code == 201


def test_update_rejects_bad_values(client, tenant):
    created = create(client)
    path = f"{URL}/{created['id']}"
    assert client.patch(path, json={"ends_at": OCT1.isoformat()}).status_code == 422
    assert client.patch(path, json={"quantity": None}).status_code == 422
    assert client.patch(path, json={"quantity": "0"}).status_code == 422
    assert client.patch(path, json={"metric": "storage.volume.gb"}).status_code == 422


def test_terms_are_locked_once_a_finalized_invoice_used_them(client, tenant):
    created = create(client)
    finalize(client)
    path = f"{URL}/{created['id']}"
    assert client.patch(path, json={"quantity": "1"}).status_code == 409
    assert client.patch(path, json={"unit_price": "1"}).status_code == 409
    assert client.patch(path, json={"starts_at": NOV1.isoformat()}).status_code == 409
    assert client.delete(path).status_code == 409
    assert client.patch(path, json={"description": "Still fine"}).status_code == 200
    assert client.patch(path, json={"quantity": "100.00"}).status_code == 200  # same value


def test_end_date_can_only_move_past_finalized_periods(client, tenant):
    created = create(client)
    finalize(client)
    path = f"{URL}/{created['id']}"
    inside = (OCT1 + timedelta(days=10)).isoformat()
    assert client.patch(path, json={"ends_at": inside}).status_code == 409
    r = client.patch(path, json={"ends_at": (NOV1 + timedelta(days=10)).isoformat()})
    assert r.status_code == 200
    # An end inside the open period can move or go away.
    assert client.patch(path, json={"ends_at": None}).status_code == 200


def test_ended_commitment_inside_a_finalized_period_cannot_be_reopened(client, tenant):
    created = create(client, ends_at=(OCT1 + timedelta(days=10)).isoformat())
    finalize(client)
    assert client.patch(f"{URL}/{created['id']}", json={"ends_at": None}).status_code == 409


def test_new_commitment_cannot_reach_into_a_finalized_period(client, tenant):
    finalize(client)
    assert client.post(URL, json=body()).status_code == 409
    assert client.post(URL, json=body(starts_at=NOV1.isoformat())).status_code == 201


def test_a_draft_does_not_lock_anything(client, tenant):
    created = create(client)
    client.post("/v1/tenants/acme/invoices", params={"period": "2026-10"})
    assert client.patch(f"{URL}/{created['id']}", json={"quantity": "1"}).status_code == 200
    assert client.delete(f"{URL}/{created['id']}").status_code == 204


def test_finalized_invoice_survives_a_later_commitment_change(client, tenant):
    created = create(client, ends_at=DEC1.isoformat())
    inv = finalize(client)
    client.patch(f"{URL}/{created['id']}", json={"ends_at": (NOV1 + timedelta(days=1)).isoformat()})
    assert client.get(f"/v1/invoices/{inv['id']}").json()["lines"] == inv["lines"]


def test_api_key_is_enforced():
    with TestClient(create_app(make_settings(api_key="top-secret"))) as c:
        assert c.get(URL).status_code == 401
        assert c.post(URL, json=body()).status_code == 401
