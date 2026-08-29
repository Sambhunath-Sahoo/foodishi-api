"""Weekly payout statements per restaurant.

`restaurant_settlements` was empty, and the consequence was specific: the partner
console's `GET /restaurants/{id}/earnings` computes `gross`, `commission`,
`refunds` and `net` live and correctly, but reads `settled` and `pending` from
this table — so both showed 0.00 permanently, and `GET /ledger`'s payout lines
were always empty. A restaurant looking at "₹0.00 settled" had no way to tell
that from "nothing has been paid yet".

`app/services/settlements.py:cut_settlement` exists to write these rows and has
NO CALLER anywhere in the codebase — no route, no job, no CLI. That is a real gap
and this phase does not close it: seeding statements gives the screens data, it
does not give the platform a way to cut a statement in production. The function
still needs a route or a scheduled job behind it.

The arithmetic here deliberately mirrors `compute_period`: gross over DELIVERED
orders in the window, commission at the restaurant's own rate on the FOOD value
(never on tax or delivery — taking a percentage of the government's money is the
comment `app/routers/metrics.py` makes about this), GST on the commission, minus
refunds that completed in the window, and `net` stored rather than derived so a
statement cannot change under the restaurant after the fact.
"""

import logging
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Restaurant
from app.models.enums import OrderStatus, RefundStatus, SettlementStatus
from app.models.finance import Settlement
from app.models.order import Order
from app.models.payment import Refund
from app.services.money import money

logger = logging.getLogger(__name__)

#: GST charged on the platform's commission — a service fee, taxed as one. Kept
#: as its own constant rather than reusing pricing.TAX_RATE, which is the tax on
#: FOOD: the two happen to match today and are not the same rule.
COMMISSION_TAX_RATE = Decimal("0.18")

#: How many complete weeks back to cut statements for.
#:
#: Four, so the payouts screen has a short history rather than a single row, and
#: so the oldest can be `paid` while the newest is still `scheduled` — the
#: transition every payout screen has to render.
WEEKS = 4

#: Newest first: the most recent week is still scheduled, the one before it is
#: processing, and everything older has been paid. One `failed` row is forced in
#: as well, because a bank rejection is the status a restaurant most needs to see
#: and the partner console had no entry for it until recently.
STATUS_BY_AGE: tuple[SettlementStatus, ...] = (
    SettlementStatus.SCHEDULED,
    SettlementStatus.PROCESSING,
    SettlementStatus.PAID,
    SettlementStatus.PAID,
)


def _week_bounds(weeks_ago: int, today: date) -> tuple[date, date]:
    """The Monday-to-Sunday week that ended `weeks_ago` weeks before this one."""
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=weeks_ago)
    return start, start + timedelta(days=6)


async def build(session: AsyncSession, rng: random.Random) -> dict[str, int]:
    """Cut one statement per restaurant per completed week."""
    restaurants = list(await session.scalars(select(Restaurant)))
    if not restaurants:
        logger.warning("No restaurants — skipping settlements.")
        return {"statements": 0, "paid": 0, "failed": 0}

    today = datetime.now(UTC).date()
    written = paid = failed = 0

    # One restaurant gets a failed payout, so the state is reachable without
    # making it look common.
    failed_for = restaurants[0].id if restaurants else None

    for restaurant in restaurants:
        percent = Decimal(str(restaurant.commission_percent or 0))

        for weeks_ago in range(1, WEEKS + 1):
            period_from, period_to = _week_bounds(weeks_ago, today)
            window_start = datetime.combine(
                period_from, datetime.min.time(), tzinfo=UTC
            )
            window_end = datetime.combine(
                period_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            )

            totals = (
                await session.execute(
                    select(
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.total_amount), Decimal(0)),
                        func.coalesce(func.sum(Order.subtotal), Decimal(0)),
                    ).where(
                        Order.restaurant_id == restaurant.id,
                        Order.status == OrderStatus.DELIVERED,
                        Order.placed_at >= window_start,
                        Order.placed_at < window_end,
                    )
                )
            ).one()
            orders_count, gross_raw, food_raw = totals

            refunds_raw = await session.scalar(
                select(func.coalesce(func.sum(Refund.amount), Decimal(0)))
                .join(Order, Order.id == Refund.order_id)
                .where(
                    Order.restaurant_id == restaurant.id,
                    Refund.status == RefundStatus.COMPLETED,
                    Refund.completed_at >= window_start,
                    Refund.completed_at < window_end,
                )
            )

            # A week with no delivered orders gets NO statement. An all-zero row
            # is not a payout, and it would pad the history with lines nobody can
            # act on.
            if not orders_count:
                continue

            gross = money(gross_raw or 0)
            # Commission on the food value only, never on tax or delivery.
            commission = money(money(food_raw or 0) * percent / Decimal("100"))
            tax_on_commission = money(commission * COMMISSION_TAX_RATE)
            refunds = money(refunds_raw or 0)
            net = money(gross - commission - tax_on_commission - refunds)

            status = STATUS_BY_AGE[min(weeks_ago - 1, len(STATUS_BY_AGE) - 1)]
            if restaurant.id == failed_for and weeks_ago == 2:
                status = SettlementStatus.FAILED

            session.add(
                Settlement(
                    restaurant_id=restaurant.id,
                    # Human-quotable on a support call, and unique so it can be
                    # searched on. ISO week keeps it sortable as text.
                    reference=(
                        f"STL-{restaurant.id:03d}-{period_from.isocalendar().year}"
                        f"W{period_from.isocalendar().week:02d}"
                    ),
                    period_from=period_from,
                    period_to=period_to,
                    orders_count=int(orders_count),
                    gross=gross,
                    commission=commission,
                    tax_on_commission=tax_on_commission,
                    refunds=refunds,
                    net=net,
                    status=status,
                    # Stamped only when the money is recorded as having moved.
                    # SettlementStatus' own docstring is clear that `paid` means
                    # a row was stamped, not that a transfer happened.
                    paid_at=(
                        window_end + timedelta(days=2)
                        if status is SettlementStatus.PAID
                        else None
                    ),
                    account_last4=f"{rng.randint(0, 9999):04d}",
                )
            )
            written += 1
            if status is SettlementStatus.PAID:
                paid += 1
            elif status is SettlementStatus.FAILED:
                failed += 1

    await session.flush()
    return {"statements": written, "paid": paid, "failed": failed}
