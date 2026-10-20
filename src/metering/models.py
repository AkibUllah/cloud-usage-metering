"""ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base, Money, UTCDateTime
from .periods import utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    assignments: Mapped[list[PlanAssignment]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class UsageSample(Base):
    """One metering record. The client-supplied ``id`` is the dedupe key."""

    __tablename__ = "usage_samples"
    __table_args__ = (
        Index("ix_usage_tenant_window", "tenant_id", "start", "end"),
        Index("ix_usage_tenant_metric", "tenant_id", "metric"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PricingPlan(Base):
    __tablename__ = "pricing_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    prices: Mapped[list[PlanPrice]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanPrice.metric"
    )


class PlanPrice(Base):
    __tablename__ = "plan_prices"
    __table_args__ = (
        UniqueConstraint("plan_id", "metric", "region", name="uq_plan_metric_region"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("pricing_plans.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(128), nullable=False)  # exact name or glob
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    free_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    tiers: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    plan: Mapped[PricingPlan] = relationship(back_populates="prices")


class PlanAssignment(Base):
    __tablename__ = "plan_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("pricing_plans.id", ondelete="RESTRICT"))
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="assignments")
    plan: Mapped[PricingPlan] = relationship()


class RatedCharge(Base):
    """Output of the rating engine: one row per (metric, region, bucket)."""

    __tablename__ = "rated_charges"
    __table_args__ = (Index("ix_charges_tenant_bucket", "tenant_id", "bucket_start"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rated_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    bucket_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    free_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    priced: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Credit(Base):
    __tablename__ = "credits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    remaining: Mapped[Decimal] = mapped_column(Money, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Discount(Base):
    """Percentage discount, optionally limited to one metric (exact name or glob)."""

    __tablename__ = "discounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    percent: Mapped[Decimal] = mapped_column(Money, nullable=False)
    metric: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "period", name="uq_invoice_tenant_period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    period_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    credit_total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discounts_applied: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    credits_applied: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finalized_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.position"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(nullable=False)
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    rated_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    free_quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(
        Money, nullable=False
    )  # effective (amount / billable)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class BudgetRule(Base):
    __tablename__ = "budget_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    threshold_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    metric: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BudgetEvent(Base):
    __tablename__ = "budget_events"
    __table_args__ = (UniqueConstraint("rule_id", "period", name="uq_budget_event_rule_period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("budget_rules.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    spend: Mapped[Decimal] = mapped_column(Money, nullable=False)
    threshold_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
