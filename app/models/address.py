from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin


class Address(Base, TimestampMixin):
    __tablename__ = "addresses"
    __table_args__ = (
        # Partial unique index: any number of addresses, at most one default.
        Index(
            "ix_addresses_one_default_per_user",
            "user_id",
            unique=True,
            postgresql_where="is_default",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(40))
    line1: Mapped[str] = mapped_column(String(240))
    line2: Mapped[str | None] = mapped_column(String(240))
    city: Mapped[str] = mapped_column(String(60))
    pincode: Mapped[str] = mapped_column(String(10))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
