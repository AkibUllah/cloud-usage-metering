"""Batch ingestion with per-sample validation and idempotent dedupe on the sample id."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .metrics import is_known_metric
from .models import Tenant, UsageSample
from .schemas import UsageSampleIn

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict] = field(default_factory=list)
    tenants: set[str] = field(default_factory=set)


def ingest_samples(db: Session, samples: list[UsageSampleIn]) -> IngestResult:
    result = IngestResult()
    if not samples:
        return result

    ids = [s.id for s in samples]
    existing = set(db.scalars(select(UsageSample.id).where(UsageSample.id.in_(ids))))
    known_tenants = set(
        db.scalars(select(Tenant.id).where(Tenant.id.in_({s.tenant_id for s in samples})))
    )
    seen_in_batch: set[str] = set()

    for s in samples:
        if s.id in existing or s.id in seen_in_batch:
            result.duplicates += 1
            continue
        if s.tenant_id not in known_tenants:
            result.rejected.append({"id": s.id, "reason": f"unknown tenant {s.tenant_id!r}"})
            continue
        if not is_known_metric(s.metric):
            result.rejected.append({"id": s.id, "reason": f"unknown metric {s.metric!r}"})
            continue
        seen_in_batch.add(s.id)
        db.add(
            UsageSample(
                id=s.id,
                tenant_id=s.tenant_id,
                resource_id=s.resource_id,
                metric=s.metric,
                quantity=s.quantity,
                region=s.region,
                start=s.start,
                end=s.end,
                attributes=s.attributes,
            )
        )
        result.accepted += 1
        result.tenants.add(s.tenant_id)

    db.commit()
    log.info(
        "ingest accepted=%d duplicates=%d rejected=%d",
        result.accepted,
        result.duplicates,
        len(result.rejected),
    )
    return result
