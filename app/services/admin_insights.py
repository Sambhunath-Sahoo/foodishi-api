"""Platform-wide aggregates: what every kitchen did, and where the work is.

One module because three callers need the same per-kitchen roll-up and must not
disagree about it — the navigation's counts, the commission ledger, and the
restaurant report. A second copy of "which kitchens are slipping" is how a
sidebar ends up contradicting the page it links to.

`routers/metrics.py` has a private `_restaurant_metrics_statement` that computes
an overlapping aggregate for `GET /admin/metrics/restaurants`. That is the older
of the two and should be replaced by `kitchen_rows` below when metrics.py is
next touched; it is left alone here only because that file is being edited in
another session. The two agree today — same delivered-only revenue, same
placed-to-delivered minutes — and this note exists so the next person does not
have to work out which one is authoritative.

Nothing here writes. No router imports, no HTTPException.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Restaurant
from app.models.coupon import Coupon
from app.models.delivery import Delivery
from app.models.enums import DeliveryStatus, OrderStatus, PaymentStatus, RefundStatus
from app.models.order import Order
from app.models.payment import Payment, Refund
from app.services import onboarding
from app.services.money import money
from app.services.order_state import TERMINAL

#: The trading day is cut in IST, not UTC. Measured on live data a UTC midnight
#: put 6 orders in "today" where the local day held 130 — see the same note in
#: routers/metrics.py and services/settlements.py.
DAY_BOUNDARY_TZ = ZoneInfo("Asia/Kolkata")

SECONDS_PER_MINUTE = 60
PERCENT_DIVISOR = Decimal("100")

#: A ride that is still somebody's problem.
ACTIVE_DELIVERY_STATUSES = (DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP)

#: Statuses an order can sit in and still be in flight. Derived from the
#: lifecycle module rather than hand-listed, so a newly terminal status stops
#: being counted as live everywhere at once.
LIVE_STATUSES = tuple(status for status in OrderStatus if status not in TERMINAL)

#: How far above the platform's typical overhead a kitchen may sit before it is
#: called out. Mirrors apps/operator/lib/kitchen-gap.ts, which grades the same
#: judgement for the reader — the console shows the grade, this counts them.
WARN_MINUTES_OVER_MEDIAN = 8


def start_of_today(now: datetime) -> datetime:
    """Midnight that began the current trading day, as an aware UTC instant."""
    local_midnight = datetime.combine(
        now.astimezone(DAY_BOUNDARY_TZ).date(), time.min, tzinfo=DAY_BOUNDARY_TZ
    )
    return local_midnight.astimezone(UTC)


def window_start(now: datetime, days: int) -> datetime:
    """The instant a window of whole local days opened. 0 days means all time."""
    if days <= 0:
        return datetime.min.replace(tzinfo=UTC)
    # Today counts as day one, so 30 days is 30 buckets rather than 31.
    return start_of_today(now) - timedelta(days=days - 1)


@dataclass(frozen=True)
class KitchenRow:
    """One kitchen's trade over a window, before any judgement is applied."""

    restaurant_id: int
    name: str
    city: str
    is_active: bool
    commission_percent: Decimal
    avg_prep_minutes: int
    orders: int
    delivered: int
    cancelled: int
    #: What customers paid for delivered orders, all in.
    gross: Decimal
    #: The food alone, before fees and tax. Carried so a ledger can show what a
    #: commission on food rather than on gross would come to — see commission_on.
    food_value: Decimal
    #: Placed to handed over, averaged over delivered orders. None with none.
    avg_delivery_minutes: float | None

    @property
    def gap_minutes(self) -> float | None:
        """How much longer an order really takes than the kitchen declares.

        The measure the platform grades kitchens on: a delivery promise is built
        on the declared prep time, so when the real end-to-end time drifts above
        it, every promise built on it runs late.
        """
        if self.avg_delivery_minutes is None:
            return None
        return self.avg_delivery_minutes - self.avg_prep_minutes

    @property
    def cancellation_rate(self) -> float:
        return round(self.cancelled / self.orders, 4) if self.orders else 0.0


def _kitchen_statement(start: datetime, end: datetime) -> Select:
    """Per-restaurant aggregate over one window.

    Outer join, so a kitchen that sold nothing in the window is still a row at
    zero rather than vanishing — "why is this one empty" is a real question and
    a missing row cannot answer it.

    The window predicate sits in the JOIN condition, not in WHERE: in a WHERE it
    would also discard the restaurants with no orders, which is the exact thing
    the outer join is for.
    """
    delivered = Order.status == OrderStatus.DELIVERED
    in_window = (Order.placed_at >= start) & (Order.placed_at < end)

    return (
        select(
            Restaurant.id,
            Restaurant.name,
            Restaurant.city,
            Restaurant.is_active,
            Restaurant.commission_percent,
            Restaurant.avg_prep_minutes,
            func.count(Order.id),
            func.count(Order.id).filter(delivered),
            func.count(Order.id).filter(Order.status == OrderStatus.CANCELLED),
            func.coalesce(
                func.sum(Order.total_amount).filter(delivered), Decimal("0")
            ),
            func.coalesce(func.sum(Order.subtotal).filter(delivered), Decimal("0")),
            func.avg(
                func.extract("epoch", Order.delivered_at - Order.placed_at)
                / SECONDS_PER_MINUTE
            ).filter(Order.delivered_at.is_not(None)),
        )
        .outerjoin(Order, (Order.restaurant_id == Restaurant.id) & in_window)
        .group_by(
            Restaurant.id,
            Restaurant.name,
            Restaurant.city,
            Restaurant.is_active,
            Restaurant.commission_percent,
            Restaurant.avg_prep_minutes,
        )
        .order_by(Restaurant.id)
    )


async def kitchen_rows(
    session: AsyncSession, days: int, *, now: datetime | None = None
) -> list[KitchenRow]:
    """Every kitchen's trade over the last `days` local days. 0 means all time."""
    moment = now or datetime.now(UTC)
    start = window_start(moment, days)
    # Half-open and one day past today, so every instant of today is inside it.
    end = start_of_today(moment) + timedelta(days=1)

    result = await session.execute(_kitchen_statement(start, end))
    return [
        KitchenRow(
            restaurant_id=row[0],
            name=row[1],
            city=row[2],
            is_active=row[3],
            commission_percent=row[4],
            avg_prep_minutes=row[5],
            orders=row[6],
            delivered=row[7],
            cancelled=row[8],
            gross=money(row[9]),
            food_value=money(row[10]),
            avg_delivery_minutes=round(float(row[11]), 1) if row[11] is not None else None,
        )
        for row in result.all()
    ]


def slipping(rows: list[KitchenRow]) -> list[KitchenRow]:
    """The kitchens whose overhead has drifted above the platform's own.

    Graded against the median rather than a fixed number of minutes: a slow
    evening everywhere would otherwise flag all 25 at once, and a fast platform
    would flag none of the kitchens that are actually the problem.
    """
    gaps = [row.gap_minutes for row in rows if row.gap_minutes is not None]
    if not gaps:
        return []
    threshold = median(gaps) + WARN_MINUTES_OVER_MEDIAN
    return [
        row
        for row in rows
        if row.gap_minutes is not None and row.gap_minutes >= threshold
    ]


def commission_on(gross: Decimal, percent: Decimal) -> Decimal:
    """The platform's cut of a kitchen's sales over a window.

    Charged on GROSS, deliberately, because that is what
    `services/settlements.commission_for` charges it on and what a partner's
    frozen statement therefore says. The operator's ledger and the kitchen's own
    statement have to agree to the paisa, and the only way to guarantee that is
    to share the base.

    Worth naming as an open question rather than leaving as an accident: a
    commission on gross also takes a cut of the delivery fee, the packaging fee
    and the GST — a percentage of money the platform passed through and of money
    that belongs to the government. Charging it on `subtotal` instead is the
    defensible reading, and `food_value` is carried on every ledger row so the
    difference is visible before anybody decides. Changing the base is a
    commercial decision and would have to move settlements.py with it, or the
    two disagree again.
    """
    return money(gross * percent / PERCENT_DIVISOR)


@dataclass(frozen=True)
class WorkloadCounts:
    """Where the work is. One figure per section that has something to say."""

    live_orders: int
    orders_late: int
    deliveries_out: int
    deliveries_late: int
    restaurants_slipping: int
    coupons_exhausted: int
    payments_failed: int
    refunds_breached: int
    refunds_owed: Decimal
    #: Restaurants waiting to be let onto the platform. Unlike every other
    #: figure here this one never resolves itself — an application sits in the
    #: queue until a person answers it — which is exactly why it belongs in the
    #: chrome rather than on a page somebody has to remember to open.
    applications_pending: int


async def workload(session: AsyncSession) -> WorkloadCounts:
    """Every figure the operator console's navigation reports, in one round trip.

    Deliberately one call. It is read on every page of the console, and six
    queries behind the chrome would cost more than the board beside it.
    """
    now = datetime.now(UTC)

    # Both live counts in one pass: how many are in flight, and how many of
    # those are already past what the customer was promised.
    live_orders, orders_late = (
        await session.execute(
            select(
                func.count(Order.id),
                func.count(Order.id).filter(Order.promised_at < now),
            ).where(Order.status.in_(LIVE_STATUSES))
        )
    ).one()

    # One pass over the active rides: how many are out, and how many of those
    # are already past what the customer was promised. Two queries would have
    # to agree about "active" and eventually would not.
    out, late = (
        await session.execute(
            select(
                func.count(Delivery.id),
                func.count(Delivery.id).filter(Order.promised_at < now),
            )
            .join(Order, Order.id == Delivery.order_id)
            .where(Delivery.status.in_(ACTIVE_DELIVERY_STATUSES))
        )
    ).one()

    # A code at its cap still validates — as a failure — when a customer types
    # it in, which is why an exhausted code is worth an operator's attention
    # rather than being quietly inert. Uncapped codes can never be exhausted.
    coupons_exhausted = await session.scalar(
        select(func.count(Coupon.id)).where(
            Coupon.is_active.is_(True),
            Coupon.usage_limit_total.is_not(None),
            Coupon.times_used >= Coupon.usage_limit_total,
        )
    )

    # Scoped to the last day: a badge implies "now", and the all-time count of
    # failed attempts is a number nobody can act on.
    payments_failed = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.status == PaymentStatus.FAILED,
            Payment.created_at >= now - timedelta(days=1),
        )
    )

    # The platform's own definition of breached, and the only one: past the due
    # time and not yet completed. A failed refund counts — the money never went
    # back. Same rule as GET /refunds/{id}.sla_breached.
    breached_count, breached_owed = (
        await session.execute(
            select(
                func.count(Refund.id),
                func.coalesce(func.sum(Refund.amount), Decimal("0")),
            ).where(
                Refund.status != RefundStatus.COMPLETED,
                Refund.sla_due_at < now,
            )
        )
    ).one()

    rows = await kitchen_rows(session, days=0, now=now)

    # Delegated rather than counted here: app/services/onboarding.py owns the
    # applications table, and a second copy of "what pending means" is a second
    # thing to update when a state is added.
    applications_pending = await onboarding.pending_count(session)

    return WorkloadCounts(
        live_orders=int(live_orders or 0),
        orders_late=int(orders_late or 0),
        deliveries_out=int(out or 0),
        deliveries_late=int(late or 0),
        restaurants_slipping=len(slipping(rows)),
        coupons_exhausted=int(coupons_exhausted or 0),
        payments_failed=int(payments_failed or 0),
        refunds_breached=int(breached_count or 0),
        refunds_owed=money(breached_owed),
        applications_pending=applications_pending,
    )


def is_breached(refund: Refund, now: datetime) -> bool:
    """Past the promise and still not paid. A failed refund counts."""
    return refund.status != RefundStatus.COMPLETED and refund.sla_due_at < now


def delivered_late_expression():
    """SQL for "handed over after the time the customer was promised".

    An expression rather than a helper that takes rows, because the order report
    counts these in the database over a window that can hold every order the
    platform has ever taken.
    """
    return case(
        (
            (Order.delivered_at.is_not(None))
            & (Order.delivered_at > Order.promised_at),
            1,
        ),
        else_=None,
    )
