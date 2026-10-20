from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_api_key
from ..models import PlanPrice, PricingPlan
from ..schemas import PlanCreate, PlanOut

router = APIRouter(prefix="/v1/plans", tags=["plans"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(payload: PlanCreate, db: Session = Depends(get_db)) -> PricingPlan:
    if db.get(PricingPlan, payload.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Plan id already exists")
    plan = PricingPlan(
        id=payload.id,
        name=payload.name,
        currency=payload.currency.upper(),
        description=payload.description,
    )
    for p in payload.prices:
        plan.prices.append(
            PlanPrice(
                metric=p.metric,
                region=p.region,
                unit_price=p.unit_price,
                free_quantity=p.free_quantity,
                tiers=[
                    {
                        "up_to": None if t.up_to is None else str(t.up_to),
                        "unit_price": str(t.unit_price),
                    }
                    for t in p.tiers
                ],
            )
        )
    db.add(plan)
    db.commit()
    return plan


@router.get("", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db)) -> list[PricingPlan]:
    return list(db.scalars(select(PricingPlan).order_by(PricingPlan.created_at)))


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str, db: Session = Depends(get_db)) -> PricingPlan:
    plan = db.get(PricingPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return plan
