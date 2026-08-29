"""Money in, money back, and what the platform keeps — across every kitchen.

Three lists that had no cross-order endpoint before this. `GET /orders/{id}/payments`
and `GET /orders/{id}/refunds` answer one order, which is right for a customer
and useless for whoever has to work a queue: the operator console was reduced to
walking refund ids one at a time to find the breached ones, and that is not a
design, it is a workaround for a missing route.

The commission ledger deliberately shares its base with
`services/settlements.commission_for` — the number an operator reads here and
the number frozen onto a kitchen's statement have to agree to the paisa. See the
docstring on `services/admin_insights.commission_on` for the open question about
whether that base should be gross at all.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, func, or_, select

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.models.enums import PaymentMethod, PaymentStatus, RefundStatus
from app.models.payment import Payment, Refund
from app.schemas.admin import CommissionLedger, CommissionRow
from app.schemas.payment import PaymentRead, RefundDetail, RefundRead
from app.services import admin_insights, platform_settings
from app.services.money import money

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_role())],
)

ADMIN_RESPONSES = {**UNAUTHENTICATED, **NOT_PLATFORM}

DEFAULT_LEDGER_DAYS = 30
MAX_LEDGER_DAYS = 365

#: sum()'s start value, so an empty ledger totals to Decimal("0.00") rather than
#: the int 0 — which would serialise as `0` where every other figure is `0.00`.
_ZERO = Decimal("0.00")


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _by_reference(statement: Select, model, term: str) -> Select:
    """Match a provider reference, or an order id when the term is a number.

    Those are the two things somebody has in front of them: a reference copied
    out of a gateway dashboard, or an order number off a support ticket.
    """
    predicates = [model.provider_ref.ilike(_like(term), escape="\\")]
    bare = term.strip().lstrip("#")
    if bare.isdigit():
        predicates.append(model.order_id == int(bare))
    return statement.where(or_(*predicates))


@router.get("/payments", response_model=Page[PaymentRead], responses=ADMIN_RESPONSES)
async def list_payments(
    session: SessionDep,
    page: PageDep,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status: Annotated[
        list[PaymentStatus] | None,
        Query(
            description=(
                "Repeatable. `?status=refunded&status=partially_refunded` lists "
                "both. Omit for every status."
            )
        ),
    ] = None,
    method: PaymentMethod | None = None,
):
    """Every payment attempt on the platform, newest first.

    Attempts, not payments: a failed row is a customer who tried to pay and
    could not, and it is the most useful row on the list. Filtering it out by
    default would hide the only thing here anybody has to act on.

    `status` is a LIST because one of the console's stage cards is honestly two
    statuses: "Sent back" counts `refunded` and `partially_refunded` together,
    since as a card they are one idea — money went back. Taking a single status
    forced the client to either send one of the two and under-report, or filter
    after paging and disagree with its own total. A repeated query parameter
    costs nothing and removes the choice.
    """
    statement = select(Payment)
    if status:
        # IN, not equality: a one-element list behaves exactly as the old single
        # value did, so no existing caller changes.
        statement = statement.where(Payment.status.in_(status))
    if method is not None:
        statement = statement.where(Payment.method == method)
    if q is not None:
        statement = _by_reference(statement, Payment, q)

    statement = statement.order_by(Payment.created_at.desc(), Payment.id.desc())
    items, total = await paginate(session, statement, page)
    return Page[PaymentRead](
        items=items, total=total, limit=page.limit, offset=page.offset
    )


@router.get("/refunds", response_model=Page[RefundDetail], responses=ADMIN_RESPONSES)
async def list_refunds(
    session: SessionDep,
    page: PageDep,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status: RefundStatus | None = None,
    breached: Annotated[
        bool,
        Query(description="Only refunds past the time the customer was promised"),
    ] = False,
):
    """Every refund, worst first, carrying the platform's own SLA verdict.

    `RefundDetail` rather than `RefundRead` on a list: `sla_breached` is the
    whole reason somebody opens this screen, and computing it client-side would
    mean the console and the API could disagree about which refunds are late.

    Ordered breached-first then by due time, so the row that has been owed
    longest is the first one read. A refund inside its promise needs nobody's
    attention yet and sorts below every one that does.
    """
    now = datetime.now(UTC)
    outstanding = Refund.status != RefundStatus.COMPLETED
    is_breached = outstanding & (Refund.sla_due_at < now)

    statement = select(Refund)
    if breached:
        statement = statement.where(is_breached)
    if status is not None:
        statement = statement.where(Refund.status == status)
    if q is not None:
        statement = _by_reference(statement, Refund, q)

    statement = statement.order_by(
        is_breached.desc(), Refund.sla_due_at.asc(), Refund.id.asc()
    )
    items, total = await paginate(session, statement, page)

    return Page[RefundDetail](
        items=[
            RefundDetail(
                **RefundRead.model_validate(refund).model_dump(),
                sla_breached=admin_insights.is_breached(refund, now),
            )
            for refund in items
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/refunds/{refund_id}/retry",
    response_model=RefundDetail,
    responses={**ADMIN_RESPONSES, **NOT_FOUND, **CONFLICT},
)
async def retry_refund(refund_id: int, session: SessionDep):
    """Hand a stuck refund back to the provider.

    It becomes PROCESSING and not COMPLETED, and the distinction is the point:
    the money has been asked for again, not delivered. A retry that reported
    "completed" would tell an operator a customer has been paid when nothing has
    left the platform, and that is the one lie this screen must not tell.

    Only a refund that is genuinely stuck can be retried. FAILED is the obvious
    case; INITIATED counts too, because a refund that never reached the provider
    is as stuck as one the provider rejected. PROCESSING is refused rather than
    treated as a no-op — a second attempt on something already in flight is how
    a customer gets paid twice.
    """
    refund = await session.get(Refund, refund_id)
    if refund is None:
        raise not_found("refund", refund_id)

    if refund.status is RefundStatus.COMPLETED:
        raise conflict(
            f"Refund {refund_id} already landed. There is nothing to retry."
        )
    if refund.status is RefundStatus.PROCESSING:
        raise conflict(
            f"Refund {refund_id} is already with the provider. Wait for it to "
            f"settle or fail before retrying."
        )

    refund.status = RefundStatus.PROCESSING
    await session.flush()
    await session.refresh(refund)

    return RefundDetail(
        **RefundRead.model_validate(refund).model_dump(),
        sla_breached=admin_insights.is_breached(refund, datetime.now(UTC)),
    )


@router.get("/commission", response_model=CommissionLedger, responses=ADMIN_RESPONSES)
async def get_commission_ledger(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_LEDGER_DAYS)] = DEFAULT_LEDGER_DAYS,
):
    """What every kitchen sold in the window, and what the platform kept.

    Not paginated, deliberately: there are twenty-five kitchens and the totals at
    the bottom are only true if every row is in the response. A paged ledger
    whose footer summed one page would be a finance screen that does not add up.

    Kitchens that delivered nothing are still rows, at zero. "Why is this one
    empty" is a real question and a row that vanished cannot answer it.
    """
    settings = await platform_settings.load(session)
    rows = await admin_insights.kitchen_rows(session, days)

    ledger_rows = [
        CommissionRow(
            restaurant_id=row.restaurant_id,
            name=row.name,
            city=row.city,
            delivered_orders=row.delivered,
            gross=row.gross,
            food_value=row.food_value,
            commission_percent=row.commission_percent,
            is_negotiated=row.commission_percent
            != settings.commission_default_percent,
            commission=admin_insights.commission_on(row.gross, row.commission_percent),
            payout=row.gross
            - admin_insights.commission_on(row.gross, row.commission_percent),
        )
        for row in rows
    ]
    # Biggest earner first. Ordering by the derived figure rather than in SQL
    # because the rate is per kitchen — Postgres would have to join the same
    # column back to compute it, and the list is twenty-five rows long.
    ledger_rows.sort(key=lambda row: row.commission, reverse=True)

    return CommissionLedger(
        rows=ledger_rows,
        gross=sum((row.gross for row in ledger_rows), start=_ZERO),
        food_value=sum((row.food_value for row in ledger_rows), start=_ZERO),
        commission=sum((row.commission for row in ledger_rows), start=_ZERO),
        payout=sum((row.payout for row in ledger_rows), start=_ZERO),
        default_percent=settings.commission_default_percent,
        settlement_days=settings.commission_settlement_days,
        days=days,
    )


class RefundTally(BaseModel):
    """The refund queue's shape, in one round trip.

    Mirrors GET /admin/payments/count and GET /admin/deliveries/count, which
    exist for the same reason: a rail needs several counts, and one list call per
    count is one round trip per number Postgres can group in a single query.

    `breached` and `owed` are also on GET /admin/workload, deliberately — that
    route answers "what needs attention across the whole platform" and this one
    answers "what does the refund queue look like". Both read the same predicate
    (not completed, past sla_due_at), so they cannot disagree.
    """

    model_config = ConfigDict(from_attributes=True)

    #: Every RefundStatus, including the ones at zero: a rail that omits an empty
    #: status renders a gap rather than a zero.
    by_status: dict[str, int]
    #: Past the promised time and not completed. A FAILED refund counts -- the
    #: money still has not reached the customer.
    breached: int
    #: What those breached refunds are worth, as a decimal string.
    owed: Decimal


@router.get("/refunds/count", response_model=RefundTally, responses=ADMIN_RESPONSES)
async def count_refunds(session: SessionDep):
    """The refund queue's shape: per status, plus what is breached and owed."""
    rows = await session.execute(
        select(Refund.status, func.count(Refund.id)).group_by(Refund.status)
    )
    by_status = {status.value: 0 for status in RefundStatus}
    for status, count in rows.all():
        by_status[status.value] = int(count)

    now = datetime.now(UTC)
    breached_predicate = (Refund.status != RefundStatus.COMPLETED) & (
        Refund.sla_due_at < now
    )
    breached, owed = (
        await session.execute(
            select(
                func.count(Refund.id),
                func.coalesce(func.sum(Refund.amount), Decimal(0)),
            ).where(breached_predicate)
        )
    ).one()

    return RefundTally(
        by_status=by_status,
        breached=int(breached or 0),
        owed=money(owed or 0),
    )


@router.get("/payments/count", response_model=dict[str, int], responses=ADMIN_RESPONSES)
async def count_payments(session: SessionDep):
    """How many attempts sit in each status, in one round trip.

    A rail needs five counts, and five list calls with `limit=1` to read five
    totals is five round trips for numbers Postgres can group in one.
    """
    rows = await session.execute(
        select(Payment.status, func.count(Payment.id)).group_by(Payment.status)
    )
    counts = {status.value: 0 for status in PaymentStatus}
    for status, count in rows.all():
        counts[status.value] = int(count)
    return counts
