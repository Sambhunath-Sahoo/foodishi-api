from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.ids import DbId
from app.models.enums import ActorType, OrderStatus


class OrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    menu_item_id: DbId
    quantity: int = Field(ge=1, le=50)
    notes: str | None = Field(default=None, max_length=280)


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    restaurant_id: DbId
    address_id: DbId
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    coupon_code: str | None = Field(default=None, max_length=40)


class OrderCreate(QuoteRequest):
    """A quote's payload, plus the one thing that is not a price.

    `delivery_note` is deliberately absent from QuoteRequest: "leave it at the
    gate" changes nothing about what the order costs, so quoting with it would
    invite a client to send it twice and let the two copies disagree. It is
    written once, at placement, and never edited — it is what the customer asked
    for at the time, and both the kitchen and whoever delivers read it.

    Until this field existed the customer app kept the note in the browser and
    the kitchen never saw it, which made every "you didn't ring the bell"
    complaint unanswerable.
    """

    delivery_note: str | None = Field(default=None, max_length=200)


class QuoteLineRead(BaseModel):
    menu_item_id: int
    item_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


class QuoteRead(BaseModel):
    lines: list[QuoteLineRead]
    subtotal: Decimal
    packaging_fee: Decimal
    delivery_fee: Decimal
    tax_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    distance_km: Decimal
    promised_at: datetime
    cancellable_until: datetime
    coupon_code: str | None = None
    coupon_message: str | None = None


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    menu_item_id: int
    item_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    notes: str | None


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    restaurant_id: int
    address_id: int
    coupon_id: int | None
    status: OrderStatus
    subtotal: Decimal
    packaging_fee: Decimal
    delivery_fee: Decimal
    tax_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    distance_km: Decimal
    placed_at: datetime
    cancellable_until: datetime
    promised_at: datetime
    cancelled_at: datetime | None
    cancellation_reason: str | None
    delivered_at: datetime | None
    # Read by the kitchen and by whoever delivers, so it is on the list row and
    # not only on the detail: a courier looking at a pickup board should not have
    # to open an order to find out it goes to the gate.
    delivery_note: str | None


class OrderDetail(OrderRead):
    items: list[OrderItemRead]


class OrderStatusRead(BaseModel):
    """Deliberately small — the tracking screen polls this every few seconds."""

    id: int
    status: OrderStatus
    promised_at: datetime
    minutes_remaining: int
    is_late: bool
    cancellable_until: datetime
    is_cancellable: bool


class OrderEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: OrderStatus | None
    to_status: OrderStatus
    actor_type: ActorType
    actor_id: int | None
    reason: str | None
    created_at: datetime


class OrderStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: OrderStatus
    actor_type: ActorType = ActorType.SYSTEM
    actor_id: int | None = None
    reason: str | None = Field(default=None, max_length=280)


class OrderCancel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = Field(default=None, max_length=280)
    actor_type: ActorType = ActorType.USER
    actor_id: int | None = None

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        nulls = sorted(
            f for f in self.model_fields_set
            if f in {"actor_type"} and getattr(self, f) is None
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class CancelResult(BaseModel):
    order: OrderRead
    within_window: bool
    cancellation_fee: Decimal
    refund_amount: Decimal
    refund_id: int | None
    refund_due_at: datetime | None
