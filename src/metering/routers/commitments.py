from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..commitments import finalized_until
from ..database import get_db
from ..deps import get_tenant, require_api_key
from ..models import Commitment, Tenant
from ..periods import as_utc
from ..plans import plan_for
from ..schemas import CommitmentCreate, CommitmentOut, CommitmentUpdate

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/commitments",
    tags=["commitments"],
    dependencies=[Depends(require_api_key)],
)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def _commitment(commitment_id: str, tenant: Tenant, db: Session) -> Commitment:
    commitment = db.get(Commitment, commitment_id)
    if commitment is None or commitment.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Commitment {commitment_id!r} not found")
    return commitment


def _currency(payload: CommitmentCreate, tenant: Tenant, db: Session) -> str:
    plan = plan_for(db, tenant.id, payload.starts_at)
    if plan is None and payload.currency is None:
        raise _conflict("No plan in force at starts_at; pass currency explicitly")
    if plan is not None and payload.currency not in (None, plan.currency):
        raise _conflict(f"Plan {plan.id!r} bills in {plan.currency}, not {payload.currency}")
    return payload.currency or plan.currency


@router.post("", response_model=CommitmentOut, status_code=status.HTTP_201_CREATED)
def create_commitment(
    payload: CommitmentCreate, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> Commitment:
    if payload.id is not None and db.get(Commitment, payload.id) is not None:
        raise _conflict("Commitment id already exists")
    locked = finalized_until(db, tenant.id, payload.starts_at, payload.ends_at)
    if locked is not None:
        raise _conflict(f"A finalized invoice covers this window, up to {locked.isoformat()}")
    values = payload.model_dump(exclude_none=True, exclude={"currency"})
    commitment = Commitment(tenant_id=tenant.id, currency=_currency(payload, tenant, db), **values)
    db.add(commitment)
    try:
        db.commit()
    except IntegrityError as exc:  # lost a race on a client-chosen id
        db.rollback()
        raise _conflict("Commitment id already exists") from exc
    return commitment


@router.get("", response_model=list[CommitmentOut])
def list_commitments(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[Commitment]:
    return list(
        db.scalars(
            select(Commitment)
            .where(Commitment.tenant_id == tenant.id)
            .order_by(Commitment.starts_at, Commitment.id)
        )
    )


@router.get("/{commitment_id}", response_model=CommitmentOut)
def get_commitment(
    commitment_id: str, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> Commitment:
    return _commitment(commitment_id, tenant, db)


def _check_finalized_periods(
    db: Session, commitment: Commitment, starts_at: datetime, ends_at: datetime | None, terms: bool
) -> None:
    """Refuse a change that would contradict an invoice already finalized."""
    locks = [
        finalized_until(db, commitment.tenant_id, commitment.starts_at, commitment.ends_at),
        finalized_until(db, commitment.tenant_id, starts_at, ends_at),
    ]
    locked = max((lock for lock in locks if lock is not None), default=None)
    if locked is None:
        return
    if terms:
        raise _conflict(
            "A finalized invoice used these terms; end this commitment and add a new one"
        )
    if ends_at != commitment.ends_at:
        for end in (commitment.ends_at, ends_at):
            if end is not None and as_utc(end) < locked:
                raise _conflict(f"ends_at cannot move inside the finalized period ({locked})")


@router.patch("/{commitment_id}", response_model=CommitmentOut)
def update_commitment(
    commitment_id: str,
    payload: CommitmentUpdate,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> Commitment:
    commitment = _commitment(commitment_id, tenant, db)
    changes = payload.model_dump(exclude_unset=True)
    starts_at = changes.get("starts_at", commitment.starts_at)
    ends_at = changes.get("ends_at", commitment.ends_at)
    if ends_at is not None and ends_at <= starts_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ends_at must be after starts_at")
    terms = (
        changes.get("quantity", commitment.quantity) != commitment.quantity
        or changes.get("unit_price", commitment.unit_price) != commitment.unit_price
        or starts_at != commitment.starts_at
    )
    _check_finalized_periods(db, commitment, starts_at, ends_at, terms)
    for name, value in changes.items():
        setattr(commitment, name, value)
    db.commit()
    return commitment


@router.delete("/{commitment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_commitment(
    commitment_id: str, tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> Response:
    commitment = _commitment(commitment_id, tenant, db)
    locked = finalized_until(db, tenant.id, commitment.starts_at, commitment.ends_at)
    if locked is not None:
        raise _conflict("A finalized invoice used this commitment; set ends_at instead")
    db.delete(commitment)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
