"""Pydantic request/response models. Money and quantities are Decimals, serialised as strings."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .pricing import Tier, validate_tiers


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------- tenants


class TenantCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=255)
    default_region: str | None = Field(default=None, max_length=64)


class TenantOut(ORMModel):
    id: str
    name: str
    default_region: str | None
    created_at: datetime


# ----------------------------------------------------------------------------- plans


class TierIn(BaseModel):
    up_to: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal = Field(ge=0)


class PlanPriceIn(BaseModel):
    metric: str = Field(min_length=1, max_length=128, description="Exact metric name or glob")
    region: str | None = Field(default=None, max_length=64)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    free_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    tiers: list[TierIn] = Field(default_factory=list)

    @field_validator("tiers")
    @classmethod
    def check_tiers(cls, tiers: list[TierIn]) -> list[TierIn]:
        validate_tiers([Tier(t.up_to, t.unit_price) for t in tiers])
        return tiers


class PlanCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=255)
    currency: str = Field(default="BDT", min_length=3, max_length=8)
    description: str = Field(default="", max_length=2000)
    prices: list[PlanPriceIn] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_metric_region(self) -> PlanCreate:
        seen = set()
        for p in self.prices:
            key = (p.metric, p.region)
            if key in seen:
                raise ValueError(f"duplicate price for metric={p.metric!r} region={p.region!r}")
            seen.add(key)
        return self


class PlanPriceOut(ORMModel):
    metric: str
    region: str | None
    unit_price: Decimal
    free_quantity: Decimal
    tiers: list[dict]


class PlanOut(ORMModel):
    id: str
    name: str
    currency: str
    description: str
    prices: list[PlanPriceOut]
    created_at: datetime


class PlanAssignIn(BaseModel):
    plan_id: str
    effective_from: datetime


class PlanAssignmentOut(ORMModel):
    id: str
    tenant_id: str
    plan_id: str
    effective_from: datetime
    effective_to: datetime | None


# ----------------------------------------------------------------------------- usage


class UsageSampleIn(BaseModel):
    id: str = Field(min_length=1, max_length=128, description="Client-generated, used for dedupe")
    tenant_id: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(ge=0)
    region: str | None = Field(default=None, max_length=64)
    start: datetime
    end: datetime
    attributes: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def end_after_start(self) -> UsageSampleIn:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class UsageBatchIn(BaseModel):
    samples: list[UsageSampleIn] = Field(min_length=1)


class BudgetEventOut(ORMModel):
    id: str
    rule_id: str
    tenant_id: str
    period: str
    spend: Decimal
    threshold_amount: Decimal
    message: str
    triggered_at: datetime


class IngestOut(BaseModel):
    accepted: int
    duplicates: int
    rejected: list[dict]
    budget_events: list[BudgetEventOut]


class UsageSampleOut(ORMModel):
    id: str
    tenant_id: str
    resource_id: str
    metric: str
    quantity: Decimal
    region: str | None
    start: datetime
    end: datetime
    attributes: dict
    received_at: datetime


class SummaryLineOut(BaseModel):
    metric: str
    region: str | None
    rated_unit: str
    quantity: Decimal
    free_quantity: Decimal
    amount: Decimal
    priced: bool


class UsageSummaryOut(BaseModel):
    tenant_id: str
    period: str
    currency: str
    granularity: str
    lines: list[SummaryLineOut]
    subtotal: Decimal


# ----------------------------------------------------------------------------- invoices


class InvoiceLineOut(ORMModel):
    position: int
    metric: str
    region: str | None
    description: str
    rated_unit: str
    quantity: Decimal
    free_quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class InvoiceOut(ORMModel):
    id: str
    number: str
    tenant_id: str
    period: str
    period_start: datetime
    period_end: datetime
    currency: str
    status: str
    subtotal: Decimal
    discount_total: Decimal
    credit_total: Decimal
    total: Decimal
    discounts_applied: list[dict]
    credits_applied: list[dict]
    lines: list[InvoiceLineOut]
    created_at: datetime
    finalized_at: datetime | None


# ----------------------------------------------------------------------------- credits & discounts


class CreditCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    reason: str = Field(default="", max_length=255)
    expires_at: datetime | None = None


class CreditOut(ORMModel):
    id: str
    tenant_id: str
    amount: Decimal
    remaining: Decimal
    reason: str
    expires_at: datetime | None
    created_at: datetime


class DiscountCreate(BaseModel):
    percent: Decimal = Field(gt=0, le=100)
    metric: str | None = Field(default=None, max_length=128)
    description: str = Field(default="", max_length=255)
    valid_from: datetime
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def window_ok(self) -> DiscountCreate:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class DiscountOut(ORMModel):
    id: str
    tenant_id: str
    percent: Decimal
    metric: str | None
    description: str
    valid_from: datetime
    valid_to: datetime | None


# ----------------------------------------------------------------------------- budgets


class BudgetRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    threshold_amount: Decimal = Field(gt=0)
    metric: str | None = Field(default=None, max_length=128)
    enabled: bool = True


class BudgetRuleOut(ORMModel):
    id: str
    tenant_id: str
    name: str
    threshold_amount: Decimal
    metric: str | None
    enabled: bool
    created_at: datetime


# ----------------------------------------------------------------------------- misc


class MetricOut(BaseModel):
    pattern: str
    kind: str
    unit: str
    rated_unit: str
    description: str


class HealthOut(BaseModel):
    status: str
    version: str
    database: str


class MessageOut(BaseModel):
    message: str
