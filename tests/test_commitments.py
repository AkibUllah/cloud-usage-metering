from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from metering.budgets import month_to_date_spend
from metering.commitments import CommitmentLedger, active_at, consumption_order, entitlement
from metering.models import Commitment, RatedCharge
from metering.periods import parse_period
from metering.pricing import prorate
from metering.rating import rate_period, summarize

from .conftest import OCT1, ingest, sample

D = Decimal
UTC = timezone.utc
METRIC = "compute.instance.m1.small"
OCT_START, OCT_END = parse_period("2026-10")
FEB_START, FEB_END = parse_period("2026-02")
WEEK = timedelta(days=7)


def commitment(
    cid: str = "c1",
    quantity="310",
    unit_price="2.00",
    starts_at: datetime = OCT_START,
    ends_at: datetime | None = None,
    metric: str = METRIC,
    region: str | None = None,
) -> Commitment:
    return Commitment(
        id=cid,
        tenant_id="acme",
        metric=metric,
        region=region,
        quantity=D(quantity),
        unit_price=D(unit_price),
        currency="BDT",
        starts_at=starts_at,
        ends_at=ends_at,
    )


def oct_(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def test_prorate_rounds_once_and_half_up():
    assert prorate(D("1"), 1, 3) == D("0.333333")
    assert prorate(D("2"), 1, 3) == D("0.666667")
    assert prorate(D("0.000001"), 1, 2) == D("0.000001")
    assert prorate(D("300"), 1, 1) == D("300")


def test_prorate_is_not_limited_by_the_decimal_context():
    assert prorate(D("9999999999.999999"), 31, 31) == D("9999999999.999999")
    assert prorate(D("9999999999999999.123456"), 1, 1) == D("9999999999999999.123456")


def test_prorate_rejects_an_empty_whole():
    with pytest.raises(ValueError):
        prorate(D("1"), 1, 0)


def test_full_period_gets_the_whole_quantity_whatever_the_month_length():
    assert entitlement(commitment(), OCT_START, OCT_END) == D("310")
    assert entitlement(commitment(starts_at=FEB_START), FEB_START, FEB_END) == D("310")


def test_commitment_starting_mid_period_is_prorated_by_time():
    c = commitment(starts_at=oct_(22))  # the last 10 of 31 days
    assert entitlement(c, OCT_START, OCT_END) == D("100")


def test_commitment_ending_mid_period_is_prorated_by_time():
    c = commitment(ends_at=oct_(11))  # the first 10 of 31 days
    assert entitlement(c, OCT_START, OCT_END) == D("100")


def test_commitment_starting_and_ending_inside_one_period():
    c = commitment(starts_at=oct_(11), ends_at=oct_(21))
    assert entitlement(c, OCT_START, OCT_END) == D("100")


def test_proration_is_exact_to_the_second():
    c = commitment(quantity="744", starts_at=oct_(1, 0, 30))  # 744 h month minus 30 minutes
    assert entitlement(c, OCT_START, OCT_END) == D("743.5")


def test_commitment_outside_the_period_gets_nothing():
    c = commitment(starts_at=OCT_END)
    assert entitlement(c, OCT_START, OCT_END) == 0
    assert entitlement(commitment(ends_at=OCT_START), OCT_START, OCT_END) == 0


def test_active_window_is_half_open():
    c = commitment(starts_at=oct_(5), ends_at=oct_(6))
    assert not active_at(c, oct_(4, 23))
    assert active_at(c, oct_(5))
    assert active_at(c, oct_(5, 23))
    assert not active_at(c, oct_(6))


def test_usage_within_the_commitment_is_fully_covered():
    ledger = CommitmentLedger([commitment()])
    draws, overage = ledger.draw(METRIC, None, OCT_START, D("10"))
    assert [d.quantity for d in draws] == [D("10")] and overage == 0


def test_usage_beyond_the_commitment_returns_the_overage():
    ledger = CommitmentLedger([commitment(quantity="10")])
    ledger.draw(METRIC, None, OCT_START, D("8"))
    draws, overage = ledger.draw(METRIC, None, oct_(2), D("5"))
    assert [d.quantity for d in draws] == [D("2")] and overage == D("3")
    assert ledger.draw(METRIC, None, oct_(3), D("1"))[0] == []


def test_other_metrics_and_inactive_times_are_not_covered():
    ledger = CommitmentLedger([commitment(starts_at=oct_(5))])
    assert ledger.draw("storage.volume.gb", None, oct_(6), D("1")) == ([], D("1"))
    assert ledger.draw(METRIC, None, oct_(4), D("1")) == ([], D("1"))


def test_regional_commitment_covers_only_its_region():
    ledger = CommitmentLedger([commitment(region="ctg-1")])
    assert ledger.draw(METRIC, "dhaka-1", OCT_START, D("1")) == ([], D("1"))
    assert ledger.draw(METRIC, None, OCT_START, D("1")) == ([], D("1"))
    assert ledger.draw(METRIC, "ctg-1", OCT_START, D("1"))[1] == 0


def test_commitment_without_region_covers_all_regions_from_one_pool():
    ledger = CommitmentLedger([commitment(quantity="3")])
    assert ledger.draw(METRIC, "dhaka-1", OCT_START, D("2"))[1] == 0
    assert ledger.draw(METRIC, "ctg-1", OCT_START, D("2"))[1] == D("1")


def test_pool_is_refilled_each_period():
    ledger = CommitmentLedger([commitment(quantity="5")])
    assert ledger.draw(METRIC, None, OCT_START, D("5"))[1] == 0
    assert ledger.draw(METRIC, None, OCT_END, D("5"))[1] == 0
    assert ledger.draw(METRIC, None, OCT_END, D("1"))[1] == D("1")


def test_consumption_prefers_regional_then_soonest_ending_then_cheapest():
    specific = commitment("specific", region="ctg-1")
    ending = commitment("ending", ends_at=oct_(20))
    cheap = commitment("cheap", unit_price="1.00")
    pricey = commitment("pricey", unit_price="1.50")
    ordered = sorted([pricey, cheap, ending, specific], key=consumption_order)
    assert [c.id for c in ordered] == ["specific", "ending", "cheap", "pricey"]


def test_soonest_ending_first_leaves_nothing_stranded():
    # Used the other way round the short commitment would expire unused and usage would overflow.
    short = commitment("short", quantity="310", ends_at=oct_(11))  # entitled to 100
    long = commitment("long", quantity="100")
    ledger = CommitmentLedger([long, short])
    assert ledger.draw(METRIC, None, oct_(1), D("100"))[1] == 0
    assert ledger.draw(METRIC, None, oct_(15), D("100"))[1] == 0
    assert {s.commitment.id: s.unused for s in ledger.settle(OCT_START, OCT_END)} == {
        "short": 0,
        "long": 0,
    }


def test_overlapping_commitments_stack():
    ledger = CommitmentLedger([commitment("a", quantity="2"), commitment("b", quantity="3")])
    draws, overage = ledger.draw(METRIC, None, OCT_START, D("6"))
    assert sum(d.quantity for d in draws) == D("5") and overage == D("1")


def test_settlement_splits_the_fee_into_used_and_unused():
    ledger = CommitmentLedger([commitment(quantity="100", unit_price="2.00")])
    ledger.draw(METRIC, None, OCT_START, D("40"))
    settled = ledger.settle(OCT_START, OCT_END)[0]
    assert (settled.entitlement, settled.used, settled.unused) == (D("100"), D("40"), D("60"))
    assert (settled.committed_amount, settled.unused_amount) == (D("80.00"), D("120.00"))


@pytest.mark.parametrize("used", ["0", "0.001", "0.002", "1.333333", "2.5", "2.999999", "3.333"])
def test_fee_does_not_depend_on_how_much_was_used(used):
    # 0.005 steps per unit force both halves to round: the parts must still add up to the fee.
    ledger = CommitmentLedger([commitment(quantity="3.333", unit_price="1.005")])
    if D(used) > 0:
        ledger.draw(METRIC, None, OCT_START, D(used))
    settled = ledger.settle(OCT_START, OCT_END)[0]
    assert settled.committed_amount + settled.unused_amount == D("3.35")
    assert settled.used + settled.unused == D("3.333")


def test_settlement_lists_unused_commitments_and_skips_those_outside_the_period():
    ledger = CommitmentLedger([commitment("idle"), commitment("later", starts_at=OCT_END)])
    assert [s.commitment.id for s in ledger.settle(OCT_START, OCT_END)] == ["idle"]


def test_cut_points_are_the_boundaries_inside_the_window():
    inside = oct_(5, 10, 30)
    ledger = CommitmentLedger(
        [commitment("a", starts_at=OCT_START, ends_at=inside), commitment("b", starts_at=OCT_END)]
    )
    assert ledger.cut_points(OCT_START, OCT_END) == [inside]


def test_settled_amounts_use_the_prorated_quantity():
    c = commitment(quantity="1", unit_price="3.00", starts_at=OCT_START + timedelta(days=30))
    settled = CommitmentLedger([c]).settle(OCT_START, OCT_END)[0]
    assert settled.entitlement == D("0.032258")  # 1 / 31
    assert settled.unused_amount == D("0.10")


# ----------------------------------------------------------------------------- rating


def rate(db, granularity="hour"):
    return rate_period(db, "acme", OCT_START, OCT_END, granularity, persist=False)


def add(db, *items: Commitment) -> None:
    db.add_all(items)
    db.commit()


def split(charges) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(committed quantity, committed amount, overage quantity, overage amount)."""
    committed = [c for c in charges if c.commitment_id]
    overage = [c for c in charges if not c.commitment_id]
    return (
        sum((c.quantity for c in committed), D(0)),
        sum((c.amount for c in committed), D(0)),
        sum((c.quantity for c in overage), D(0)),
        sum((c.amount for c in overage), D(0)),
    )


def test_usage_within_the_commitment_uses_the_commitment_price(client, db, tenant):
    add(db, commitment(quantity="100", unit_price="1.50"))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=10)])
    assert split(rate(db).charges) == (D("10"), D("15"), D("0"), D("0"))


def test_overage_uses_the_plan_price(client, db, tenant):
    add(db, commitment(quantity="6", unit_price="1.50"))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=10)])
    # 6 h at 1.50 + 4 h at the plan's 2.50
    assert split(rate(db).charges) == (D("6"), D("9"), D("4"), D("10"))


def test_a_bucket_straddling_the_end_of_the_pool_is_split(client, db, tenant):
    add(db, commitment(quantity="1.5", unit_price="1.00"))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=2)])
    rows = [(c.bucket_start.hour, c.quantity, c.amount, c.commitment_id) for c in rate(db).charges]
    assert rows == [
        (0, D("1"), D("1"), "c1"),
        (1, D("0.5"), D("0.5"), "c1"),
        (1, D("0.5"), D("1.25"), None),
    ]


def test_committed_usage_does_not_touch_free_or_graduated_tiers(client, db, tenant):
    # Plan: 100 GB free, then 8.00. 300 GB: 150 committed, 100 free, 50 at 8.00.
    add(db, commitment(metric="network.bandwidth.egress.gb", quantity="150", unit_price="3.00"))
    ingest(client, [sample("e", "network.bandwidth.egress.gb", 300, OCT1, hours=3)])
    charges = rate(db).charges
    assert split(charges) == (D("150"), D("450"), D("150"), D("400"))
    assert sum(c.free_quantity for c in charges) == D("100")


def test_overage_walks_graduated_tiers_from_the_first_tier(client, db, tenant):
    # 2200 GB: 100 committed, then 100 free, 1000 at 8.00 and 1000 at 6.00.
    add(db, commitment(metric="network.bandwidth.egress.gb", quantity="100", unit_price="3.00"))
    ingest(client, [sample("e", "network.bandwidth.egress.gb", 2200, OCT1, hours=4)])
    assert split(rate(db).charges)[3] == D("14000")


def test_usage_before_a_commitment_starts_stays_pay_as_you_go(client, db, tenant):
    add(db, commitment(quantity="744", unit_price="1.00", starts_at=oct_(1, 2)))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=4)])
    assert split(rate(db).charges) == (D("2"), D("2"), D("2"), D("5"))


def test_usage_after_a_commitment_ends_stays_pay_as_you_go(client, db, tenant):
    add(db, commitment(quantity="744", unit_price="1.00", ends_at=oct_(1, 2)))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=4)])
    assert split(rate(db).charges) == (D("2"), D("2"), D("2"), D("5"))


@pytest.mark.parametrize("granularity", ["hour", "day"])
def test_boundary_inside_a_bucket_splits_usage_at_that_instant(client, db, tenant, granularity):
    add(db, commitment(quantity="744", unit_price="1.00", starts_at=oct_(1, 10, 30)))
    ingest(client, [sample("c", METRIC, 1, oct_(1, 10), hours=1)])
    assert split(rate(db, granularity).charges) == (D("0.5"), D("0.5"), D("0.5"), D("1.25"))


def test_exhausted_pool_can_be_followed_by_a_commitment_starting_later(client, db, tenant):
    add(
        db,
        commitment("early", quantity="620", unit_price="1.00", ends_at=oct_(1, 2) + WEEK),
        commitment("late", quantity="310", unit_price="2.00", starts_at=oct_(21)),
    )
    ingest(
        client,
        [
            sample("a", METRIC, 1, OCT1, hours=2),
            sample("b", METRIC, 1, oct_(25), hours=2),
        ],
    )
    run = rate(db)
    assert {(c.commitment_id, c.amount) for c in run.charges} == {
        ("early", D("1")),
        ("late", D("2")),
    }


def test_plain_tenants_rate_exactly_as_before(client, db, tenant):
    ingest(client, [sample("e", "network.bandwidth.egress.gb", 150, OCT1, hours=3)])
    run = rate(db)
    assert run.settlements == []
    assert [c.quantity for c in run.charges] == [D("50"), D("50"), D("50")]
    assert all(c.commitment_id is None for c in run.charges)


def test_commitment_of_another_tenant_is_ignored(client, db, tenant):
    client.post("/v1/tenants", json={"id": "other", "name": "Other"})
    other = commitment(quantity="100")
    other.tenant_id = "other"
    add(db, other)
    ingest(client, [sample("c", METRIC, 1, OCT1)])
    assert split(rate(db).charges) == (D("0"), D("0"), D("1"), D("2.5"))


def test_unpriced_overage_is_flagged_but_committed_usage_is_billed(client, db, tenant):
    add(db, commitment(metric="storage.snapshot.gb", quantity="4", unit_price="0.01"))
    ingest(client, [sample("s", "storage.snapshot.gb", 10, OCT1)])
    charges = rate(db).charges
    assert split(charges) == (D("4"), D("0.04"), D("6"), D("0"))
    assert [c.priced for c in charges] == [True, False]


def test_rerating_is_deterministic_and_replaces_rows(client, db, tenant):
    add(db, commitment(quantity="3", unit_price="1.50"))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=5)])
    first = rate_period(db, "acme", OCT_START, OCT_END).charges
    db.commit()
    second = rate_period(db, "acme", OCT_START, OCT_END).charges
    db.commit()
    assert split(first) == split(second)
    assert db.query(RatedCharge).filter_by(tenant_id="acme").count() == len(second) == 5


def test_budget_spend_counts_committed_usage(client, db, tenant):
    add(db, commitment(quantity="100", unit_price="1.50"))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=10)])
    assert month_to_date_spend(db, "acme", OCT1) == D("15.00")


def test_summarize_keeps_one_row_per_metric_with_the_committed_part(client, db, tenant):
    add(db, commitment(quantity="6", unit_price="1.50"))
    ingest(client, [sample("c", METRIC, 1, OCT1, hours=10)])
    rows = summarize(rate(db).charges)
    assert len(rows) == 1
    assert (rows[0]["quantity"], rows[0]["amount"]) == (D("10"), D("19"))
    assert (rows[0]["committed_quantity"], rows[0]["committed_amount"]) == (D("6"), D("9"))
