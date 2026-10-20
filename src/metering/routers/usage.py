from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..budgets import evaluate_budgets
from ..database import get_db
from ..deps import get_tenant, require_api_key
from ..ingest import ingest_samples
from ..models import Tenant, UsageSample
from ..periods import parse_period, period_key
from ..plans import NoPlanAssigned, plan_for
from ..pricing import ZERO, quantize_money
from ..rating import rate_tenant_window, summarize
from ..schemas import IngestOut, SummaryLineOut, UsageBatchIn, UsageSampleOut, UsageSummaryOut

router = APIRouter(tags=["usage"], dependencies=[Depends(require_api_key)])


@router.post("/v1/usage", response_model=IngestOut, status_code=status.HTTP_202_ACCEPTED)
def ingest(payload: UsageBatchIn, request: Request, db: Session = Depends(get_db)) -> IngestOut:
    settings = request.app.state.settings
    if len(payload.samples) > settings.max_batch_size:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Batch too large: max {settings.max_batch_size} samples",
        )
    result = ingest_samples(db, payload.samples)

    # Evaluate budgets for every (tenant, billing period) this batch touched.
    touched: set[tuple[str, str]] = set()
    for s in payload.samples:
        if s.tenant_id in result.tenants:
            touched.add((s.tenant_id, period_key(s.start)))
    events = []
    for tenant_id, period in sorted(touched):
        at, _ = parse_period(period)
        events.extend(evaluate_budgets(db, tenant_id, at, settings.rating_granularity))

    return IngestOut(
        accepted=result.accepted,
        duplicates=result.duplicates,
        rejected=result.rejected,
        budget_events=events,
    )


@router.get("/v1/usage", response_model=list[UsageSampleOut])
def list_usage(
    db: Session = Depends(get_db),
    tenant_id: str | None = None,
    metric: str | None = None,
    resource_id: str | None = None,
    start: datetime | None = Query(default=None, description="Only samples ending after this"),
    end: datetime | None = Query(default=None, description="Only samples starting before this"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[UsageSample]:
    stmt = select(UsageSample)
    if tenant_id:
        stmt = stmt.where(UsageSample.tenant_id == tenant_id)
    if metric:
        stmt = stmt.where(UsageSample.metric == metric)
    if resource_id:
        stmt = stmt.where(UsageSample.resource_id == resource_id)
    if start:
        stmt = stmt.where(UsageSample.end > start)
    if end:
        stmt = stmt.where(UsageSample.start < end)
    stmt = stmt.order_by(UsageSample.start, UsageSample.id).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/v1/tenants/{tenant_id}/usage/summary", response_model=UsageSummaryOut)
def usage_summary(
    request: Request,
    period: str = Query(..., description="Billing period, YYYY-MM"),
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> UsageSummaryOut:
    """Rate the period on the fly (without persisting) and return per-metric quantities and cost."""
    try:
        start, end = parse_period(period)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    granularity = request.app.state.settings.rating_granularity
    plan = plan_for(db, tenant.id, start)
    try:
        charges = rate_tenant_window(db, tenant.id, start, end, granularity, persist=False)
    except NoPlanAssigned as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    lines = [SummaryLineOut(**row) for row in summarize(charges)]
    return UsageSummaryOut(
        tenant_id=tenant.id,
        period=period,
        currency=plan.currency if plan else "",
        granularity=granularity,
        lines=lines,
        subtotal=quantize_money(sum((ln.amount for ln in lines), ZERO)),
    )
