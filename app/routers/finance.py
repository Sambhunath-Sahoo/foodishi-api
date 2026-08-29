"""What a restaurant has earned, what came off it, and what has been paid.

**Nothing in this file moves money.** There is no payment gateway call, no bank
disbursement, no email. A settlement row records that an amount is due or was
paid, and `status` is moved by whoever runs the transfer — a person, for now.
That is worth stating at the top because every figure below reads like a
statement from a system that pays out, and it is not one yet. When a real
disbursement API arrives it writes to `restaurant_settlements` and this file
does not change.

The split from app/routers/reports.py is deliberate and is about audience, not
about tables. Reports are the numbers on the wall by the pass and a shift worker
reads them. This is the partner's contract with Foodishi — commission, GST, payouts
— so every route here is `admin_of_restaurant`. A cook has no business knowing
the platform's cut, and a manager needs it to reconcile a bank statement.
"""

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select

from app.core.errors import not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import FORBIDDEN, UNAUTHENTICATED
from app.dependencies.scope import admin_of_restaurant
from app.models.enums import OrderStatus, RefundStatus, SettlementStatus
from app.models.finance import Settlement
from app.models.order import Order
from app.models.payment import Refund

# One definition of a validated date window, imported rather than re-typed.
# A router importing a router is not lovely, but the alternative is two copies
# of the same bounds check and the same timezone constant, and those drift —
# which is how /earnings and /reports end up disagreeing about which orders fell
# in "last week". If a third caller ever needs it, this moves to a service.
from app.routers.reports import ReportWindow, report_window
from app.schemas.finance import (
    EarningsSummary,
    LedgerEntry,
    LedgerKind,
    SettlementRead,
)
from app.services.money import money
from app.services.settlements import (
    UnknownRestaurant,
    commission_for,
    commission_percent_for,
    compute_period,
)

logger = logging.getLogger(__name__)

# On the router, not the routes. There is nothing under this prefix a shift
# worker should see, so the guard being inherited by whatever gets added next is
# the desired behaviour rather than an omission waiting to happen.
router = APIRouter(
    prefix="/restaurants/{restaurant_id}",
    tags=["finance"],
    dependencies=[Depends(admin_of_restaurant)],
)

SCOPED = {**UNAUTHENTICATED, **FORBIDDEN}

# Only a paid settlement counts as settled. `processing` is money the platform
# has committed to sending and has not sent, which is precisely what a partner
# chasing a payment is asking about — so it belongs in `pending`, not `settled`.
SETTLED_STATUSES = (SettlementStatus.PAID,)

ZERO = Decimal("0.00")


@router.get(
    "/earnings",
    response_model=EarningsSummary,
    responses=SCOPED,
    summary="What this restaurant earned over a window, and what is still owed",
)
async def get_earnings(
    restaurant_id: int,
    session: SessionDep,
    window: Annotated[ReportWindow, Depends(report_window)],
):
    """The subtraction, written out.

    A partner opens this to answer one question — "why is the number in my bank
    smaller than the number on my till" — and the only useful answer is every
    deduction named separately. So commission and the GST on it are two fields
    rather than one netted figure, and `refunds` is shown even though it is not
    subtracted from `gross` (a refunded order was never delivered, so it was
    never in `gross`; it is money that moved and a period with a lot of it is
    worth looking at).

    `gross` starts from the order total, delivery fee included, because that is
    what the customer paid through the platform — so a statement reconciles
    against a single receipt. Netting the delivery fee out first would leave a
    restaurant unable to tie any of this to an order it can actually look at.
    """
    try:
        totals = await compute_period(
            session, restaurant_id, window.date_from, window.date_to
        )
    except UnknownRestaurant as exc:  # pragma: no cover - see UnknownRestaurant
        # admin_of_restaurant already 403s a caller with no membership, and the
        # membership FK cascades, so there is no reachable path here. Mapped
        # anyway rather than letting a LookupError become a 500.
        raise not_found("restaurant", restaurant_id) from exc

    # Across ALL periods, not just the window. "What am I owed" is a question
    # about the balance, not about the seven days currently on screen — a
    # manager filtering to today would otherwise see `pending` collapse to zero
    # and conclude they had been paid.
    settled, pending = await _settlement_balances(session, restaurant_id)

    return EarningsSummary(
        date_from=window.date_from,
        date_to=window.date_to,
        gross=totals.gross,
        commission=totals.commission,
        commission_percent=totals.commission_percent,
        tax_on_commission=totals.tax_on_commission,
        refunds=totals.refunds,
        net=totals.net,
        settled=settled,
        pending=pending,
    )


async def _settlement_balances(
    session: SessionDep, restaurant_id: int
) -> tuple[Decimal, Decimal]:
    """Paid and not-yet-paid, in one round trip rather than two.

    Grouped in SQL and summed here: two `select(func.sum(...))` calls would be
    two queries over the same index for one pair of numbers.
    """
    rows = await session.execute(
        select(Settlement.status, func.coalesce(func.sum(Settlement.net), 0))
        .where(Settlement.restaurant_id == restaurant_id)
        .group_by(Settlement.status)
    )
    settled = ZERO
    pending = ZERO
    for status, total in rows:
        if status in SETTLED_STATUSES:
            settled += money(total)
        else:
            pending += money(total)
    return settled, pending


@router.get(
    "/settlements",
    response_model=Page[SettlementRead],
    responses=SCOPED,
    summary="Every statement cut for this restaurant",
)
async def list_settlements(
    restaurant_id: int,
    session: SessionDep,
    page: PageDep,
    status: Annotated[
        SettlementStatus | None,
        Query(description="Only statements in this state."),
    ] = None,
):
    """Newest period first — the one a partner is waiting on is the one they came for."""
    statement: Select = select(Settlement).where(
        Settlement.restaurant_id == restaurant_id
    )
    if status is not None:
        statement = statement.where(Settlement.status == status)

    items, total = await paginate(
        session,
        statement.order_by(Settlement.period_from.desc(), Settlement.id.desc()),
        page,
    )
    return Page[SettlementRead](
        items=[SettlementRead.model_validate(row) for row in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/ledger",
    response_model=Page[LedgerEntry],
    responses=SCOPED,
    summary="The per-transaction trail behind the earnings figures",
)
async def get_ledger(
    restaurant_id: int,
    session: SessionDep,
    page: PageDep,
    window: Annotated[ReportWindow, Depends(report_window)],
):
    """One line per thing that happened, newest first.

    Assembled in Python rather than as a SQL UNION, and that is a considered
    choice: the four kinds come from three tables with different shapes, one of
    them (`commission`) has no table at all, and each needs a human sentence
    built from columns the others do not have. A UNION would need every branch
    padded to one column list and the description concatenated in SQL, which is
    harder to read and no faster at this size — the window is capped at 366 days
    of one restaurant's orders.

    It does mean the whole window is materialised before paging. That is bounded
    by the same cap, and the alternative — paging each source separately and
    merging — cannot produce a correct global ordering.
    """
    percent = await commission_percent_for(session, restaurant_id)
    entries: list[LedgerEntry] = []

    entries.extend(await _order_lines(session, restaurant_id, window, percent))
    entries.extend(await _refund_lines(session, restaurant_id, window))
    entries.extend(await _payout_lines(session, restaurant_id, window))

    # Newest first, with the id as a tiebreak so a page boundary is stable
    # between two requests — without it, two lines sharing a timestamp can swap
    # places and a client paging through sees one twice and misses another.
    entries.sort(key=lambda entry: (entry.occurred_at, entry.id), reverse=True)

    return Page[LedgerEntry](
        items=entries[page.offset : page.offset + page.limit],
        total=len(entries),
        limit=page.limit,
        offset=page.offset,
    )


async def _order_lines(
    session: SessionDep,
    restaurant_id: int,
    window: ReportWindow,
    percent: Decimal,
) -> list[LedgerEntry]:
    """A delivered order and the commission taken off it, as two lines.

    Two lines from one row on purpose. A single net figure would be impossible
    to check: a partner querying a charge needs to see the order total they can
    look up beside the deduction that was applied to it.

    Dated by `delivered_at` rather than `placed_at`, because that is when the
    money became the restaurant's. An order placed on the 31st and delivered
    after midnight belongs to the month it was earned in.
    """
    rows = await session.execute(
        select(Order.id, Order.total_amount, Order.delivered_at, Order.placed_at)
        .where(
            Order.restaurant_id == restaurant_id,
            Order.status == OrderStatus.DELIVERED,
            Order.placed_at >= window.start,
            Order.placed_at < window.end,
        )
        .order_by(Order.id)
    )

    lines: list[LedgerEntry] = []
    for order_id, total_amount, delivered_at, placed_at in rows:
        # delivered_at is set whenever status is DELIVERED, but a row hand-edited
        # or migrated could disagree; falling back keeps the line dated rather
        # than dropping it or raising.
        at = delivered_at or placed_at
        commission = commission_for(total_amount, percent)
        lines.append(
            LedgerEntry(
                id=f"{LedgerKind.ORDER}:{order_id}",
                kind=LedgerKind.ORDER,
                occurred_at=at,
                order_id=order_id,
                settlement_id=None,
                description=f"Order #{order_id} delivered",
                amount=money(total_amount),
            )
        )
        lines.append(
            LedgerEntry(
                id=f"{LedgerKind.COMMISSION}:{order_id}",
                kind=LedgerKind.COMMISSION,
                occurred_at=at,
                order_id=order_id,
                settlement_id=None,
                description=f"Platform commission on #{order_id} at {percent}%",
                amount=-commission,
            )
        )
    return lines


async def _refund_lines(
    session: SessionDep, restaurant_id: int, window: ReportWindow
) -> list[LedgerEntry]:
    """Money that went back to a customer.

    Completed refunds only. An initiated refund is an intention — showing it as
    a deduction would understate a balance that has not moved yet, and the
    partner would chase a figure that was never taken.

    Dated by `completed_at`, for the same reason order lines are dated by
    delivery: that is when it left.
    """
    rows = await session.execute(
        select(
            Refund.id,
            Refund.order_id,
            Refund.amount,
            Refund.completed_at,
            Refund.reason,
        )
        .join(Order, Order.id == Refund.order_id)
        .where(
            Order.restaurant_id == restaurant_id,
            Refund.status == RefundStatus.COMPLETED,
            Refund.completed_at.is_not(None),
            Refund.completed_at >= window.start,
            Refund.completed_at < window.end,
        )
        .order_by(Refund.id)
    )
    return [
        LedgerEntry(
            id=f"{LedgerKind.REFUND}:{refund_id}",
            kind=LedgerKind.REFUND,
            occurred_at=completed_at,
            order_id=order_id,
            settlement_id=None,
            description=f"Order #{order_id} refunded — {str(reason).replace('_', ' ')}",
            amount=-money(amount),
        )
        for refund_id, order_id, amount, completed_at, reason in rows
    ]


async def _payout_lines(
    session: SessionDep, restaurant_id: int, window: ReportWindow
) -> list[LedgerEntry]:
    """A statement that has been paid.

    Negative, which reads oddly until you see what the column is: this is the
    running balance of what Foodishi owes the restaurant, and paying it out reduces
    that balance to zero. The order lines above are what built it up.

    Unpaid statements are absent by design — nothing has happened yet, and
    `pending` on /earnings is where they are accounted for.
    """
    rows = await session.execute(
        select(
            Settlement.id,
            Settlement.reference,
            Settlement.net,
            Settlement.paid_at,
            Settlement.account_last4,
        )
        .where(
            Settlement.restaurant_id == restaurant_id,
            Settlement.status == SettlementStatus.PAID,
            Settlement.paid_at.is_not(None),
            Settlement.paid_at >= window.start,
            Settlement.paid_at < window.end,
        )
        .order_by(Settlement.id)
    )
    return [
        LedgerEntry(
            id=f"{LedgerKind.PAYOUT}:{settlement_id}",
            kind=LedgerKind.PAYOUT,
            occurred_at=paid_at,
            order_id=None,
            settlement_id=settlement_id,
            description=(
                f"Paid out · {reference}"
                + (f" · account ending {account_last4}" if account_last4 else "")
            ),
            amount=-money(net),
        )
        for settlement_id, reference, net, paid_at, account_last4 in rows
    ]
