from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    NOT_PLATFORM,
    PLATFORM_ROLE_RANK,
    UNAUTHENTICATED,
    CurrentUser,
    OptionalPlatformStaff,
    require_platform_role,
)
from app.dependencies.ownership import readable_order, readable_refund
from app.dependencies.scope import staff_of_order
from app.models.catalog import RestaurantPolicy
from app.models.enums import PaymentStatus, PlatformRole, RefundStatus
from app.models.order import Order
from app.models.payment import Payment, Refund
from app.schemas.payment import RefundCreate, RefundDetail, RefundRead
from app.services.money import money
from app.services.policy import refund_sla_due

router = APIRouter(tags=["refunds"])

# A failed refund never left the building, so it does not consume headroom.
OUTSTANDING_STATUSES = (
    RefundStatus.INITIATED,
    RefundStatus.PROCESSING,
    RefundStatus.COMPLETED,
)
CLOSED_STATUSES = (RefundStatus.COMPLETED, RefundStatus.FAILED)


def _is_breached(refund: Refund, now: datetime) -> bool:
    return now > refund.sla_due_at and refund.status != RefundStatus.COMPLETED


def _detail(refund: Refund) -> RefundDetail:
    read = RefundRead.model_validate(refund)
    return RefundDetail(
        **read.model_dump(), sla_breached=_is_breached(refund, datetime.now(UTC))
    )


async def _captured_payment_id(session: SessionDep, order_id: int) -> int | None:
    # Refunds hang off a payment row; the latest capture is the one that holds
    # the money we are giving back.
    return await session.scalar(
        select(Payment.id)
        .where(Payment.order_id == order_id, Payment.status == PaymentStatus.CAPTURED)
        .order_by(Payment.id.desc())
        .limit(1)
    )


async def _assert_within_captured(session: SessionDep, order_id: int, amount: Decimal):
    """Refunding more than was collected is the expensive bug here, and no
    per-row schema check can catch it — it only shows up across rows.
    """
    captured = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.order_id == order_id, Payment.status == PaymentStatus.CAPTURED
        )
    )
    outstanding = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.order_id == order_id, Refund.status.in_(OUTSTANDING_STATUSES)
        )
    )
    headroom = money(captured or 0) - money(outstanding or 0)
    if amount > headroom:
        raise conflict(
            f"Refunding {amount} exceeds the {headroom} still refundable"
            f" on order {order_id}"
        )


async def may_refund_order(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    platform: OptionalPlatformStaff,
) -> None:
    """Who may put a refund on the books against an order.

    Deliberately narrower than the readable_order guarding the reads below.
    That one admits the customer who placed the order — which is right for
    reading their own money, and wrong for writing it: on this route it let the
    payer issue their own refund, capped only by what they had paid. Refunding
    is spending, so it belongs to the kitchen carrying the cost or to Foodishi
    settling against that kitchen.

    Platform is checked first because an operator is staff of no restaurant and
    staff_of_order would refuse them before their role was ever looked at. An
    order that does not exist is still the 404 both staff_of_order and the
    handler already give, so a bad id reads the same from either branch.
    """
    if (
        platform is not None
        and PLATFORM_ROLE_RANK[platform.role] >= PLATFORM_ROLE_RANK[PlatformRole.ADMIN]
    ):
        return
    await staff_of_order(request, session, user)


@router.post(
    "/orders/{order_id}/refunds",
    dependencies=[Depends(may_refund_order)],
    response_model=RefundRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
)
async def create_refund(order_id: int, payload: RefundCreate, session: SessionDep):
    # FOR UPDATE, and on the ORDER row rather than on payments or refunds,
    # because the order is the one row every money-out path touches:
    # ordering.cancel already locks it for exactly this reason. Without a shared
    # lock the headroom guard below is a read-then-write -- two operators
    # refunding 500 each against 500 captured both compute headroom 500, both
    # pass, and both insert. Nothing in the schema can refuse that, because the
    # invariant only exists across rows. Locking here makes the pair serialise.
    order = await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    if order is None:
        raise not_found("order", order_id)

    amount = money(payload.amount)
    await _assert_within_captured(session, order_id, amount)
    payment_id = await _captured_payment_id(session, order_id)
    if payment_id is None:
        raise conflict(f"Order {order_id} has no captured payment to refund")

    policy = await session.get(RestaurantPolicy, order.restaurant_id)
    if policy is None:
        raise unprocessable(
            f"Restaurant {order.restaurant_id} has no policy, so no refund SLA applies"
        )

    now = datetime.now(UTC)
    refund = Refund(
        payment_id=payment_id,
        order_id=order_id,
        amount=amount,
        reason=payload.reason,
        status=RefundStatus.INITIATED,
        initiated_at=now,
        sla_due_at=refund_sla_due(now, policy.refund_sla_hours),
    )
    session.add(refund)
    await session.flush()
    await session.refresh(refund)  # created_at is a server default
    return refund


@router.get("/orders/{order_id}/refunds",
    dependencies=[Depends(readable_order)], response_model=Page[RefundRead])
async def list_order_refunds(order_id: int, session: SessionDep, params: PageDep):
    statement = select(Refund).where(Refund.order_id == order_id).order_by(Refund.id)
    items, total = await paginate(session, statement, params)
    return Page[RefundRead](
        items=items, total=total, limit=params.limit, offset=params.offset
    )


@router.get(
    "/refunds/{refund_id}",
    response_model=RefundDetail,
    dependencies=[Depends(readable_refund)],
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def get_refund(refund_id: int, session: SessionDep):
    refund = await session.get(Refund, refund_id)
    if refund is None:
        raise not_found("refund", refund_id)
    return _detail(refund)


# Settling is the moment the platform declares the money returned, and it is
# named in app/models/platform.py as this side of the boundary: "the refund
# adjudicated against a kitchen". Not readable_refund — that admits the payer,
# and a customer marking their own refund complete is the last link in a
# money-out loop. OPS rather than SUPPORT for the same reason a coupon is: this
# spends, it does not answer a question.
@router.post(
    "/refunds/{refund_id}/complete",
    dependencies=[Depends(require_platform_role(PlatformRole.ADMIN))],
    response_model=RefundDetail,
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **NOT_PLATFORM},
)
async def complete_refund(refund_id: int, session: SessionDep):
    refund = await session.get(Refund, refund_id)
    if refund is None:
        raise not_found("refund", refund_id)
    if refund.status in CLOSED_STATUSES:
        raise conflict(f"Refund {refund_id} is already {refund.status.value}")

    refund.status = RefundStatus.COMPLETED
    refund.completed_at = datetime.now(UTC)
    await session.flush()
    return _detail(refund)
