from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PaymentMethod, PaymentStatus, RefundReason, RefundStatus


class PaymentCreate(BaseModel):
    # provider, amount and status are the server's: the amount always comes
    # from the order, never from the client, or a caller could underpay.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    method: PaymentMethod


class PaymentCallback(BaseModel):
    """What the provider tells us after an authorization settles."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: Literal["captured", "failed"]
    failed_reason: str | None = Field(default=None, max_length=500)


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    method: PaymentMethod
    provider: str
    provider_ref: str | None
    amount: Decimal
    currency: str
    status: PaymentStatus
    authorized_at: datetime | None
    captured_at: datetime | None
    failed_reason: str | None
    created_at: datetime


class RefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # The over-refund guard needs the other refunds on the order, so it cannot
    # live here — see the router.
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: RefundReason


class RefundRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    payment_id: int
    amount: Decimal
    reason: RefundReason
    status: RefundStatus
    sla_due_at: datetime
    initiated_at: datetime
    completed_at: datetime | None
    provider_ref: str | None
    created_at: datetime


class RefundDetail(RefundRead):
    # Derived from the clock, so it is computed per response rather than stored
    # — a refund silently becomes breached with no row ever being written.
    sla_breached: bool
