"""Pure pricing logic: unit prices, free tier and graduated tiers, all in Decimal.

Tiers are *graduated* (like income tax brackets): the first ``up_to`` units of the period are
priced at tier 1, the next slice at tier 2 and so on. Tier boundaries are expressed in billable
units, i.e. after the free quantity has been consumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CHARGE_PLACES = Decimal("0.000001")  # per-bucket charges keep 6 decimals
MONEY_PLACES = Decimal("0.01")  # invoice lines/totals are rounded once, at the end


def D(value) -> Decimal:  # noqa: N802 - short constructor on purpose
    return value if isinstance(value, Decimal) else Decimal(str(value))


def quantize_charge(amount: Decimal) -> Decimal:
    return amount.quantize(CHARGE_PLACES, rounding=ROUND_HALF_UP)


def quantize_money(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Tier:
    up_to: Decimal | None  # cumulative billable quantity where this tier ends; None = unbounded
    unit_price: Decimal

    @classmethod
    def from_dict(cls, raw: dict) -> Tier:
        up_to = raw.get("up_to")
        return cls(up_to=None if up_to is None else D(up_to), unit_price=D(raw["unit_price"]))

    def to_dict(self) -> dict:
        return {
            "up_to": None if self.up_to is None else str(self.up_to),
            "unit_price": str(self.unit_price),
        }


@dataclass(frozen=True)
class PriceSpec:
    metric: str
    unit_price: Decimal = ZERO
    free_quantity: Decimal = ZERO
    tiers: tuple[Tier, ...] = ()
    region: str | None = None

    def __post_init__(self) -> None:
        validate_tiers(self.tiers)

    def effective_tiers(self) -> tuple[Tier, ...]:
        return self.tiers or (Tier(up_to=None, unit_price=self.unit_price),)


def validate_tiers(tiers: tuple[Tier, ...] | list[Tier]) -> None:
    previous = ZERO
    for index, tier in enumerate(tiers):
        is_last = index == len(tiers) - 1
        if tier.up_to is None and not is_last:
            raise ValueError("only the last tier may be unbounded (up_to=None)")
        if tier.up_to is not None and tier.up_to <= previous:
            raise ValueError("tier boundaries must be strictly increasing")
        if tier.unit_price < ZERO:
            raise ValueError("tier unit_price cannot be negative")
        if tier.up_to is not None:
            previous = tier.up_to


@dataclass(frozen=True)
class TierSlice:
    tier_index: int  # -1 for the free tier
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass
class RatingResult:
    amount: Decimal
    slices: list[TierSlice] = field(default_factory=list)

    @property
    def billable_quantity(self) -> Decimal:
        return sum((s.quantity for s in self.slices if s.tier_index >= 0), ZERO)

    @property
    def free_quantity(self) -> Decimal:
        return sum((s.quantity for s in self.slices if s.tier_index < 0), ZERO)


def rate_increment(spec: PriceSpec, already_consumed: Decimal, quantity: Decimal) -> RatingResult:
    """Price ``quantity`` additional units given ``already_consumed`` units earlier in the period.

    Processing usage chronologically with a running ``already_consumed`` total makes free tier and
    graduated tiers work per bucket while still being exactly equal to pricing the period total.
    """
    already_consumed, quantity = D(already_consumed), D(quantity)
    if quantity <= ZERO:
        return RatingResult(amount=ZERO)

    result = RatingResult(amount=ZERO)
    remaining = quantity
    cursor = already_consumed

    # Free tier first.
    free_left = max(spec.free_quantity - cursor, ZERO)
    if free_left > ZERO:
        free_part = min(remaining, free_left)
        result.slices.append(TierSlice(-1, free_part, ZERO, ZERO))
        remaining -= free_part
        cursor += free_part
    if remaining <= ZERO:
        return result

    # Graduated tiers over billable (post-free) quantity.
    billable_cursor = cursor - spec.free_quantity
    lower = ZERO
    for index, tier in enumerate(spec.effective_tiers()):
        upper = tier.up_to
        if remaining <= ZERO:
            break
        if upper is not None and billable_cursor >= upper:
            lower = upper
            continue
        room = remaining if upper is None else min(remaining, upper - billable_cursor)
        amount = room * tier.unit_price
        result.slices.append(TierSlice(index, room, tier.unit_price, amount))
        result.amount += amount
        remaining -= room
        billable_cursor += room
        lower = upper if upper is not None else lower
    return result


def rate_total(spec: PriceSpec, quantity: Decimal) -> RatingResult:
    """Convenience: price a whole period's quantity in one go."""
    return rate_increment(spec, ZERO, quantity)


class PriceBook:
    """Resolves the price spec for (metric, region) with region-specific overrides.

    Resolution order: exact metric + region, exact metric without region, then glob patterns
    (``compute.instance.*``) with and without region.
    """

    def __init__(self, specs: list[PriceSpec]):
        self._specs = list(specs)

    def resolve(self, metric: str, region: str | None) -> PriceSpec | None:
        from fnmatch import fnmatchcase

        def pick(exact: bool, with_region: bool) -> PriceSpec | None:
            for spec in self._specs:
                name_ok = spec.metric == metric if exact else fnmatchcase(metric, spec.metric)
                region_ok = spec.region == region if with_region else spec.region is None
                if name_ok and region_ok:
                    return spec
            return None

        for exact in (True, False):
            if region is not None:
                found = pick(exact, with_region=True)
                if found:
                    return found
            found = pick(exact, with_region=False)
            if found:
                return found
        return None
