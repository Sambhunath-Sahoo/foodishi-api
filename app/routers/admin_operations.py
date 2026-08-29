"""The platform's orders and rides, across every kitchen.

Why these are not query parameters bolted onto the routes that already exist:

  * `GET /orders` is the PARTNER queue. Its restaurant scope comes from a
    dependency that resolves the caller against restaurant_staff, and the
    partner app's history board depends on that scope. An operator asks a
    different question — "find this order anywhere on the platform" — and
    widening the partner route to answer it would mean one route with two
    scopes and a dependency that sometimes applies.

  * `PATCH /deliveries/{id}` advances a ride through its own lifecycle and is
    guarded by staff of that ride's restaurant. Reassigning a rider is not a
    lifecycle transition and platform staff are not restaurant staff, so it gets
    its own route under this guard rather than a wider one on that.

Everything here is read-only except the two rider actions, and both of those
record what they did on the order's own status trail — a ride that changed hands
and left no trace is exactly what makes the next support call unanswerable.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, or_, select

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    current_user,
    require_platform_role,
)
from app.models.catalog import Restaurant
from app.models.delivery import Delivery, DeliveryPartner
from app.models.enums import ActorType, DeliveryStatus, OrderStatus
from app.models.order import Order, OrderStatusEvent
from app.models.user import User
from app.schemas.admin import (
    AdminDeliveryRow,
    AdminOrderSort,
    DeliveryFail,
    DeliveryFilter,
    DeliveryReassign,
)
from app.schemas.delivery import DeliveryPartnerRead
from app.schemas.order import OrderRead
from app.services.admin_insights import ACTIVE_DELIVERY_STATUSES, window_start

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_role())],
)

ADMIN_RESPONSES = {**UNAUTHENTICATED, **NOT_PLATFORM}

#: Statuses each filter admits. None admits every one of them.
DELIVERY_FILTERS: dict[DeliveryFilter, tuple[DeliveryStatus, ...] | None] = {
    DeliveryFilter.ACTIVE: ACTIVE_DELIVERY_STATUSES,
    DeliveryFilter.ASSIGNED: (DeliveryStatus.ASSIGNED,),
    DeliveryFilter.PICKED_UP: (DeliveryStatus.PICKED_UP,),
    DeliveryFilter.DELIVERED: (DeliveryStatus.DELIVERED,),
    DeliveryFilter.FAILED: (DeliveryStatus.FAILED,),
    DeliveryFilter.ANY: None,
}


def _escape_like(term: str) -> str:
    """A user's search term as a safe LIKE pattern.

    `%` and `_` in a search box are literal characters to whoever typed them, so
    they are escaped rather than left to match everything. Same treatment as the
    customer search in routers/users.py.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _order_search(statement: Select, term: str) -> Select:
    """Match an order id, the customer who placed it, or the kitchen it came from.

    Three joins for one search box, because those are the three things somebody
    holds when they pick up the phone. An order-id-only search would mean every
    support call started with "can you read me the number".

    The id branch is guarded on `isdigit` rather than cast: casting the column to
    text to match a pattern would discard the primary key index on a table that
    grows forever.
    """
    pattern = _escape_like(term)
    predicates = [
        User.name.ilike(pattern, escape="\\"),
        User.email.ilike(pattern, escape="\\"),
        Restaurant.name.ilike(pattern, escape="\\"),
    ]
    if term.strip().lstrip("#").isdigit():
        predicates.append(Order.id == int(term.strip().lstrip("#")))

    return statement.join(User, User.id == Order.user_id).join(
        Restaurant, Restaurant.id == Order.restaurant_id
    ).where(or_(*predicates))


ORDER_SORTS = {
    AdminOrderSort.NEWEST: (Order.placed_at.desc(), Order.id.desc()),
    AdminOrderSort.OLDEST: (Order.placed_at.asc(), Order.id.asc()),
    AdminOrderSort.LARGEST: (Order.total_amount.desc(), Order.id.desc()),
    # Oldest promise first, which on a live list is the most overdue first.
    AdminOrderSort.OLDEST_PROMISE: (Order.promised_at.asc(), Order.id.asc()),
}


@router.get("/orders", response_model=Page[OrderRead], responses=ADMIN_RESPONSES)
async def list_orders(
    session: SessionDep,
    page: PageDep,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status: OrderStatus | None = None,
    restaurant_id: int | None = None,
    live: Annotated[
        bool, Query(description="Only orders not delivered or cancelled")
    ] = False,
    within_days: Annotated[
        int, Query(ge=0, le=365, description="Placed in the last N local days. 0 = all")
    ] = 0,
    sort: AdminOrderSort = AdminOrderSort.NEWEST,
):
    """Every order on the platform, searched and sorted.

    `live` overrides `status` when both are sent, because "in flight" is a
    question about the whole live set and a single status is a narrower one; a
    request that means both is a request that means nothing, and answering the
    broader reading is the one that cannot silently return an empty page.
    """
    statement = select(Order)

    if live:
        statement = statement.where(
            Order.status.not_in((OrderStatus.DELIVERED, OrderStatus.CANCELLED))
        )
    elif status is not None:
        statement = statement.where(Order.status == status)

    if restaurant_id is not None:
        statement = statement.where(Order.restaurant_id == restaurant_id)

    if within_days > 0:
        statement = statement.where(
            Order.placed_at >= window_start(datetime.now(UTC), within_days)
        )

    if q is not None:
        statement = _order_search(statement, q)

    statement = statement.order_by(*ORDER_SORTS[sort])
    items, total = await paginate(session, statement, page)
    return Page[OrderRead](
        items=items, total=total, limit=page.limit, offset=page.offset
    )


def _delivery_row(delivery: Delivery, partner: DeliveryPartner, order: Order):
    """One board row from the three rows it was joined out of.

    Composed by hand rather than through `from_attributes`, because `Delivery`
    declares no ORM relationship to either its partner or its order — the
    existing GET /orders/{id}/delivery joins them the same way. Adding
    relationships would be the tidier fix and belongs in models/delivery.py,
    which is not this change.
    """
    return AdminDeliveryRow(
        id=delivery.id,
        order_id=delivery.order_id,
        partner_id=delivery.partner_id,
        distance_km=delivery.distance_km,
        eta_minutes=delivery.eta_minutes,
        status=delivery.status,
        assigned_at=delivery.assigned_at,
        picked_up_at=delivery.picked_up_at,
        delivered_at=delivery.delivered_at,
        partner=DeliveryPartnerRead.model_validate(partner),
        order=OrderRead.model_validate(order),
    )


def _deliveries_statement(q: str | None, status: DeliveryFilter) -> Select:
    """Rides with their rider and their order, filtered and ordered.

    Ordered rides-still-out first, then most recently assigned: a finished ride
    is history, and history does not belong above the thing somebody has to act
    on.
    """
    statement = (
        select(Delivery, DeliveryPartner, Order)
        .join(DeliveryPartner, DeliveryPartner.id == Delivery.partner_id)
        .join(Order, Order.id == Delivery.order_id)
    )

    allowed = DELIVERY_FILTERS[status]
    if allowed is not None:
        statement = statement.where(Delivery.status.in_(allowed))

    if q is not None:
        pattern = _escape_like(q)
        predicates = [
            DeliveryPartner.name.ilike(pattern, escape="\\"),
            DeliveryPartner.phone.ilike(pattern, escape="\\"),
        ]
        bare = q.strip().lstrip("#")
        if bare.isdigit():
            predicates.append(Delivery.order_id == int(bare))
        statement = statement.where(or_(*predicates))

    return statement.order_by(
        Delivery.status.in_(ACTIVE_DELIVERY_STATUSES).desc(),
        Delivery.assigned_at.desc(),
        Delivery.id.desc(),
    )


@router.get(
    "/deliveries", response_model=Page[AdminDeliveryRow], responses=ADMIN_RESPONSES
)
async def list_deliveries(
    session: SessionDep,
    page: PageDep,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status: DeliveryFilter = DeliveryFilter.ACTIVE,
):
    """Every ride, with the order it carries and the rider carrying it.

    The join is here and not in the client. `GET /orders/{id}/delivery` answers
    one ride, and a board needs a hundred — one request per row would spend the
    screen's whole budget on its own chrome.
    """
    statement = _deliveries_statement(q, status)

    # core.pagination.paginate cannot be reused: it returns .scalars() and these
    # rows are three-column joins. Same reason routers/metrics.py has its own
    # pair. The count reuses the statement's own filters via the subquery, so a
    # predicate can never be applied to the page and forgotten in the total.
    total = await session.scalar(
        select(func.count()).select_from(statement.subquery())
    )
    rows = await session.execute(
        statement.limit(page.limit).offset(page.offset)
    )

    return Page[AdminDeliveryRow](
        items=[_delivery_row(*row) for row in rows.all()],
        total=int(total or 0),
        limit=page.limit,
        offset=page.offset,
    )


async def _record(
    session: SessionDep, order: Order, actor: User, reason: str
) -> None:
    """Note on the order's own trail what an operator did to its ride.

    from_status and to_status are the same on purpose: the ORDER did not move,
    only the ride under it. The trail is the only place a support agent can later
    find out that a rider was swapped, so a reassignment that wrote nothing here
    would be invisible five minutes after it happened.
    """
    session.add(
        OrderStatusEvent(
            order_id=order.id,
            from_status=order.status,
            to_status=order.status,
            actor_type=ActorType.SYSTEM,
            actor_id=actor.id,
            reason=reason,
        )
    )


async def _require_ride(session: SessionDep, delivery_id: int) -> Delivery:
    delivery = await session.get(Delivery, delivery_id)
    if delivery is None:
        raise not_found("delivery", delivery_id)
    return delivery


async def _reload(session: SessionDep, delivery_id: int) -> AdminDeliveryRow:
    """Re-read the ride joined to its rider and order, for the response body."""
    row = (
        await session.execute(
            select(Delivery, DeliveryPartner, Order)
            .join(DeliveryPartner, DeliveryPartner.id == Delivery.partner_id)
            .join(Order, Order.id == Delivery.order_id)
            .where(Delivery.id == delivery_id)
        )
    ).one()
    return _delivery_row(*row)


@router.post(
    "/deliveries/{delivery_id}/reassign",
    response_model=AdminDeliveryRow,
    responses={**ADMIN_RESPONSES, **NOT_FOUND, **CONFLICT},
)
async def reassign_delivery(
    delivery_id: int,
    payload: DeliveryReassign,
    session: SessionDep,
    actor: Annotated[User, Depends(current_user)],
):
    """Hand a stalled ride to another rider.

    The ride goes back to ASSIGNED whatever it had reached, and picked_up_at is
    cleared: a new rider starts from the kitchen, and leaving a pickup timestamp
    from the rider who gave up would claim the food is already collected.

    The customer's promise does not move. It was made at checkout and a
    reassignment is the platform's problem, not theirs.
    """
    delivery = await _require_ride(session, delivery_id)

    if delivery.status is DeliveryStatus.DELIVERED:
        raise conflict(
            f"Order {delivery.order_id} has already been handed over — there is "
            f"nothing left to reassign."
        )

    partner = await session.get(DeliveryPartner, payload.partner_id)
    if partner is None:
        raise not_found("delivery partner", payload.partner_id)
    if partner.id == delivery.partner_id:
        raise conflict(f"{partner.name} is already on this delivery.")

    order = await session.get(Order, delivery.order_id)
    if order is None:  # pragma: no cover - FK cascade makes this unreachable
        raise not_found("order", delivery.order_id)

    # Hand the availability flag over with the ride. app/routers/delivery.py now
    # claims a rider on assign and releases them on a terminal status, so a
    # reassign that moved partner_id without moving the flag would strand the old
    # rider as permanently busy and double-book the new one.
    previous = await session.get(DeliveryPartner, delivery.partner_id)
    if previous is not None:
        previous.is_available = True
    partner.is_available = False

    delivery.partner_id = partner.id
    delivery.status = DeliveryStatus.ASSIGNED
    delivery.assigned_at = datetime.now(UTC)
    delivery.picked_up_at = None

    await _record(session, order, actor, f"Delivery reassigned to {partner.name}")
    await session.flush()
    return await _reload(session, delivery_id)


@router.post(
    "/deliveries/{delivery_id}/fail",
    response_model=AdminDeliveryRow,
    responses={**ADMIN_RESPONSES, **NOT_FOUND, **CONFLICT},
)
async def fail_delivery(
    delivery_id: int,
    payload: DeliveryFail,
    session: SessionDep,
    actor: Annotated[User, Depends(current_user)],
):
    """Give up on a ride, with the reason support will read back.

    The reason is required and is not a free-text afterthought: it is what the
    customer is told, and it is the only field that makes a wall of failed rides
    countable afterwards.

    The ORDER is deliberately not cancelled here. Whether the customer gets a
    refund, a replacement or a redelivery is a decision with money attached, and
    it belongs to the cancel and refund routes that already know how to make it.
    """
    delivery = await _require_ride(session, delivery_id)

    if delivery.status is DeliveryStatus.DELIVERED:
        raise conflict(
            f"Order {delivery.order_id} was handed over. A delivered ride "
            f"cannot be marked failed."
        )
    if delivery.status is DeliveryStatus.FAILED:
        raise conflict(f"Delivery {delivery_id} is already recorded as failed.")

    order = await session.get(Order, delivery.order_id)
    if order is None:  # pragma: no cover - FK cascade makes this unreachable
        raise not_found("order", delivery.order_id)

    delivery.status = DeliveryStatus.FAILED

    await _record(session, order, actor, f"Delivery failed — {payload.reason}")
    await session.flush()
    return await _reload(session, delivery_id)


@router.get("/deliveries/count", response_model=dict[str, int], responses=ADMIN_RESPONSES)
async def count_deliveries(session: SessionDep):
    """How many rides sit in each status, in one round trip.

    A board needs five counts to draw its rail, and five list calls with
    `limit=1` to read five totals is five round trips for numbers Postgres can
    group in one.
    """
    rows = await session.execute(
        select(Delivery.status, func.count(Delivery.id)).group_by(Delivery.status)
    )
    counts = {status.value: 0 for status in DeliveryStatus}
    for status, count in rows.all():
        counts[status.value] = int(count)
    return counts
