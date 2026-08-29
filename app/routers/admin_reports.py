"""Five reports over the whole platform.

The partner's own reports live in routers/reports.py, scoped to one restaurant by
`require_staff`. These are the other half of the same question and cannot share a
route: "which of our kitchens is slipping" is not something a restaurant may ask
about its competitors.

Two rules hold throughout, and they are the two that reports get wrong:

  * Revenue means DELIVERED orders only. A cancelled or in-flight basket has sold
    nothing. Said again at every place it is computed.
  * A day is a LOCAL calendar day, cut in IST. The window arrives as a number of
    days and becomes UTC instants in services/admin_insights, the same module the
    navigation's counts and the commission ledger use — so a sales report and the
    ledger can never disagree about which day a 23:30 order belongs to.

Everything is one aggregate query per report. Nothing loops over orders in
Python, because the window can legitimately be every order the platform has ever
taken.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, select

from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.models.catalog import Restaurant
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.user import User
from app.schemas.admin import CommissionLedger, CommissionRow
from app.schemas.admin_reports import (
    CommissionCityRow,
    CommissionReport,
    CustomerReport,
    CustomerReportRow,
    OrderHourRow,
    OrderReport,
    OrderStatusRow,
    RestaurantReport,
    RestaurantReportRow,
    SalesDay,
    SalesReport,
)
from app.services import platform_settings
from app.services.admin_insights import (
    DAY_BOUNDARY_TZ,
    SECONDS_PER_MINUTE,
    commission_on,
    kitchen_rows,
    slipping,
    start_of_today,
    window_start,
)
from app.services.money import money

router = APIRouter(
    prefix="/admin/reports",
    tags=["admin"],
    dependencies=[Depends(require_platform_role())],
)

ADMIN_RESPONSES = {**UNAUTHENTICATED, **NOT_PLATFORM}

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365
HOURS_PER_DAY = 24
_ZERO = Decimal("0.00")

#: Rows in the customer report. Enough to act on, not a directory dump — the
#: directory is what GET /users is for.
TOP_CUSTOMERS = 25

WindowDays = Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)]


def _bounds(days: int) -> tuple[datetime, datetime]:
    """The window as a half-open pair of UTC instants.

    Half-open on purpose: `>= start, < end` covers every instant of today
    without the "23:59:59.999999" fencepost that silently drops the last
    microsecond of a day, and it lets Postgres use the placed_at index.
    """
    now = datetime.now(UTC)
    return window_start(now, days), start_of_today(now) + timedelta(days=1)


def _local_day(column):
    """A UTC timestamp column as the local calendar day it fell on.

    The conversion is in SQL rather than in Python because the grouping happens
    there: bucketing by a UTC date would put every order after 18:30 IST on the
    following day, which is most of an Indian evening.
    """
    return cast(func.timezone(str(DAY_BOUNDARY_TZ), column), Date)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _mean_value(total: Decimal, count: int) -> Decimal:
    return money(total / count) if count else _ZERO


@router.get("/sales", response_model=SalesReport, responses=ADMIN_RESPONSES)
async def sales_report(session: SessionDep, days: WindowDays = DEFAULT_WINDOW_DAYS):
    """How much came in, day by day.

    Commission is summed per order at that order's own kitchen rate, not applied
    to a daily gross at one platform rate: two kitchens on different rates
    selling the same amount do not earn the platform the same money, and a
    report that pretended otherwise would not reconcile against the ledger.
    """
    start, end = _bounds(days)
    delivered = Order.status == OrderStatus.DELIVERED
    day = _local_day(Order.placed_at).label("day")

    # The per-order commission expression: the order's total times its own
    # restaurant's rate. Joined rather than looked up per row.
    commission_expr = func.coalesce(
        func.sum(
            Order.total_amount * Restaurant.commission_percent / 100
        ).filter(delivered),
        _ZERO,
    )

    rows = (
        await session.execute(
            select(
                day,
                func.count(Order.id),
                func.count(Order.id).filter(delivered),
                func.coalesce(
                    func.sum(Order.total_amount).filter(delivered), _ZERO
                ),
                commission_expr,
            )
            .join(Restaurant, Restaurant.id == Order.restaurant_id)
            .where(Order.placed_at >= start, Order.placed_at < end)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    sales_days = [
        SalesDay(
            day=row[0],
            orders=int(row[1]),
            delivered=int(row[2]),
            gross=money(row[3]),
            commission=money(row[4]),
            avg_order_value=_mean_value(money(row[3]), int(row[2])),
        )
        for row in rows
    ]

    cancelled = await session.scalar(
        select(func.count(Order.id)).where(
            Order.status == OrderStatus.CANCELLED,
            Order.placed_at >= start,
            Order.placed_at < end,
        )
    )

    orders_total = sum(row.orders for row in sales_days)
    delivered_total = sum(row.delivered for row in sales_days)
    gross_total = sum((row.gross for row in sales_days), start=_ZERO)

    return SalesReport(
        days=sales_days,
        orders=orders_total,
        delivered=delivered_total,
        cancelled=int(cancelled or 0),
        gross=gross_total,
        commission=sum((row.commission for row in sales_days), start=_ZERO),
        avg_order_value=_mean_value(gross_total, delivered_total),
        peak=max(sales_days, key=lambda row: row.orders) if sales_days else None,
        window_days=days,
    )


@router.get(
    "/restaurants", response_model=RestaurantReport, responses=ADMIN_RESPONSES
)
async def restaurant_report(
    session: SessionDep, days: WindowDays = DEFAULT_WINDOW_DAYS
):
    """Which kitchens earned the money, and which are costing it.

    Reads the same per-kitchen aggregate the navigation's slipping count and the
    commission ledger read, so the three cannot disagree about a kitchen.
    """
    rows = await kitchen_rows(session, days)
    slipping_ids = {row.restaurant_id for row in slipping(rows)}

    gaps = [row.gap_minutes for row in rows if row.gap_minutes is not None]

    report_rows = [
        RestaurantReportRow(
            restaurant_id=row.restaurant_id,
            name=row.name,
            city=row.city,
            is_active=row.is_active,
            orders=row.orders,
            delivered_orders=row.delivered,
            cancelled_orders=row.cancelled,
            cancellation_rate=row.cancellation_rate,
            gross=row.gross,
            food_value=row.food_value,
            commission_percent=row.commission_percent,
            commission=commission_on(row.gross, row.commission_percent),
            payout=row.gross - commission_on(row.gross, row.commission_percent),
            avg_prep_minutes=row.avg_prep_minutes,
            avg_delivery_minutes=row.avg_delivery_minutes,
            gap_minutes=row.gap_minutes,
            is_slipping=row.restaurant_id in slipping_ids,
        )
        for row in rows
    ]
    report_rows.sort(key=lambda row: row.gross, reverse=True)

    return RestaurantReport(
        rows=report_rows,
        gross=sum((row.gross for row in report_rows), start=_ZERO),
        commission=sum((row.commission for row in report_rows), start=_ZERO),
        median_gap_minutes=round(median(gaps), 1) if gaps else 0.0,
        slipping=len(slipping_ids),
        window_days=days,
    )


@router.get("/orders", response_model=OrderReport, responses=ADMIN_RESPONSES)
async def order_report(session: SessionDep, days: WindowDays = DEFAULT_WINDOW_DAYS):
    """Where orders ended up, and when they are placed."""
    start, end = _bounds(days)
    in_window = (Order.placed_at >= start, Order.placed_at < end)

    status_rows = (
        await session.execute(
            select(
                Order.status,
                func.count(Order.id),
                func.coalesce(func.sum(Order.total_amount), _ZERO),
            )
            .where(*in_window)
            .group_by(Order.status)
        )
    ).all()
    counts = {row[0]: (int(row[1]), money(row[2])) for row in status_rows}
    total = sum(count for count, _ in counts.values())

    # Every status is emitted, present or not: a funnel with holes in it reads as
    # missing data rather than as a stage nobody reached.
    statuses = [
        OrderStatusRow(
            status=status,
            orders=counts.get(status, (0, _ZERO))[0],
            share=_rate(counts.get(status, (0, _ZERO))[0], total),
            gross=counts.get(status, (0, _ZERO))[1],
        )
        for status in OrderStatus
    ]

    hour = func.extract(
        "hour", func.timezone(str(DAY_BOUNDARY_TZ), Order.placed_at)
    ).label("hour")
    hour_rows = dict(
        (int(row[0]), int(row[1]))
        for row in (
            await session.execute(
                select(hour, func.count(Order.id))
                .where(*in_window)
                .group_by(hour)
            )
        ).all()
    )
    hours = [
        OrderHourRow(hour=value, orders=hour_rows.get(value, 0))
        for value in range(HOURS_PER_DAY)
    ]

    # cancelled_at IS NULL is excluded from both sides: with no cancellation
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
                *in_window,
                Order.status == OrderStatus.CANCELLED,
                Order.cancelled_at.is_not(None),
            )
        )
    ).one()

    late, minutes = (
        await session.execute(
            select(
                func.count(Order.id).filter(Order.delivered_at > Order.promised_at),
                func.avg(
                    func.extract("epoch", Order.delivered_at - Order.placed_at)
                    / SECONDS_PER_MINUTE
                ),
            ).where(*in_window, Order.delivered_at.is_not(None))
        )
    ).one()

    return OrderReport(
        statuses=statuses,
        hours=hours,
        orders=total,
        cancelled_inside_window=int(inside or 0),
        cancelled_outside_window=int(outside or 0),
        delivered_late=int(late or 0),
        avg_minutes_to_deliver=round(float(minutes), 1) if minutes is not None else None,
        window_days=days,
    )


@router.get("/customers", response_model=CustomerReport, responses=ADMIN_RESPONSES)
async def customer_report(
    session: SessionDep, days: WindowDays = DEFAULT_WINDOW_DAYS
):
    """Who is spending, and whether they came back."""
    start, end = _bounds(days)
    in_window = (Order.placed_at >= start, Order.placed_at < end)
    delivered = Order.status == OrderStatus.DELIVERED

    rows = (
        await session.execute(
            select(
                User.id,
                User.name,
                User.email,
                User.city,
                User.is_active,
                func.count(Order.id),
                func.count(Order.id).filter(delivered),
                func.count(Order.id).filter(Order.status == OrderStatus.CANCELLED),
                func.coalesce(
                    func.sum(Order.total_amount).filter(delivered), _ZERO
                ),
                func.max(Order.placed_at),
            )
            .join(Order, Order.user_id == User.id)
            .where(*in_window)
            .group_by(User.id, User.name, User.email, User.city, User.is_active)
        )
    ).all()

    report_rows = [
        CustomerReportRow(
            user_id=row[0],
            name=row[1],
            email=row[2],
            city=row[3],
            is_active=row[4],
            orders=int(row[5]),
            delivered=int(row[6]),
            cancelled=int(row[7]),
            spend=money(row[8]),
            avg_order_value=_mean_value(money(row[8]), int(row[6])),
            last_ordered_at=row[9],
        )
        for row in rows
    ]
    report_rows.sort(key=lambda row: row.spend, reverse=True)

    # "New" means the FIRST EVER order landed inside the window. Computed from
    # each customer's own minimum placed_at rather than from the account's
    # created_at: somebody who signed up in March and finally ordered this week
    # is new to the business, whatever the account says.
    first_orders = (
        await session.execute(
            select(Order.user_id, func.min(Order.placed_at)).group_by(Order.user_id)
        )
    ).all()
    first_by_user = {user_id: first for user_id, first in first_orders}
    ordering_ids = {row.user_id for row in report_rows}
    new_customers = sum(
        1
        for user_id in ordering_ids
        if (first := first_by_user.get(user_id)) is not None and first >= start
    )

    never_ordered = await session.scalar(
        select(func.count(User.id)).where(
            ~select(Order.id)
            .where(Order.user_id == User.id)
            .exists()
        )
    )

    return CustomerReport(
        rows=report_rows[:TOP_CUSTOMERS],
        customers=len(report_rows),
        new_customers=new_customers,
        returning_customers=len(report_rows) - new_customers,
        never_ordered=int(never_ordered or 0),
        spend=sum((row.spend for row in report_rows), start=_ZERO),
        window_days=days,
    )


@router.get(
    "/commission", response_model=CommissionReport, responses=ADMIN_RESPONSES
)
async def commission_report(
    session: SessionDep, days: WindowDays = DEFAULT_WINDOW_DAYS
):
    """What the platform kept, by city and by kitchen.

    The per-kitchen half IS `GET /admin/commission` — same call, same figures.
    Read city-first, then kitchen: three cities behave differently enough that a
    single platform-wide figure hides the only actionable thing in it, one city
    carrying the revenue while another carries the rate.
    """
    settings = await platform_settings.load(session)
    rows = await kitchen_rows(session, days)

    ledger_rows = [
        CommissionRow(
            restaurant_id=row.restaurant_id,
            name=row.name,
            city=row.city,
            delivered_orders=row.delivered,
            gross=row.gross,
            food_value=row.food_value,
            commission_percent=row.commission_percent,
            is_negotiated=row.commission_percent
            != settings.commission_default_percent,
            commission=commission_on(row.gross, row.commission_percent),
            payout=row.gross - commission_on(row.gross, row.commission_percent),
        )
        for row in rows
    ]
    ledger_rows.sort(key=lambda row: row.commission, reverse=True)

    by_city: dict[str, list[CommissionRow]] = defaultdict(list)
    for row in ledger_rows:
        by_city[row.city].append(row)

    cities = [
        CommissionCityRow(
            city=city,
            restaurants=len(city_rows),
            delivered_orders=sum(row.delivered_orders for row in city_rows),
            gross=sum((row.gross for row in city_rows), start=_ZERO),
            commission=sum((row.commission for row in city_rows), start=_ZERO),
        )
        for city, city_rows in by_city.items()
    ]
    cities.sort(key=lambda row: row.commission, reverse=True)

    ledger = CommissionLedger(
        rows=ledger_rows,
        gross=sum((row.gross for row in ledger_rows), start=_ZERO),
        food_value=sum((row.food_value for row in ledger_rows), start=_ZERO),
        commission=sum((row.commission for row in ledger_rows), start=_ZERO),
        payout=sum((row.payout for row in ledger_rows), start=_ZERO),
        default_percent=settings.commission_default_percent,
        settlement_days=settings.commission_settlement_days,
        days=days,
    )
    return CommissionReport(ledger=ledger, cities=cities)
