#!/usr/bin/env python3
"""Generate realistic OpenStack-style usage for a few tenants and push it through the API.

Examples::

    # against a running server, 3 days of hourly samples, then generate invoices
    python scripts/simulate_usage.py --base-url http://localhost:8000 --days 3 --invoice

    # just write the samples to a JSON file
    python scripts/simulate_usage.py --days 1 --out samples.json

The sample ids are deterministic (resource + metric + hour), so re-running the simulator is
idempotent: the service reports the repeats as duplicates.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

FLAVORS = {
    "m1.small": {"vcpus": 1, "ram_gb": 2},
    "m1.medium": {"vcpus": 2, "ram_gb": 4},
    "m1.large": {"vcpus": 4, "ram_gb": 8},
}

PLAN_ID = "standard-bdt"


def standard_plan() -> dict[str, Any]:
    """A BDT price list in the spirit of a Dhaka-hosted OpenStack cloud."""
    return {
        "id": PLAN_ID,
        "name": "Standard (BDT)",
        "currency": "BDT",
        "description": "Hourly compute, GB-hour storage, one free floating IP, 100 GB free egress",
        "prices": [
            {"metric": "compute.instance.m1.small", "unit_price": "2.50"},
            {"metric": "compute.instance.m1.medium", "unit_price": "5.00"},
            {"metric": "compute.instance.m1.large", "unit_price": "10.00"},
            {"metric": "compute.instance.*", "unit_price": "15.00"},
            {"metric": "compute.instance.m1.small", "region": "ctg-1", "unit_price": "2.00"},
            {"metric": "storage.volume.gb", "unit_price": "0.0002"},
            {"metric": "storage.snapshot.gb", "unit_price": "0.0001"},
            {"metric": "network.floating_ip", "unit_price": "0.50", "free_quantity": "744"},
            {
                "metric": "network.bandwidth.egress.gb",
                "free_quantity": "100",
                "tiers": [
                    {"up_to": "1000", "unit_price": "8.00"},
                    {"up_to": None, "unit_price": "6.00"},
                ],
            },
        ],
    }


@dataclass
class Server:
    id: str
    flavor: str
    region: str
    volume_gb: int
    floating_ip: bool
    office_hours_only: bool = False


@dataclass
class TenantSim:
    id: str
    name: str
    region: str
    servers: list[Server] = field(default_factory=list)


def build_tenants(count: int, rng: random.Random) -> list[TenantSim]:
    catalogue = [
        ("pathao-labs", "Pathao Labs", "dhaka-1"),
        ("chaldal-infra", "Chaldal Infrastructure", "dhaka-1"),
        ("shohoz-platform", "Shohoz Platform", "ctg-1"),
        ("brac-digital", "BRAC Digital", "dhaka-1"),
        ("sheba-xyz", "Sheba Platform", "dhaka-1"),
        ("bkash-sandbox", "bKash Developer Sandbox", "ctg-1"),
    ]
    tenants: list[TenantSim] = []
    for tid, name, region in catalogue[:count]:
        tenant = TenantSim(id=tid, name=name, region=region)
        for n in range(rng.randint(2, 5)):
            tenant.servers.append(
                Server(
                    id=f"{tid}-srv-{n + 1:02d}",
                    flavor=rng.choice(list(FLAVORS)),
                    region=region,
                    volume_gb=rng.choice([20, 50, 100, 200]),
                    floating_ip=rng.random() < 0.6,
                    office_hours_only=rng.random() < 0.3,
                )
            )
        tenants.append(tenant)
    return tenants


def generate_samples(
    tenants: list[TenantSim], start: datetime, days: int, rng: random.Random
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    hours = days * 24
    for h in range(hours):
        s = start + timedelta(hours=h)
        e = s + timedelta(hours=1)
        s_iso, e_iso = s.isoformat(), e.isoformat()
        for tenant in tenants:
            for srv in tenant.servers:
                # Office-hours servers are stopped between 20:00 and 08:00 Dhaka time (UTC+6).
                dhaka_hour = (s.hour + 6) % 24
                running = not srv.office_hours_only or 8 <= dhaka_hour < 20
                base = {"tenant_id": tenant.id, "region": srv.region, "start": s_iso, "end": e_iso}
                if running:
                    samples.append(
                        {
                            **base,
                            "id": f"{srv.id}:compute:{s_iso}",
                            "resource_id": srv.id,
                            "metric": f"compute.instance.{srv.flavor}",
                            "quantity": "1",
                            "attributes": {"flavor": srv.flavor, **FLAVORS[srv.flavor]},
                        }
                    )
                    egress = round(rng.uniform(0.05, 3.5), 3)
                    samples.append(
                        {
                            **base,
                            "id": f"{srv.id}:egress:{s_iso}",
                            "resource_id": srv.id,
                            "metric": "network.bandwidth.egress.gb",
                            "quantity": str(egress),
                        }
                    )
                # Volumes and floating IPs bill whether or not the server is running.
                samples.append(
                    {
                        **base,
                        "id": f"{srv.id}-vol:volume:{s_iso}",
                        "resource_id": f"{srv.id}-vol",
                        "metric": "storage.volume.gb",
                        "quantity": str(srv.volume_gb),
                    }
                )
                if srv.floating_ip:
                    samples.append(
                        {
                            **base,
                            "id": f"{srv.id}-fip:fip:{s_iso}",
                            "resource_id": f"{srv.id}-fip",
                            "metric": "network.floating_ip",
                            "quantity": "1",
                        }
                    )
    return samples


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def bootstrap(client, tenants: list[TenantSim], effective_from: datetime) -> None:
    r = client.post("/v1/plans", json=standard_plan())
    if r.status_code not in (201, 409):
        raise SystemExit(f"creating plan failed: {r.status_code} {r.text}")
    for t in tenants:
        r = client.post(
            "/v1/tenants", json={"id": t.id, "name": t.name, "default_region": t.region}
        )
        if r.status_code not in (201, 409):
            raise SystemExit(f"creating tenant failed: {r.status_code} {r.text}")
        if r.status_code == 201:
            r = client.post(
                f"/v1/tenants/{t.id}/plan",
                json={"plan_id": PLAN_ID, "effective_from": effective_from.isoformat()},
            )
            r.raise_for_status()


def run(
    client,
    *,
    days: int,
    tenants: int,
    seed: int,
    start: datetime,
    batch_size: int = 500,
    invoice: bool = False,
    log=print,
) -> dict[str, Any]:
    rng = random.Random(seed)
    sims = build_tenants(tenants, rng)
    bootstrap(client, sims, effective_from=start.replace(day=1, hour=0, minute=0, second=0))
    samples = generate_samples(sims, start, days, rng)

    totals = {"accepted": 0, "duplicates": 0, "rejected": 0, "budget_events": 0}
    for batch in chunked(samples, batch_size):
        r = client.post("/v1/usage", json={"samples": batch})
        r.raise_for_status()
        body = r.json()
        totals["accepted"] += body["accepted"]
        totals["duplicates"] += body["duplicates"]
        totals["rejected"] += len(body["rejected"])
        totals["budget_events"] += len(body["budget_events"])
    log(
        f"samples={len(samples)} accepted={totals['accepted']} duplicates={totals['duplicates']} "
        f"rejected={totals['rejected']}"
    )

    invoices: list[dict[str, Any]] = []
    if invoice:
        period = f"{start.year:04d}-{start.month:02d}"
        for t in sims:
            r = client.post(f"/v1/tenants/{t.id}/invoices", params={"period": period})
            r.raise_for_status()
            inv = r.json()
            invoices.append(inv)
            log(
                f"{inv['number']}  {t.name:<28} {inv['currency']} {inv['total']:>12}  "
                f"({len(inv['lines'])} lines)"
            )
    return {"samples": len(samples), **totals, "invoices": invoices}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key", default=None, help="Value for X-API-Key if the server requires one"
    )
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--tenants", type=int, default=3, choices=range(1, 7))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--start",
        default=None,
        help="UTC start, ISO 8601 (default: first day of the current month)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--invoice", action="store_true", help="Generate invoices after ingesting")
    parser.add_argument(
        "--out", default=None, help="Write samples to this JSON file instead of posting"
    )
    args = parser.parse_args(argv)

    if args.start:
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if args.out:
        rng = random.Random(args.seed)
        samples = generate_samples(build_tenants(args.tenants, rng), start, args.days, rng)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"samples": samples}, fh, indent=2)
        print(f"wrote {len(samples)} samples to {args.out}")
        return 0

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=60) as client:
        run(
            client,
            days=args.days,
            tenants=args.tenants,
            seed=args.seed,
            start=start,
            batch_size=args.batch_size,
            invoice=args.invoice,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
