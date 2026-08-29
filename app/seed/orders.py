import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import Address
from app.models.catalog import MenuItem, Restaurant, RestaurantPolicy
from app.models.coupon import Coupon, CouponRedemption
from app.models.delivery import Delivery, DeliveryPartner
from app.models.enums import (
    ActorType,
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    RefundReason,
    RefundStatus,
)
from app.models.order import Order, OrderItem, OrderStatusEvent
from app.models.payment import Payment, Refund
from app.models.user import User
from app.services import coupons as coupon_service
from app.services import eta
from app.services import policy as policy_service
from app.services.money import money
from app.services.order_state import TERMINAL
from app.services.pricing import PricingError, quote

#: 150 orders across 5 restaurants and 100 customers.
#:
#: Scaled down from 800 with the catalog, but NOT proportionally. The floor is
#: set by STATUS_MIX below, not by the restaurant count: the mix allocates each
#: status a percentage, and the rarest of them is 3%, so the total has to stay
#: high enough that every status still gets several orders. At 150 the thinnest
#: bucket lands around four or five, which is enough for a board to have
#: something in it and for a filter to return more than one row. Much below 100
#: and statuses start coming out empty, which reads on screen as a broken filter.
ORDER_COUNT = 150

# Roughly what a real week looks like: most orders land, a few die, the rest are
# still moving. A uniform split would hide bugs that only bite one branch.
STATUS_MIX = (
    [OrderStatus.DELIVERED] * 70
    + [OrderStatus.CANCELLED] * 12
    + [OrderStatus.PENDING] * 3
    + [OrderStatus.CONFIRMED] * 4
    + [OrderStatus.PREPARING] * 5
    + [OrderStatus.READY_FOR_PICKUP] * 3
    + [OrderStatus.OUT_FOR_DELIVERY] * 3
)

CHAIN = [
    OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING,
    OrderStatus.READY_FOR_PICKUP, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED,
]
PAST_DELIVERY = {OrderStatus.READY_FOR_PICKUP, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED}


class Catalogue:
    """Everything the generator needs, loaded once instead of per order."""

    def __init__(self, restaurants, policies, items, users, addresses, coupons, partners):
        self.restaurants = restaurants
        self.policies = policies
        self.items = items
        self.users = users
        self.addresses = addresses
        self.coupons = coupons
        self.partners = partners
        # Precomputed so the generator never picks an address the restaurant
        # cannot reach, instead of retrying blindly until one fits.
        self.reachable: dict[int, list[Address]] = {}
        for restaurant in restaurants:
            limit = policies[restaurant.id].max_delivery_distance_km
            self.reachable[restaurant.id] = [
                a for a in addresses
                if eta.haversine_km(restaurant.latitude, restaurant.longitude,
                                    a.latitude, a.longitude) <= limit
            ]


async def load(session: AsyncSession) -> Catalogue:
    restaurants = list((await session.execute(select(Restaurant))).scalars())
    policies = {p.restaurant_id: p for p in (await session.execute(select(RestaurantPolicy))).scalars()}
    items: dict[int, list[MenuItem]] = {}
    for item in (await session.execute(select(MenuItem).where(MenuItem.is_available))).scalars():
        items.setdefault(item.restaurant_id, []).append(item)
    users = list((await session.execute(select(User))).scalars())
    addresses = list((await session.execute(select(Address))).scalars())
    coupons = list((await session.execute(select(Coupon))).scalars())
    partners = list((await session.execute(select(DeliveryPartner))).scalars())
    return Catalogue(restaurants, policies, items, users, addresses, coupons, partners)


def _pick_cart(menu: list[MenuItem], min_value: Decimal, rng: random.Random):
    """Keep adding lines until the cart clears the restaurant's minimum, so the
    generator never produces an order the pricing service would reject."""
    cart, subtotal = [], Decimal("0")
    for _ in range(rng.randint(1, 5)):
        item = rng.choice(menu)
        qty = rng.choices([1, 2, 3], weights=[70, 22, 8])[0]
        cart.append((item, qty))
        subtotal += item.price * qty
    guard = 0
    while subtotal < min_value and guard < 12:
        item = rng.choice(menu)
        cart.append((item, 1))
        subtotal += item.price
        guard += 1
    return cart


def _coupon_for(cat: Catalogue, restaurant_id: int, subtotal: Decimal,
                placed_at: datetime, rng: random.Random):
    if rng.random() > 0.25:
        return None, Decimal("0")
    coupon = rng.choice(cat.coupons)
    outcome = coupon_service.evaluate(
        coupon, subtotal=subtotal, restaurant_id=restaurant_id,
        cuisine_ids=set(), user_redemption_count=0, now=placed_at,
    )
    return (coupon, outcome.discount) if outcome.applicable else (None, Decimal("0"))


COMMIT_EVERY = 50


async def build(session: AsyncSession, rng: random.Random) -> int:
    cat = await load(session)
    now = datetime.now(UTC)
    created = 0

    for _ in range(ORDER_COUNT):
        restaurant = rng.choice(cat.restaurants)
        menu = cat.items.get(restaurant.id)
        reachable = cat.reachable.get(restaurant.id)
        if not menu or not reachable:
            continue

        status = rng.choice(STATUS_MIX)
        # Live orders must be recent or the board shows week-old "preparing".
        placed_at = (
            now - timedelta(minutes=rng.randint(3, 110))
            if status not in TERMINAL
            else now - timedelta(days=rng.randint(0, 89), minutes=rng.randint(0, 1439))
        )
        address = rng.choice(reachable)
        policy = cat.policies[restaurant.id]
        cart = _pick_cart(menu, policy.min_order_value, rng)
        subtotal = money(sum(i.price * q for i, q in cart))
        coupon, discount = _coupon_for(cat, restaurant.id, subtotal, placed_at, rng)

        try:
            q = quote(
                restaurant=restaurant, policy=policy, items=cart,
                latitude=address.latitude, longitude=address.longitude,
                discount=discount, placed_at=placed_at,
            )
        except PricingError:
            continue

        order = Order(
            user_id=address.user_id, restaurant_id=restaurant.id, address_id=address.id,
            coupon_id=coupon.id if coupon else None, status=status,
            subtotal=q.subtotal, packaging_fee=q.packaging_fee, delivery_fee=q.delivery_fee,
            tax_amount=q.tax_amount, discount_amount=q.discount_amount,
            total_amount=q.total_amount, distance_km=q.distance_km,
            placed_at=placed_at, cancellable_until=q.cancellable_until,
            promised_at=q.promised_at,
        )
        session.add(order)
        await session.flush()

        session.add_all(
            OrderItem(order_id=order.id, menu_item_id=line.menu_item_id,
                      item_name=line.item_name, unit_price=line.unit_price,
                      quantity=line.quantity, line_total=line.line_total,
                      notes="Less spicy please" if rng.random() < 0.08 else None)
            for line in q.lines
        )
        if coupon:
            session.add(CouponRedemption(coupon_id=coupon.id, user_id=order.user_id,
                                         order_id=order.id, discount_applied=q.discount_amount))

        _write_events(session, order, status, placed_at, rng)
        payment = _write_payment(session, order, placed_at, status, rng)
        await session.flush()

        if status in PAST_DELIVERY and cat.partners:
            _write_delivery(session, order, rng.choice(cat.partners), placed_at, status, rng)
        if status == OrderStatus.CANCELLED and payment is not None:
            _write_refund(session, order, payment, policy, rng)
        elif status == OrderStatus.DELIVERED and payment is not None and rng.random() < 0.03:
            _write_refund(session, order, payment, policy, rng,
                          reason=RefundReason.LATE_DELIVERY, partial=True)
        created += 1
        # Batched: each flush is a round trip to a remote database, and one
        # giant transaction means a late failure throws away everything.
        if created % COMMIT_EVERY == 0:
            await session.commit()
            print(f"    …{created} orders", flush=True)

    await session.commit()
    return created


def _write_events(session, order, status, placed_at, rng):
    if status == OrderStatus.CANCELLED:
        # Half the cancellations happen inside the free window, half outside,
        # so both branches of the policy have real rows behind them.
        inside = rng.random() < 0.5
        offset = rng.randint(1, 3) if inside else rng.randint(12, 40)
        cancelled_at = placed_at + timedelta(minutes=offset)
        reached = rng.choice([OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING])
        chain = CHAIN[: CHAIN.index(reached) + 1] + [OrderStatus.CANCELLED]
        order.cancelled_at = cancelled_at
        order.cancellation_reason = rng.choice(
            ["Changed my mind", "Ordered by mistake", "Taking too long", "Restaurant unavailable"]
        )
    else:
        chain = CHAIN[: CHAIN.index(status) + 1]
        if status == OrderStatus.DELIVERED:
            order.delivered_at = order.promised_at + timedelta(minutes=rng.randint(-8, 22))

    previous, stamp = None, placed_at
    for step in chain:
        session.add(OrderStatusEvent(
            order_id=order.id, from_status=previous, to_status=step,
            actor_type=ActorType.USER if step in (OrderStatus.PENDING, OrderStatus.CANCELLED)
            else ActorType.RESTAURANT if step in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY_FOR_PICKUP)
            else ActorType.SYSTEM,
            actor_id=order.user_id if step == OrderStatus.PENDING else None,
            reason=order.cancellation_reason if step == OrderStatus.CANCELLED else None,
            created_at=stamp,
        ))
        previous = step
        stamp += timedelta(minutes=rng.randint(2, 9))


def _write_payment(session, order, placed_at, status, rng):
    method = rng.choices(
        [PaymentMethod.UPI, PaymentMethod.CARD, PaymentMethod.COD, PaymentMethod.WALLET],
        weights=[55, 25, 15, 5],
    )[0]
    # A failed attempt is a row, not a discarded error. The retry is a second row.
    if rng.random() < 0.04:
        session.add(Payment(
            order_id=order.id, method=method, provider="mock",
            provider_ref=f"pay_{rng.randrange(16**10):010x}", amount=order.total_amount,
            status=PaymentStatus.FAILED, failed_reason="Issuer declined the transaction",
            created_at=placed_at,
        ))
    if method == PaymentMethod.COD and status != OrderStatus.DELIVERED:
        return None
    payment = Payment(
        order_id=order.id, method=method, provider="mock",
        provider_ref=f"pay_{rng.randrange(16**10):010x}", amount=order.total_amount,
        status=PaymentStatus.AUTHORIZED if status == OrderStatus.PENDING else PaymentStatus.CAPTURED,
        authorized_at=placed_at,
        captured_at=None if status == OrderStatus.PENDING else placed_at + timedelta(seconds=20),
        created_at=placed_at,
    )
    session.add(payment)
    return payment


def _write_delivery(session, order, partner, placed_at, status, rng):
    assigned = placed_at + timedelta(minutes=rng.randint(8, 25))
    mapping = {
        OrderStatus.READY_FOR_PICKUP: DeliveryStatus.ASSIGNED,
        OrderStatus.OUT_FOR_DELIVERY: DeliveryStatus.PICKED_UP,
        OrderStatus.DELIVERED: DeliveryStatus.DELIVERED,
    }
    session.add(Delivery(
        order_id=order.id, partner_id=partner.id, distance_km=order.distance_km,
        eta_minutes=eta.travel_minutes(order.distance_km), status=mapping[status],
        assigned_at=assigned,
        picked_up_at=assigned + timedelta(minutes=rng.randint(3, 12))
        if status in (OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED) else None,
        delivered_at=order.delivered_at if status == OrderStatus.DELIVERED else None,
    ))


def _write_refund(session, order, payment, policy, rng, *, reason=None, partial=False):
    initiated = (order.cancelled_at or order.delivered_at or order.placed_at) + timedelta(minutes=5)
    amount = money(order.total_amount * Decimal("0.3")) if partial else order.total_amount
    status = rng.choices(
        [RefundStatus.COMPLETED, RefundStatus.PROCESSING, RefundStatus.INITIATED],
        weights=[75, 15, 10],
    )[0]
    session.add(Refund(
        payment_id=payment.id, order_id=order.id, amount=amount,
        reason=reason or RefundReason.CANCELLED_BY_USER, status=status,
        sla_due_at=policy_service.refund_sla_due(initiated, policy.refund_sla_hours),
        initiated_at=initiated,
        completed_at=initiated + timedelta(hours=rng.randint(1, 20))
        if status == RefundStatus.COMPLETED else None,
        provider_ref=f"rfnd_{rng.randrange(16**10):010x}",
    ))
