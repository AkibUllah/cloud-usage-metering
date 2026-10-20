from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from metering.main import create_app

from .conftest import OCT1, ingest, make_settings, sample, standard_plan


def test_batch_is_accepted_and_listed(client, tenant):
    body = ingest(
        client,
        [
            sample("s1", "compute.instance.m1.small", 1, OCT1),
            sample("s2", "storage.volume.gb", 50, OCT1, resource_id="vol-1"),
        ],
    )
    assert body == {"accepted": 2, "duplicates": 0, "rejected": [], "budget_events": []}
    rows = client.get("/v1/usage", params={"tenant_id": "acme"}).json()
    assert [r["id"] for r in rows] == ["s1", "s2"]
    assert rows[1]["quantity"] == "50"
    only_volume = client.get("/v1/usage", params={"metric": "storage.volume.gb"}).json()
    assert [r["id"] for r in only_volume] == ["s2"]


def test_duplicates_within_and_across_batches(client, tenant):
    first = ingest(
        client,
        [
            sample("dup", "compute.instance.m1.small", 1, OCT1),
            sample("dup", "compute.instance.m1.small", 1, OCT1),
        ],
    )
    assert (first["accepted"], first["duplicates"]) == (1, 1)
    second = ingest(
        client,
        [
            sample("dup", "compute.instance.m1.small", 1, OCT1),
            sample("new", "compute.instance.m1.small", 1, OCT1),
        ],
    )
    assert (second["accepted"], second["duplicates"]) == (1, 1)
    assert len(client.get("/v1/usage").json()) == 2


def test_duplicate_replay_does_not_double_bill(client, tenant):
    s = sample("once", "compute.instance.m1.small", 1, OCT1, hours=4)
    ingest(client, [s])
    ingest(client, [s])
    summary = client.get("/v1/tenants/acme/usage/summary", params={"period": "2026-10"}).json()
    assert Decimal(summary["subtotal"]) == Decimal("10.00")


def test_rejections_carry_reasons_but_do_not_block_the_batch(client, tenant):
    body = ingest(
        client,
        [
            sample("ok", "compute.instance.m1.small", 1, OCT1),
            sample("bad-tenant", "compute.instance.m1.small", 1, OCT1, tenant_id="ghost"),
            sample("bad-metric", "compute.quantum.qubits", 1, OCT1),
        ],
    )
    assert body["accepted"] == 1
    assert {r["id"]: r["reason"] for r in body["rejected"]} == {
        "bad-tenant": "unknown tenant 'ghost'",
        "bad-metric": "unknown metric 'compute.quantum.qubits'",
    }


def test_validation_errors(client, tenant):
    bad_window = sample("w", "compute.instance.m1.small", 1, OCT1)
    bad_window["end"] = bad_window["start"]
    r = client.post("/v1/usage", json={"samples": [bad_window]})
    assert r.status_code == 422
    negative = sample("n", "compute.instance.m1.small", -1, OCT1)
    assert client.post("/v1/usage", json={"samples": [negative]}).status_code == 422
    assert client.post("/v1/usage", json={"samples": []}).status_code == 422


def test_batch_size_limit():
    with TestClient(create_app(make_settings(max_batch_size=2))) as c:
        c.post("/v1/plans", json=standard_plan())
        c.post("/v1/tenants", json={"id": "acme", "name": "Acme"})
        samples = [
            sample(f"s{i}", "compute.instance.m1.small", 1, OCT1 + timedelta(hours=i))
            for i in range(3)
        ]
        assert c.post("/v1/usage", json={"samples": samples}).status_code == 413


def test_api_key_is_enforced_when_configured():
    with TestClient(create_app(make_settings(api_key="top-secret"))) as c:
        assert c.get("/health").status_code == 200  # health stays open
        assert c.get("/v1/tenants").status_code == 401
        assert c.get("/v1/tenants", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/v1/tenants", headers={"X-API-Key": "top-secret"}).status_code == 200


def test_usage_summary_lists_quantities_and_cost(client, tenant):
    ingest(
        client,
        [
            sample("c", "compute.instance.m1.small", 1, OCT1, hours=10),
            sample("v", "storage.volume.gb", 100, OCT1, hours=10, resource_id="vol"),
            sample("f", "network.floating_ip", 1, OCT1, hours=10, resource_id="fip"),
        ],
    )
    summary = client.get("/v1/tenants/acme/usage/summary", params={"period": "2026-10"}).json()
    lines = {ln["metric"]: ln for ln in summary["lines"]}
    assert Decimal(lines["compute.instance.m1.small"]["quantity"]) == 10
    assert Decimal(lines["compute.instance.m1.small"]["amount"]) == Decimal("25")
    assert Decimal(lines["storage.volume.gb"]["quantity"]) == 1000
    assert Decimal(lines["storage.volume.gb"]["amount"]) == Decimal("0.2")
    assert Decimal(lines["network.floating_ip"]["free_quantity"]) == 10
    assert Decimal(lines["network.floating_ip"]["amount"]) == 0
    assert summary["subtotal"] == "25.20"
    assert summary["currency"] == "BDT"
    assert (
        client.get("/v1/tenants/acme/usage/summary", params={"period": "2026-13"}).status_code
        == 422
    )


def test_health_and_metric_registry(client):
    assert client.get("/health").json()["status"] == "ok"
    metrics = client.get("/v1/metrics").json()
    kinds = {m["pattern"]: m["kind"] for m in metrics}
    assert kinds["compute.instance.*"] == "gauge"
    assert kinds["network.bandwidth.egress.gb"] == "delta"
