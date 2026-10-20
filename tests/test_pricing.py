from __future__ import annotations

from decimal import Decimal

import pytest

from metering.pricing import PriceBook, PriceSpec, Tier, rate_increment, rate_total, validate_tiers

D = Decimal


def test_flat_unit_price():
    spec = PriceSpec("compute.instance.m1.small", unit_price=D("2.50"))
    result = rate_total(spec, D("10"))
    assert result.amount == D("25.00")
    assert result.billable_quantity == D("10")
    assert result.free_quantity == D("0")


def test_free_tier_then_unit_price():
    spec = PriceSpec("network.floating_ip", unit_price=D("0.50"), free_quantity=D("100"))
    result = rate_total(spec, D("150"))
    assert result.free_quantity == D("100")
    assert result.billable_quantity == D("50")
    assert result.amount == D("25.00")
    assert [s.tier_index for s in result.slices] == [-1, 0]


def test_usage_within_free_tier_costs_nothing():
    spec = PriceSpec("network.floating_ip", unit_price=D("0.50"), free_quantity=D("744"))
    result = rate_total(spec, D("10"))
    assert result.amount == D("0")
    assert result.free_quantity == D("10")


def test_graduated_tiers():
    spec = PriceSpec(
        "network.bandwidth.egress.gb",
        tiers=(Tier(D("100"), D("1.0")), Tier(D("500"), D("0.8")), Tier(None, D("0.5"))),
    )
    result = rate_total(spec, D("700"))
    # 100 @ 1.0 + 400 @ 0.8 + 200 @ 0.5
    assert result.amount == D("520.0")
    assert [(s.tier_index, s.quantity) for s in result.slices] == [
        (0, D("100")),
        (1, D("400")),
        (2, D("200")),
    ]


def test_incremental_rating_equals_total_rating():
    spec = PriceSpec(
        "network.bandwidth.egress.gb",
        free_quantity=D("50"),
        tiers=(Tier(D("100"), D("1.0")), Tier(D("500"), D("0.8")), Tier(None, D("0.5"))),
    )
    increments = [D("30"), D("100"), D("250"), D("320"), D("0.5")]
    consumed, total = D("0"), D("0")
    for inc in increments:
        total += rate_increment(spec, consumed, inc).amount
        consumed += inc
    assert total == rate_total(spec, sum(increments)).amount


def test_free_tier_combined_with_tiers():
    spec = PriceSpec(
        "x", free_quantity=D("50"), tiers=(Tier(D("100"), D("1")), Tier(None, D("0.5")))
    )
    result = rate_total(spec, D("200"))
    # 50 free, then 100 @ 1 and 50 @ 0.5 over the billable 150
    assert result.free_quantity == D("50")
    assert result.amount == D("125.0")


def test_zero_quantity_is_free():
    spec = PriceSpec("x", unit_price=D("9"))
    assert rate_total(spec, D("0")).amount == D("0")
    assert rate_increment(spec, D("5"), D("-1")).amount == D("0")


def test_tier_validation():
    with pytest.raises(ValueError):
        validate_tiers([Tier(None, D("1")), Tier(D("10"), D("1"))])  # unbounded not last
    with pytest.raises(ValueError):
        validate_tiers([Tier(D("10"), D("1")), Tier(D("10"), D("1"))])  # not increasing
    with pytest.raises(ValueError):
        validate_tiers([Tier(D("10"), D("-1"))])
    validate_tiers([Tier(D("10"), D("1")), Tier(None, D("0.5"))])


def test_price_book_resolution_order():
    book = PriceBook(
        [
            PriceSpec("compute.instance.*", unit_price=D("15")),
            PriceSpec("compute.instance.m1.small", unit_price=D("2.5")),
            PriceSpec("compute.instance.m1.small", unit_price=D("2.0"), region="ctg-1"),
            PriceSpec("compute.instance.*", unit_price=D("12"), region="ctg-1"),
        ]
    )
    assert book.resolve("compute.instance.m1.small", "ctg-1").unit_price == D("2.0")
    assert book.resolve("compute.instance.m1.small", "dhaka-1").unit_price == D("2.5")
    assert book.resolve("compute.instance.m1.small", None).unit_price == D("2.5")
    assert book.resolve("compute.instance.c2.xlarge", "ctg-1").unit_price == D("12")
    assert book.resolve("compute.instance.c2.xlarge", "dhaka-1").unit_price == D("15")
    assert book.resolve("storage.volume.gb", None) is None
