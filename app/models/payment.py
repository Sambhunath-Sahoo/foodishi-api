from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import PaymentMethod, PaymentStatus, RefundReason, RefundStatus
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class Payment(Base, TimestampMixin):
    """One attempt to collect money. Many rows per order is normal — a failed
    UPI attempt followed by a successful card payment is two rows, and
    overwriting the first would erase the evidence of the failure.
    """

    __tablename__ = "payments"

    # At most one LIVE payment per order, enforced where it cannot be raced.
    #
    # create_payment checks for an existing authorized-or-captured row and then
    # inserts, which under READ COMMITTED is a check-then-act: a double-tap on a
    # slow phone had both requests see zero rows and both insert, then both
    # settle, so captured_total returned twice the order total and the
    # cancellation path refunded 1000 on a 500 order. A partial unique index is
    # the only thing that can refuse the second insert.
    #
    # Partial, not plain: failed attempts must stay: "a failed UPI attempt
    # followed by a successful card payment is two rows", per the docstring
    # above, and a plain unique on order_id would forbid the retry.
    __table_args__ = (
        Index(
            "uq_payments_one_live_per_order",
            "order_id",
            unique=True,
            postgresql_where=text(
                "status in ('authorized', 'captured', 'partially_refunded')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"))
    provider: Mapped[str] = mapped_column(String(40))
    provider_ref: Mapped[str | None] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default="INR")
    status: Mapped[PaymentStatus] = mapped_column(pg_enum(PaymentStatus, "payment_status"))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[str | None] = mapped_column(Text)


class Refund(Base, TimestampMixin):
    """A reversal against a payment. Separate table because partial and
    repeated refunds against one order are both normal.
    """

    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        # The refund SLA watch filters and orders on (status, sla_due_at) and the
        # table had only order_id, so GET /admin/finance/refunds scanned all of
        # `refunds`, sorted the whole table on a computed boolean, and kept 20
        # rows -- twice, because paginate counts over the same statement. Partial
        # so each index holds only the rows anybody queries.
        Index(
            "ix_refunds_outstanding_due",
            "sla_due_at",
            postgresql_where="status <> 'completed'",
        ),
        Index(
            "ix_refunds_completed_at",
            "completed_at",
            postgresql_where="status = 'completed'",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    reason: Mapped[RefundReason] = mapped_column(pg_enum(RefundReason, "refund_reason"))
    status: Mapped[RefundStatus] = mapped_column(pg_enum(RefundStatus, "refund_status"))
    # Frozen policy: initiated_at + restaurant_policies.refund_sla_hours.
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_ref: Mapped[str | None] = mapped_column(String(80))
