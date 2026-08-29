from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import CouponScope, DiscountType
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class Coupon(Base, TimestampMixin):
    __tablename__ = "coupons"
    __table_args__ = (
        CheckConstraint("valid_until > valid_from", name="ck_coupons_valid_range"),
        # A percent coupon with no cap is an unbounded liability.
        CheckConstraint(
            "discount_type <> 'percent' OR max_discount_amount IS NOT NULL",
            name="ck_coupons_percent_needs_cap",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    description: Mapped[str] = mapped_column(Text)

    discount_type: Mapped[DiscountType] = mapped_column(pg_enum(DiscountType, "discount_type"))
    discount_value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    min_order_value: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    scope: Mapped[CouponScope] = mapped_column(pg_enum(CouponScope, "coupon_scope"))
    restaurant_id: Mapped[int | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    cuisine_id: Mapped[int | None] = mapped_column(
        ForeignKey("cuisines.id", ondelete="CASCADE"), index=True
    )

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usage_limit_total: Mapped[int | None] = mapped_column(Integer)
    usage_limit_per_user: Mapped[int] = mapped_column(Integer, default=1)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CouponRedemption(Base):
    """One row per successful application. Doubles as the per-user limit
    counter and the fraud trail.
    """

    __tablename__ = "coupon_redemptions"
    __table_args__ = (Index("ix_coupon_redemptions_coupon_user", "coupon_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id", ondelete="RESTRICT"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True
    )
    discount_applied: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
