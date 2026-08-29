from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, Select, cast, distinct, func, select

from app.core.pagination import Page, PageDep, PageParams
from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.models.catalog import Restaurant
from app.models.enums import OrderStatus, RefundStatus
from app.models.order import Order
from app.models.payment import Refund
from app.models.user import User
from app.schemas.metrics import (
    MetricsSummary,
    OrderFunnel,
    OrdersOverTimePoint,
    RestaurantMetrics,
    StatusCount,
)
from app.services.admin_insights import KitchenRow, commission_on, kitchen_rows
from app.services.order_state import TERMINAL

# The guard hangs off the router rather than the four routes because there is
# no such thing as a customer-scoped or restaurant-scoped number under this
# prefix: every figure here spans the whole platform — revenue across all
# kitchens, the total user count. Four decorators would be four chances to
# forget one, and the fifth chart somebody adds inherits the guard for free.
# require_platform_role() defaults to the only platform rank there is, ADMIN.
router = APIRouter(
    prefix="/admin/metrics",
    tags=["metrics"],
    dependencies=[Depends(require_platform_role())],
)

# Derived from order_state.TERMINAL, not a second hand-written list. Which
# statuses are final is the lifecycle module's rule; a copy here would keep
# counting a newly-added terminal status as "live" on the operator dashboard.
LIVE_STATUSES = tuple(status for status in OrderStatus if status not in TERMINAL)

# Commission is no longer a constant here, and the comment that used to stand in
# this place said what would replace it: `restaurants.commission_percent` now
# exists, defaults to 18.00, and is the rate each kitchen is actually on.
#
# The old flat 0.20 was not merely stale, it was wrong by a measurable amount —
# on live data it reported the platform earning 20% of a global gross where the
# ledger and every partner statement charge each kitchen its own rate. So the
# figure below is a SUM of per-order commission, not a rate applied to a total:
# two kitchens on different rates selling the same amount do not earn the
# platform the same money, and there is no single rate that can stand in for
# them. See services/settlements.commission_for, which is the same arithmetic
# for one restaurant over one period.
# The trading day these figures are cut on. Not UTC — see _start_of_today.
DAY_BOUNDARY_TZ = ZoneInfo("Asia/Kolkata")

PERCENT_DIVISOR = Decimal("100")

MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 30
SECONDS_PER_MINUTE = 60


def _delivered_revenue():
    # Revenue is money that actually landed, so cancelled and in-flight orders
    # contribute nothing. COALESCE keeps an empty window at 0 rather than null.
    return func.coalesce(
        func.sum(Order.total_amount).filter(Order.status == OrderStatus.DELIVERED),
        Decimal("0"),
    )


def _start_of_today(now: datetime) -> datetime:
    """Midnight that began the current trading day, as an aware UTC instant.

    The boundary is IST, not UTC, and the difference is not cosmetic: measured
    on live data a UTC midnight put 6 orders in "today" where the trading day
    actually held 130. Every operator reading this dashboard is in India and
    every order it counts was placed there, so a UTC boundary silently reports
    a fifth of a day and calls it today.

    Storage is unchanged — every timestamp stays UTC timestamptz. Only the
    boundary is localised, then converted back so the comparison stays aware.
    """
    local_midnight = datetime.combine(
        now.astimezone(DAY_BOUNDARY_TZ).date(), time.min, tzinfo=DAY_BOUNDARY_TZ
    )
    return local_midnight.astimezone(UTC)


def _window_start(now: datetime, days: int) -> datetime:
    # Today counts as day one, so a 30-day window is 30 buckets rather than 31.
    # Shared with the summary's active-customer count so the tile and the chart
    # cannot disagree about where "the last 30 days" begins.
    return _start_of_today(now) - timedelta(days=days - 1)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


async def _count_total(session: SessionDep, statement: Select) -> int:
    # Same trick as core.pagination.paginate, which cannot be reused here: it
    # returns .scalars(), and these rows are multi-column aggregates.
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    return int(total or 0)


async def _page_rows(session: SessionDep, statement: Select, params: PageParams):
    result = await session.execute(statement.limit(params.limit).offset(params.offset))
    return result.all()


@router.get(
    "/summary",
    response_model=MetricsSummary,
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def get_summary(session: SessionDep):
    now = datetime.now(UTC)
    today = _start_of_today(now)
    active_since = _window_start(now, DEFAULT_WINDOW_DAYS)

    # Every order-shaped figure on the rail comes back in one round trip. The
    # dashboard polls this endpoint, so a scalar select per tile would be eight
    # sequential awaits for numbers Postgres can count in a single pass.
    #
    # Today's revenue is attributed by placed_at, so it lines up with
    # orders_today instead of drifting against a separate delivery clock.
    orders = (
        await session.execute(
            select(
                func.count(Order.id),
                func.count(Order.id).filter(Order.placed_at >= today),
                func.coalesce(
                    func.sum(Order.total_amount).filter(
                        Order.status == OrderStatus.DELIVERED,
                        Order.placed_at >= today,
                    ),
                    Decimal("0"),
                ),
                func.count(Order.id).filter(Order.status.in_(LIVE_STATUSES)),
                # The other two thirds of the rail. LIVE_STATUSES is the
                # complement of order_state.TERMINAL and these are the two
                # members of it, so live + delivered + cancelled reconciles to
                # total_orders. Should TERMINAL gain a third member, the live
                # count stays correct because it is derived, and the shortfall
                # surfaces as a visible gap rather than a mislabelled bucket.
                func.count(Order.id).filter(Order.status == OrderStatus.DELIVERED),
                func.count(Order.id).filter(Order.status == OrderStatus.CANCELLED),
                # An active customer is one who has been trading, not one whose
                # account is merely not switched off: users.is_active defaults
                # true and only moves when support disables somebody, so
                # counting it would print total_users under a second label.
                # Distinct orderers over the window the trend chart defaults to,
                # which ix_orders_user_placed already covers.
                func.count(distinct(Order.user_id)).filter(
                    Order.placed_at >= active_since
                ),
            )
        )
    ).one()

    # Gross and commission come from the shared per-kitchen aggregate, not from
    # the select above, and the reason is rounding rather than tidiness.
    #
    # Commission has to be rounded PER KITCHEN and then summed, because that is
    # what services/settlements.commission_for does for a statement and what the
    # ledger does for the operator. Rounding one global raw sum instead gives a
    # different answer — measured, seven paise across 25 kitchens — and a
    # dashboard that disagrees with the ledger by any amount is worse than one
    # that disagrees by a lot, because nobody can tell which is broken.
    #
    # Gross is taken from the same rows for the same reason: two queries summing
    # the same column are two things that can drift.
    kitchens = await kitchen_rows(session, days=0)
    gross = sum((row.gross for row in kitchens), start=Decimal("0.00"))
    commission = sum(
        (commission_on(row.gross, row.commission_percent) for row in kitchens),
        start=Decimal("0.00"),
    )

    breached = await session.scalar(
        select(func.count(Refund.id)).where(
            Refund.sla_due_at < now, Refund.status != RefundStatus.COMPLETED
        )
    )
    restaurants = await session.scalar(
        select(func.count(Restaurant.id)).where(Restaurant.is_active)
    )
    users = await session.scalar(select(func.count(User.id)))

    (
        total_orders,
        orders_today,
        revenue_today,
        live_orders,
        delivered_orders,
        cancelled_orders,
        active_customers,
    ) = orders
    # Delivered orders are the denominator, deliberately — the numerator is
    # delivered-only money, so dividing by every placement would spread earned
    # revenue over baskets that were cancelled or are still in flight and
    # understate the typical order. Same paise quantisation as the commission,
    # and 0 until something has actually been delivered.
    avg_order_value = (
        (gross / delivered_orders).quantize(Decimal("0.01"))
        if delivered_orders
        else Decimal("0.00")
    )
    return MetricsSummary(
        total_orders=total_orders,
        orders_today=orders_today,
        revenue_today=revenue_today,
        gross_revenue=gross,
        commission_revenue=commission,
        live_orders=live_orders,
        delivered_orders=delivered_orders,
        cancelled_orders=cancelled_orders,
        avg_order_value=avg_order_value,
        breached_refunds=breached or 0,
        active_restaurants=restaurants or 0,
        active_customers=active_customers or 0,
        active_customer_window_days=DEFAULT_WINDOW_DAYS,
        total_users=users or 0,
    )


@router.get(
    "/orders-over-time",
    response_model=list[OrdersOverTimePoint],
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def get_orders_over_time(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = DEFAULT_WINDOW_DAYS,
):
    since = _window_start(datetime.now(UTC), days)
    # Bucketed in the session's timezone (UTC), so the buckets line up with the
    # UTC window `since` was computed in.
    day = cast(Order.placed_at, Date)

    # Days with no orders are absent rather than zero-filled — the caller knows
    # the window it asked for and can pad it however its chart wants.
    rows = (
        await session.execute(
            select(day, func.count(Order.id), _delivered_revenue())
            .where(Order.placed_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    return [
        OrdersOverTimePoint(date=bucket, order_count=count, revenue=revenue)
        for bucket, count, revenue in rows
    ]


@router.get(
    "/funnel",
    response_model=OrderFunnel,
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def get_funnel(session: SessionDep):
    counts = dict(
        (
            await session.execute(
                select(Order.status, func.count(Order.id)).group_by(Order.status)
            )
        ).all()
    )

    # Every status is emitted, present or not: a funnel with holes in it reads
    # as missing data rather than as a stage nobody reached.
    statuses = [
        StatusCount(status=status, order_count=counts.get(status, 0))
        for status in OrderStatus
    ]

    # cancelled_at IS NULL is excluded from both sides: without a cancellation
    # instant there is nothing to compare the frozen window against.
    inside, outside = (
        await session.execute(
            select(
                func.count(Order.id).filter(
                    Order.cancelled_at <= Order.cancellable_until
                ),
                func.count(Order.id).filter(
                    Order.cancelled_at > Order.cancellable_until
                ),
            ).where(
                Order.status == OrderStatus.CANCELLED, Order.cancelled_at.is_not(None)
            )
        )
    ).one()

    total = sum(counts.values())
    cancelled = counts.get(OrderStatus.CANCELLED, 0)
    return OrderFunnel(
        statuses=statuses,
        total_orders=total,
        cancelled_orders=cancelled,
        cancellation_rate=_rate(cancelled, total),
        cancelled_inside_window=inside,
        cancelled_outside_window=outside,
    )


def _to_restaurant_metrics(row: KitchenRow) -> RestaurantMetrics:
    """One kitchen's aggregate as this endpoint's response shape.

    The aggregate itself now comes from services/admin_insights.kitchen_rows,
    which is the one per-kitchen roll-up in the codebase. This endpoint used to
    build its own nearly-identical statement; the two agreed, but "nearly
    identical and agreeing" is a state that lasts until somebody changes one of
    them. The commission ledger, the navigation's slipping count and the
    restaurant report all read the same rows, so none of them can disagree with
    this tile.
    """
    return RestaurantMetrics(
        restaurant_id=row.restaurant_id,
        name=row.name,
        order_count=row.orders,
        revenue=row.gross,
        cancellation_rate=row.cancellation_rate,
        avg_delivery_minutes=row.avg_delivery_minutes,
        avg_prep_minutes=row.avg_prep_minutes,
    )


@router.get(
    "/restaurants",
    response_model=Page[RestaurantMetrics],
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def get_restaurant_metrics(session: SessionDep, params: PageDep):
    """Every kitchen that exists, busiest first.

    Paged in Python rather than in SQL, and that is a deliberate downgrade: the
    aggregate is twenty-five rows wide because it is one row per restaurant, and
    windowing it in the database would mean either duplicating the roll-up here
    or pushing a LIMIT into a shared service for one caller's benefit. If the
    platform ever has enough kitchens for that to matter, `kitchen_rows` grows a
    page parameter and this reverts.

    Busiest first, as before — the ordering is this endpoint's, not the shared
    aggregate's, which returns rows in id order.
    """
    rows = await kitchen_rows(session, days=0)
    rows.sort(key=lambda row: (-row.orders, row.restaurant_id))
    window = rows[params.offset : params.offset + params.limit]
    return Page[RestaurantMetrics](
        items=[_to_restaurant_metrics(row) for row in window],
        total=len(rows),
        limit=params.limit,
        offset=params.offset,
    )
