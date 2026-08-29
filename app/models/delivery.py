from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import DeliveryStatus
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class DeliveryPartner(Base, TimestampMixin):
    __tablename__ = "delivery_partners"
    __table_args__ = (
        # Keeps the "claim the next free rider" select a one-row index scan.
        Index(
            "ix_delivery_partners_available",
            "id",
            postgresql_where="is_available",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20))
    vehicle_type: Mapped[str] = mapped_column(String(30))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base, TimestampMixin):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One delivery per order — enforced, not assumed.
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True
    )
    partner_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_partners.id", ondelete="RESTRICT"), index=True
    )
    distance_km: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    eta_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[DeliveryStatus] = mapped_column(pg_enum(DeliveryStatus, "delivery_status"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
