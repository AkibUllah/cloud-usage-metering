"""Invoice generation: rated charges -> lines -> discounts -> credits -> totals, plus CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from fnmatch import fnmatchcase

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Credit, Discount, Invoice, InvoiceLine, RatedCharge, Tenant
from .periods import as_utc, parse_period, utcnow
from .plans import NoPlanAssigned, plan_for
from .pricing import ZERO, quantize_money
from .rating import rate_tenant_window, summarize


class InvoiceFinalized(Exception):
    pass


def _next_number(db: Session, period: str) -> str:
    count = (
        db.scalar(select(func.count()).select_from(Invoice).where(Invoice.period == period)) or 0
    )
    return f"INV-{period.replace('-', '')}-{count + 1:05d}"


def active_discounts(db: Session, tenant_id: str, start: datetime, end: datetime) -> list[Discount]:
    return list(
        db.scalars(
            select(Discount).where(
                Discount.tenant_id == tenant_id,
                Discount.valid_from < as_utc(end),
                (Discount.valid_to.is_(None)) | (Discount.valid_to > as_utc(start)),
            )
        )
    )


def available_credits(db: Session, tenant_id: str, at: datetime) -> list[Credit]:
    return list(
        db.scalars(
            select(Credit)
            .where(
                Credit.tenant_id == tenant_id,
                Credit.remaining > 0,
                (Credit.expires_at.is_(None)) | (Credit.expires_at > as_utc(at)),
            )
            .order_by(Credit.expires_at.is_(None), Credit.expires_at, Credit.created_at)
        )
    )


def apply_discounts(lines: list[dict], discounts: list[Discount]) -> tuple[Decimal, list[dict]]:
    total = ZERO
    applied: list[dict] = []
    for d in discounts:
        base = sum(
            (
                ln["amount"]
                for ln in lines
                if d.metric is None or fnmatchcase(ln["metric"], d.metric)
            ),
            ZERO,
        )
        amount = quantize_money(base * d.percent / Decimal(100))
        if amount <= ZERO:
            continue
        total += amount
        applied.append(
            {
                "discount_id": d.id,
                "description": d.description or f"{d.percent}% discount",
                "percent": str(d.percent),
                "metric": d.metric,
                "amount": str(amount),
            }
        )
    return total, applied


def plan_credits(credits: list[Credit], due: Decimal) -> tuple[Decimal, list[dict]]:
    """Decide how much of each credit to use, oldest-expiring first. Does not mutate."""
    total = ZERO
    applied: list[dict] = []
    for c in credits:
        if due <= ZERO:
            break
        use = min(c.remaining, due)
        if use <= ZERO:
            continue
        due -= use
        total += use
        applied.append({"credit_id": c.id, "reason": c.reason, "amount": str(quantize_money(use))})
    return quantize_money(total), applied


def build_lines(charges: list[RatedCharge], currency: str) -> list[dict]:
    lines: list[dict] = []
    for pos, row in enumerate(summarize(charges), start=1):
        billable = row["quantity"] - row["free_quantity"]
        amount = quantize_money(row["amount"])
        unit_price = (row["amount"] / billable) if billable > ZERO else ZERO
        region = f" ({row['region']})" if row["region"] else ""
        free = f", {row['free_quantity']:.2f} free" if row["free_quantity"] > ZERO else ""
        note = "" if row["priced"] else " [no price configured]"
        lines.append(
            {
                "position": pos,
                "metric": row["metric"],
                "region": row["region"],
                "rated_unit": row["rated_unit"],
                "description": (
                    f"{row['metric']}{region}: {row['quantity']:.2f} "
                    f"{row['rated_unit']}{free}{note}"
                ),
                "quantity": row["quantity"],
                "free_quantity": row["free_quantity"],
                "unit_price": unit_price.quantize(Decimal("0.00000001")),
                "amount": amount,
            }
        )
    return lines


def generate_invoice(
    db: Session, tenant: Tenant, period: str, granularity: str = "hour"
) -> Invoice:
    """Create or refresh the draft invoice for a tenant and YYYY-MM period."""
    start, end = parse_period(period)
    existing = db.scalar(
        select(Invoice).where(Invoice.tenant_id == tenant.id, Invoice.period == period)
    )
    if existing is not None and existing.status == "finalized":
        raise InvoiceFinalized(f"invoice {existing.number} for {period} is already finalized")

    plan = plan_for(db, tenant.id, start)
    if plan is None:
        raise NoPlanAssigned(tenant.id, start)

    charges = rate_tenant_window(db, tenant.id, start, end, granularity=granularity, persist=True)
    lines = build_lines(charges, plan.currency)
    subtotal = sum((ln["amount"] for ln in lines), ZERO)
    discount_total, discounts_applied = apply_discounts(
        lines, active_discounts(db, tenant.id, start, end)
    )
    due = max(subtotal - discount_total, ZERO)
    credit_total, credits_applied = plan_credits(available_credits(db, tenant.id, end), due)
    total = max(due - credit_total, ZERO)

    if existing is None:
        existing = Invoice(
            number=_next_number(db, period),
            tenant_id=tenant.id,
            period=period,
            period_start=start,
            period_end=end,
            currency=plan.currency,
            status="draft",
            subtotal=ZERO,
            discount_total=ZERO,
            credit_total=ZERO,
            total=ZERO,
        )
        db.add(existing)
        db.flush()
    else:
        existing.lines.clear()
        db.flush()

    existing.currency = plan.currency
    existing.subtotal = quantize_money(subtotal)
    existing.discount_total = discount_total
    existing.credit_total = credit_total
    existing.total = quantize_money(total)
    existing.discounts_applied = discounts_applied
    existing.credits_applied = credits_applied
    existing.lines = [InvoiceLine(**ln) for ln in lines]
    db.commit()
    db.refresh(existing)
    return existing


def finalize_invoice(db: Session, invoice: Invoice) -> Invoice:
    """Lock the invoice and actually consume the credits it planned to use."""
    if invoice.status == "finalized":
        return invoice
    due = max(invoice.subtotal - invoice.discount_total, ZERO)
    credits = available_credits(db, invoice.tenant_id, invoice.period_end)
    credit_total, applied = plan_credits(credits, due)
    by_id = {c.id: c for c in credits}
    for entry in applied:
        credit = by_id[entry["credit_id"]]
        credit.remaining = credit.remaining - Decimal(entry["amount"])
    invoice.credit_total = credit_total
    invoice.credits_applied = applied
    invoice.total = quantize_money(max(due - credit_total, ZERO))
    invoice.status = "finalized"
    invoice.finalized_at = utcnow()
    db.commit()
    db.refresh(invoice)
    return invoice


def invoice_to_csv(invoice: Invoice) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "invoice",
            "tenant_id",
            "period",
            "position",
            "metric",
            "region",
            "rated_unit",
            "quantity",
            "free_quantity",
            "unit_price",
            "amount",
            "currency",
        ]
    )
    for ln in invoice.lines:
        writer.writerow(
            [
                invoice.number,
                invoice.tenant_id,
                invoice.period,
                ln.position,
                ln.metric,
                ln.region or "",
                ln.rated_unit,
                f"{ln.quantity:.6f}",
                f"{ln.free_quantity:.6f}",
                f"{ln.unit_price:.8f}",
                f"{ln.amount:.2f}",
                invoice.currency,
            ]
        )
    writer.writerow([])
    writer.writerow(["subtotal", f"{invoice.subtotal:.2f}", invoice.currency])
    writer.writerow(["discounts", f"-{invoice.discount_total:.2f}", invoice.currency])
    writer.writerow(["credits", f"-{invoice.credit_total:.2f}", invoice.currency])
    writer.writerow(["total", f"{invoice.total:.2f}", invoice.currency])
    return buf.getvalue()
