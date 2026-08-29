from sqlalchemy import Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import PlatformRole
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class PlatformStaff(Base, TimestampMixin):
    """Which people may act for Foodishi itself, rather than for one restaurant.

    The second authorization boundary in the system, and deliberately shaped
    like the first. restaurant_staff answers "may this person act for this
    kitchen"; this answers "may this person act for the platform". Supabase Auth
    still only says who someone is.

    A row here is the whole difference between the partner app and the
    operations console. Without one, the owner of twenty restaurants is still
    only the owner of twenty restaurants — the coupon that comes out of Foodishi's
    own margin, the refund adjudicated against a kitchen, and the platform-wide
    order board all sit on this side of the line. Before this table existed the
    console admitted anyone who could sign in at all.
    """

    __tablename__ = "platform_staff"
    __table_args__ = (
        # One row per person. Unlike restaurant_staff there is no second axis to
        # be a member of, so two rows would be two answers to one question.
        # The unique index this creates is also the lookup index the dependency
        # uses, which is why the column does not declare its own.
        UniqueConstraint("user_id", name="uq_platform_staff_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[PlatformRole] = mapped_column(pg_enum(PlatformRole, "platform_role"))
    # Revoking access without losing the record of who once had it — the same
    # reason restaurant_staff carries this instead of deleting the row.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
