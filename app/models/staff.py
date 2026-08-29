from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import StaffRole
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class RestaurantStaff(Base, TimestampMixin):
    """Which people may act for which restaurant.

    This table is the authorization boundary for the partner app. Supabase Auth
    says who someone is; this says what they are allowed to touch. A UI filter
    is not a substitute — it is bypassed by editing an id in the URL.
    """

    __tablename__ = "restaurant_staff"
    __table_args__ = (
        UniqueConstraint("user_id", "restaurant_id", name="uq_staff_user_restaurant"),
        Index("ix_restaurant_staff_restaurant", "restaurant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE")
    )
    role: Mapped[StaffRole] = mapped_column(pg_enum(StaffRole, "staff_role"))
    # Revoking access without losing the record of who once had it.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Extra permissions granted to THIS person on top of what their role already
    # carries. A role is the floor; this is the short list a manager may add to
    # it, and it exists because "may this shift worker turn an order away" is not
    # a role question — one kitchen says yes and the next says no, for the same
    # role. The API refuses any value outside its grantable set, so a row edited
    # by hand cannot widen anybody past what a manager could have granted.
    #
    # An array rather than a join table: it is read on every scope check and is
    # never queried across restaurants, so a second round trip would buy nothing.
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), server_default=text("'{}'::varchar[]"), default=list
    )

    # When this person's sessions were last invalidated and a fresh sign-in link
    # sent. No link is stored — only the fact and the time, which is all a
    # manager standing at a tablet can act on.
    access_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
