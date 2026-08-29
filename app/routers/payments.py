import hmac
import logging
import os
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import is_auth_enabled
from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import FORBIDDEN, forbidden
from app.dependencies.ownership import readable_order, readable_payment
from app.models.enums import PaymentStatus
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.payment import PaymentCallback, PaymentCreate, PaymentRead
from app.services import order_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

PROVIDER = "mock"
DEFAULT_FAILURE_REASON = "Declined by provider"

# The statuses that mean "this order already has money against it", either
# collected or still held. Either one blocks a second attempt. FAILED, REFUNDED
# and PARTIALLY_REFUNDED deliberately do not: paying again after a decline or a
# reversal is the normal thing to do. PENDING is not listed because nothing in
# this codebase ever writes it — an attempt is AUTHORIZED from birth.
SETTLED_OR_OUTSTANDING = (PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED)

# The callback is a machine-to-machine webhook, so it is authenticated by a
# shared secret rather than by any dependency in app/dependencies/identity.py:
# a gateway has no Supabase session and no public.users row to map onto.

# NAME of a header, not secrets. The value is read from os.environ.
WEBHOOK_SECRET_ENV = "PAYMENT_WEBHOOK_SECRET"  # noqa: S105
WEBHOOK_SECRET_HEADER = "X-Payment-Webhook-Secret"  # noqa: S105

# Same wording for a missing and for a wrong secret: telling a prober which of
# the two it got is telling it half the answer.
WEBHOOK_DENIED = "Invalid payment webhook credentials"

# Shared OpenAPI response description, matching the style of app/core/errors.py.
BAD_WEBHOOK_SECRET = {403: {"description": "Missing or invalid webhook secret"}}


def _provider_ref() -> str:
    # Stands in for the gateway's own reference; unique so it can key a lookup.
    return f"{PROVIDER}_{uuid4().hex}"


def payment_webhook_secret() -> str | None:
    """The secret a payment callback must present, or None when unconfigured.

    A named accessor over os.environ for the reason app/config.py gives: the
    failure mode reads as this variable's own name rather than as a KeyError
    three frames down. Read per request rather than cached at import, exactly
    like is_auth_enabled(), so a test can flip it with monkeypatch.setenv and
    so a deployment cannot run with a value that was captured before the
    environment was fully assembled.

    Not require_env(): unset must not raise here — see verify_webhook_secret.
    It lives in this module only because this change is scoped to one file; it
    belongs beside is_auth_enabled() in app/config.py.
    """
    return os.environ.get(WEBHOOK_SECRET_ENV, "").strip() or None


def verify_webhook_secret(
    presented: Annotated[str | None, Header(alias=WEBHOOK_SECRET_HEADER)] = None,
) -> None:
    """Authenticate the gateway behind a payment callback.

    UNSET BEHAVIOUR — it follows AUTH_ENABLED, and that is the whole point.
    This route decides whether an order counts as paid, so "no secret
    configured" must not mean "anyone may settle a payment" on a host that is
    otherwise demanding real tokens. AUTH_ENABLED is the only signal this
    codebase has for "not just a laptop", so:

      AUTH_ENABLED=true  + no secret -> refuse every callback. Loud, and the
          only thing it can break is a webhook that was never authenticated
          anyway. The alternative was a money endpoint open to the internet.
      AUTH_ENABLED=false + no secret -> allow, and log at WARNING every time,
          so the mock provider still works on a laptop with nothing set up.

    Refusing rather than raising at import keeps the failure diagnosable: the
    app still boots and /health still answers, exactly as app/config.py's
    require_env comment argues for.

    Once the variable IS set, this fails closed in both modes: no header and a
    wrong header are both refused, so a caller cannot reopen the hole by
    omitting it.

    403 rather than 401 for all three refusals: there is no interactive session
    to refresh, so bouncing the caller through a login screen is meaningless,
    and a WWW-Authenticate: Bearer challenge would point a gateway at Supabase.
    """
    expected = payment_webhook_secret()
    if expected is None:
        if is_auth_enabled():
            logger.error(
                "Refusing payment callback: %s is unset while AUTH_ENABLED is "
                "true. Set the secret the gateway will send, or the callback "
                "cannot be authenticated at all.",
                WEBHOOK_SECRET_ENV,
            )
            raise forbidden(WEBHOOK_DENIED)
        logger.warning(
            "UNVERIFIED WEBHOOK: %s is unset and AUTH_ENABLED is false, so any "
            "caller that can reach this port can settle a payment. Never run "
            "this configuration anywhere reachable.",
            WEBHOOK_SECRET_ENV,
        )
        return

    # compare_digest, never ==, so the number of matching leading characters
    # cannot be read off the response time and the secret guessed a byte at a
    # time. It needs bytes: on str it raises TypeError for anything non-ASCII,
    # which a pasted secret can easily be.
    candidate = (presented or "").strip().encode()
    if not hmac.compare_digest(candidate, expected.encode()):
        logger.warning(
            "Rejected payment callback: %s header %s.",
            WEBHOOK_SECRET_HEADER,
            "missing" if presented is None else "did not match",
        )
        raise forbidden(WEBHOOK_DENIED)


@router.post(
    "/orders/{order_id}/payments",
    dependencies=[Depends(readable_order)],
    response_model=PaymentRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
)
async def create_payment(order_id: int, payload: PaymentCreate, session: SessionDep):
    """Authorize one attempt to collect what the order says is owed.

    Never settles anything: the row is written AUTHORIZED and only
    POST /payments/{payment_id}/callback — a gateway holding the shared secret —
    can turn it into CAPTURED. See verify_webhook_secret().

    NOT idempotent by key, and it cannot be: the replay in
    app/routers/orders.py works because orders carries a unique
    idempotency_key column, and payments has none. So the guard below is the
    order's own state instead — an order with money already held or already
    taken refuses a second attempt rather than authorizing the full total
    twice, which is what a double-tap on a slow phone used to do. A client that
    gets the 409 should re-read GET /orders/{order_id}/payments: the attempt it
    was told about is the one already there.
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise not_found("order", order_id)

    # A cancelled or delivered order must not accept new money. A rejected order
    # with nothing captured books no refund (there is nothing to refund), and
    # assert_transition refuses any move out of CANCELLED -- so a payment that
    # authorized and captured after the rejection left the money stranded with no
    # endpoint that would ever send it back. Terminal means terminal.
    if order.status in order_state.TERMINAL:
        raise conflict(
            f"Order {order_id} is {order.status.value} and cannot take a payment"
        )

    existing = await session.scalar(
        select(Payment)
        .where(Payment.order_id == order_id, Payment.status.in_(SETTLED_OR_OUTSTANDING))
        .order_by(Payment.id)
        .limit(1)
    )
    if existing is not None:
        # Two different sentences, because they call for two different things
        # from whoever reads them: one is done, the other is waiting.
        if existing.status == PaymentStatus.CAPTURED:
            raise conflict(f"Order {order_id} is already paid by payment {existing.id}")
        raise conflict(
            f"Payment {existing.id} for order {order_id} is already authorized "
            "and waiting to settle — no second payment is needed"
        )

    payment = Payment(
        order_id=order_id,
        method=payload.method,
        provider=PROVIDER,
        provider_ref=_provider_ref(),
        # Never the client's number: the order decides what is owed.
        amount=order.total_amount,
        status=PaymentStatus.AUTHORIZED,
        authorized_at=datetime.now(UTC),
    )
    # SAVEPOINT, not a bare flush.
    #
    # uq_payments_one_live_per_order can fire here: another request authorized
    # this order between the SELECT above and this INSERT. To answer with the
    # same 409 the fast path gives -- naming the row that won, so a retrying
    # client can tell "yours" from "someone else's" -- this handler has to run a
    # SELECT after the failure.
    #
    # `await session.rollback()` cannot be used for that. get_session() holds the
    # request inside `async with Session.begin()`, and rolling back inside a
    # context-managed transaction CLOSES it: the next statement raises
    # `InvalidRequestError: Can't operate on closed transaction inside context
    # manager`, which is a 500 -- the exact failure this code exists to avoid.
    # Verified empirically, not assumed.
    #
    # begin_nested() issues a SAVEPOINT instead, so the failed INSERT is rolled
    # back to it and the outer transaction stays usable.
    session.add(payment)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        winner = await session.scalar(
            select(Payment)
            .where(
                Payment.order_id == order_id,
                Payment.status.in_(SETTLED_OR_OUTSTANDING),
            )
            .order_by(Payment.id)
            .limit(1)
        )
        raise conflict(
            f"Payment {winner.id if winner else '?'} for order {order_id} was "
            "authorized at the same moment — no second payment is needed"
        ) from exc
    await session.refresh(payment)  # currency and created_at are server defaults
    return payment


@router.get("/orders/{order_id}/payments",
    dependencies=[Depends(readable_order)], response_model=Page[PaymentRead])
async def list_order_payments(order_id: int, session: SessionDep, params: PageDep):
    # Failed attempts are part of the answer, not noise — "my card was charged
    # twice" is only answerable if every attempt is listed, oldest first.
    statement = (
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.id)
    )
    items, total = await paginate(session, statement, params)
    return Page[PaymentRead](
        items=items, total=total, limit=params.limit, offset=params.offset
    )


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    dependencies=[Depends(readable_payment)],
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def get_payment(payment_id: int, session: SessionDep):
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise not_found("payment", payment_id)
    return payment


@router.post(
    "/payments/{payment_id}/callback",
    dependencies=[Depends(verify_webhook_secret)],
    response_model=PaymentRead,
    responses={**BAD_WEBHOOK_SECRET, **NOT_FOUND, **CONFLICT},
)
async def apply_callback(payment_id: int, payload: PaymentCallback, session: SessionDep):
    """Settle an authorized payment on the gateway's word.

    The body is trusted — `outcome` alone decides whether an order counts as
    paid — so the caller is what has to be proved. That is the shared secret in
    verify_webhook_secret(); read its docstring before changing anything here,
    including what happens when the secret is unset.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise not_found("payment", payment_id)
    if payment.status != PaymentStatus.AUTHORIZED:
        raise conflict(
            f"Payment {payment_id} is {payment.status.value}"
            " and cannot accept a callback"
        )

    now = datetime.now(UTC)
    if payload.outcome == "captured":
        payment.status = PaymentStatus.CAPTURED
        payment.captured_at = now
    else:
        # The row stays, marked failed. Deleting it would erase the evidence a
        # customer needs when their bank shows a pending hold.
        payment.status = PaymentStatus.FAILED
        payment.failed_reason = payload.failed_reason or DEFAULT_FAILURE_REASON

    await session.flush()
    return payment
