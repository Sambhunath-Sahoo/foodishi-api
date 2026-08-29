"""Rows the acceptance checks in docs/IMPLEMENTATION_PLAN.md target by name.

Random generation will produce something *like* these eventually, but not
reliably, and a test that only sometimes has data to run against is not a test.
"""

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import Address
from app.models.catalog import MenuItem, Restaurant, RestaurantPolicy
from app.models.enums import (
    ActorType,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    RefundReason,
    RefundStatus,
)
from app.models.order import Order, OrderItem, OrderStatusEvent
from app.models.payment import Payment, Refund
from app.services import eta
from app.services.pricing import quote


async def build(session: AsyncSession, rng: random.Random) -> list[str]:
    now = datetime.now(UTC)
    notes: list[str] = []

    restaurant = (await session.execute(select(Restaurant).limit(1))).scalar_one()
    policy = await session.get(RestaurantPolicy, restaurant.id)
    menu = list((await session.execute(
        select(MenuItem).where(MenuItem.restaurant_id == restaurant.id,
                               MenuItem.is_available).limit(3)
    )).scalars())
    # Nearest address to this restaurant, so the quote never trips the
    # max-delivery-distance rule.
    candidates = list((await session.execute(select(Address))).scalars())
    address = min(candidates, key=lambda a: eta.haversine_km(
        restaurant.latitude, restaurant.longitude, a.latitude, a.longitude))

    async def make(status: OrderStatus, placed_at: datetime, window_mins: int) -> Order:
        q = quote(restaurant=restaurant, policy=policy, items=[(menu[0], 2), (menu[1], 1)],
                  latitude=address.latitude, longitude=address.longitude, placed_at=placed_at)
        order = Order(
            user_id=address.user_id, restaurant_id=restaurant.id, address_id=address.id,
            status=status, subtotal=q.subtotal, packaging_fee=q.packaging_fee,
            delivery_fee=q.delivery_fee, tax_amount=q.tax_amount,
            discount_amount=q.discount_amount, total_amount=q.total_amount,
            distance_km=q.distance_km, placed_at=placed_at,
            cancellable_until=placed_at + timedelta(minutes=window_mins),
            promised_at=q.promised_at,
        )
        session.add(order)
        await session.flush()
        session.add_all(
            OrderItem(order_id=order.id, menu_item_id=l.menu_item_id, item_name=l.item_name,
                      unit_price=l.unit_price, quantity=l.quantity, line_total=l.line_total)
            for l in q.lines
        )
        session.add(OrderStatusEvent(order_id=order.id, from_status=None,
                                     to_status=OrderStatus.PENDING,
                                     actor_type=ActorType.USER, actor_id=order.user_id,
                                     reason="Order placed", created_at=placed_at))
        return order

    # 1. Still inside its cancellation window — cancelling now must be free.
    inside = await make(OrderStatus.CONFIRMED, now - timedelta(minutes=1), 15)
    _pay(session, inside, now)
    notes.append(f"order {inside.id}: inside cancellation window (free cancel)")

    # 2. Just past it — cancelling now must apply the fee.
    outside = await make(OrderStatus.PREPARING, now - timedelta(minutes=25), 5)
    _pay(session, outside, now)
    notes.append(f"order {outside.id}: past cancellation window (fee applies)")

    await session.flush()

    # 3. A refund that has blown its SLA.
    breached_order = await make(OrderStatus.CANCELLED, now - timedelta(days=4), 5)
    breached_order.cancelled_at = now - timedelta(days=4) + timedelta(minutes=30)
    breached_order.cancellation_reason = "Restaurant could not fulfil the order"
    payment = _pay(session, breached_order, now - timedelta(days=4))
    await session.flush()
    initiated = now - timedelta(days=4)
    session.add(Refund(
        payment_id=payment.id, order_id=breached_order.id, amount=breached_order.total_amount,
        reason=RefundReason.CANCELLED_BY_RESTAURANT, status=RefundStatus.PROCESSING,
        sla_due_at=initiated + timedelta(hours=24),   # due three days ago
        initiated_at=initiated, provider_ref="rfnd_breached01",
    ))
    notes.append(f"order {breached_order.id}: refund SLA breached by ~3 days")

    await session.flush()
    return notes


def _pay(session: AsyncSession, order: Order, when: datetime) -> Payment:
    payment = Payment(
        order_id=order.id, method=PaymentMethod.UPI, provider="mock",
        provider_ref=f"pay_edge{order.id:05d}", amount=order.total_amount,
        status=PaymentStatus.CAPTURED, authorized_at=when,
        captured_at=when + timedelta(seconds=15),
    )
    session.add(payment)
    return payment
