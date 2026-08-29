"""Customer reviews on delivered orders.

`reviews` was empty, so `GET /restaurants/{id}/reviews` returned nothing, the
partner console's reviews screen had no rows, and — more quietly —
`restaurants.rating` and `rating_count` were seeded as random numbers with no
reviews behind them, so the average on a restaurant card disagreed with the
reviews listed under it by construction.

This phase fixes both: it writes the reviews AND recomputes each restaurant's
rating from them, using the same "recompute, never nudge" rule as
`app/routers/reviews.py:_refresh_restaurant_rating`. Two places must not
implement that average differently, so the arithmetic here is deliberately the
same shape: a full aggregate over the rows, quantized to one decimal place.
"""

import logging
import random
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Restaurant
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.review import Review

logger = logging.getLogger(__name__)

#: One decimal place, matching restaurants.rating (Numeric(3, 1)) and the
#: RATING_PLACES the reviews router quantizes to.
RATING_PLACES = Decimal("0.1")

#: What share of delivered orders get reviewed. Real-world review rates are far
#: lower than this; a higher share is chosen so five restaurants still end up
#: with enough rows for a list to page and an average to be meaningful.
REVIEW_SHARE = 0.45

#: Stars are weighted, not uniform. A flat 1-to-5 spread gives every restaurant
#: an average near 3.0, which makes the rating column useless for sorting and
#: hides any bug in it. This leans positive the way delivery reviews actually do,
#: while still producing enough 1s and 2s for the low-rating paths to have data.
STAR_MIX = [5] * 9 + [4] * 7 + [3] * 3 + [2] * 2 + [1] * 1

#: Keyed by stars, so a comment never contradicts the score beside it — the kind
#: of detail that makes a seeded screen unusable for judging a design.
COMMENTS: dict[int, tuple[str, ...]] = {
    5: (
        "Hot, on time, and packed properly. No notes.",
        "Best biryani I have ordered on here. Rider called before arriving.",
        "Portions are generous and the packaging did not leak at all.",
    ),
    4: (
        "Food was very good, arrived about ten minutes late.",
        "Tasty, though the gravy was milder than I expected.",
        "Good value. Would have liked more raita.",
    ),
    3: (
        "Fine but nothing special. Arrived warm rather than hot.",
        "Order was correct, delivery took a while.",
    ),
    2: (
        "One item was missing and the rest had gone cold.",
        "Took over an hour and the packaging had spilled.",
    ),
    1: (
        "Completely wrong order and nobody picked up the support call.",
    ),
}

#: A kitchen replies to some reviews and not others, so both states render.
#: Weighted towards the low scores, because that is who actually gets a reply.
REPLY_SHARE = {5: 0.10, 4: 0.15, 3: 0.35, 2: 0.70, 1: 0.90}

REPLIES: tuple[str, ...] = (
    "Thank you for the kind words — we will pass this to the kitchen.",
    "Sorry about this. We have spoken to the rider and would like to make it right.",
    "Apologies for the missing item. Support has been asked to refund it.",
    "Thanks for the feedback, we are looking at our packaging this week.",
)


async def build(session: AsyncSession, rng: random.Random) -> dict[str, int]:
    """Review a share of delivered orders, then recompute every rating."""
    delivered = list(
        await session.scalars(
            select(Order)
            .where(Order.status == OrderStatus.DELIVERED)
            .order_by(Order.id)
        )
    )
    if not delivered:
        logger.warning("No delivered orders — skipping reviews.")
        return {"reviews": 0, "replies": 0, "ratings_recomputed": 0}

    written = replies = 0
    for order in delivered:
        if rng.random() > REVIEW_SHARE:
            continue

        stars = rng.choice(STAR_MIX)
        # Sub-scores straddle the overall score, so "great food, cold on arrival"
        # is expressible — which is the whole reason the two columns exist.
        food = min(5, max(1, stars + rng.choice([0, 0, 0, 1, -1])))
        delivery = min(5, max(1, stars + rng.choice([0, 0, -1, -1, 1])))

        review = Review(
            order_id=order.id,
            user_id=order.user_id,
            # Denormalised from the order, exactly as the model intends.
            restaurant_id=order.restaurant_id,
            stars=stars,
            comment=rng.choice(COMMENTS[stars]),
            food_rating=Decimal(str(food)),
            delivery_rating=Decimal(str(delivery)),
        )

        if rng.random() < REPLY_SHARE[stars]:
            review.reply = rng.choice(REPLIES)
            # Replied after the order was delivered, never before it.
            review.replied_at = order.delivered_at or order.placed_at
            replies += 1

        session.add(review)
        written += 1

    await session.flush()
    recomputed = await _refresh_ratings(session)
    await session.flush()
    return {"reviews": written, "replies": replies, "ratings_recomputed": recomputed}


async def _refresh_ratings(session: AsyncSession) -> int:
    """Recompute restaurants.rating and rating_count from the rows just written.

    RECOMPUTED, never nudged — the same rule as
    app/routers/reviews.py:_refresh_restaurant_rating, and for the same reason: a
    stored aggregate that is incremented can drift from the rows behind it, and
    then the number on the card and the reviews under it disagree with nothing to
    say which is right.

    A restaurant with no reviews is left at 0.0/0 rather than keeping the random
    seeded value it used to carry. "No reviews yet" is a real state with its own
    empty state, and a 4.3 with nothing behind it is worse than an honest zero.
    """
    rows = await session.execute(
        select(
            Review.restaurant_id,
            func.count(Review.id),
            func.avg(Review.stars),
        ).group_by(Review.restaurant_id)
    )
    tally = {
        restaurant_id: (int(count), average)
        for restaurant_id, count, average in rows.all()
    }

    updated = 0
    for restaurant in await session.scalars(select(Restaurant)):
        count, average = tally.get(restaurant.id, (0, None))
        restaurant.rating_count = count
        restaurant.rating = (
            Decimal(str(average)).quantize(RATING_PLACES)
            if average is not None
            else Decimal("0.0")
        )
        updated += 1
    return updated
