"""Rating engine: usage samples -> time buckets -> priced charges.

Steps for one tenant and billing window:

1. Load samples overlapping the window and clip each to it.
2. Split every sample across hourly (or daily) buckets. Gauges become ``level x hours`` per bucket;
   deltas are divided proportionally to the time overlap. This is the proration step.
3. Aggregate to (metric, region, bucket) quantities.
4. Walk buckets chronologically, resolve the plan in force at each bucket start, and price the
   increment with a running per-(metric, region) consumed total so free tiers and graduated tiers
   apply across the whole period.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .metrics import MetricKind, MetricSpec, resolve_metric
from .models import RatedCharge, UsageSample
from .periods import as_utc, iter_buckets, overlap_seconds
from .plans import NoPlanAssigned, PriceBook, plan_for, price_book
from .pricing import ZERO, D, quantize_charge, rate_increment

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BucketUsage:
    metric: str
    region: str | None
    bucket_start: datetime
    bucket_end: datetime
    quantity: Decimal
    spec: MetricSpec


def prorate_sample(
    sample: UsageSample,
    window_start: datetime,
    window_end: datetime,
    granularity: str = "hour",
) -> list[BucketUsage]:
    """Split one sample into bucket quantities, clipped to [window_start, window_end)."""
    spec = resolve_metric(sample.metric)
    if spec is None:
        return []
    s_start, s_end = as_utc(sample.start), as_utc(sample.end)
    clip_start, clip_end = max(s_start, as_utc(window_start)), min(s_end, as_utc(window_end))
    if clip_end <= clip_start:
        return []
    total_seconds = (s_end - s_start).total_seconds()
    quantity = D(sample.quantity)

    out: list[BucketUsage] = []
    for b_start, b_end in iter_buckets(clip_start, clip_end, granularity):
        seconds = overlap_seconds(clip_start, clip_end, b_start, b_end)
        if seconds <= 0:
            continue
        if spec.kind is MetricKind.GAUGE:
            bucket_qty = quantity * D(seconds) / D(3600)
        else:
            bucket_qty = quantity * D(seconds) / D(total_seconds)
        out.append(BucketUsage(sample.metric, sample.region, b_start, b_end, bucket_qty, spec))
    return out


def aggregate(buckets: list[BucketUsage]) -> list[BucketUsage]:
    """Sum quantities per (metric, region, bucket_start), sorted chronologically."""
    totals: dict[tuple, BucketUsage] = {}
    sums: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for b in buckets:
        key = (b.bucket_start, b.metric, b.region)
        sums[key] += b.quantity
        totals.setdefault(key, b)
    merged = [
        BucketUsage(b.metric, b.region, b.bucket_start, b.bucket_end, sums[key], b.spec)
        for key, b in totals.items()
    ]
    merged.sort(key=lambda b: (b.bucket_start, b.metric, b.region or ""))
    return merged


def load_samples(
    db: Session, tenant_id: str, window_start: datetime, window_end: datetime
) -> list[UsageSample]:
    return list(
        db.scalars(
            select(UsageSample).where(
                UsageSample.tenant_id == tenant_id,
                UsageSample.start < as_utc(window_end),
                UsageSample.end > as_utc(window_start),
            )
        )
    )


def rate_buckets(db: Session, tenant_id: str, buckets: list[BucketUsage]) -> list[RatedCharge]:
    """Price aggregated buckets. Raises NoPlanAssigned if a bucket has no plan in force."""
    consumed: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
    books: dict[str, PriceBook] = {}
    charges: list[RatedCharge] = []

    for b in buckets:
        plan = plan_for(db, tenant_id, b.bucket_start)
        if plan is None:
            raise NoPlanAssigned(tenant_id, b.bucket_start)
        book = books.setdefault(plan.id, price_book(plan))
        spec = book.resolve(b.metric, b.region)
        key = (b.metric, b.region)

        if spec is None:
            log.warning(
                "no price for metric=%s region=%s in plan=%s (tenant=%s)",
                b.metric,
                b.region,
                plan.id,
                tenant_id,
            )
            charges.append(
                RatedCharge(
                    tenant_id=tenant_id,
                    plan_id=plan.id,
                    metric=b.metric,
                    region=b.region,
                    rated_unit=b.spec.rated_unit,
                    bucket_start=b.bucket_start,
                    bucket_end=b.bucket_end,
                    quantity=quantize_charge(b.quantity),
                    free_quantity=ZERO,
                    amount=ZERO,
                    priced=False,
                )
            )
            continue

        result = rate_increment(spec, consumed[key], b.quantity)
        consumed[key] += b.quantity
        charges.append(
            RatedCharge(
                tenant_id=tenant_id,
                plan_id=plan.id,
                metric=b.metric,
                region=b.region,
                rated_unit=b.spec.rated_unit,
                bucket_start=b.bucket_start,
                bucket_end=b.bucket_end,
                quantity=quantize_charge(b.quantity),
                free_quantity=quantize_charge(result.free_quantity),
                amount=quantize_charge(result.amount),
                priced=True,
            )
        )
    return charges


def rate_tenant_window(
    db: Session,
    tenant_id: str,
    window_start: datetime,
    window_end: datetime,
    granularity: str = "hour",
    persist: bool = True,
) -> list[RatedCharge]:
    """Rate everything a tenant used inside the window. Re-rating replaces stored charges."""
    samples = load_samples(db, tenant_id, window_start, window_end)
    buckets: list[BucketUsage] = []
    for sample in samples:
        buckets.extend(prorate_sample(sample, window_start, window_end, granularity))
    charges = rate_buckets(db, tenant_id, aggregate(buckets))

    if persist:
        db.execute(
            delete(RatedCharge).where(
                RatedCharge.tenant_id == tenant_id,
                RatedCharge.bucket_start >= as_utc(window_start),
                RatedCharge.bucket_start < as_utc(window_end),
            )
        )
        db.add_all(charges)
        db.flush()
    return charges


def summarize(charges: list[RatedCharge]) -> list[dict]:
    """Collapse charges to one row per (metric, region)."""
    grouped: dict[tuple[str, str | None], dict] = {}
    for c in charges:
        row = grouped.setdefault(
            (c.metric, c.region),
            {
                "metric": c.metric,
                "region": c.region,
                "rated_unit": c.rated_unit,
                "quantity": ZERO,
                "free_quantity": ZERO,
                "amount": ZERO,
                "priced": c.priced,
            },
        )
        row["quantity"] += c.quantity
        row["free_quantity"] += c.free_quantity
        row["amount"] += c.amount
        row["priced"] = row["priced"] and c.priced
    return sorted(grouped.values(), key=lambda r: (r["metric"], r["region"] or ""))
