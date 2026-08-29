from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ActorType, OrderStatus
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        # A pricing bug becomes an insert failure instead of a wrong charge.
        CheckConstraint(
            "total_amount = subtotal + packaging_fee + delivery_fee"
            " + tax_amount - discount_amount",
            name="ck_orders_total_reconciles",
        ),
        CheckConstraint("discount_amount <= subtotal", name="ck_orders_discount_bounded"),
        Index("ix_orders_user_placed", "user_id", "placed_at"),
        Index("ix_orders_restaurant_status", "restaurant_id", "status"),
        Index(
            "ix_orders_live",
            "status",
            postgresql_where="status NOT IN ('delivered', 'cancelled')",
        ),
        # placed_at ALONE. ix_orders_user_placed cannot serve a bare
        # `placed_at >= :start AND placed_at < :end` because placed_at is not its
        # leading column -- and that predicate is the dominant one across the
        # whole reporting layer: every /admin/reports/*, /admin/metrics/*,
        # admin_insights.workload (read on every operator page load), the
        # settlement window and the partner's own reports. All of them were
        # sequential scans.
        Index("ix_orders_placed_at", "placed_at"),
        # Serves admin_insights' "orders_late" and AdminOrderSort.OLDEST_PROMISE.
        Index("ix_orders_promised_at", "promised_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # A double-tap on a slow network must not buy dinner twice. Unique, so the
    # database refuses the duplicate even if two requests race past the lookup.
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="RESTRICT")
    )
    # index=True on every FK below: Postgres does NOT index a foreign key for
    # you, and an unindexed FK makes both the referential-integrity check and
    # every join over it a sequential scan. DELETE /addresses/{id} scanned
    # `orders` twice -- once for its own guard, once for the RESTRICT trigger.
    address_id: Mapped[int] = mapped_column(
        ForeignKey("addresses.id", ondelete="RESTRICT"), index=True
    )
    coupon_id: Mapped[int | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"),
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
    )

    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    packaging_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    distance_km: Mapped[Decimal] = mapped_column(Numeric(4, 1))

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Frozen policy: free cancellation before this instant, fee after.
    cancellable_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    promised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    # "Leave it at the gate." Written once at placement and never edited: it is
    # what the customer asked for at the time, and the kitchen and whoever
    # delivers both read it. Order-level on purpose — order_items already has a
    # per-dish note, and "ring the bell twice" belongs to no dish.
    delivery_note: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Left lazy on purpose: list endpoints never touch it, and detail endpoints
    # ask for it with selectinload. Eager-by-default would add a query to every
    # order listing for data nobody read.
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    """A line on an order.

    item_name and unit_price are snapshots, not joins. Menus change; a receipt
    must not. menu_item_id is kept for analytics, never for display.
    """

    __tablename__ = "order_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_order_items_quantity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    menu_item_id: Mapped[int] = mapped_column(
        ForeignKey("menu_items.id", ondelete="RESTRICT"), index=True
    )
    item_name: Mapped[str] = mapped_column(String(160))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    quantity: Mapped[int] = mapped_column(Integer)
    line_total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    order: Mapped["Order"] = relationship("Order", back_populates="items")


class OrderStatusEvent(Base):
    """Append-only status history.

    orders.status answers "where is it now". This answers "when was it
    confirmed" and "why was it cancelled" — the questions support actually gets.
    """

    __tablename__ = "order_status_events"
    __table_args__ = (
        Index("ix_order_status_events_order_time", "order_id", "created_at"),
        # The performance report aggregates every READY_FOR_PICKUP and every
        # CONFIRMED event for the ENTIRE platform, then joins to one restaurant's
        # windowed orders -- so its cost was unrelated to the window asked for, on
        # the fastest-growing table in the schema (~5 rows per order, forever).
        # The existing index leads with order_id and cannot serve a to_status
        # predicate. INCLUDE(created_at) makes the min() an index-only scan.
        Index(
            "ix_order_status_events_status_order",
            "to_status",
            "order_id",
            postgresql_include=["created_at"],
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    from_status: Mapped[OrderStatus | None] = mapped_column(pg_enum(OrderStatus, "order_status"))
    to_status: Mapped[OrderStatus] = mapped_column(pg_enum(OrderStatus, "order_status"))
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"))
    actor_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
