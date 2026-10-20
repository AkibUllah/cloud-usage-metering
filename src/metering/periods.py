"""Billing periods and time buckets. Everything is UTC."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_period(period: str) -> tuple[datetime, datetime]:
    """``"2026-10"`` -> (2026-10-01T00:00Z, 2026-11-01T00:00Z)."""
    match = _PERIOD_RE.match(period)
    if not match:
        raise ValueError(f"period must look like YYYY-MM, got {period!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month in period {period!r}")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    return start, end


def period_key(dt: datetime) -> str:
    dt = as_utc(dt)
    return f"{dt.year:04d}-{dt.month:02d}"


def current_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    return parse_period(period_key(now or utcnow()))


def floor_to(dt: datetime, granularity: str) -> datetime:
    dt = as_utc(dt)
    if granularity == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown granularity {granularity!r}")


def bucket_size(granularity: str) -> timedelta:
    if granularity == "hour":
        return HOUR
    if granularity == "day":
        return DAY
    raise ValueError(f"unknown granularity {granularity!r}")


def iter_buckets(
    start: datetime, end: datetime, granularity: str
) -> Iterator[tuple[datetime, datetime]]:
    """Yield (bucket_start, bucket_end) windows covering [start, end)."""
    size = bucket_size(granularity)
    cursor = floor_to(start, granularity)
    end = as_utc(end)
    while cursor < end:
        yield cursor, cursor + size
        cursor += size


def overlap_seconds(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    start = max(as_utc(a_start), as_utc(b_start))
    end = min(as_utc(a_end), as_utc(b_end))
    return max((end - start).total_seconds(), 0.0)
