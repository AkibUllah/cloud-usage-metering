from __future__ import annotations

import random
from decimal import Decimal

from .conftest import OCT1


def test_generation_is_deterministic_and_ids_unique(simulator):
    def gen(seed):
        rng = random.Random(seed)
        return simulator.generate_samples(simulator.build_tenants(3, rng), OCT1, 1, rng)

    a, b = gen(7), gen(7)
    assert a == b
    ids = [s["id"] for s in a]
    assert len(ids) == len(set(ids))
    metrics = {s["metric"] for s in a}
    assert {"storage.volume.gb", "network.bandwidth.egress.gb"} <= metrics
    assert any(m.startswith("compute.instance.m1.") for m in metrics)


def test_end_to_end_run_bootstraps_ingests_and_invoices(client, simulator):
    logs: list[str] = []
    result = simulator.run(
        client, days=1, tenants=2, seed=1, start=OCT1, invoice=True, log=logs.append
    )
    assert result["accepted"] == result["samples"] > 0
    assert result["rejected"] == 0
    assert len(result["invoices"]) == 2
    assert all(Decimal(inv["total"]) > 0 for inv in result["invoices"])
    assert any(line.startswith("INV-202610-") for line in logs)

    # Re-running is idempotent: everything is a duplicate, invoices are regenerated in place.
    again = simulator.run(
        client, days=1, tenants=2, seed=1, start=OCT1, invoice=True, log=logs.append
    )
    assert again["accepted"] == 0 and again["duplicates"] == result["samples"]
    assert [i["id"] for i in again["invoices"]] == [i["id"] for i in result["invoices"]]
    assert [i["total"] for i in again["invoices"]] == [i["total"] for i in result["invoices"]]


def test_cli_writes_json_file(simulator, tmp_path):
    out = tmp_path / "samples.json"
    rc = simulator.main(
        ["--days", "1", "--tenants", "1", "--start", "2026-10-01T00:00:00Z", "--out", str(out)]
    )
    assert rc == 0
    import json

    data = json.loads(out.read_text())
    assert len(data["samples"]) > 0
    assert data["samples"][0]["tenant_id"] == "swift-courier"
