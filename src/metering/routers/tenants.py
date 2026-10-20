from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_tenant, require_api_key
from ..models import Credit, Discount, PlanAssignment, PricingPlan, Tenant
from ..periods import utcnow
from ..plans import assign_plan, plan_for
from ..schemas import (
    CreditCreate,
    CreditOut,
    DiscountCreate,
    DiscountOut,
    PlanAssignIn,
    PlanAssignmentOut,
    PlanOut,
    TenantCreate,
    TenantOut,
)

router = APIRouter(prefix="/v1/tenants", tags=["tenants"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)) -> Tenant:
    if db.get(Tenant, payload.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Tenant id already exists")
    tenant = Tenant(id=payload.id, name=payload.name, default_region=payload.default_region)
    db.add(tenant)
    db.commit()
    return tenant


@router.get("", response_model=list[TenantOut])
def list_tenants(db: Session = Depends(get_db)) -> list[Tenant]:
    return list(db.scalars(select(Tenant).order_by(Tenant.created_at)))


@router.get("/{tenant_id}", response_model=TenantOut)
def get_tenant_detail(tenant: Tenant = Depends(get_tenant)) -> Tenant:
    return tenant


@router.post(
    "/{tenant_id}/plan", response_model=PlanAssignmentOut, status_code=status.HTTP_201_CREATED
)
def assign_tenant_plan(
    payload: PlanAssignIn, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> PlanAssignment:
    if db.get(PricingPlan, payload.plan_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Plan {payload.plan_id!r} not found")
    assignment = assign_plan(db, tenant.id, payload.plan_id, payload.effective_from)
    db.commit()
    return assignment


@router.get("/{tenant_id}/plan", response_model=PlanOut)
def current_plan(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> PricingPlan:
    plan = plan_for(db, tenant.id, utcnow())
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No plan currently assigned")
    return plan


@router.get("/{tenant_id}/plan/history", response_model=list[PlanAssignmentOut])
def plan_history(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[PlanAssignment]:
    return list(
        db.scalars(
            select(PlanAssignment)
            .where(PlanAssignment.tenant_id == tenant.id)
            .order_by(PlanAssignment.effective_from)
        )
    )


@router.post("/{tenant_id}/credits", response_model=CreditOut, status_code=status.HTTP_201_CREATED)
def add_credit(
    payload: CreditCreate, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> Credit:
    credit = Credit(
        tenant_id=tenant.id,
        amount=payload.amount,
        remaining=payload.amount,
        reason=payload.reason,
        expires_at=payload.expires_at,
    )
    db.add(credit)
    db.commit()
    return credit


@router.get("/{tenant_id}/credits", response_model=list[CreditOut])
def list_credits(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[Credit]:
    return list(
        db.scalars(select(Credit).where(Credit.tenant_id == tenant.id).order_by(Credit.created_at))
    )


@router.post(
    "/{tenant_id}/discounts", response_model=DiscountOut, status_code=status.HTTP_201_CREATED
)
def add_discount(
    payload: DiscountCreate, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> Discount:
    discount = Discount(tenant_id=tenant.id, **payload.model_dump())
    db.add(discount)
    db.commit()
    return discount


@router.get("/{tenant_id}/discounts", response_model=list[DiscountOut])
def list_discounts(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[Discount]:
    return list(
        db.scalars(
            select(Discount).where(Discount.tenant_id == tenant.id).order_by(Discount.valid_from)
        )
    )
