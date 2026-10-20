from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from metering.models import RatedCharge, UsageSample
from metering.plans import NoPlanAssigned
from metering.rating import aggregate, prorate_sample, rate_tenant_window

from .conftest import OCT1, ingest, sample

D = Decimal
UTC = timezone.utc


def mk(metric: str, quantity, start: datetime, end: datetime, region=None) -> UsageSample:
    return UsageSample(
        id="s",
        tenant_id="acme",
        resource_id="r",
        metric=metric,
        quantity=D(str(quantity)),
        region=region,
        start=start,
        end=end,
    )


def test_gauge_partial_hour_is_prorated():
    s = mk(
        "compute.instance.m1.small",
        1,
        datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 10, 30, tzinfo=UTC),
    )
    buckets = prorate_sample(s, OCT1, OCT1 + timedelta(days=31))
    assert len(buckets) == 1
    assert buckets[0].quantity == D("0.5")
    assert buckets[0].bucket_start == datetime(2026, 10, 1, 10, tzinfo=UTC)


def test_gauge_spanning_three_hours_splits_by_overlap():
    s = mk(
        "compute.instance.m1.small",
        1,
        datetime(2026, 10, 1, 10, 30, tzinfo=UTC),
        datetime(2026, 10, 1, 12, 15, tzinfo=UTC),
    )
    buckets = prorate_sample(s, OCT1, OCT1 + timedelta(days=31))
    assert [b.quantity for b in buckets] == [D("0.5"), D("1"), D("0.25")]
    assert sum(b.quantity for b in buckets) == D("1.75")


def test_gauge_level_times_hours_gives_gb_hours():
    s = mk(
        "storage.volume.gb",
        100,
        datetime(2026, 10, 1, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 2, tzinfo=UTC),
    )
    buckets = prorate_sample(s, OCT1, OCT1 + timedelta(days=31))
    assert [b.quantity for b in buckets] == [D("100"), D("100")]
    assert buckets[0].spec.rated_unit == "GB-hour"


def test_delta_is_split_proportionally_to_time():
    s = mk(
        "network.bandwidth.egress.gb",
        12,
        datetime(2026, 10, 1, 10, tzinfo=UTC),
        datetime(2026, 10, 1, 13, tzinfo=UTC),
    )
    buckets = prorate_sample(s, OCT1, OCT1 + timedelta(days=31))
    assert [b.quantity for b in buckets] == [D("4"), D("4"), D("4")]


def test_sample_is_clipped_to_the_window():
    s = mk(
        "compute.instance.m1.small",
        1,
        datetime(2026, 10, 31, 23, tzinfo=UTC),
        datetime(2026, 11, 1, 2, tzinfo=UTC),
    )
    october = prorate_sample(s, OCT1, datetime(2026, 11, 1, tzinfo=UTC))
    november = prorate_sample(
        s, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    )
    assert [b.quantity for b in october] == [D("1")]
    assert [b.quantity for b in november] == [D("1"), D("1")]


def test_daily_granularity():
    s = mk("compute.instance.m1.small", 1, OCT1, OCT1 + timedelta(hours=36))
    buckets = prorate_sample(s, OCT1, OCT1 + timedelta(days=31), granularity="day")
    assert [b.quantity for b in buckets] == [D("24"), D("12")]


def test_unknown_metric_and_out_of_window_yield_nothing():
    s = mk("compute.gpu.hours", 1, OCT1, OCT1 + timedelta(hours=1))
    assert prorate_sample(s, OCT1, OCT1 + timedelta(days=31)) == []
    s = mk("compute.instance.m1.small", 1, OCT1 - timedelta(days=2), OCT1 - timedelta(days=1))
    assert prorate_sample(s, OCT1, OCT1 + timedelta(days=31)) == []


def test_aggregate_sums_same_bucket_and_sorts():
    a = mk("compute.instance.m1.small", 1, OCT1 + timedelta(hours=1), OCT1 + timedelta(hours=2))
    b = mk("compute.instance.m1.small", 1, OCT1, OCT1 + timedelta(hours=2))
    buckets = prorate_sample(a, OCT1, OCT1 + timedelta(days=1)) + prorate_sample(
        b, OCT1, OCT1 + timedelta(days=1)
    )
    merged = aggregate(buckets)
    assert [(m.bucket_start.hour, m.quantity) for m in merged] == [(0, D("1")), (1, D("2"))]


def test_rate_tenant_window_applies_free_tier_across_buckets(client, db, tenant):
    # 150 GB egress spread over 3 hours: first 100 GB free, remaining 50 GB at 8.00
    ingest(client, [sample("e1", "network.bandwidth.egress.gb", 150, OCT1, hours=3)])
    charges = rate_tenant_window(db, "acme", OCT1, OCT1 + timedelta(days=31), persist=False)
    assert [c.quantity for c in charges] == [D("50"), D("50"), D("50")]
    assert [c.free_quantity for c in charges] == [D("50"), D("50"), D("0")]
    assert [c.amount for c in charges] == [D("0"), D("0"), D("400")]


def test_regional_price_override_is_used(client, db, tenant):
    ingest(
        client,
        [
            sample(
                "a", "compute.instance.m1.small", 1, OCT1, resource_id="dhaka-box", region="dhaka-1"
            ),
            sample(
                "b", "compute.instance.m1.small", 1, OCT1, resource_id="ctg-box", region="ctg-1"
            ),
        ],
    )
    charges = rate_tenant_window(db, "acme", OCT1, OCT1 + timedelta(days=31), persist=False)
    by_region = {c.region: c.amount for c in charges}
    assert by_region == {"dhaka-1": D("2.5"), "ctg-1": D("2.0")}


def test_plan_change_mid_period_prices_each_bucket_with_its_plan(client, db, tenant):
    client.post(
        "/v1/plans",
        json={
            "id": "promo",
            "name": "Promo",
            "currency": "BDT",
            "prices": [{"metric": "compute.instance.m1.small", "unit_price": "1.00"}],
        },
    )
    r = client.post(
        "/v1/tenants/acme/plan",
        json={"plan_id": "promo", "effective_from": (OCT1 + timedelta(days=1)).isoformat()},
    )
    assert r.status_code == 201
    history = client.get("/v1/tenants/acme/plan/history").json()
    assert [h["plan_id"] for h in history] == ["standard-bdt", "promo"]
    assert history[0]["effective_to"] == (OCT1 + timedelta(days=1)).isoformat().replace(
        "+00:00", "Z"
    )

    ingest(
        client, [sample("x", "compute.instance.m1.small", 1, OCT1 + timedelta(hours=23), hours=2)]
    )
    charges = rate_tenant_window(db, "acme", OCT1, OCT1 + timedelta(days=31), persist=False)
    assert [(c.plan_id, c.amount) for c in charges] == [
        ("standard-bdt", D("2.5")),
        ("promo", D("1")),
    ]


def test_missing_plan_raises(client, db):
    client.post("/v1/tenants", json={"id": "acme", "name": "No Plan Co"})
    ingest(client, [sample("x", "compute.instance.m1.small", 1, OCT1)])
    with pytest.raises(NoPlanAssigned):
        rate_tenant_window(db, "acme", OCT1, OCT1 + timedelta(days=31), persist=False)


def test_unpriced_metric_is_recorded_with_zero_amount(client, db, tenant):
    ingest(client, [sample("snap", "storage.snapshot.gb", 10, OCT1)])
    charges = rate_tenant_window(db, "acme", OCT1, OCT1 + timedelta(days=31), persist=False)
    assert len(charges) == 1
    assert (
        charges[0].priced is False
        and charges[0].amount == D("0")
        and charges[0].quantity == D("10")
    )


def test_persisted_rating_is_replaced_on_rerun(client, db, tenant):
    ingest(client, [sample("x", "compute.instance.m1.small", 1, OCT1, hours=3)])
    end = OCT1 + timedelta(days=31)
    rate_tenant_window(db, "acme", OCT1, end, persist=True)
    rate_tenant_window(db, "acme", OCT1, end, persist=True)
    db.commit()
    rows = db.query(RatedCharge).filter_by(tenant_id="acme").all()
    assert len(rows) == 3
    assert sum(r.amount for r in rows) == D("7.5")
