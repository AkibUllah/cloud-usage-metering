from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_tenant, require_api_key
from ..invoices import InvoiceFinalized, finalize_invoice, generate_invoice, invoice_to_csv
from ..models import Invoice, Tenant
from ..periods import parse_period
from ..plans import NoPlanAssigned
from ..schemas import InvoiceOut

router = APIRouter(tags=["invoices"], dependencies=[Depends(require_api_key)])


def _invoice(db: Session, invoice_id: str) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return invoice


@router.post(
    "/v1/tenants/{tenant_id}/invoices",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_invoice(
    request: Request,
    period: str = Query(..., description="Billing period, YYYY-MM"),
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> Invoice:
    try:
        parse_period(period)
        return generate_invoice(db, tenant, period, request.app.state.settings.rating_granularity)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except NoPlanAssigned as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except InvoiceFinalized as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/tenants/{tenant_id}/invoices", response_model=list[InvoiceOut])
def list_invoices(
    tenant: Tenant = Depends(get_tenant), db: Session = Depends(get_db)
) -> list[Invoice]:
    return list(
        db.scalars(select(Invoice).where(Invoice.tenant_id == tenant.id).order_by(Invoice.period))
    )


@router.get("/v1/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, db: Session = Depends(get_db)) -> Invoice:
    return _invoice(db, invoice_id)


@router.get("/v1/invoices/{invoice_id}/csv", response_class=PlainTextResponse)
def invoice_csv(invoice_id: str, db: Session = Depends(get_db)) -> PlainTextResponse:
    invoice = _invoice(db, invoice_id)
    return PlainTextResponse(
        invoice_to_csv(invoice),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{invoice.number}.csv"'},
    )


@router.post("/v1/invoices/{invoice_id}/finalize", response_model=InvoiceOut)
def finalize(invoice_id: str, db: Session = Depends(get_db)) -> Invoice:
    return finalize_invoice(db, _invoice(db, invoice_id))
