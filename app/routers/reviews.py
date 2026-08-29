"""Ratings, and the restaurant's right to read and answer them.

Before this existed a rating lived in the customer's browser and the kitchen
never saw it, which made every "why has our score dropped" question
unanswerable. So the write is scoped hard to the person who ate the food, the
read is public because that is what a rating is for, and the restaurant gets
exactly one reply.

`readable_order` is NOT used on the write. It admits the order's customer OR
active staff of the restaurant cooking it, which is right for reading a ticket
and wrong here: it would let a restaurant's own staff five-star their own
kitchen. The ownership check on POST is therefore done inline against
`orders.user_id`, and that is the only place in this file that does not delegate.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    NO_PROFILE,
    UNAUTHENTICATED,
    CurrentUser,
)
from app.dependencies.ownership import readable_order
from app.dependencies.scope import staff_of_row
from app.models.catalog import Restaurant
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.review import Review
from app.models.user import User
from app.schemas.review import (
    MAX_STARS,
    MIN_STARS,
    ReviewCreate,
    ReviewRead,
    ReviewReply,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reviews"])

IDENTIFIED = {**UNAUTHENTICATED, **NO_PROFILE}
PERMITTED = {**IDENTIFIED, **FORBIDDEN}

UNIQUE_REVIEW = "uq_review_order"

#: Numeric(2,1) on the column, so the aggregate is rounded to match. Storing
#: 4.3333 into it would round on the way in anyway; doing it here means the
#: value we log and the value we stored are the same number.
RATING_PLACES = Decimal("0.1")


async def _review_restaurant(session: AsyncSession, review_id: int) -> int | None:
    return await session.scalar(
        select(Review.restaurant_id).where(Review.id == review_id)
    )


#: The reply route is keyed by review id, so the restaurant is one hop off the
#: path — the same shape as the menu-item routes, and the same factory.
#: STAFF rather than ADMIN: answering a customer is front-of-house work, and
#: making a manager the only person who can say "sorry, we'll do better" means
#: nobody says it.
staff_of_review = staff_of_row(
    path_param="review_id", resolve=_review_restaurant, resource="review"
)


def _display_name(name: str) -> str:
    """"Aarav Menon" -> "Aarav M." — enough to look human, not enough to trace.

    A public review list is scraped. A full name beside a restaurant in a known
    neighbourhood identifies somebody far more precisely than anyone expects a
    star rating to, so the surname is reduced to an initial. An empty or
    single-word name is returned as-is rather than mangled.
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return name.strip() or "A customer"
    return f"{parts[0]} {parts[-1][0]}."


def _to_read(review: Review, reviewer_name: str) -> ReviewRead:
    # Assembled field by field rather than model_validate: ReviewRead is public,
    # and an allowlist built by hand is what stops a column added to Review
    # later from being published by accident.
    return ReviewRead(
        id=review.id,
        order_id=review.order_id,
        restaurant_id=review.restaurant_id,
        stars=review.stars,
        comment=review.comment,
        food_rating=review.food_rating,
        delivery_rating=review.delivery_rating,
        reply=review.reply,
        replied_at=review.replied_at,
        created_at=review.created_at,
        reviewer_name=_display_name(reviewer_name),
    )


async def _refresh_restaurant_rating(
    session: AsyncSession, restaurant_id: int
) -> None:
    """Recompute the restaurant's rating from every review it has.

    RECOMPUTED, never nudged. An incremental average — new = old + (stars-old)/n
    — drifts with floating point, and worse, it cannot be repaired: once it is
    wrong there is no way to tell by how much. A full aggregate over one
    restaurant's reviews is one indexed scan and is always right.

    Note the seeded `rating` / `rating_count` predate this table, so the first
    real review replaces a hand-authored 4.4-from-1284 with 5.0-from-1. That is
    expected rather than a bug: the column now means what it says. Seeding
    review rows for the existing counts is the fix, and it is a seed change.
    """
    aggregate = await session.execute(
        select(func.count(Review.id), func.avg(Review.stars)).where(
            Review.restaurant_id == restaurant_id
        )
    )
    count, average = aggregate.one()
    restaurant = await session.get(Restaurant, restaurant_id)
    if restaurant is None:  # pragma: no cover - FK cascade makes this unreachable
        return
    restaurant.rating_count = int(count or 0)
    restaurant.rating = (
        Decimal(average).quantize(RATING_PLACES) if average is not None else Decimal("0.0")
    )


@router.post(
    "/orders/{order_id}/reviews",
    response_model=ReviewRead,
    status_code=status.HTTP_201_CREATED,
    responses={**IDENTIFIED, **NOT_FOUND, **CONFLICT},
    summary="Rate a delivered order (the customer who ordered it)",
)
async def create_review(
    order_id: int,
    payload: ReviewCreate,
    session: SessionDep,
    user: CurrentUser,
):
    """You rate what you actually ate.

    Two rules, and both are the point of the endpoint:

    * The order must be YOURS. Checked against orders.user_id here rather than
      through readable_order, which also admits the restaurant's own staff — see
      the module docstring.
    * The order must be DELIVERED. Rating a pending order rates an expectation,
      and a one-star on food nobody has tasted is not information. 422 with the
      status named, so a client can say why the button is disabled.
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise not_found("order", order_id)
    if order.user_id != user.id:
        # 404, not 403: whether somebody else's order exists is not this
        # caller's to learn, and ownership.py takes the same line.
        raise not_found("order", order_id)
    if order.status is not OrderStatus.DELIVERED:
        raise unprocessable(
            f"Order {order_id} is {order.status} — an order can only be reviewed "
            "once it has been delivered"
        )

    review = Review(
        order_id=order_id,
        user_id=user.id,
        restaurant_id=order.restaurant_id,
        stars=payload.stars,
        comment=payload.comment,
        food_rating=payload.food_rating,
        delivery_rating=payload.delivery_rating,
    )
    session.add(review)
    try:
        await session.flush()
    except IntegrityError as exc:
        if UNIQUE_REVIEW in str(exc.orig):
            raise conflict(
                f"Order {order_id} has already been reviewed. A review cannot be "
                "replaced — it is what you thought at the time."
            ) from exc
        raise
    # Same transaction as the insert, so the aggregate can never disagree with
    # the rows behind it.
    await _refresh_restaurant_rating(session, order.restaurant_id)
    await session.refresh(review)
    logger.info(
        "Review %s on order %s: %s stars for restaurant %s",
        review.id,
        order_id,
        payload.stars,
        order.restaurant_id,
    )
    return _to_read(review, user.name)


@router.get(
    "/orders/{order_id}/reviews",
    response_model=ReviewRead,
    dependencies=[Depends(readable_order)],
    responses={**PERMITTED, **NOT_FOUND},
    summary="The review on one order (its customer, or the kitchen)",
)
async def get_order_review(order_id: int, session: SessionDep):
    """readable_order is exactly right here: both parties may read it.

    The customer needs to see what they wrote, and the kitchen needs to see what
    was said about the order before it can answer.
    """
    review = await session.scalar(select(Review).where(Review.order_id == order_id))
    if review is None:
        raise not_found("review for order", order_id)
    name = await session.scalar(select(User.name).where(User.id == review.user_id))
    return _to_read(review, name or "")


@router.get(
    "/restaurants/{restaurant_id}/reviews",
    response_model=Page[ReviewRead],
    responses=NOT_FOUND,
    summary="A restaurant's reviews (public)",
)
async def list_restaurant_reviews(
    restaurant_id: int,
    session: SessionDep,
    page: PageDep,
    min_stars: Annotated[
        int | None,
        Query(ge=MIN_STARS, le=MAX_STARS, description="Only reviews at or above this."),
    ] = None,
):
    """Unauthenticated, because this is what a rating is for.

    Newest first: a restaurant that was bad two years ago and is good now
    deserves to be read in that order, and so does the opposite.
    """
    exists = await session.scalar(
        select(Restaurant.id).where(Restaurant.id == restaurant_id)
    )
    if exists is None:
        raise not_found("restaurant", restaurant_id)

    statement = select(Review).where(Review.restaurant_id == restaurant_id)
    if min_stars is not None:
        statement = statement.where(Review.stars >= min_stars)

    items, total = await paginate(
        session, statement.order_by(Review.created_at.desc(), Review.id.desc()), page
    )
    names = await _names_for(session, [review.user_id for review in items])
    return Page[ReviewRead](
        items=[_to_read(review, names.get(review.user_id, "")) for review in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


async def _names_for(session: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    """One query for the page rather than one per row."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(User.id, User.name).where(User.id.in_(set(user_ids)))
    )
    return {user_id: name for user_id, name in rows}


@router.post(
    "/reviews/{review_id}/reply",
    response_model=ReviewRead,
    dependencies=[Depends(staff_of_review)],
    responses={**PERMITTED, **NOT_FOUND, **CONFLICT},
    summary="Answer a review, once (the restaurant's staff)",
)
async def reply_to_review(
    review_id: int, payload: ReviewReply, session: SessionDep
):
    """One reply, and it cannot be edited afterwards.

    That is a deliberate decision, not a missing feature. The reply is public and
    a customer may already have read it; letting a restaurant quietly rewrite an
    apology into something else after the fact is the kind of thing a review
    system exists to prevent. If a reply is wrong, the answer is another
    channel — support — not a silent edit.
    """
    review = await session.get(Review, review_id)
    if review is None:
        raise not_found("review", review_id)
    if review.reply is not None:
        raise conflict(
            f"Review {review_id} has already been answered. A public reply cannot "
            "be edited once a customer may have read it."
        )

    review.reply = payload.reply
    review.replied_at = datetime.now(UTC)
    await session.flush()
    name = await session.scalar(select(User.name).where(User.id == review.user_id))
    logger.info("Restaurant %s replied to review %s", review.restaurant_id, review_id)
    return _to_read(review, name or "")
