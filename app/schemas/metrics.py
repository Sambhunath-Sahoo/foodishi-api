from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import OrderStatus


class MetricsSummary(BaseModel):
    """The single row an operator dashboard opens on."""

    model_config = ConfigDict(from_attributes=True)

    total_orders: int
    orders_today: int
    revenue_today: Decimal
    # All-time gross value of delivered orders, and Foodishi's cut of it. The cut
    # is what the platform actually earns, so it is the figure the overview
    # leads on; the gross is kept beside it because a commission with no base
    # is a number nobody can sanity-check.
    gross_revenue: Decimal
    commission_revenue: Decimal
    # The three buckets every order falls into, so the rail can be read as a
    # whole: live_orders + delivered_orders + cancelled_orders == total_orders.
    live_orders: int
    delivered_orders: int
    cancelled_orders: int
    # gross_revenue over delivered_orders — a delivered basket, not a placed
    # one. See the comment on the denominator in routers/metrics.py.
    avg_order_value: Decimal
    breached_refunds: int
    active_restaurants: int
    # Customers who placed at least one order in the last
    # active_customer_window_days, which is trading activity rather than
    # users.is_active. The window ships with the figure so the dashboard can
    # say which window it is showing instead of hardcoding a number that
    # drifts from the server's.
    active_customers: int
    active_customer_window_days: int
    total_users: int


class OrdersOverTimePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date
    order_count: int
    revenue: Decimal


class StatusCount(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: OrderStatus
    order_count: int


class OrderFunnel(BaseModel):
    """Counts per status, plus how cancellations split against the promise.

    inside/outside refers to cancellable_until — the window frozen onto the
    order at placement. Outside-window cancellations are the ones that cost
    money, so they are worth separating from the headline rate.
    """

    model_config = ConfigDict(from_attributes=True)

    statuses: list[StatusCount]
    total_orders: int
    cancelled_orders: int
    cancellation_rate: float
    cancelled_inside_window: int
    cancelled_outside_window: int


class RestaurantMetrics(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    restaurant_id: int
    name: str
    order_count: int
    revenue: Decimal
    cancellation_rate: float
    # Measured placed_at -> delivered_at. None until a first order lands.
    avg_delivery_minutes: float | None
    # The restaurant's own estimate, for comparison against the measurement.
    avg_prep_minutes: int
