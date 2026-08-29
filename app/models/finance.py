"""What a restaurant is owed, and what has been paid.

There is deliberately NO payment-gateway integration here. `POST /orders/{id}/
payments` already records a Payment row without calling a provider, and a
settlement in this table is a *record* that a payout is due or was made — not a
bank transfer. `status = paid` means a row was stamped by whoever runs the
payout, and `paid_at` is when they stamped it. Wiring a real disbursement API in
later changes this table not at all, which is the point of writing the ledger
down before the plumbing exists.

Every money figure is computed from captured payments and completed refunds at
the moment the settlement is cut, then FROZEN on the row. That matters: an order
refunded next week must not silently change what last week's statement said.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import SettlementStatus
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum


class Settlement(Base, TimestampMixin):
    """One period of trade, totalled and closed.

    The period is a pair of local calendar dates rather than instants: a
    restaurant reconciles a statement against days, and "the week of the 17th"
    has to mean the same thing to them as it does to us.
    """

    __tablename__ = "restaurant_settlements"
    __table_args__ = (
        # One statement per restaurant per period. Re-cutting the same week must
        # update the existing row, never add a second one that double-counts it.
        UniqueConstraint(
            "restaurant_id", "period_from", "period_to", name="uq_settlement_period"
        ),
        Index("ix_settlement_restaurant_period", "restaurant_id", "period_from"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    #: Human-quotable on a support call. Unique so it can be searched on.
    reference: Mapped[str] = mapped_column(String(40), unique=True)

    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)

    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    #: What customers paid through the platform on delivered orders in the
    #: period, delivery fee included — so a statement reconciles to a receipt.
    gross: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    commission: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_on_commission: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    #: Money that went back out to customers in the period.
    refunds: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    #: gross - commission - tax - refunds. Stored, not derived, so the statement
    #: cannot change under the restaurant after the fact.
    net: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))

    status: Mapped[SettlementStatus] = mapped_column(
        pg_enum(SettlementStatus, "settlement_status"),
        default=SettlementStatus.SCHEDULED,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The last four digits of the destination account and nothing more of it.
    #: Storing a full account number to render a statement line would be a
    #: liability with no upside.
    account_last4: Mapped[str | None] = mapped_column(String(4))
