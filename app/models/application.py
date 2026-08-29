"""A restaurant asking to join, before there is a restaurant.

This table is the one place in the schema where a row describes a kitchen that
does not exist yet. Everything else about a restaurant hangs off
`restaurants.id`, and that id is what an approval mints — so an application
carries its own copy of the details rather than a foreign key to them.

Why a copy and not a dormant `restaurants` row with a status column: a
restaurants row is reachable by every catalog query, every scope check and every
report on the platform. Making "not accepted yet" one more value those all have
to exclude means every one of them is a place to forget it, and the first
forgotten one publishes a kitchen nobody approved. A separate table cannot be
forgotten by a query that never names it.
"""

from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import ApplicationStatus
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class RestaurantApplication(Base, TimestampMixin):
    """One request to put a restaurant on Foodishi, and how it was answered."""

    __tablename__ = "restaurant_applications"
    __table_args__ = (
        # One open application per person. Partial, so the constraint binds only
        # while an application is pending: a rejected applicant may apply again,
        # and an approved one may apply for a second restaurant, but nobody can
        # sit in the operator's queue twice at once — which is what a
        # double-tapped submit button and a bored applicant both produce.
        Index(
            "uq_one_pending_application_per_user",
            "applicant_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The queue reads "pending, oldest first" on every load.
        Index("ix_restaurant_applications_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Who applied. Their name, email and phone live on the users row and are
    # NOT copied here: the operator reviewing this needs the current address to
    # write back to, not the one that was true on the day of the application.
    applicant_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    status: Mapped[ApplicationStatus] = mapped_column(
        pg_enum(ApplicationStatus, "application_status"),
        default=ApplicationStatus.PENDING,
        server_default=text("'pending'"),
    )

    # ---- The restaurant as proposed -------------------------------------
    #
    # Every column the `restaurants` table requires, so an approval needs no
    # further input from anybody. The two `restaurants` columns deliberately
    # absent are the ones an applicant does not get to declare: `rating` is
    # earned from customers, and `commission_percent` is Foodishi's side of a
    # commercial deal.
    #
    # `slug` is NOT unique here. Two applicants may propose the same one and
    # both rows are legitimate until one is approved; uniqueness belongs to
    # `restaurants.slug`, where it already exists, and the approval reports the
    # clash. Enforcing it here as well would refuse the second applicant for
    # something the first one had not been granted yet.
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(180))
    description: Mapped[str | None] = mapped_column(Text)

    city: Mapped[str] = mapped_column(String(60))
    area: Mapped[str] = mapped_column(String(80))
    address_line: Mapped[str] = mapped_column(String(240))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    phone: Mapped[str] = mapped_column(String(20))

    price_for_two: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    avg_prep_minutes: Mapped[int] = mapped_column(Integer)
    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)

    # Anything the applicant wants an operator to know that no column asks for
    # — "we already deliver for two other platforms", "opening in March".
    note: Mapped[str | None] = mapped_column(Text)

    # ---- How it was answered --------------------------------------------

    # The operator who decided, by users.id rather than platform_staff.id: a
    # person can leave Foodishi and lose their platform row, and the record of
    # who approved a restaurant must outlive their employment. SET NULL rather
    # than CASCADE for the same reason — deleting the reviewer must not delete
    # the decision.
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Why. Required by the API when rejecting and optional when approving,
    # because a refusal the applicant cannot act on is worse than no answer:
    # they resubmit the same thing and wait again.
    decision_note: Mapped[str | None] = mapped_column(Text)

    # What the approval created. Nullable because a pending or rejected
    # application never had one, and SET NULL because a restaurant that is
    # later deleted must not take its own origin story with it.
    restaurant_id: Mapped[int | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="SET NULL")
    )
