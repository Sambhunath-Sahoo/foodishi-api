import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.dependencies.ownership import readable_order
from app.dependencies.scope import staff_of_delivery, staff_of_order
from app.models.delivery import Delivery, DeliveryPartner
from app.models.enums import ActorType, DeliveryStatus, OrderStatus, PlatformRole
from app.models.order import Order
from app.schemas.delivery import (
    DeliveryDetail,
    DeliveryPartnerCreate,
    DeliveryPartnerRead,
    DeliveryRead,
    DeliveryUpdate,
)
from app.services import order_state, ordering

# One router, no prefix: the delivery domain hangs off three different roots
# (/orders, /deliveries, /delivery-partners) and a shared prefix would fit none.
router = APIRouter(tags=["delivery"])

PARTNERS = "/delivery-partners"

# assigned -> picked_up -> delivered, and a rider can fail out of either leg.
# Terminal states are absent on purpose: no key means no way out.
ALLOWED_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.ASSIGNED: frozenset(
        {DeliveryStatus.PICKED_UP, DeliveryStatus.FAILED}
    ),
    DeliveryStatus.PICKED_UP: frozenset(
        {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED}
    ),
}

#: A ride that is over, either way. The rider goes back in the pool.
TERMINAL_DELIVERY_STATUSES = frozenset(
    {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED}
)

# The status that stamps each timestamp column.
STATUS_TIMESTAMPS: dict[DeliveryStatus, str] = {
    DeliveryStatus.PICKED_UP: "picked_up_at",
    DeliveryStatus.DELIVERED: "delivered_at",
}

# What each delivery move means for the parent order. A pickup puts the order
# on the road; a drop completes it. A failed delivery says nothing about the
# order — support decides that one.
ORDER_STATUS_MIRROR: dict[DeliveryStatus, OrderStatus] = {
    DeliveryStatus.PICKED_UP: OrderStatus.OUT_FOR_DELIVERY,
    DeliveryStatus.DELIVERED: OrderStatus.DELIVERED,
}


def _live_eta_minutes(promised_at: datetime, now: datetime) -> int:
    """Minutes left against the promise, never negative.

    A late order counts down to 0 and stops there — a negative ETA reads as a
    bug to every client that renders it.
    """
    remaining = (promised_at - now).total_seconds()
    return max(0, math.ceil(remaining / 60))


@router.post(
    "/orders/{order_id}/delivery/assign",
    response_model=DeliveryRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
    dependencies=[Depends(staff_of_order)],
)
async def assign_delivery(order_id: int, session: SessionDep):
    """Attach a rider to an order. Staff of that order's restaurant only.

    Same rule and same dependency as PATCH /deliveries/{id}: the restaurant is
    re-derived from orders.restaurant_id, never taken from the caller. Without
    it this write trusted a client-supplied order_id outright — anyone who
    could reach the port could dispatch a rider against any restaurant's order.
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise not_found("order", order_id)

    # The kitchen must have finished the food before a rider is dispatched to it.
    # Nothing checked, so a PENDING order could be assigned, picked up and
    # delivered while _mirror_onto_order silently declined every mirror (the
    # order cannot transition PENDING -> OUT_FOR_DELIVERY) -- leaving a DELIVERED
    # delivery against a PENDING order, still inside CANCELLABLE_STATUSES, so the
    # customer could cancel food they had already eaten and collect a refund.
    if order.status is not OrderStatus.READY_FOR_PICKUP:
        raise conflict(
            f"Order {order_id} is {order.status.value}; a rider can only be "
            "assigned once it is ready for pickup"
        )

    existing = await session.scalar(
        select(Delivery.id).where(Delivery.order_id == order_id)
    )
    if existing is not None:
        raise conflict(f"Order {order_id} already has a delivery")

    # FOR UPDATE SKIP LOCKED, ORDER BY, and is_available actually written.
    #
    # This was `select(...).where(is_available).limit(1)` with no lock and no
    # ordering, and NOTHING in the codebase ever set is_available to False -- only
    # the seeder and POST /delivery-partners, both to True. So the same rider was
    # returned for every assignment on the platform, forever, and two concurrent
    # assigns read the same row anyway. SKIP LOCKED is the standard worker-queue
    # claim: concurrent callers take different riders instead of queueing.
    partner = await session.scalar(
        select(DeliveryPartner)
        .where(DeliveryPartner.is_available.is_(True))
        .order_by(DeliveryPartner.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if partner is None:
        raise conflict("No delivery partner is available")
    # Released in update_delivery when the ride reaches a terminal status.
    partner.is_available = False

    now = datetime.now(UTC)
    delivery = Delivery(
        order_id=order_id,
        partner_id=partner.id,
        distance_km=order.distance_km,
        eta_minutes=_live_eta_minutes(order.promised_at, now),
        status=DeliveryStatus.ASSIGNED,
        assigned_at=now,
    )
    session.add(delivery)
    try:
        await session.flush()
    except IntegrityError as exc:
        # deliveries.order_id is UNIQUE: a concurrent assign got here first,
        # past the lookup above.
        raise conflict(f"Order {order_id} already has a delivery") from exc
    await session.refresh(delivery)
    return delivery


@router.get(
    "/orders/{order_id}/delivery",
    response_model=DeliveryDetail,
    responses={**NOT_FOUND, **UNAUTHENTICATED, **FORBIDDEN},
    dependencies=[Depends(readable_order)],
)
async def get_order_delivery(order_id: int, session: SessionDep):
    """Where the rider is, for whoever may already see the order.

    Deliberately readable_order and not staff_of_order like the two writes
    around it: the customer standing at the door is the main reader of this
    route, and ownership.readable_order is the one rule that admits them and
    the kitchen cooking their food and nobody else. It needed *some* guard,
    because DeliveryDetail carries the courier's name and phone: open, an
    incrementing order_id walked the whole courier roster one delivery at a
    time, which is the same leak as the partner listing below by another door.
    """
    row = (
        await session.execute(
            select(Delivery, DeliveryPartner, Order.promised_at)
            .join(DeliveryPartner, DeliveryPartner.id == Delivery.partner_id)
            .join(Order, Order.id == Delivery.order_id)
            .where(Delivery.order_id == order_id)
        )
    ).first()
    if row is None:
        raise not_found("delivery for order", order_id)

    delivery, partner, promised_at = row
    stored = DeliveryRead.model_validate(delivery).model_dump()
    return DeliveryDetail(
        **{**stored, "eta_minutes": _live_eta_minutes(promised_at, datetime.now(UTC))},
        partner=DeliveryPartnerRead.model_validate(partner),
    )


@router.patch(
    "/deliveries/{delivery_id}",
    response_model=DeliveryRead,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
    dependencies=[Depends(staff_of_delivery)],
)
async def update_delivery(
    delivery_id: int, payload: DeliveryUpdate, session: SessionDep
):
    """Advance a delivery, and the order behind it.

    A delivery has no restaurant column, so the scope comes from its order:
    only staff of that restaurant may move it. The in-process system actor is
    unaffected — _mirror_onto_order below calls the ordering service directly
    and never crosses this dependency. There is deliberately no HTTP lane for
    a "system" caller here, because the auth core has no service identity to
    check one against yet; a dispatch service needs one before it can call in.
    """
    delivery = await session.get(Delivery, delivery_id)
    if delivery is None:
        raise not_found("delivery", delivery_id)

    target = payload.status
    # Re-sending the current status is a conflict too: a rider cannot be picked
    # up twice, and treating it as a no-op would hide a double-submit.
    if target not in ALLOWED_TRANSITIONS.get(delivery.status, frozenset()):
        raise conflict(
            f"Delivery {delivery_id} cannot move from {delivery.status} to {target}"
        )

    delivery.status = target
    stamp = STATUS_TIMESTAMPS.get(target)
    if stamp is not None:
        setattr(delivery, stamp, datetime.now(UTC))

    # Put the rider back in the pool. assign_delivery now claims a partner by
    # setting is_available = False, so without this release the fleet drains to
    # empty after one ride each and every assign answers "No delivery partner is
    # available". The pair has to exist together or neither should.
    if target in TERMINAL_DELIVERY_STATUSES:
        partner = await session.get(DeliveryPartner, delivery.partner_id)
        if partner is not None:
            partner.is_available = True

    await _mirror_onto_order(session, delivery.order_id, target)
    await session.flush()
    return delivery


async def _mirror_onto_order(
    session: SessionDep, order_id: int, target: DeliveryStatus
) -> None:
    """Advance the parent order to match the delivery, and record the event.

    Routed through the ordering service so the order state machine validates
    the move and writes its OrderStatusEvent. Skipped when the move is not
    legal from where the order already is — the orders domain may have moved it
    first, and a delivery update must not 409 because the order is ahead.
    """
    mirrored = ORDER_STATUS_MIRROR.get(target)
    if mirrored is None:
        return
    order = await session.get(Order, order_id)
    if order is None or not order_state.can_transition(order.status, mirrored):
        return
    await ordering.transition(
        session, order, to_status=mirrored, actor_type=ActorType.SYSTEM
    )


# The courier roster is bulk PII: every name and phone number Foodishi dispatches
# with, paginated for the convenience of whoever is reading it. Couriers are
# owned by the platform and not by any one kitchen, so there is no restaurant
# to derive and no scope.py guard that could ever fit — it is platform staff or
# nobody. SUPPORT is the floor, as on the coupon board: looking up who is
# carrying an order is exactly what support does when a customer asks.
@router.get(
    PARTNERS,
    response_model=Page[DeliveryPartnerRead],
    dependencies=[Depends(require_platform_role())],
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def list_delivery_partners(
    session: SessionDep, page: PageDep, is_available: bool | None = None
):
    statement = select(DeliveryPartner).order_by(DeliveryPartner.id)
    if is_available is not None:
        statement = statement.where(DeliveryPartner.is_available.is_(is_available))
    rows, total = await paginate(session, statement, page)
    return Page[DeliveryPartnerRead](
        items=rows, total=total, limit=page.limit, offset=page.offset
    )


# OPS rather than SUPPORT, because a row inserted here is not bookkeeping: it
# is immediately assignable to a real order by assign_delivery above, which
# takes the first available partner it finds. Adding a courier is a dispatch
# decision, so it sits one rung up from reading the roster.
@router.post(
    PARTNERS,
    response_model=DeliveryPartnerRead,
    status_code=201,
    dependencies=[Depends(require_platform_role(PlatformRole.ADMIN))],
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def create_delivery_partner(payload: DeliveryPartnerCreate, session: SessionDep):
    partner = DeliveryPartner(**payload.model_dump())
    session.add(partner)
    await session.flush()
    await session.refresh(partner)
    return partner
