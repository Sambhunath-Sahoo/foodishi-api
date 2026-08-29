"""Five platform-wide reports.

Distinct from schemas/reports.py, which is one restaurant's own trade read by the
partner app. These span every kitchen and are read by nobody but platform staff:
"which of our kitchens is slipping" is not a question a restaurant may ask about
its competitors.

All five are computed over the same window from the same orders, so they cannot
disagree with each other about last week. A reports section assembled from five
independent queries is a section whose first contradiction makes the reader stop
trusting all of it.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.models.enums import OrderStatus
from app.schemas.admin import CommissionLedger

# ------------------------------------------------------------------ sales


class SalesDay(BaseModel):
    """One local calendar day of trade."""

    day: date
    orders: int
    delivered: int
    #: What customers paid for the delivered ones, all in.
    gross: Decimal
    commission: Decimal
    #: Delivered revenue over delivered orders. Zero on a day with none.
    avg_order_value: Decimal


class SalesReport(BaseModel):
    """How much came in, day by day.

    Attributed by when the order was PLACED, not when it was handed over, so the
    money on a row always lines up with the order count beside it. An order
    placed at 23:50 and delivered at 00:20 belongs to the evening somebody
    worked.
    """

    days: list[SalesDay]
    orders: int
    delivered: int
    cancelled: int
    gross: Decimal
    commission: Decimal
    avg_order_value: Decimal
    #: The busiest day in the window, for a caption. None on an empty window.
    peak: SalesDay | None
    window_days: int


# ------------------------------------------------------------ restaurants


class RestaurantReportRow(BaseModel):
    restaurant_id: int
    name: str
    city: str
    is_active: bool
    orders: int
    delivered_orders: int
    cancelled_orders: int
    cancellation_rate: float
    gross: Decimal
    food_value: Decimal
    commission_percent: Decimal
    commission: Decimal
    payout: Decimal
    #: The prep time the kitchen promises on.
    avg_prep_minutes: int
    #: Placed to handed over, for real. None with no deliveries in the window.
    avg_delivery_minutes: float | None
    #: How far the second sits above the first. The platform's own measure.
    gap_minutes: float | None
    #: True when that gap is above the platform's median by enough to act on.
    is_slipping: bool


class RestaurantReport(BaseModel):
    """Which kitchens earned the money, and which are costing it.

    `is_slipping` is graded against the platform's own median overhead rather
    than a fixed number of minutes: a slow evening everywhere would otherwise
    flag every kitchen at once, and a fast week would flag none of the ones that
    are actually the problem.
    """

    rows: list[RestaurantReportRow]
    gross: Decimal
    commission: Decimal
    #: The median overhead the grade above is measured against.
    median_gap_minutes: float
    slipping: int
    window_days: int


# ----------------------------------------------------------------- orders


class OrderStatusRow(BaseModel):
    status: OrderStatus
    orders: int
    share: float
    gross: Decimal


class OrderHourRow(BaseModel):
    """Orders placed in one hour of the local day, summed over the window."""

    hour: int
    orders: int


class OrderReport(BaseModel):
    """Where orders ended up, and when they are placed.

    The hour histogram is the half that changes decisions — two peaks four hours
    apart is a staffing question. Every hour is emitted, including the empty
    ones: a chart that skipped 04:00 would hide the shape of a night shift
    rather than showing it as quiet.
    """

    statuses: list[OrderStatusRow]
    hours: list[OrderHourRow]
    orders: int
    #: Cancelled inside the frozen free window — cost the customer nothing.
    cancelled_inside_window: int
    #: Cancelled after it. These are the ones support hears about.
    cancelled_outside_window: int
    #: Delivered after the time the customer was promised. A fixed verdict.
    delivered_late: int
    #: Placed to handed over, averaged. None with no deliveries in the window.
    avg_minutes_to_deliver: float | None
    window_days: int


# -------------------------------------------------------------- customers


class CustomerReportRow(BaseModel):
    user_id: int
    name: str
    email: str
    city: str
    is_active: bool
    orders: int
    delivered: int
    cancelled: int
    #: What they actually paid: the total of their delivered orders.
    spend: Decimal
    avg_order_value: Decimal
    last_ordered_at: datetime | None


class CustomerReport(BaseModel):
    """Who is spending, and whether they came back.

    New against returning is the figure worth watching: a window where almost
    everybody is new looks like growth and is usually churn wearing growth's
    clothes. "New" means their FIRST EVER order fell inside the window, not that
    their account was created in it — somebody who signed up in March and
    finally ordered this week is new to the business, whatever the account says.
    """

    #: Biggest spenders first, capped. The directory is what /users is for.
    rows: list[CustomerReportRow]
    #: Placed at least one order inside the window.
    customers: int
    new_customers: int
    returning_customers: int
    #: Registered and has never ordered at all, in any window.
    never_ordered: int
    spend: Decimal
    window_days: int


# ------------------------------------------------------------- commission


class CommissionCityRow(BaseModel):
    city: str
    restaurants: int
    delivered_orders: int
    gross: Decimal
    commission: Decimal


class CommissionReport(BaseModel):
    """What the platform kept, by city and by kitchen.

    The per-kitchen half is the same object `GET /admin/commission` returns, from
    the same call — one definition of "what this kitchen owes us", so the finance
    screen and the report cannot print different percentages.
    """

    ledger: "CommissionLedger"
    cities: list[CommissionCityRow]
