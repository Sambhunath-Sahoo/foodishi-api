"""Shapes the operator console needs and no other caller does.

Everything here spans the whole platform: counts across every kitchen, one row
per restaurant, a list of rides that belongs to no single order. That is why it
is a module of its own rather than additions to schemas/order.py or
schemas/finance.py — those are read by the customer and partner apps, and a
platform-wide figure has no meaning to either.

Money is Decimal throughout, never float. Percentages are Decimal too and are
whole percents (18.00), not fractions (0.18): a rate is written on a contract
and typed into a form as a percent, and converting at the edge means one place
can get it wrong instead of five.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.ids import DbId
from app.models.enums import DeliveryStatus
from app.schemas.delivery import DeliveryPartnerRead
from app.schemas.order import OrderRead

# ---------------------------------------------------------------- workload


class Workload(BaseModel):
    """Where the work is, right now — one figure per section that has something
    to say.

    Read on every page of the console, because it is what the navigation
    reports. One call rather than six: six queries behind the chrome would cost
    more than the board beside it.

    Sections with nothing to report are deliberately absent. A badge on a
    section nobody has to act on teaches the reader to ignore the ones that
    matter.
    """

    #: Orders somewhere between placed and handed over, platform-wide.
    live_orders: int
    #: How many of those are already past the time the customer was told. The
    #: order's own promise, not its ride's — an order can be late before a rider
    #: has been assigned at all.
    orders_late: int
    #: Rides still out, and how many of those are past the customer's promise.
    deliveries_out: int
    deliveries_late: int
    #: Kitchens whose real end-to-end time has drifted above the platform's.
    restaurants_slipping: int
    #: Codes at their cap: still typed in at checkout, and refused.
    coupons_exhausted: int
    #: Payment attempts in the last day that never went through.
    payments_failed: int
    #: Refunds past the time the customer was promised their money.
    refunds_breached: int
    #: What those refunds are worth — money the platform is holding.
    refunds_owed: Decimal
    #: Restaurants asking to join, still unanswered. The only figure here that
    #: cannot resolve itself: an application waits until a person decides.
    applications_pending: int


# ---------------------------------------------------------------- settings


class DeliverySettingsRead(BaseModel):
    base_fee: Decimal
    per_km_fee: Decimal
    free_delivery_above: Decimal
    surge_multiplier: Decimal
    surge_after_minutes_late: int
    max_distance_km: Decimal
    packaging_fee: Decimal


class NegotiatedRate(BaseModel):
    """One kitchen that is not on the platform default.

    Derived from `restaurants.commission_percent`, never stored here — see
    services/platform_settings.negotiated_rates for why the overrides are the
    restaurants table rather than a second list.
    """

    restaurant_id: int
    name: str
    percent: Decimal


class CommissionSettingsRead(BaseModel):
    #: What a NEW restaurant is created on. Never read for an existing one.
    default_percent: Decimal
    settlement_days: int
    #: Read-only here. Change one through PUT /admin/restaurants/{id}/commission.
    negotiated: list[NegotiatedRate]


class TaxSettingsRead(BaseModel):
    gst_percent: Decimal
    is_packaging_taxable: bool
    is_delivery_taxable: bool
    gstin: str


class OrderRuleSettingsRead(BaseModel):
    min_order_value: Decimal
    max_items_per_order: int
    free_cancellation_minutes: int
    late_cancellation_fee_percent: Decimal
    refund_sla_hours: int
    auto_cancel_unconfirmed_minutes: int
    prep_buffer_minutes: int


class PlatformSettingsRead(BaseModel):
    """What the platform charges, keeps and refuses.

    Grouped rather than flat because the four groups are four different
    conversations — pricing, the commercial deal, tax, and what an order is
    allowed to be. The table behind it is flat; the mapping lives in
    routers/admin_platform.py and nowhere else.
    """

    delivery: DeliverySettingsRead
    commission: CommissionSettingsRead
    tax: TaxSettingsRead
    order_rules: OrderRuleSettingsRead
    updated_at: datetime


class DeliverySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_fee: Decimal | None = Field(default=None, ge=0)
    per_km_fee: Decimal | None = Field(default=None, ge=0)
    free_delivery_above: Decimal | None = Field(default=None, ge=0)
    surge_multiplier: Decimal | None = Field(default=None, ge=1)
    surge_after_minutes_late: int | None = Field(default=None, ge=0, le=180)
    max_distance_km: Decimal | None = Field(default=None, gt=0)
    packaging_fee: Decimal | None = Field(default=None, ge=0)


class CommissionSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    settlement_days: int | None = Field(default=None, ge=1, le=60)


class TaxSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    gst_percent: Decimal | None = Field(default=None, ge=0, lt=100)
    is_packaging_taxable: bool | None = None
    is_delivery_taxable: bool | None = None
    gstin: str | None = Field(default=None, max_length=20)


class OrderRuleSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_order_value: Decimal | None = Field(default=None, ge=0)
    max_items_per_order: int | None = Field(default=None, ge=1, le=200)
    free_cancellation_minutes: int | None = Field(default=None, ge=0, le=60)
    late_cancellation_fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    refund_sla_hours: int | None = Field(default=None, ge=1, le=720)
    auto_cancel_unconfirmed_minutes: int | None = Field(default=None, ge=1, le=120)
    prep_buffer_minutes: int | None = Field(default=None, ge=0, le=60)


#: Group name -> the column prefix it writes. The one place the nested request
#: shape meets the flat table, so a renamed column has a single site to fix.
SETTINGS_PREFIX: dict[str, str] = {
    "delivery": "delivery_",
    "commission": "commission_",
    "tax": "tax_",
    "order_rules": "rule_",
}

#: The four fields whose API name is not its column name minus the prefix.
SETTINGS_ALIAS: dict[str, str] = {
    "delivery_free_delivery_above": "delivery_free_above",
    "delivery_surge_after_minutes_late": "delivery_surge_after_minutes",
    "tax_is_packaging_taxable": "tax_packaging_taxable",
    "tax_is_delivery_taxable": "tax_delivery_taxable",
}


class PlatformSettingsUpdate(BaseModel):
    """A partial update. Every group and every field inside it is optional.

    Partial on purpose: a form that submitted all twenty fields would silently
    overwrite a setting somebody else changed while it was open.
    """

    model_config = ConfigDict(extra="forbid")

    delivery: DeliverySettingsUpdate | None = None
    commission: CommissionSettingsUpdate | None = None
    tax: TaxSettingsUpdate | None = None
    order_rules: OrderRuleSettingsUpdate | None = None

    @model_validator(mode="after")
    def require_at_least_one_group(self):
        if not any((self.delivery, self.commission, self.tax, self.order_rules)):
            raise ValueError(
                "Send at least one of delivery, commission, tax or order_rules. "
                "An empty body changes nothing and is more likely a mistake."
            )
        return self

    def to_columns(self) -> dict[str, object]:
        """Flatten to the column names services/platform_settings.save expects."""
        columns: dict[str, object] = {}
        for group_name, prefix in SETTINGS_PREFIX.items():
            group = getattr(self, group_name)
            if group is None:
                continue
            for field, value in group.model_dump(exclude_unset=True).items():
                name = f"{prefix}{field}"
                columns[SETTINGS_ALIAS.get(name, name)] = value
        return columns


class RestaurantCommissionUpdate(BaseModel):
    """Put one kitchen on its own rate."""

    model_config = ConfigDict(extra="forbid")

    # max_digits/decimal_places pinned to the column, numeric(5, 2). Without
    # them "0.001" passed `0 < x < 100`, Postgres stored 0.00, and the response
    # echoed back "0.001" from the un-refreshed in-memory attribute -- so an
    # operator believed a kitchen was on 0.001% when it was on ZERO, and
    # settlements.commission_for charged nothing on every delivered order until
    # somebody noticed. "18.999" stored 19.00 and reported 18.999 the same way.
    # Every other percent field in this project already pins both.
    percent: Decimal = Field(gt=0, lt=100, max_digits=5, decimal_places=2)


# -------------------------------------------------------------- deliveries


class DeliveryFilter(StrEnum):
    """Which rides a board is asking for.

    `active` is the default question an operations screen has — a ride still
    somebody's problem — and it spans two statuses, which is why this is not
    simply an optional DeliveryStatus.
    """

    ACTIVE = "active"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    FAILED = "failed"
    ANY = "any"


class AdminDeliveryRow(BaseModel):
    """A ride with the order it belongs to, and the rider carrying it.

    The join is done server-side because a board needs a hundred of these and
    `GET /orders/{id}/delivery` answers one. A screen that fired one request per
    row would spend its whole budget on the chrome.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    partner_id: int
    distance_km: Decimal
    eta_minutes: int
    status: DeliveryStatus
    assigned_at: datetime
    picked_up_at: datetime | None
    delivered_at: datetime | None
    partner: DeliveryPartnerRead
    #: The order, so the board can show the promise and the total it is carrying.
    order: OrderRead


class DeliveryReassign(BaseModel):
    """Hand a stalled ride to somebody else."""

    model_config = ConfigDict(extra="forbid")

    partner_id: DbId


class DeliveryFail(BaseModel):
    """Give up on a ride, with the reason support will read back."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=200)


# ----------------------------------------------------------------- orders


class AdminOrderSort(StrEnum):
    """What the operator's order board can be ordered by.

    Distinct from the partner queue, which is always newest-first: an operator
    arrives with a question ("what is the biggest order today", "what has been
    waiting longest") and the answer is an ordering.
    """

    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    OLDEST_PROMISE = "oldest_promise"


# ------------------------------------------------------------- commission


class CommissionRow(BaseModel):
    """What one kitchen sold in a window, and what the platform kept of it."""

    restaurant_id: int
    name: str
    city: str
    delivered_orders: int
    #: What customers paid, all in — food, packaging, delivery and tax.
    gross: Decimal
    #: The food alone. Commission is charged on this and nothing else.
    food_value: Decimal
    commission_percent: Decimal
    #: True when this kitchen is not on the platform default.
    is_negotiated: bool
    commission: Decimal
    #: Gross less commission: what the kitchen is due at settlement.
    payout: Decimal


class CommissionLedger(BaseModel):
    """Every kitchen's commission over a window, biggest earner first.

    Kitchens that delivered nothing are still listed, at zero: "why is this one
    empty" is a real question and a row that vanished could not answer it.
    """

    rows: list[CommissionRow]
    gross: Decimal
    food_value: Decimal
    commission: Decimal
    payout: Decimal
    #: Echoed so a heading can name the window it is printing over.
    default_percent: Decimal
    settlement_days: int
    days: int
