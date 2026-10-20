"""Plan lookup helpers: which plan applies to a tenant at a point in time, and its price book."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PlanAssignment, PlanPrice, PricingPlan
from .periods import as_utc
from .pricing import PriceBook, PriceSpec, Tier


class NoPlanAssigned(Exception):
    def __init__(self, tenant_id: str, at: datetime):
        super().__init__(f"tenant {tenant_id!r} has no pricing plan effective at {at.isoformat()}")
        self.tenant_id = tenant_id
        self.at = at


def plan_for(db: Session, tenant_id: str, at: datetime) -> PricingPlan | None:
    at = as_utc(at)
    assignment = db.scalar(
        select(PlanAssignment)
        .where(
            PlanAssignment.tenant_id == tenant_id,
            PlanAssignment.effective_from <= at,
            (PlanAssignment.effective_to.is_(None)) | (PlanAssignment.effective_to > at),
        )
        .order_by(PlanAssignment.effective_from.desc())
        .limit(1)
    )
    return assignment.plan if assignment else None


def price_spec_from_row(row: PlanPrice) -> PriceSpec:
    return PriceSpec(
        metric=row.metric,
        unit_price=row.unit_price,
        free_quantity=row.free_quantity,
        tiers=tuple(Tier.from_dict(t) for t in row.tiers or []),
        region=row.region,
    )


def price_book(plan: PricingPlan) -> PriceBook:
    return PriceBook([price_spec_from_row(row) for row in plan.prices])


def assign_plan(
    db: Session, tenant_id: str, plan_id: str, effective_from: datetime
) -> PlanAssignment:
    """Assign a plan from ``effective_from`` onwards, closing any open-ended earlier assignment."""
    effective_from = as_utc(effective_from)
    open_assignments = db.scalars(
        select(PlanAssignment).where(
            PlanAssignment.tenant_id == tenant_id,
            PlanAssignment.effective_to.is_(None),
            PlanAssignment.effective_from < effective_from,
        )
    ).all()
    for previous in open_assignments:
        previous.effective_to = effective_from
    assignment = PlanAssignment(
        tenant_id=tenant_id, plan_id=plan_id, effective_from=effective_from, effective_to=None
    )
    db.add(assignment)
    db.flush()
    return assignment
