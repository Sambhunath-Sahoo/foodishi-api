"""What a customer thought of an order.

Tied to an order, not to a restaurant: you rate what you actually ate, and that
is also what stops one account rating a kitchen fifty times. `order_id` is
unique for exactly that reason.

The restaurant's own `rating` / `rating_count` columns stay the platform's
rolling aggregate. This table is the evidence behind them, and it is what lets a
kitchen read the sentence a customer wrote rather than only the number.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin


class Review(Base, TimestampMixin):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_review_order"),
        # One to five. Enforced in the database as well as the schema: a rating
        # of 0 or 9 would silently poison the restaurant's average, and a
        # constraint is the only thing that stops a bad backfill doing it.
        CheckConstraint("stars >= 1 and stars <= 5", name="ck_review_stars"),
        Index("ix_review_restaurant", "restaurant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: Denormalised from the order so a restaurant's reviews are one index scan
    #: rather than a join through every order it has ever cooked.
    #:
    #: No index=True: ix_review_restaurant above is (restaurant_id, created_at)
    #: and restaurant_id is its leading column, so it already serves every lookup
    #: a single-column index would. The two coexisted, costing two index writes
    #: per row for one index's worth of reads.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE")
    )

    stars: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    #: Optional separate marks, so "the food was great, it arrived cold" is
    #: sayable. Null means the customer did not answer that part.
    food_rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    delivery_rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))

    #: A kitchen may answer once, publicly. Null means they have not.
    reply: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
