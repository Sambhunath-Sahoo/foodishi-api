import logging
import math
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    UNAUTHENTICATED,
    CurrentUser,
    forbidden,
)
from app.dependencies.ownership import ReadableOrder, readable_order
from app.dependencies.scope import (
    OrderListRestaurants,
    StaffOfOrder,
    staff_of_order_with,
)
from app.models.enums import ActorType, OrderStatus
from app.models.order import Order, OrderItem, OrderStatusEvent
from app.models.staff import RestaurantStaff
from app.repositories import orders as repo
from app.schemas.order import (
    CancelResult,
    OrderCancel,
    OrderCreate,
    OrderDetail,
    OrderEventRead,
    OrderItemIn,
    OrderRead,
    OrderStatusRead,
    OrderStatusUpdate,
    QuoteLineRead,
    QuoteRead,
    QuoteRequest,
)
from app.services import ordering, permissions
from app.services.order_state import TransitionError
from app.services.policy import CANCELLABLE_STATUSES
from app.services.pricing import PricingError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


async def order_owner_id(
    user: CurrentUser,
    user_id: int | None = Query(
        default=None,
        description="Your own users.id. Optional and redundant — a cart is "
        "always priced for the signed-in caller — but still accepted so "
        "existing clients keep working.",
    ),
) -> int:
    """Whose cart this is. The caller's own, on every route that uses it.

    `user_id` used to be the only answer to that question, and nothing bound it
    to the caller: anyone who could reach the port could bill dinner to any
    customer by typing a different integer. The /me/orders docstring in
    app/routers/me.py names this exact shape as the thing /me exists to avoid;
    it was left live on the write side.

    The parameter is still accepted, because the customer app sends its own id
    today and a checkout that rejects a caller passing their OWN id is a
    regression, not a fix. A mismatch is a 403 rather than a silent
    substitution: a client naming somebody else's id is either a bug or an
    attack, and quietly pricing the caller's cart instead would hide both.
    """
    if user_id is not None and user_id != user.id:
        logger.info(
            "Order owner mismatch: caller=%s named user_id=%s", user.id, user_id
        )
        raise forbidden(
            "A cart is always priced for the signed-in caller "
            f"(users.id={user.id}); pass that id or leave user_id out"
        )
    return user.id


OrderOwnerId = Annotated[int, Depends(order_owner_id)]


@router.post(
    "/quote",
    response_model=QuoteRead,
    responses={**UNAUTHENTICATED, **FORBIDDEN},
)
async def quote_order(payload: QuoteRequest, session: SessionDep, owner_id: OrderOwnerId):
    """Price a cart without creating anything.

    POST /orders calls the same service, so the quoted price and the charged
    price cannot drift apart. That promise is the reason the owner is resolved
    here exactly as it is there, instead of staying an optional query
    parameter: a quote changes nothing, but it is not impersonal. The coupon's
    per-user redemption count and the address the delivery fee is measured to
    both belong to one customer, so priced for anyone but the caller this
    endpoint answers questions about a stranger — which coupons they have left,
    which address ids are theirs — and can quote a price the checkout will then
    refuse.
    """
    try:
        priced = await ordering.price(
            session,
            restaurant_id=payload.restaurant_id,
            address_id=payload.address_id,
            items=_cart_lines(payload.items),
            coupon_code=payload.coupon_code,
            user_id=owner_id,
            placed_at=ordering.utcnow(),
        )
    except PricingError as exc:
        raise unprocessable(str(exc)) from exc

    q = priced.quote
    return QuoteRead(
        lines=[
            QuoteLineRead.model_validate(line, from_attributes=True) for line in q.lines
        ],
        subtotal=q.subtotal,
        packaging_fee=q.packaging_fee,
        delivery_fee=q.delivery_fee,
        tax_amount=q.tax_amount,
        discount_amount=q.discount_amount,
        total_amount=q.total_amount,
        distance_km=q.distance_km,
        promised_at=q.promised_at,
        cancellable_until=q.cancellable_until,
        coupon_code=priced.coupon.code if priced.coupon else None,
        coupon_message=priced.coupon_message,
    )


@router.post(
    "",
    response_model=OrderDetail,
    status_code=201,
    responses={
        **NOT_FOUND,
        **CONFLICT,
        **UNAUTHENTICATED,
        **FORBIDDEN,
        422: {"description": "Order violates a restaurant rule"},
    },
)
async def place_order(
    payload: OrderCreate,
    session: SessionDep,
    owner_id: OrderOwnerId,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Place an order, always billed to the caller. See order_owner_id above.

    Placing on a customer's behalf — the phone order an operator takes — is
    deliberately not here. It would need to record which operator placed it and
    for whom, and orders has no column for that, so an ops-placed order would
    be indistinguishable from one the customer placed themselves: the exact
    accountability gap this route was closing. Platform staff get no branch
    until there is somewhere to write that down.
    """
    if idempotency_key:
        existing = await repo.find_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            # A replay is only a replay for the customer who placed it. The key
            # is client-chosen and unique across the whole table, so returning
            # the order to whoever presents it hands a stranger that order's
            # address, items and totals for the price of guessing a string.
            # Someone else's key is a collision, not a retry.
            if existing.user_id != owner_id:
                logger.info(
                    "Idempotency-Key replay refused: caller=%s order=%s",
                    owner_id,
                    existing.id,
                )
                raise conflict(
                    "This Idempotency-Key has already been used by another order"
                )
            return await _detail(session, existing.id)

    try:
        # SAVEPOINT around the whole placement.
        #
        # orders.idempotency_key is unique, so a replay that raced past the
        # lookup above loses on INSERT. The model comment promises the database
        # refuses the duplicate, and it does -- but an uncaught IntegrityError is
        # a 500, and 500 is the one answer from which a retrying client cannot
        # tell whether its order was created. The winner is one SELECT away.
        #
        # That SELECT needs a usable session, and `await session.rollback()`
        # would not give one: get_session() holds the request inside
        # `async with Session.begin()`, and rolling back inside a context-managed
        # transaction CLOSES it, so the next statement raises InvalidRequestError
        # -- another 500. Verified empirically, not assumed. begin_nested()
        # issues a SAVEPOINT, so ordering.place()'s several flushes unwind to it
        # and the outer transaction survives.
        async with session.begin_nested():
            order = await ordering.place(
                session,
                user_id=owner_id,
                restaurant_id=payload.restaurant_id,
                address_id=payload.address_id,
                items=_cart_lines(payload.items),
                coupon_code=payload.coupon_code,
                idempotency_key=idempotency_key,
                delivery_note=payload.delivery_note,
            )
    except PricingError as exc:
        # An unapplicable coupon, a closed restaurant, a subtotal under the
        # minimum. The savepoint has unwound the partial order; the outer
        # transaction rolls back when this HTTPException leaves get_session().
        raise unprocessable(str(exc)) from exc
    except IntegrityError as exc:
        if not idempotency_key:
            raise
        winner = await repo.find_by_idempotency_key(session, idempotency_key)
        if winner is None:
            raise
        if winner.user_id != owner_id:
            raise conflict(
                "This Idempotency-Key has already been used by another order"
            ) from exc
        return await _detail(session, winner.id)

    return await _detail(session, order.id)


@router.get("", response_model=Page[OrderRead], responses={**UNAUTHENTICATED, **FORBIDDEN})
async def list_orders(
    session: SessionDep,
    page: PageDep,
    restaurants: OrderListRestaurants,
    user_id: int | None = None,
    status: OrderStatus | None = None,
    live: bool = Query(default=False, description="Only orders not delivered or cancelled"),
    placed_from: datetime | None = None,
    placed_to: datetime | None = None,
):
    """The partner order queue, scoped to the caller's own restaurants.

    `restaurant_id` is still a query parameter — it is declared by the scope
    dependency, which checks it against restaurant_staff before it reaches the
    filter. Customers read their own orders at /me/orders instead.
    """
    statement = repo.order_query(
        user_id=user_id,
        status=status,
        live=live,
        placed_from=placed_from,
        placed_to=placed_to,
    )
    # The one place the restaurant filter is applied, and it is never optional:
    # the dependency always returns a non-empty set, so there is no request
    # shape that lists another restaurant's orders. sorted() only keeps the
    # generated SQL stable between requests.
    statement = statement.where(Order.restaurant_id.in_(sorted(restaurants)))
    items, total = await paginate(session, statement, page)
    return Page[OrderRead](items=items, total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/{order_id}",
    response_model=OrderDetail,
    dependencies=[Depends(readable_order)],
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def get_order(order_id: int, session: SessionDep):
    return await _detail(session, order_id)


@router.get(
    "/{order_id}/status",
    response_model=OrderStatusRead,
    dependencies=[Depends(readable_order)],
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def get_order_status(order_id: int, session: SessionDep):
    order = await session.get(Order, order_id)
    if order is None:
        raise not_found("order", order_id)

    now = ordering.utcnow()
    remaining = math.ceil((order.promised_at - now).total_seconds() / 60)
    return OrderStatusRead(
        id=order.id,
        status=order.status,
        promised_at=order.promised_at,
        # Recomputed every call, never a stored countdown that can go stale.
        minutes_remaining=max(remaining, 0),
        is_late=now > order.promised_at and order.status != OrderStatus.DELIVERED,
        cancellable_until=order.cancellable_until,
        is_cancellable=order.status in CANCELLABLE_STATUSES,
    )


@router.get(
    "/{order_id}/events",
    response_model=list[OrderEventRead],
    dependencies=[Depends(readable_order)],
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def get_order_events(order_id: int, session: SessionDep):
    if await session.get(Order, order_id) is None:
        raise not_found("order", order_id)
    rows = await session.execute(
        select(OrderStatusEvent)
        .where(OrderStatusEvent.order_id == order_id)
        .order_by(OrderStatusEvent.created_at, OrderStatusEvent.id)
    )
    return list(rows.scalars())


def _note_ignored_actor(
    *,
    route: str,
    order_id: int,
    claimed: ActorType | None,
    derived: ActorType,
    caller_id: int,
) -> None:
    """Write down a body that disagreed with the token. Never refuse it.

    actor_type and actor_id stay on both request schemas because both shipped
    clients send them and send them truthfully — the partner app pins
    "restaurant", the customer app pins "user" — and a client telling the truth
    must not start getting a 422 for the privilege. They are simply no longer
    believed, so the only thing left to do with a disagreement is log it: it is
    either a client bug or somebody trying an actor_type on for size, and both
    are worth being able to grep for. Retiring the two fields is a schema
    change, and app/schemas/order.py is not this change's to make.
    """
    # None is an omitted field rather than a claim — both schemas default
    # actor_type, so a caller who left it out never said anything to disagree
    # with and must not fill the log with one line per queue tap.
    if claimed is None or claimed == derived:
        return
    logger.info(
        "Ignored actor_type on /orders/%s/%s: caller=%s claimed=%s, recorded as %s",
        order_id,
        route,
        caller_id,
        claimed.value,
        derived.value,
    )


def _claimed_actor(payload: OrderStatusUpdate | OrderCancel) -> ActorType | None:
    """The actor_type the caller actually typed, or None if they left it out."""
    if "actor_type" not in payload.model_fields_set:
        return None
    return payload.actor_type


def _cancel_result(result: ordering.CancellationResult) -> CancelResult:
    """One shape for cancelling and rejecting — they return the same facts."""
    return CancelResult(
        order=OrderRead.model_validate(result.order),
        within_window=result.within_window,
        cancellation_fee=result.fee,
        refund_amount=result.refund.amount if result.refund else 0,
        refund_id=result.refund.id if result.refund else None,
        refund_due_at=result.refund.sla_due_at if result.refund else None,
    )


@router.patch(
    "/{order_id}/status",
    response_model=OrderRead,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
)
async def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    session: SessionDep,
    staff: StaffOfOrder,
):
    """Move an order along its lifecycle. Staff of that order's restaurant only.

    The actor written to the audit trail is the staff row the guard just
    resolved, not the body's copy of it. Believing the body made actor_type a
    one-word bypass of the state machine it feeds: `system` is the actor
    reserved for dispatch, so a kitchen could claim it and make the two moves
    the table keeps for the courier — ready_for_pickup -> out_for_delivery and
    out_for_delivery -> delivered — which is a restaurant marking its own food
    delivered. The membership check never stopped that, because it asks whether
    you staff this restaurant and not whether you are who you say you are.
    Dispatch's honest lane is PATCH /deliveries/{id}, which derives SYSTEM
    server-side the same way this derives RESTAURANT.

    A customer's only legal move is cancelling, which is POST /{id}/cancel.
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise not_found("order", order_id)

    # The state machine permits restaurant -> cancelled from four statuses, so
    # without this the route is a way around the entire cancellation path:
    # ordering.transition flips the column and appends the event, while the
    # fee, the Refund row, cancelled_at and cancellation_reason all live in
    # ordering.cancel and are reachable only through the two routes below. A
    # paid order cancelled through here is a customer who silently never gets
    # their money back.
    if payload.status == OrderStatus.CANCELLED:
        raise conflict(
            "Cancelling is not a status update, because the fee and the refund "
            f"are worked out elsewhere. Use POST /orders/{order_id}/cancel — or "
            f"POST /orders/{order_id}/reject to refuse a pending order."
        )

    _note_ignored_actor(
        route="status",
        order_id=order_id,
        claimed=_claimed_actor(payload),
        derived=ActorType.RESTAURANT,
        caller_id=staff.user_id,
    )
    try:
        await ordering.transition(
            session, order,
            to_status=payload.status,
            actor_type=ActorType.RESTAURANT,
            # The person who pressed the button, not the restaurant: "restaurant
            # 7 did it" names nobody when someone asks who confirmed this.
            actor_id=staff.user_id,
            reason=payload.reason,
        )
    except TransitionError as exc:
        raise conflict(str(exc)) from exc
    return order


@router.post(
    "/{order_id}/reject",
    response_model=CancelResult,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
)
async def reject_order(
    order_id: int,
    payload: OrderCancel,
    session: SessionDep,
    staff: Annotated[
        RestaurantStaff, Depends(staff_of_order_with("orders.reject"))
    ],
):
    """Refuse a pending order — the kitchen's "no", called by its own name.

    The queue's accept button had no opposite. Turning a ticket down meant
    POST /cancel, which is the wording for food a customer is already waiting
    for, and it asked a cook to cancel an order the kitchen had never agreed
    to cook. This route is that missing half, so accept and reject can sit side
    by side and read as one decision.

    Underneath it is still a cancellation, and that is a compromise rather than
    a design: there is no OrderStatus.REJECTED, order_status is a real Postgres
    enum type, and adding a value to one takes a migration this codebase does
    not run. So the rejection is recorded as pending -> cancelled by a
    restaurant actor with RefundReason.CANCELLED_BY_RESTAURANT — see
    app/services/ordering.py REJECTION_REASON for what that costs us, namely
    that a rejection and a pending-stage restaurant cancel are afterwards the
    same row. The customer is refunded in full: ordering.cancel charges the
    restaurant's fee percentage only to a USER actor, so a kitchen's refusal is
    free however long the tablet was left unattended.

    StaffRole.STAFF is enough, deliberately. Refusing a ticket is queue work
    and the same shift worker may already accept, start and finish one; making
    it a manager's job would mean a kitchen at capacity with no manager on the
    floor has no way to say no and just lets the clock run out.
    """
    order = await session.get(Order, order_id)
    if order is None:  # staff_of_order 404s first; belt and braces for a race.
        raise not_found("order", order_id)

    _note_ignored_actor(
        route="reject",
        order_id=order_id,
        claimed=_claimed_actor(payload),
        derived=ActorType.RESTAURANT,
        caller_id=staff.user_id,
    )
    try:
        result = await ordering.reject(
            session, order, actor_id=staff.user_id, reason=payload.reason
        )
    except (ordering.CancellationRefused, TransitionError) as exc:
        raise conflict(str(exc)) from exc
    return _cancel_result(result)


@router.post(
    "/{order_id}/cancel",
    response_model=CancelResult,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
)
async def cancel_order(
    order_id: int,
    payload: OrderCancel,
    session: SessionDep,
    user: CurrentUser,
    order: ReadableOrder,
):
    """Cancel an order. The customer who placed it, or its kitchen.

    Who that was is derived here, not read from the body, and it needs no
    extra query: readable_order admits exactly the customer who owns the order
    or active staff of the restaurant cooking it, and refuses platform staff on
    an unsafe method, so "is this the order's user_id" is the whole question.
    Trusting the body let a customer file their own change of mind as the
    kitchen's fault — actor_type="restaurant" is booked as
    RefundReason.CANCELLED_BY_RESTAURANT and, now that the fee follows the
    actor, would waive their cancellation fee too.

    A caller who is both the customer and staff of that restaurant is recorded
    as the customer, because it is their order. Nothing is lost by that: USER
    may cancel from every status policy.CANCELLABLE_STATUSES allows, so the
    derivation never blocks a move the actor map would have permitted.
    """
    actor = ActorType.USER if order.user_id == user.id else ActorType.RESTAURANT

    # The permission gate applies to the KITCHEN's cancel, never the customer's:
    # a customer cancelling their own order holds no restaurant_staff row and
    # needs none. readable_order admits any active staff of the restaurant, so
    # without this a shift worker whose admin granted them nothing could cancel
    # orders the console had hidden the button for. orders.cancel is one of the
    # two permissions app/services/permissions.py deliberately withholds from
    # STAFF_FLOOR; this is the check that makes withholding it mean something.
    if actor is ActorType.RESTAURANT:
        staff_row = await session.scalar(
            select(RestaurantStaff).where(
                RestaurantStaff.user_id == user.id,
                RestaurantStaff.restaurant_id == order.restaurant_id,
                RestaurantStaff.is_active,
            )
        )
        if staff_row is None or "orders.cancel" not in permissions.resolve(
            staff_row.role, staff_row.permissions
        ):
            raise forbidden(
                "Your access at this restaurant does not include orders.cancel"
            )

    _note_ignored_actor(
        route="cancel",
        order_id=order_id,
        claimed=_claimed_actor(payload),
        derived=actor,
        caller_id=user.id,
    )
    try:
        result = await ordering.cancel(
            session, order,
            actor_type=actor,
            actor_id=user.id,
            reason=payload.reason,
        )
    except (ordering.CancellationRefused, TransitionError) as exc:
        raise conflict(str(exc)) from exc

    return _cancel_result(result)


def _cart_lines(items: list[OrderItemIn]) -> list[ordering.CartLine]:
    """The request's lines in the service's shape — quote and placement must
    read a cart identically, so both build it here."""
    return [
        ordering.CartLine(
            menu_item_id=i.menu_item_id,
            quantity=i.quantity,
            notes=i.notes,
            option_ids=tuple(i.option_ids),
        )
        for i in items
    ]


async def _detail(session: SessionDep, order_id: int) -> Order:
    # Chained, so the lines' chosen options arrive in ONE more query for the
    # whole order (WHERE order_item_id IN (...)), not one per line.
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items).selectinload(OrderItem.modifiers))
    )
    if order is None:
        raise not_found("order", order_id)
    return order
