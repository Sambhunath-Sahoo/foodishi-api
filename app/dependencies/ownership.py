"""Who may read a specific row.

`scope.py` answers "may this person act for this restaurant". This answers the
other half: "is this row theirs". Authentication alone is not authorization —
without these, any signed-in account reads every order, address, phone number
and payment in the database by incrementing an integer.

The rule for an order is: the customer who placed it, or staff of the restaurant
cooking it. Nobody else, including other customers.

Since platform_staff exists there is a third answer, and it is narrow on
purpose: Foodishi's own staff may READ an order, a refund and a profile, because
the operations console lists rows it must then be able to open, and its
operators staff no restaurant. Reads only — several write routes are guarded by
these same dependencies, which is what _may_read_as_platform is about.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import not_found
from app.db import SessionDep
from app.dependencies.identity import CurrentUser, forbidden
from app.dependencies.scope import path_int, platform_reader
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment, Refund
from app.models.staff import RestaurantStaff
from app.models.user import User

# Deliberately the same wording as an unrelated refusal. A message that
# distinguishes "not yours" from "does not exist" is an existence oracle.
DENIED = "You do not have access to this resource"

# The message was uniform; the STATUS CODE was the oracle. Every dependency below
# raised not_found BEFORE testing ownership, so a caller walking /orders/1..N read
# 403 for "exists, someone else's" and 404 for "does not exist" -- which sizes the
# platform's order count, customer base and payment volume, from any signed-in
# account. app/routers/reviews.py already claims this module takes the other line:
# "404, not 403: whether somebody else's order exists is not this caller's to
# learn, and ownership.py takes the same line." It did not; now it does.
#
# The distinction that matters: a denial that depends on the ROW's contents must
# be indistinguishable from a missing row, because telling them apart is the leak.
# A denial that depends only on the caller's ROLE leaks nothing about any row and
# stays a 403 -- readable_address refusing platform staff outright is a designed
# refusal the consoles render as such, not an oracle.

# RFC 9110's safe methods: the ones that only ask a question. The platform
# widening in this module is confined to these.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def _is_staff_of(session: AsyncSession, user_id: int, restaurant_id: int) -> bool:
    return bool(
        await session.scalar(
            select(RestaurantStaff.id).where(
                RestaurantStaff.user_id == user_id,
                RestaurantStaff.restaurant_id == restaurant_id,
                RestaurantStaff.is_active.is_(True),
            )
        )
    )


async def _may_read_as_platform(
    request: Request, session: AsyncSession, user: User
) -> bool:
    """Whether this is an operator reading, as opposed to an operator writing.

    scope.platform_reader owns the role floor, which is support: reading a
    customer's order to answer their message is the entire job of the support
    tier, so there is no lower rung to withhold it from.

    The method check is the load-bearing half, and the reason it is a method
    check rather than a per-route decision is that these dependencies are named
    readable_* but four writes hang off them — POST /orders/{id}/payments, POST
    /orders/{id}/refunds, POST /orders/{id}/cancel and PATCH /users/{id}. That
    was never sloppy: for the customer who owns the row, "may read it" and "may
    cancel it" are the same question. They are not the same question for Foodishi.
    An operator has to open the drawer on the Live board without being able to
    cancel the order inside it, refund it against the kitchen, or rewrite the
    customer's email. Until those routes carry a platform-write dependency of
    their own, the widening stops here and they refuse an operator exactly as
    they did before.

    Awaited only after the ownership checks have failed, so the customer reading
    their own order never pays for the platform_staff lookup.
    """
    if request.method not in SAFE_METHODS:
        return False
    return await platform_reader(session, user) is not None


async def readable_order(
    request: Request, session: SessionDep, user: CurrentUser
) -> Order:
    """The order named in the path, if this caller may see it.

    Used by every /orders/{order_id}/... read: the detail, the event timeline,
    the payments and the refunds all expose the same customer.
    """
    order_id = path_int(request, "order_id", caller="readable_order")
    order = await session.get(Order, order_id)
    if order is not None:
        if order.user_id == user.id:
            return order
        if await _is_staff_of(session, user.id, order.restaurant_id):
            return order
        # The Live board lists this order platform-wide; whoever may see it in
        # the list must be able to open it.
        if await _may_read_as_platform(request, session, user):
            return order
    # One answer for "no such order" and "not your order" -- see the note on
    # DENIED. Worded exactly as a genuinely missing row is worded.
    raise not_found("order", order_id)


async def readable_address(
    request: Request, session: SessionDep, user: CurrentUser
) -> Address:
    """An address is only ever the customer's own.

    Restaurant staff see the delivery address through the order they are
    cooking, which carries just that one address — not through this endpoint,
    which would expose every address the customer has ever saved.

    Platform staff are not admitted here, and that is a decision rather than an
    omission: nothing in the console addresses an address by its own id. Where
    it needs a customer's book it asks for that customer's list through
    /users/{user_id}/addresses, which readable_user already covers.
    """
    address_id = path_int(request, "address_id", caller="readable_address")
    address = await session.get(Address, address_id)
    if address is None or address.user_id != user.id:
        # Same answer either way -- see the note on DENIED.
        raise not_found("address", address_id)
    return address


async def readable_user(
    request: Request, session: SessionDep, user: CurrentUser
) -> User:
    """A profile is your own — or anybody's, if you are Foodishi reading it.

    The admin role the previous version of this comment was waiting for has
    arrived, and it belongs here rather than as a special case sprinkled through
    the routers: platform staff read any profile through this dependency, which
    is how the console's customer lookup and the addresses panel behind
    /users/{user_id}/addresses see anyone at all.

    It grants the read and not the write. PATCH /users/{user_id} shares this
    guard, so the safe-method half of _may_read_as_platform is the only thing
    standing between a support agent and editing a customer's email.
    """
    user_id = path_int(request, "user_id", caller="readable_user")
    if user_id == user.id:
        return user
    if not await _may_read_as_platform(request, session, user):
        raise forbidden(DENIED)

    # Answer with the profile that was actually asked for. The self case above
    # can return the caller because the two rows are the same row; an operator
    # reading someone else is the first time they differ, and handing back the
    # caller would be a quietly wrong answer for anything that uses this
    # dependency's value instead of just its refusal. The 404 is reached only
    # after the permission check has passed, so it is not an existence oracle,
    # and it is worded exactly as app/routers/users.py words its own.
    target = await session.get(User, user_id)
    if target is None:
        raise not_found("user", user_id)
    return target


async def _may_read_order(session: AsyncSession, user: User, order_id: int) -> bool:
    order = await session.get(Order, order_id)
    if order is None:
        return False
    return order.user_id == user.id or await _is_staff_of(
        session, user.id, order.restaurant_id
    )


async def readable_payment(
    request: Request, session: SessionDep, user: CurrentUser
) -> Payment:
    """A payment is reachable by whoever may read the order it settles.

    Guarded separately because /payments/{id} reaches the same customer through
    a different door than /orders/{id}/payments.
    """
    payment_id = path_int(request, "payment_id", caller="readable_payment")
    payment = await session.get(Payment, payment_id)
    if payment is None or not await _may_read_order(
        session, user, payment.order_id
    ):
        # Same answer either way -- see the note on DENIED.
        raise not_found("payment", payment_id)
    return payment


async def readable_refund(
    request: Request, session: SessionDep, user: CurrentUser
) -> Refund:
    refund_id = path_int(request, "refund_id", caller="readable_refund")
    refund = await session.get(Refund, refund_id)
    if refund is not None:
        if await _may_read_order(session, user, refund.order_id):
            return refund
        # The SLA watch lists refunds across the platform and then opens them.
        # The check is repeated here rather than folded into _may_read_order
        # because readable_payment shares that helper and is not part of this
        # widening.
        if await _may_read_as_platform(request, session, user):
            return refund
    # Same answer either way -- see the note on DENIED.
    raise not_found("refund", refund_id)


ReadableOrder = Annotated[Order, Depends(readable_order)]
ReadablePayment = Annotated[Payment, Depends(readable_payment)]
ReadableRefund = Annotated[Refund, Depends(readable_refund)]
ReadableAddress = Annotated[Address, Depends(readable_address)]
ReadableUser = Annotated[User, Depends(readable_user)]
