from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from metering.commitments import CommitmentLedger, active_at, consumption_order, entitlement
from metering.models import Commitment
from metering.periods import parse_period
from metering.pricing import prorate

D = Decimal
UTC = timezone.utc
METRIC = "compute.instance.m1.small"
OCT_START, OCT_END = parse_period("2026-10")
FEB_START, FEB_END = parse_period("2026-02")


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
