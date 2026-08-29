"""What a restaurant earned in a period, and how a statement is frozen.

Nothing in this module moves money. There is no gateway, no bank API and no
HTTP call out of this process — a `restaurant_settlements` row is a RECORD that
a payout is due or was made, exactly as app/models/finance.py says. `cut_settlement`
writes a statement; whoever actually transfers the funds does so in another
system and stamps the row afterwards.

Two rules the rest of the finance domain leans on:

  * Revenue is DELIVERED orders only. A cancelled or in-flight basket has
    earned the kitchen nothing, so it contributes nothing to gross anywhere in
    here.
  * A period is a pair of LOCAL calendar dates, converted to UTC instants here
    and nowhere else. Every caller goes through `period_bounds` so the earnings
    endpoint, the ledger and the frozen settlement row can never disagree about
    where a trading day begins.

Pure of the API: no router imports, no HTTPException. The one failure it can
raise is UnknownRestaurant, and callers behind a restaurant-scoped dependency
cannot reach it.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Restaurant
from app.models.enums import OrderStatus, RefundStatus
from app.models.finance import Settlement
from app.models.order import Order
from app.models.payment import Refund
from app.services.money import money

# GST on the commission *invoice* — Foodishi billing the restaurant for a service,
# which is taxed at 18% and is a different tax from the 5% on the food itself
# (app/services/pricing.py TAX_RATE). It lives here rather than in pricing.py
# because it is never charged to a customer and never appears on an order: it is
# a line on the kitchen's statement, computed only when a period is totalled.
#
# Not to be confused with Restaurant.commission_percent, whose default happens
# also to be 18.00. That is the platform's cut of the sale; this is the tax on
# that cut.
COMMISSION_GST_RATE = Decimal("0.18")

PERCENT_DIVISOR = Decimal("100")

# The timezone a trading day is cut on. Not UTC, and the difference is not
# cosmetic: routers/metrics.py measured a UTC midnight putting 6 orders in
# "today" where the local trading day held 130. A restaurant closing at 23:30
# IST would otherwise see half of Saturday night land on Sunday's statement.
# Storage stays UTC timestamptz throughout; only the boundary is localised.
SETTLEMENT_TZ = ZoneInfo("Asia/Kolkata")


class UnknownRestaurant(LookupError):
    """Asked to total a period for a restaurant that no longer exists.

    Unreachable from any route gated by a restaurant-scoped dependency:
    restaurant_staff.restaurant_id is a foreign key that cascades, so a deleted
    restaurant takes its memberships with it and the caller gets 403 first.
    """


@dataclass(frozen=True)
class PeriodTotals:
    """One period of trade, totalled but not yet written anywhere."""

    orders_count: int
    gross: Decimal
    commission: Decimal
    tax_on_commission: Decimal
    refunds: Decimal
    net: Decimal
    # The rate the commission above was struck at, carried with the figures
    # rather than looked up again by the caller. A statement that prints "18%"
    # next to a commission computed at some other rate is worse than no rate at
    # all, and this way the two cannot drift.
    commission_percent: Decimal


def commission_for(gross: Decimal, percent: Decimal) -> Decimal:
    """The platform's cut of a period's gross, in rupees."""
    return money(gross * percent / PERCENT_DIVISOR)


def gst_on_commission(commission: Decimal) -> Decimal:
    """GST the restaurant owes on that cut. Charged on the commission, not on gross."""
    return money(commission * COMMISSION_GST_RATE)


def period_bounds(period_from: date, period_to: date) -> tuple[datetime, datetime]:
    """Local calendar dates -> the half-open UTC instant range they cover.

    Half-open on purpose: `>= start, < end` includes every instant of period_to
    without the "23:59:59.999999" fencepost that silently drops the last
    microsecond of a day, and it lets Postgres use the placed_at indexes.
    """
    start = datetime.combine(period_from, time.min, tzinfo=SETTLEMENT_TZ)
    end = datetime.combine(period_to + timedelta(days=1), time.min, tzinfo=SETTLEMENT_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


async def commission_percent_for(session: AsyncSession, restaurant_id: int) -> Decimal:
    """The rate this kitchen is on. One indexed primary-key lookup.

    Read live rather than frozen per order: nothing on `orders` records the rate
    in force at the time, so a renegotiated rate reprices history. That is a
    schema gap worth naming — the statement rows written by `cut_settlement` are
    what protect an already-issued statement from it.
    """
    percent = await session.scalar(
        select(Restaurant.commission_percent).where(Restaurant.id == restaurant_id)
    )
    if percent is None:
        raise UnknownRestaurant(f"No restaurant with id {restaurant_id}")
    return percent


async def compute_period(
    session: AsyncSession,
    restaurant_id: int,
    period_from: date,
    period_to: date,
) -> PeriodTotals:
    """Total a window live, from orders and refunds as they stand right now.

    Live is the point: this is what `GET /earnings` answers with, and it moves
    as orders are delivered and refunds complete. `cut_settlement` calls the
    same function and freezes the answer, which is why a statement issued last
    week does not change when a refund lands this week.
    """
    start, end = period_bounds(period_from, period_to)
    percent = await commission_percent_for(session, restaurant_id)

    # Revenue means DELIVERED orders only — a cancelled or in-flight basket has
    # earned the kitchen nothing. Attributed by placed_at, matching how
    # routers/metrics.py attributes revenue, so the ledger line for an order and
    # the period it counts towards agree. COALESCE keeps an empty week at 0.00
    # rather than None.
    orders_count, gross = (
        await session.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.total_amount), Decimal("0")),
            ).where(
                Order.restaurant_id == restaurant_id,
                Order.status == OrderStatus.DELIVERED,
                Order.placed_at >= start,
                Order.placed_at < end,
            )
        )
    ).one()

    # Refunds are NOT filtered to delivered orders: money handed back on an
    # order that was cancelled still left the restaurant's balance, and leaving
    # it out would overstate the payout. Bucketed by completed_at because that
    # is when it went out — which also excludes the null completed_at of a
    # refund that is still processing, belt and braces with the status filter.
    refunds = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), Decimal("0")))
        .join(Order, Order.id == Refund.order_id)
        .where(
            Order.restaurant_id == restaurant_id,
            Refund.status == RefundStatus.COMPLETED,
            Refund.completed_at >= start,
            Refund.completed_at < end,
        )
    )

    commission = commission_for(gross, percent)
    tax = gst_on_commission(commission)
    return PeriodTotals(
        orders_count=int(orders_count),
        # Every figure is quantised once, here, on the way out. The two sums
        # arrive from Postgres already at 2dp (Numeric(10,2) columns); the
        # derived three are rounded by money() — the codebase's single
        # half-up quantize — so no consumer has to round again and disagree.
        gross=money(gross),
        commission=commission,
        tax_on_commission=tax,
        refunds=money(refunds),
        net=money(gross - commission - tax - refunds),
        commission_percent=percent,
    )


async def cut_settlement(
    session: AsyncSession,
    restaurant_id: int,
    period_from: date,
    period_to: date,
    *,
    reference: str,
    account_last4: str | None,
) -> Settlement:
    """Freeze a period onto a statement row. Creates it, or re-cuts it in place.

    No payout happens here and none is requested of anybody: the row records
    that this much is due. `status` starts at SCHEDULED and only whoever runs
    the transfer moves it.

    Upsert on uq_settlement_period rather than insert, because re-cutting a week
    is normal — a late refund completes, someone reruns the job — and a second
    row for the same week would double-count the payout. ON CONFLICT also makes
    two concurrent runs safe: the loser updates instead of dying on the
    duplicate key.
    """
    totals = await compute_period(session, restaurant_id, period_from, period_to)

    frozen = {
        "orders_count": totals.orders_count,
        "gross": totals.gross,
        "commission": totals.commission,
        "tax_on_commission": totals.tax_on_commission,
        "refunds": totals.refunds,
        "net": totals.net,
        "reference": reference,
        "account_last4": account_last4,
    }
    statement = (
        pg_insert(Settlement)
        .values(restaurant_id=restaurant_id, period_from=period_from, period_to=period_to, **frozen)
        .on_conflict_do_update(
            constraint="uq_settlement_period",
            # status and paid_at are deliberately absent: a re-cut refreshes the
            # figures, never the payout state. Resetting a row already stamped
            # paid back to scheduled would make a completed payout look
            # outstanding, and `pending` on the earnings summary would count it
            # twice over the restaurant's lifetime.
            #
            # updated_at is set by hand because ON CONFLICT bypasses the ORM's
            # Python-side onupdate — same reason as the policy upsert in
            # routers/catalog_admin.py.
            set_={**frozen, "updated_at": func.now()},
        )
        .returning(Settlement)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()
