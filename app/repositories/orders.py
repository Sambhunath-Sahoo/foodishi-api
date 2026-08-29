from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import Address
from app.models.catalog import MenuItem, Restaurant, RestaurantPolicy, restaurant_cuisines
from app.models.coupon import Coupon, CouponRedemption
from app.models.enums import OrderStatus, PaymentStatus, RefundStatus
from app.models.order import Order
from app.models.payment import Payment, Refund
from app.services.order_state import TERMINAL


async def load_restaurant_and_policy(
    session: AsyncSession, restaurant_id: int
) -> tuple[Restaurant | None, RestaurantPolicy | None]:
    restaurant = await session.get(Restaurant, restaurant_id)
    policy = await session.get(RestaurantPolicy, restaurant_id) if restaurant else None
    return restaurant, policy


async def load_menu_items(session: AsyncSession, ids: list[int]) -> dict[int, MenuItem]:
    """One query for every line, not one per line."""
    rows = await session.execute(select(MenuItem).where(MenuItem.id.in_(ids)))
    return {item.id: item for item in rows.scalars()}


async def load_address(session: AsyncSession, address_id: int) -> Address | None:
    return await session.get(Address, address_id)


async def load_coupon_by_code(session: AsyncSession, code: str) -> Coupon | None:
    return await session.scalar(select(Coupon).where(Coupon.code == code.upper()))


async def cuisine_ids_for(session: AsyncSession, restaurant_id: int) -> set[int]:
    rows = await session.execute(
        select(restaurant_cuisines.c.cuisine_id).where(
            restaurant_cuisines.c.restaurant_id == restaurant_id
        )
    )
    return {row[0] for row in rows}


async def user_redemption_count(
    session: AsyncSession, coupon_id: int, user_id: int
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_id == coupon_id,
                CouponRedemption.user_id == user_id,
            )
        )
        or 0
    )


async def captured_total(session: AsyncSession, order_id: int) -> Decimal:
    """What the customer has actually paid. Authorized-but-not-captured money
    has not moved, so it is not refundable."""
    return Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.order_id == order_id,
                Payment.status.in_(
                    [PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED]
                ),
            )
        )
        or 0
    )


async def refunded_total(session: AsyncSession, order_id: int) -> Decimal:
    """What has already been given back, or is on its way back.

    A FAILED refund never left the building, so it does not consume headroom --
    the same rule, and deliberately the same status tuple, as
    OUTSTANDING_STATUSES in app/routers/refunds.py. The two money-out paths must
    agree on what "already refunded" means or one of them over-refunds.
    """
    return Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0)).where(
                Refund.order_id == order_id,
                Refund.status.in_(
                    [
                        RefundStatus.INITIATED,
                        RefundStatus.PROCESSING,
                        RefundStatus.COMPLETED,
                    ]
                ),
            )
        )
        or 0
    )


async def find_by_idempotency_key(session: AsyncSession, key: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.idempotency_key == key))


# Everything that is neither finished nor abandoned — what "live" means to a
# customer watching a tracker and to a restaurant working a queue.
#
# Derived from order_state.TERMINAL rather than listing DELIVERED and CANCELLED
# again: which statuses are final is that module's rule, and a second copy here
# would keep returning a newly-added terminal status as "live" long after the
# lifecycle said it was over.
LIVE_STATUSES = tuple(s for s in OrderStatus if s not in TERMINAL)


def order_query(
    *,
    user_id: int | None = None,
    restaurant_id: int | None = None,
    status: OrderStatus | None = None,
    live: bool = False,
    placed_from: datetime | None = None,
    placed_to: datetime | None = None,
) -> Select[tuple[Order]]:
    """The one place order-list filtering is expressed.

    GET /orders and GET /me/orders differ only in who is allowed to choose
    user_id, so the filters themselves live here rather than being written out
    twice — a filter fixed in one copy and missed in the other is exactly the
    bug that leaks one customer's orders to another.
    """
    # id breaks ties so paging is stable across requests. placed_at is not
    # unique -- the seed quantises it to whole minutes, so ties are guaranteed
    # rather than unlikely -- and with no tiebreaker Postgres may return tied
    # rows in a different physical order for page 2 than it did for page 1, so a
    # client paging the queue sees some orders twice and never sees others while
    # `total` keeps reporting the right count. Every other list statement in this
    # codebase already does this; this was the one that did not.
    statement = select(Order).order_by(Order.placed_at.desc(), Order.id.desc())
    if user_id is not None:
        statement = statement.where(Order.user_id == user_id)
    if restaurant_id is not None:
        statement = statement.where(Order.restaurant_id == restaurant_id)
    if status is not None:
        statement = statement.where(Order.status == status)
    if live:
        statement = statement.where(Order.status.in_(LIVE_STATUSES))
    if placed_from is not None:
        statement = statement.where(Order.placed_at >= placed_from)
    if placed_to is not None:
        statement = statement.where(Order.placed_at <= placed_to)
    return statement
