"""Budget alerts: evaluated after every ingest for the tenants that received usage.

A rule fires once per billing period when month-to-date spend (optionally for one metric) reaches
the threshold. Events are stored and logged; hooking a webhook or email sender onto
``emit_event`` is the intended extension point.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from fnmatch import fnmatchcase

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BudgetEvent, BudgetRule
from .periods import current_period, period_key
from .plans import NoPlanAssigned
from .pricing import ZERO, quantize_money
from .rating import rate_tenant_window

log = logging.getLogger(__name__)


def month_to_date_spend(
    db: Session, tenant_id: str, now: datetime, metric: str | None = None, granularity: str = "hour"
) -> Decimal:
    start, end = current_period(now)
    try:
        charges = rate_tenant_window(db, tenant_id, start, end, granularity, persist=False)
    except NoPlanAssigned:
        return ZERO
    total = sum(
        (c.amount for c in charges if metric is None or fnmatchcase(c.metric, metric)), ZERO
    )
    return quantize_money(total)


def emit_event(db: Session, rule: BudgetRule, period: str, spend: Decimal) -> BudgetEvent:
    scope = f" on {rule.metric}" if rule.metric else ""
    message = (
        f"Budget '{rule.name}' exceeded for {period}: spend {spend:.2f}{scope} "
        f">= threshold {rule.threshold_amount:.2f}"
    )
    event = BudgetEvent(
        rule_id=rule.id,
        tenant_id=rule.tenant_id,
        period=period,
        spend=spend,
        threshold_amount=rule.threshold_amount,
        message=message,
    )
    db.add(event)
    log.warning("budget alert tenant=%s rule=%s: %s", rule.tenant_id, rule.id, message)
    return event


def evaluate_budgets(
    db: Session, tenant_id: str, now: datetime, granularity: str = "hour"
) -> list[BudgetEvent]:
    period = period_key(now)
    rules = db.scalars(
        select(BudgetRule).where(BudgetRule.tenant_id == tenant_id, BudgetRule.enabled.is_(True))
    ).all()
    if not rules:
        return []

    already = set(
        db.scalars(
            select(BudgetEvent.rule_id).where(
                BudgetEvent.tenant_id == tenant_id, BudgetEvent.period == period
            )
        )
    )
    spend_cache: dict[str | None, Decimal] = {}
    events: list[BudgetEvent] = []
    for rule in rules:
        if rule.id in already:
            continue
        if rule.metric not in spend_cache:
            spend_cache[rule.metric] = month_to_date_spend(
                db, tenant_id, now, rule.metric, granularity
            )
        spend = spend_cache[rule.metric]
        if spend >= rule.threshold_amount:
            events.append(emit_event(db, rule, period, spend))
    if events:
        db.commit()
    return events
