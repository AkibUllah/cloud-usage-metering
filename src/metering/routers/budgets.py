from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_tenant, require_api_key
from ..models import BudgetEvent, BudgetRule, Tenant
from ..schemas import BudgetEventOut, BudgetRuleCreate, BudgetRuleOut, MessageOut

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}", tags=["budgets"], dependencies=[Depends(require_api_key)]
)


@router.post("/budgets", response_model=BudgetRuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: BudgetRuleCreate, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> BudgetRule:
    rule = BudgetRule(tenant_id=tenant.id, **payload.model_dump())
    db.add(rule)
    db.commit()
    return rule


@router.get("/budgets", response_model=list[BudgetRuleOut])
def list_rules(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[BudgetRule]:
    return list(
        db.scalars(
            select(BudgetRule)
            .where(BudgetRule.tenant_id == tenant.id)
            .order_by(BudgetRule.created_at)
        )
    )


@router.delete("/budgets/{rule_id}", response_model=MessageOut)
def delete_rule(
    rule_id: str, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> MessageOut:
    rule = db.scalar(
        select(BudgetRule).where(BudgetRule.id == rule_id, BudgetRule.tenant_id == tenant.id)
    )
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Budget rule not found")
    db.delete(rule)
    db.commit()
    return MessageOut(message="Budget rule deleted")


@router.get("/budget-events", response_model=list[BudgetEventOut])
def list_events(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[BudgetEvent]:
    return list(
        db.scalars(
            select(BudgetEvent)
            .where(BudgetEvent.tenant_id == tenant.id)
            .order_by(BudgetEvent.triggered_at.desc())
        )
    )
