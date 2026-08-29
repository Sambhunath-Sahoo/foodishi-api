"""What a customer thought of an order, and the kitchen's one answer."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

MIN_STARS = 1
MAX_STARS = 5
COMMENT_MAX = 1000
REPLY_MAX = 1000


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # No default. A review with no score is not a review, and defaulting to 5
    # would quietly inflate every restaurant's rating with unrated orders.
    stars: int = Field(ge=MIN_STARS, le=MAX_STARS)
    comment: str | None = Field(default=None, max_length=COMMENT_MAX)
    # Optional and separate, so "the food was great, it arrived cold" is
    # sayable. Null means the customer did not answer that part — which is not
    # the same as a zero, and is why these are nullable rather than defaulted.
    food_rating: Decimal | None = Field(
        default=None, ge=MIN_STARS, le=MAX_STARS, max_digits=2, decimal_places=1
    )
    delivery_rating: Decimal | None = Field(
        default=None, ge=MIN_STARS, le=MAX_STARS, max_digits=2, decimal_places=1
    )


class ReviewReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reply: str = Field(min_length=1, max_length=REPLY_MAX)


class ReviewRead(BaseModel):
    """A review as anybody may read it — including the public.

    THE IMPORTANT THING IN THIS FILE: the reviewer is a display name and
    nothing else. No email, no phone, no user id. GET
    /restaurants/{id}/reviews is unauthenticated, so any field added here is
    published; a user id alone would let anyone correlate one person's reviews
    across every restaurant on the platform, and an email would turn a review
    list into a scrapeable customer directory.

    `model_config` is deliberately NOT from_attributes: this is assembled by
    hand in the router from a Review plus a name, precisely so a column added
    to the model later cannot appear here by accident.
    """

    id: int
    order_id: int
    restaurant_id: int
    stars: int
    comment: str | None
    food_rating: Decimal | None
    delivery_rating: Decimal | None
    #: The kitchen's answer, once. Null until they write one.
    reply: str | None
    replied_at: datetime | None
    created_at: datetime
    #: First name only where one can be taken — "Aarav M." rather than the full
    #: name, because a review list is public and a full name plus a
    #: neighbourhood is more identifying than anybody expects a star rating to be.
    reviewer_name: str
