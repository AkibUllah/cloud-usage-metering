"""Commitments: prepaid quantity per billing period, drawn down before pay-as-you-go pricing.

A commitment gives the tenant ``quantity`` units per billing period. A commitment that only covers
part of a period gets the same share of its quantity as of the period's time, so 300 units a month
active for the last 10 days of October is entitled to ``300 x 10 / 31`` units. Usage draws that
pool down in time order and only while the commitment is active; what the pool cannot cover falls
through to the plan's prices. Whatever is left at the end of the period is still owed, so the fee
for a period depends on the commitment alone and never on how much of it was used.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Commitment
from .periods import as_utc, microseconds, overlap_microseconds, parse_period, period_key
from .pricing import ZERO, D, extended_amount, prorate, quantize_charge, quantize_money

NO_END = datetime.max.replace(tzinfo=timezone.utc)


class CommitmentCurrencyMismatch(Exception):
    """The tenant's plan moved to a currency a commitment was not sold in."""

    def __init__(self, commitment_id: str, committed: str, billed: str):
        super().__init__(
            f"commitment {commitment_id!r} is in {committed} but the invoice is in {billed}"
        )


def active_at(commitment: Commitment, at: datetime) -> bool:
    return as_utc(commitment.starts_at) <= as_utc(at) < _end(commitment)


def entitlement(commitment: Commitment, period_start: datetime, period_end: datetime) -> Decimal:
    """The quantity a commitment is entitled to in a period, prorated by the time it is active."""
    active = overlap_microseconds(commitment.starts_at, _end(commitment), period_start, period_end)
    return prorate(commitment.quantity, active, microseconds(period_end - period_start))


def consumption_order(commitment: Commitment) -> tuple:
    """Region-specific first, then soonest to end, so usage goes where nothing else could cover it.

    Unit price and id only break ties, which keeps re-rating deterministic.
    """
    return (
        commitment.region is None,
        _end(commitment),
        D(commitment.unit_price),
        as_utc(commitment.starts_at),
        commitment.id,
    )


def _end(commitment: Commitment) -> datetime:
    return NO_END if commitment.ends_at is None else as_utc(commitment.ends_at)


@dataclass(frozen=True)
class Draw:
    """Part of a usage bucket covered by one commitment."""

    commitment: Commitment
    quantity: Decimal


@dataclass(frozen=True)
class Settlement:
    """Where a commitment stands for one billing period.

    ``committed_amount + unused_amount`` always equals the rounded fee for the entitlement.
    """

    commitment: Commitment
    period: str
    entitlement: Decimal
    used: Decimal
    unused: Decimal
    committed_amount: Decimal
    unused_amount: Decimal


class CommitmentLedger:
    """Tracks what each commitment has left per billing period during one rating run."""

    def __init__(self, commitments: list[Commitment]):
        self._commitments = sorted(commitments, key=consumption_order)
        self._used: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)

    def __bool__(self) -> bool:
        return bool(self._commitments)

    def cut_points(self, window_start: datetime, window_end: datetime) -> list[datetime]:
        """Commitment boundaries inside the window, where rating buckets must be split."""
        start, end = as_utc(window_start), as_utc(window_end)
        edges = {as_utc(c.starts_at) for c in self._commitments}
        edges |= {as_utc(c.ends_at) for c in self._commitments if c.ends_at is not None}
        return sorted(edge for edge in edges if start < edge < end)

    def draw(
        self, metric: str, region: str | None, at: datetime, quantity: Decimal
    ) -> tuple[list[Draw], Decimal]:
        """Cover ``quantity`` of usage at ``at``; returns the draws and the uncovered remainder."""
        draws: list[Draw] = []
        remaining = quantity
        period = period_key(at)
        for commitment in self._commitments:
            if remaining <= ZERO:
                break
            if commitment.metric != metric or not active_at(commitment, at):
                continue
            if commitment.region is not None and commitment.region != region:
                continue
            key = (commitment.id, period)
            left = entitlement(commitment, *parse_period(period)) - self._used[key]
            part = min(left, remaining)
            if part <= ZERO:
                continue
            self._used[key] += part
            remaining -= part
            draws.append(Draw(commitment, part))
        return draws, remaining

    def settle(self, period_start: datetime, period_end: datetime) -> list[Settlement]:
        """Settle every commitment with a non-zero entitlement in the period, in draw order."""
        period = period_key(period_start)
        settlements: list[Settlement] = []
        for commitment in self._commitments:
            entitled = entitlement(commitment, period_start, period_end)
            if entitled <= ZERO:
                continue
            used = quantize_charge(self._used[(commitment.id, period)])
            fee = quantize_money(extended_amount(entitled, commitment.unit_price))
            committed_amount = quantize_money(extended_amount(used, commitment.unit_price))
            settlements.append(
                Settlement(
                    commitment=commitment,
                    period=period,
                    entitlement=entitled,
                    used=used,
                    unused=entitled - used,
                    committed_amount=committed_amount,
                    unused_amount=fee - committed_amount,
                )
            )
        return settlements


def load_commitments(
    db: Session, tenant_id: str, window_start: datetime, window_end: datetime
) -> list[Commitment]:
    """Commitments of a tenant active at any moment of the window."""
    return list(
        db.scalars(
            select(Commitment).where(
                Commitment.tenant_id == tenant_id,
                Commitment.starts_at < as_utc(window_end),
                (Commitment.ends_at.is_(None)) | (Commitment.ends_at > as_utc(window_start)),
            )
        )
    )
