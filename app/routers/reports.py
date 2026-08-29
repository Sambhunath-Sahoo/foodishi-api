"""One restaurant's own trade, over a window of local calendar days.

Three read-only reports — what sold per day, which dishes sold, and how well
the promises were kept. Nothing here writes, and nothing here leaves the
process: no export service, no scheduled email, no BI provider. A report is a
query against this database and the response body is the whole deliverable.

Two rules hold throughout, and they are the two that reports get wrong:

  * Revenue means DELIVERED orders only. A cancelled or in-flight basket has
    sold nothing. Said again at every place it is computed below.
  * A day is a LOCAL calendar day. The window arrives as dates and is turned
    into UTC instants by services/settlements.period_bounds — the same function
    the statement and the ledger use, so a sales report and a payout can never
    disagree about which day a 23:30 order belongs to.

The restaurant is the one in the path and it is never taken on trust: the
router's gate resolves {restaurant_id} against restaurant_staff before any
handler runs, and every query below filters on that same id. The path segment
is a lookup key, not a claim — see the same note in app/routers/delivery.py.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, Select, cast, distinct, func, select

from app.core.errors import unprocessable
from app.db import SessionDep
from app.dependencies.identity import FORBIDDEN, UNAUTHENTICATED, require_staff
from app.models.catalog import MenuCategory, MenuItem, Restaurant
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem, OrderStatusEvent
from app.schemas.reports import PerformanceReport, PopularItem, SalesDay
from app.services.money import money
from app.services.settlements import SETTLEMENT_TZ, period_bounds

# The gate hangs off the router rather than each route because there is nothing
# under this prefix that a different rank should see: all three routes read the
# same restaurant's trade for the same reason, and a fourth report added later
# inherits the check instead of shipping unguarded.
#
# require_staff() at its default rank — STAFF, not ADMIN — is deliberate. These
# are the numbers on the wall by the pass: what has gone out today, which dish
# is selling faster than the prep list expects, whether the last hour of
# deliveries hit the promise. A shift worker acts on those, and locking them
# behind an admin makes the kitchen ask a manager for figures it can already
# read off its own order queue. What is NOT here is the money side: commission,
# payouts, settlements and anything Foodishi owes are admin_of_restaurant in
# app/routers (finance), because those are the partner's contract rather than
# the shift's work. `revenue` below is the kitchen's own takings on food it
# cooked, which is the same trade the queue already shows line by line.
router = APIRouter(
    prefix="/restaurants/{restaurant_id}/reports",
    tags=["reports"],
    dependencies=[Depends(require_staff())],
)

# Every route can refuse an unauthenticated or out-of-scope caller, declared
# once in the style of app/routers/catalog_admin.py.
SCOPED = {**UNAUTHENTICATED, **FORBIDDEN}

DEFAULT_WINDOW_DAYS = 30

# A year plus a day, so "the same period last year" and a leap year both fit
# while an open-ended range does not. Without a cap the day-by-day report is a
# sequential scan over the whole of `orders` for a busy restaurant, triggered by
# a query string anybody on shift can type.
MAX_WINDOW_DAYS = 366

SECONDS_PER_MINUTE = 60

# The zone a trading day is cut on, taken from the finance module rather than
# re-typed: the constant is the boundary these reports share with the statement
# a partner reconciles against, and two spellings of "Asia/Kolkata" is exactly
# how they would drift apart. _NAME is the same zone as Postgres needs it —
# a string for AT TIME ZONE, since the bucketing happens in SQL.
DAY_BOUNDARY_TZ = SETTLEMENT_TZ
DAY_BOUNDARY_TZ_NAME = SETTLEMENT_TZ.key


@dataclass(frozen=True)
class ReportWindow:
    """A validated, inclusive range of local dates, plus its UTC instants.

    Both forms are carried because both are needed and deriving one from the
    other twice is how they stop matching: the dates drive the day series a
    chart is plotted against, and the instants are what `placed_at` is compared
    to so the query can still use ix_orders_restaurant_status and friends.
    """

    date_from: date
    date_to: date
    #: Half-open, `>= start` and `< end` — see settlements.period_bounds for why
    #: the last microsecond of date_to is not fenced off by hand.
    start: datetime
    end: datetime


def report_window(
    date_from: Annotated[
        date | None,
        Query(description="First local calendar day, inclusive."),
    ] = None,
    date_to: Annotated[
        date | None,
        Query(description="Last local calendar day, inclusive."),
    ] = None,
) -> ReportWindow:
    """The window all three reports are cut on, defaulted and bounded.

    Defaults are computed per request rather than baked into the signature: a
    literal default would freeze "today" at the moment the process booted and
    quietly stop moving.

    Both failures are 422 rather than 400 — the request parses, it just asks for
    something the API will not do (app/core/errors.unprocessable).
    """
    # Today where the restaurant is, not where the server is. At 23:30 IST a UTC
    # "today" is still yesterday, so the default window would end on a day the
    # kitchen finished trading and omit the shift being worked right now.
    today = datetime.now(UTC).astimezone(DAY_BOUNDARY_TZ).date()
    end_day = date_to if date_to is not None else today
    # Today counts as day one, so the default is 30 buckets rather than 31 —
    # the same convention as routers/metrics.py.
    start_day = (
        date_from
        if date_from is not None
        else end_day - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    )

    if start_day > end_day:
        raise unprocessable(
            f"date_from {start_day} is after date_to {end_day}"
        )
    span_days = (end_day - start_day).days + 1
    if span_days > MAX_WINDOW_DAYS:
        raise unprocessable(
            f"Range of {span_days} days is longer than the {MAX_WINDOW_DAYS} "
            "day maximum; ask for a shorter period"
        )

    start, end = period_bounds(start_day, end_day)
    return ReportWindow(
        date_from=start_day, date_to=end_day, start=start, end=end
    )


ReportWindowDep = Annotated[ReportWindow, Depends(report_window)]


def _in_window(restaurant_id: int, window: ReportWindow):
    """This restaurant's orders placed in the window, as WHERE criteria.

    Attributed by placed_at, matching routers/metrics.py and
    services/settlements.compute_period: an order belongs to the day it was
    ordered on, so the day it appears in a report is the day it appears on a
    statement. Bucketing revenue by delivered_at instead would move a 23:50
    order onto the next day's takings and leave the two irreconcilable.
    """
    return (
        Order.restaurant_id == restaurant_id,
        Order.placed_at >= window.start,
        Order.placed_at < window.end,
    )


def _local_day(column):
    """The local calendar day a UTC timestamp falls on.

    AT TIME ZONE in SQL rather than a Python-side conversion, so the grouping
    key and the generated day series are both dates Postgres produced and the
    LEFT JOIN below can match them at all.
    """
    return cast(func.timezone(DAY_BOUNDARY_TZ_NAME, column), Date)


def _rate(part: int, whole: int) -> float:
    # 0.0 rather than ZeroDivisionError: a restaurant that has delivered nothing
    # yet still has to be able to open this report.
    return round(part / whole, 4) if whole else 0.0


def _minutes(seconds: Decimal | float | None) -> int:
    """An averaged duration in whole minutes, or 0 when nothing was measured.

    0 is genuinely ambiguous — it means "no order got that far", not "instant"
    — which is why `orders` and `delivered` travel with it in the response and
    should be read first. An int is what the tile renders; a fractional minute
    of average prep time is precision nobody acts on.
    """
    return int(round(float(seconds) / SECONDS_PER_MINUTE)) if seconds is not None else 0


def _sales_statement(restaurant_id: int, window: ReportWindow) -> Select:
    """The days that actually traded, one row each. Gaps are filled by the route.

    This was a `generate_series` LEFT JOINed to the aggregate, so that the zero
    days came out of the database. The argument was that one place should decide
    which days exist — but `ReportWindow` already decides that, and it decides it
    before either the SQL or the fill runs. So the SQL version bought no such
    guarantee, and it cost a table-valued-function alias that SQLAlchemy will not
    render with its column list (`generate_series(...) AS anon_1` with no
    `(day)`, which Postgres rejects).

    Grouping only what traded and filling from `window` is five obvious lines,
    reads the same, and cannot disagree with the window — because it IS the
    window. See `_fill_days`.
    """
    delivered = Order.status == OrderStatus.DELIVERED
    day = _local_day(Order.placed_at)
    orders = func.count(Order.id)

    trade = (
        select(
            day.label("day"),
            orders.label("orders"),
            orders.filter(delivered).label("delivered"),
            orders.filter(Order.status == OrderStatus.CANCELLED).label("cancelled"),
            # Revenue and discount are DELIVERED orders only — a cancelled or
            # in-flight basket sold nothing, so it contributes to neither. The
            # COALESCE inside covers a day whose orders were all cancelled;
            # the one outside covers a day with no orders at all.
            func.coalesce(
                func.sum(Order.total_amount).filter(delivered), Decimal("0")
            ).label("revenue"),
            func.coalesce(
                func.sum(Order.discount_amount).filter(delivered), Decimal("0")
            ).label("discount"),
        )
        .where(*_in_window(restaurant_id, window))
        .group_by(day)
        # Oldest first: this is plotted left to right.
        .order_by(day)
    )
    return trade


def _fill_days(window: ReportWindow, traded: dict[date, SalesDay]) -> list[SalesDay]:
    """Every day in the window, in order, with the quiet ones at zero.

    Days with no trade are PRESENT at zero rather than omitted. A chart that
    skips them draws a straight line across a Monday the restaurant was open and
    sold nothing, which reads as "we did fine" instead of "we did nothing" — and
    a per-day average taken over only the trading days is the same lie with a
    number attached.
    """
    days: list[SalesDay] = []
    cursor = window.date_from
    while cursor <= window.date_to:
        days.append(
            traded.get(
                cursor,
                SalesDay(
                    date=cursor,
                    orders=0,
                    delivered=0,
                    cancelled=0,
                    revenue=Decimal("0.00"),
                    discount=Decimal("0.00"),
                ),
            )
        )
        cursor += timedelta(days=1)
    return days


@router.get(
    "",
    response_model=list[SalesDay],
    responses={**SCOPED},
    summary="Day-by-day sales for one restaurant",
)
async def get_sales_report(
    restaurant_id: int, session: SessionDep, window: ReportWindowDep
):
    rows = (await session.execute(_sales_statement(restaurant_id, window))).all()
    traded = {
        day: SalesDay(
            date=day,
            orders=orders,
            delivered=delivered,
            cancelled=cancelled,
            revenue=revenue,
            discount=discount,
        )
        for day, orders, delivered, cancelled, revenue, discount in rows
    }
    return _fill_days(window, traded)


def _items_statement(restaurant_id: int, window: ReportWindow) -> Select:
    """Per-dish totals over DELIVERED orders only.

    Grouped by menu_item_id alone, never by the frozen name: a dish renamed
    mid-period would otherwise appear twice with its sales split between the
    two spellings, and the top of the chart is exactly where that matters.
    mode() picks the name it sold under most often in the window.

    Both joins to the live menu are OUTER. order_items.menu_item_id is ON
    DELETE RESTRICT, so a dish that has sold cannot in fact be deleted today —
    but an inner join would silently drop the line if that ever changed, and a
    popularity report whose revenue does not reconcile with the sales report is
    worse than one admitting it no longer knows the dish's category.
    """
    quantity = func.sum(OrderItem.quantity)

    return (
        select(
            OrderItem.menu_item_id,
            func.mode().within_group(OrderItem.item_name),
            MenuCategory.name,
            quantity,
            # Distinct, because one order can carry the same dish on more than
            # one line — a half plate and a full plate are two lines of the
            # same menu_item_id, and counting rows would report two orders.
            func.count(distinct(OrderItem.order_id)),
            func.coalesce(func.sum(OrderItem.line_total), Decimal("0")),
            MenuItem.is_available,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .outerjoin(MenuItem, MenuItem.id == OrderItem.menu_item_id)
        .outerjoin(MenuCategory, MenuCategory.id == MenuItem.category_id)
        .where(
            *_in_window(restaurant_id, window),
            # Revenue means DELIVERED orders only, here as everywhere: a
            # cancelled basket tells the kitchen nothing about what sells.
            Order.status == OrderStatus.DELIVERED,
        )
        .group_by(OrderItem.menu_item_id, MenuCategory.name, MenuItem.is_available)
        # menu_item_id breaks ties so paging or re-running the report cannot
        # shuffle two equally popular dishes past each other.
        .order_by(quantity.desc(), OrderItem.menu_item_id)
    )


@router.get(
    "/items",
    response_model=list[PopularItem],
    responses={**SCOPED},
    summary="What sold, per dish",
)
async def get_item_report(
    restaurant_id: int, session: SessionDep, window: ReportWindowDep
):
    rows = (await session.execute(_items_statement(restaurant_id, window))).all()
    return [
        PopularItem(
            menu_item_id=item_id,
            name=name,
            category_name=category_name,
            quantity=quantity,
            orders=orders,
            revenue=revenue,
            is_available=is_available,
        )
        for item_id, name, category_name, quantity, orders, revenue, is_available in rows
    ]


def _performance_statement(restaurant_id: int, window: ReportWindow) -> Select:
    """Counts and durations for the window, in one pass over the orders.

    Both joins are keyed one-row-per-order — one is grouped by order_id, the
    other is a distinct list of them — so neither can fan the orders out and
    inflate the counts. That is the whole reason they are joined subqueries
    rather than correlated sub-selects inside the aggregates.
    """
    delivered = Order.status == OrderStatus.DELIVERED
    cancelled = Order.status == OrderStatus.CANCELLED
    orders = func.count(Order.id)

    # When the kitchen first called each order ready. MIN, not MAX: an order
    # bounced back to preparing and made ready again took as long as it took
    # the first time, and the second stamp would flatter the prep figure.
    prep = (
        select(
            OrderStatusEvent.order_id.label("order_id"),
            func.min(OrderStatusEvent.created_at).label("ready_at"),
        )
        .where(OrderStatusEvent.to_status == OrderStatus.READY_FOR_PICKUP)
        .group_by(OrderStatusEvent.order_id)
        .subquery()
    )

    # Orders that were ever accepted. Read off the trail rather than from
    # orders.status, which only remembers where the order ended up: a cancelled
    # order looks identical whether the kitchen accepted it and then gave up or
    # never accepted it at all, and those are different failures.
    accepted = (
        select(distinct(OrderStatusEvent.order_id).label("order_id"))
        .where(OrderStatusEvent.to_status == OrderStatus.CONFIRMED)
        .subquery()
    )
    never_accepted = accepted.c.order_id.is_(None)

    # An order cannot be ready before it was placed, so a row that says it was
    # is a broken measurement rather than a fast kitchen. Averaging one in
    # produces a NEGATIVE prep time on a manager's screen, which is how a report
    # loses its reader's trust for good — and it takes very few such rows to do
    # it: five delivered orders with one bad stamp among them averaged to −80
    # minutes on live data (3 of 435 ready events in the seed predate their own
    # order). Excluded rather than clamped to zero: zero is a claim about how
    # long the kitchen took, and we do not know how long it took.
    prep_interval = prep.c.ready_at - Order.placed_at
    delivery_interval = Order.delivered_at - Order.placed_at
    prep_seconds = func.avg(func.extract("epoch", prep_interval)).filter(
        prep.c.ready_at > Order.placed_at
    )
    delivery_seconds = func.avg(func.extract("epoch", delivery_interval)).filter(
        Order.delivered_at > Order.placed_at
    )

    return (
        select(
            orders,
            orders.filter(delivered),
            orders.filter(cancelled),
            # Rejected: cancelled without ever reaching confirmed. A strict
            # subset of the cancelled count above, never an addition to it.
            orders.filter(cancelled, never_accepted),
            orders.filter(delivered, Order.delivered_at <= Order.promised_at),
            # Delivered orders only — the numerator of avg_order_value has to
            # be money that landed, or the average spreads earned revenue over
            # baskets nobody paid for. Same rule as routers/metrics.py.
            func.coalesce(func.sum(Order.total_amount).filter(delivered), Decimal("0")),
            # AVG skips NULL rows, which is exactly the rule we want: an order
            # with no ready_for_pickup event is excluded from the prep average
            # rather than counted as zero minutes.
            prep_seconds,
            # No is_not(None) needed: `delivered_at > placed_at` is NULL for an
            # undelivered order, and a NULL filter predicate excludes the row —
            # so the sanity check above already does the work a null check would.
            delivery_seconds,
        )
        .select_from(Order)
        .outerjoin(prep, prep.c.order_id == Order.id)
        .outerjoin(accepted, accepted.c.order_id == Order.id)
        .where(*_in_window(restaurant_id, window))
    )


@router.get(
    "/performance",
    response_model=PerformanceReport,
    responses={**SCOPED},
    summary="Promises kept, and how long they took",
)
async def get_performance_report(
    restaurant_id: int, session: SessionDep, window: ReportWindowDep
):
    (
        orders,
        delivered,
        cancelled,
        rejected,
        on_time,
        gross,
        prep_seconds,
        delivery_seconds,
    ) = (
        await session.execute(_performance_statement(restaurant_id, window))
    ).one()

    # The customer-facing rolling average, read live off the restaurant row
    # rather than recomputed from reviews in the window — see the schema.
    rating = (
        await session.execute(
            select(Restaurant.rating, Restaurant.rating_count).where(
                Restaurant.id == restaurant_id
            )
        )
    ).one_or_none()
    if rating is None:
        # Not reachable through the gate: restaurant_staff.restaurant_id
        # cascades, so a membership cannot outlive its restaurant and a caller
        # with no membership was already refused. Spelled out anyway, because
        # unpacking None here would turn a broken invariant into a 500.
        raise unprocessable(f"Restaurant {restaurant_id} no longer exists")
    stars, rating_count = rating

    return PerformanceReport(
        orders=orders,
        delivered=delivered,
        cancelled=cancelled,
        rejected=rejected,
        on_time_rate=_rate(on_time, delivered),
        avg_prep_minutes=_minutes(prep_seconds),
        avg_delivery_minutes=_minutes(delivery_seconds),
        # money() rather than a raw division: the quantisation happens once,
        # server-side, or two screens rounding it differently disagree by a
        # paisa. 0.00 until something has actually been delivered.
        avg_order_value=money(gross / delivered) if delivered else Decimal("0.00"),
        rating=stars,
        rating_count=rating_count,
    )
