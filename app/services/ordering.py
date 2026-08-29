"""Order placement and cancellation.

Sits between the router and the pure services. It owns the database writes that
must happen together -- an order and its items and its first status event, or a
cancellation and its event and its refund -- so no endpoint can perform half of
one and leave the other half missing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon, CouponRedemption
from app.models.enums import ActorType, OrderStatus, RefundReason, RefundStatus
from app.models.order import Order, OrderItem, OrderStatusEvent
from app.models.payment import Payment, Refund
from app.repositories import orders as repo
from app.services import coupons as coupon_service
from app.services import order_state
from app.services import policy as policy_service
from app.services.money import money
from app.services.pricing import PricingError, Quote
from app.services.pricing import quote as build_quote


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PricedOrder:
    quote: Quote
    coupon: Coupon | None
    coupon_message: str | None


async def price(
    session: AsyncSession,
    *,
    restaurant_id: int,
    address_id: int,
    items: list[tuple[int, int]],
    coupon_code: str | None,
    user_id: int,
    placed_at: datetime,
) -> PricedOrder:
    """Resolve ids to rows, evaluate the coupon, then delegate the arithmetic.

    Raises PricingError for anything the caller should surface as 422.

    user_id is required rather than optional: the address and the coupon are
    both that one customer's, so a price computed without knowing whose cart it
    is cannot check either of them. The routers derive it from the signed-in
    caller and never from the request.
    """
    restaurant, restaurant_policy = await repo.load_restaurant_and_policy(
        session, restaurant_id
    )
    if restaurant is None or restaurant_policy is None:
        raise PricingError(f"No restaurant with id {restaurant_id}")
    if not restaurant.is_active:
        raise PricingError(f"{restaurant.name} is not currently accepting orders")

    address = await repo.load_address(session, address_id)
    # Existence was never the whole question. An address belongs to exactly one
    # customer, and the delivery fee is measured to it and the courier is sent
    # to it, so pricing a cart against somebody else's address is how a
    # hijacked order arrives at an attacker's door. Refused in the same words
    # as a missing id on purpose: which address ids exist, and whose they are,
    # is not something a caller gets to map out by probing this endpoint.
    if address is None or address.user_id != user_id:
        raise PricingError(f"No address with id {address_id}")

    menu_items = await repo.load_menu_items(session, [i for i, _ in items])
    resolved = []
    for menu_item_id, quantity in items:
        menu_item = menu_items.get(menu_item_id)
        if menu_item is None:
            raise PricingError(f"No menu item with id {menu_item_id}")
        resolved.append((menu_item, quantity))

    # Priced once without a discount, so the coupon can be judged against the
    # real subtotal rather than a guess.
    provisional = build_quote(
        restaurant=restaurant,
        policy=restaurant_policy,
        items=resolved,
        latitude=address.latitude,
        longitude=address.longitude,
        placed_at=placed_at,
    )

    coupon, discount, message = None, Decimal("0"), None
    if coupon_code:
        coupon, discount, message = await _apply_coupon(
            session,
            code=coupon_code,
            restaurant_id=restaurant_id,
            user_id=user_id,
            subtotal=provisional.subtotal,
            now=placed_at,
        )

    final = build_quote(
        restaurant=restaurant,
        policy=restaurant_policy,
        items=resolved,
        latitude=address.latitude,
        longitude=address.longitude,
        discount=discount,
        placed_at=placed_at,
    )
    return PricedOrder(quote=final, coupon=coupon, coupon_message=message)


async def _apply_coupon(
    session: AsyncSession,
    *,
    code: str,
    restaurant_id: int,
    user_id: int | None,
    subtotal: Decimal,
    now: datetime,
) -> tuple[Coupon | None, Decimal, str | None]:
    coupon = await repo.load_coupon_by_code(session, code)
    if coupon is None:
        return None, Decimal("0"), f"Coupon {code.upper()!r} does not exist"

    redemptions = (
        await repo.user_redemption_count(session, coupon.id, user_id) if user_id else 0
    )
    outcome = coupon_service.evaluate(
        coupon,
        subtotal=subtotal,
        restaurant_id=restaurant_id,
        cuisine_ids=await repo.cuisine_ids_for(session, restaurant_id),
        user_redemption_count=redemptions,
        now=now,
    )
    if not outcome.applicable:
        return None, Decimal("0"), outcome.reason
    return coupon, outcome.discount, None


async def place(
    session: AsyncSession,
    *,
    user_id: int,
    restaurant_id: int,
    address_id: int,
    items: list[tuple[int, int, str | None]],
    coupon_code: str | None,
    idempotency_key: str | None,
    # Keyword-only with a default so every existing caller keeps working. It
    # takes no part in pricing — see the docstring on OrderCreate.
    delivery_note: str | None = None,
) -> Order:
    placed_at = utcnow()
    priced = await price(
        session,
        restaurant_id=restaurant_id,
        address_id=address_id,
        items=[(i, q) for i, q, _ in items],
        coupon_code=coupon_code,
        user_id=user_id,
        placed_at=placed_at,
    )
    # A coupon the caller asked for and did not get is a refusal, not a discount
    # of zero. price() reports the reason in coupon_message and /orders/quote
    # shows it; place() used to read only the quote, so an expired, exhausted,
    # wrong-restaurant or simply mistyped code produced a 201 at FULL PRICE --
    # the customer was charged more than the screen they tapped, and the response
    # carried no field that could have told them. The router maps PricingError to
    # 422, which is the answer /quote would already have given.
    if coupon_code and priced.coupon is None:
        raise PricingError(
            priced.coupon_message or f"Coupon {coupon_code!r} could not be applied"
        )

    q = priced.quote

    order = Order(
        idempotency_key=idempotency_key,
        user_id=user_id,
        restaurant_id=restaurant_id,
        address_id=address_id,
        coupon_id=priced.coupon.id if priced.coupon else None,
        status=OrderStatus.PENDING,
        subtotal=q.subtotal,
        packaging_fee=q.packaging_fee,
        delivery_fee=q.delivery_fee,
        tax_amount=q.tax_amount,
        discount_amount=q.discount_amount,
        total_amount=q.total_amount,
        distance_km=q.distance_km,
        placed_at=placed_at,
        cancellable_until=q.cancellable_until,
        promised_at=q.promised_at,
        delivery_note=delivery_note,
    )
    session.add(order)
    await session.flush()

    notes = {menu_item_id: note for menu_item_id, _, note in items}
    session.add_all(
        OrderItem(
            order_id=order.id,
            menu_item_id=line.menu_item_id,
            item_name=line.item_name,
            unit_price=line.unit_price,
            quantity=line.quantity,
            line_total=line.line_total,
            notes=notes.get(line.menu_item_id),
        )
        for line in q.lines
    )
    session.add(
        OrderStatusEvent(
            order_id=order.id,
            from_status=None,
            to_status=OrderStatus.PENDING,
            actor_type=ActorType.USER,
            actor_id=user_id,
            reason="Order placed",
        )
    )

    if priced.coupon is not None:
        # `times_used += 1` was a read-modify-write: the row was read without a
        # lock in coupons.evaluate, incremented in Python, and written back as a
        # literal. Twenty checkouts on a coupon at 99/100 all read 99, all passed
        # the cap check, and all wrote 100 -- 20 discounts against a cap of 1.
        #
        # This is the same claim as one atomic UPDATE whose WHERE re-checks the
        # cap. Zero rows affected means somebody else took the last one between
        # evaluate() and here, and the caller gets the refusal evaluate() would
        # have given.
        claimed = await session.execute(
            update(Coupon)
            .where(
                Coupon.id == priced.coupon.id,
                or_(
                    Coupon.usage_limit_total.is_(None),
                    Coupon.times_used < Coupon.usage_limit_total,
                ),
            )
            .values(times_used=Coupon.times_used + 1)
            .returning(Coupon.id)
        )
        if claimed.scalar_one_or_none() is None:
            raise PricingError("Coupon has reached its usage limit")

        session.add(
            CouponRedemption(
                coupon_id=priced.coupon.id,
                user_id=user_id,
                order_id=order.id,
                discount_applied=q.discount_amount,
            )
        )

    await session.flush()
    return order


async def transition(
    session: AsyncSession,
    order: Order,
    *,
    to_status: OrderStatus,
    actor_type: ActorType,
    actor_id: int | None = None,
    reason: str | None = None,
) -> Order:
    """The only way an order's status changes. Raises TransitionError."""
    order_state.assert_transition(order.status, to_status, actor_type)

    previous = order.status
    order.status = to_status
    if to_status == OrderStatus.DELIVERED:
        order.delivered_at = utcnow()

    session.add(
        OrderStatusEvent(
            order_id=order.id,
            from_status=previous,
            to_status=to_status,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
    )
    await session.flush()
    return order


# What a rejection is, in the absence of a status for it. There is no
# OrderStatus.REJECTED and none can be added from here: order_status is a real
# Postgres enum type, so a new value means ALTER TYPE -- a migration this
# codebase does not run -- and adding it to the Python enum alone would break
# every read of the column. So a rejection is a cancellation the restaurant
# makes from PENDING, and the triple (from_status=pending, to_status=cancelled,
# actor_type=restaurant) on the event trail identifies one exactly: the kitchen
# can only be undoing an order it never accepted. That is a modelling
# compromise and not a synonym -- see reject() for the half the compromise
# costs, which is that "rejected" and "cancelled by the kitchen at pending" are
# the same row and can never be told apart after the fact.
REJECTION_REASON = "Rejected by the restaurant"

# The percentage handed to the cancellation policy when the customer is not the
# one cancelling. Named, because "0" passed into a fee calculation looks like a
# missing value rather than a decision.
NO_CANCELLATION_FEE = Decimal(0)


@dataclass(frozen=True)
class CancellationResult:
    order: Order
    within_window: bool
    fee: Decimal
    refund: Refund | None


class CancellationRefused(ValueError):
    """Surface as 409."""


async def cancel(
    session: AsyncSession,
    order: Order,
    *,
    actor_type: ActorType,
    actor_id: int | None,
    reason: str | None,
) -> CancellationResult:
    # Take the row before reading anything off it. Two tablets refusing the same
    # ticket would otherwise both read PENDING, both pass assert_transition and
    # both book a refund -- paying the customer twice. FOR UPDATE makes the
    # second one wait for the first to commit, after which the status check
    # below sees CANCELLED and refuses. One extra locking read on a path that
    # already does several, and only on the refusal path.
    await session.refresh(order, with_for_update=True)

    now = utcnow()
    _, restaurant_policy = await repo.load_restaurant_and_policy(
        session, order.restaurant_id
    )
    captured = await repo.captured_total(session, order.id)
    # What has already gone back. captured_total does not shrink when a refund is
    # booked, so without this the whole captured amount is refundable twice --
    # once by POST /orders/{id}/refunds and again here. The order row is locked
    # above, and POST /orders/{id}/refunds now takes the same lock, so the two
    # money-out paths serialise on one row and this read cannot go stale.
    refunded = await repo.refunded_total(session, order.id)

    # A cancellation fee is what a customer pays for changing their mind after
    # the free window closed. Nobody is changing their mind when the kitchen or
    # dispatch backs out, so the restaurant's percentage is not theirs to
    # charge: a ticket rejected twenty minutes after checkout would otherwise
    # bill the customer for the restaurant's own refusal. evaluate_cancellation
    # takes no actor and so cannot know this, and it is not ours to change, so
    # the actor is applied here -- to the percentage handed in, which is the one
    # input that means "the customer's penalty". The refund reason below already
    # books a non-user cancellation as the restaurant's; now the money agrees
    # with the label.
    fee_percent = (
        restaurant_policy.cancellation_fee_percent
        if actor_type == ActorType.USER
        else NO_CANCELLATION_FEE
    )
    outcome = policy_service.evaluate_cancellation(
        status=order.status,
        cancellable_until=order.cancellable_until,
        total_amount=order.total_amount,
        captured_amount=captured,
        refunded_amount=refunded,
        cancellation_fee_percent=fee_percent,
        now=now,
    )
    if not outcome.allowed:
        raise CancellationRefused(outcome.reason or "Order cannot be cancelled")

    order.cancelled_at = now
    order.cancellation_reason = reason
    await transition(
        session,
        order,
        to_status=OrderStatus.CANCELLED,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason or "Cancelled",
    )

    refund = None
    if outcome.refund_amount > 0:
        # Refund against the most recent payment. Core-table select would
        # return a raw Row here, so this stays an ORM select.
        payment = await session.scalar(
            select(Payment)
            .where(Payment.order_id == order.id)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        if payment is not None:
            refund = Refund(
                payment_id=payment.id,
                order_id=order.id,
                amount=money(outcome.refund_amount),
                reason=(
                    RefundReason.CANCELLED_BY_USER
                    if actor_type == ActorType.USER
                    else RefundReason.CANCELLED_BY_RESTAURANT
                ),
                status=RefundStatus.INITIATED,
                initiated_at=now,
                sla_due_at=policy_service.refund_sla_due(
                    now, restaurant_policy.refund_sla_hours
                ),
            )
            session.add(refund)
            await session.flush()

    return CancellationResult(
        order=order, within_window=outcome.within_window, fee=outcome.fee, refund=refund
    )


async def reject(
    session: AsyncSession,
    order: Order,
    *,
    actor_id: int | None,
    reason: str | None,
) -> CancellationResult:
    """The kitchen refuses an order it never accepted.

    Written as its own function rather than left to the caller to spell as a
    cancel, because the two are different acts with the same encoding (see
    REJECTION_REASON above) and only one of them is something a queue should ask
    a cook to do: "cancel the order" is what you say about food somebody is
    already waiting for.

    PENDING only. Once the kitchen has said yes the customer has been promised
    dinner, and backing out then is a cancellation -- a different apology, and
    one the customer may be owed more than a refund for. Refused as a 409 rather
    than quietly downgraded to a cancel, so a client cannot reject its way
    through the whole lifecycle.

    Free by construction: cancel() charges the fee percentage only to a USER
    actor, so a rejection returns the full captured amount however late in the
    pending window it lands, and books it as
    RefundReason.CANCELLED_BY_RESTAURANT.
    """
    if order.status != OrderStatus.PENDING:
        raise CancellationRefused(
            f"Only a pending order can be rejected; this one is "
            f"{order.status.value}. Cancel it instead."
        )
    # A rejection is free because the kitchen, not the customer, is the one
    # backing out. When they are the same person that reasoning collapses:
    # staff of this kitchen who also placed the order could reject their way
    # out of the fee that POST /cancel would charge them. Refused rather than
    # silently rebilled as a USER cancel, because "your rejection is actually a
    # cancellation and costs you money" is not something to do behind
    # somebody's back.
    if actor_id is not None and order.user_id == actor_id:
        raise CancellationRefused(
            "This order was placed by the account signed in here, so refusing "
            "it is a customer cancellation. Cancel it instead -- the "
            "restaurant's fee applies as it would to anyone else."
        )
    return await cancel(
        session,
        order,
        actor_type=ActorType.RESTAURANT,
        actor_id=actor_id,
        reason=reason or REJECTION_REASON,
    )
