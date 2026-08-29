import enum
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import SettlementStatus


class EarningsSummary(BaseModel):
    """What one restaurant has earned over a window, and what is still owed.

    Every figure is Decimal, never float: these are the numbers a partner
    reconciles a bank statement against, and a binary float cannot hold 0.10.
    """

    model_config = ConfigDict(from_attributes=True)

    # Echoed back because both parameters are optional — a client that omitted
    # them needs to know which window it was actually given before it prints a
    # heading over these numbers.
    date_from: date
    date_to: date

    # DELIVERED orders only. Cancelled and in-flight baskets earn nothing, so
    # they are not in here — see app/services/settlements.compute_period.
    gross: Decimal
    commission: Decimal
    # The rate the commission above was struck at, so the screen can show the
    # working rather than a number the partner has to trust.
    commission_percent: Decimal
    # GST on the commission, not on the food. 18% service tax on Foodishi's cut.
    tax_on_commission: Decimal
    refunds: Decimal
    # gross - commission - tax - refunds: what the window earned.
    net: Decimal

    # settled and pending span the restaurant's WHOLE history, not the window
    # above, because "what am I owed" is never a question about one week. Earned
    # is not the same as paid: `net` is what the window produced, `settled` is
    # what has actually been stamped as paid out, and `pending` — statements cut
    # but not yet paid — is the only number the restaurant is really waiting on.
    settled: Decimal
    pending: Decimal


class SettlementRead(BaseModel):
    """One statement, exactly as it was frozen when the period was cut.

    A record that a payout is due or was made. `status = paid` means somebody
    stamped the row; no transfer is performed by this API and none is requested
    of a provider.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    restaurant_id: int
    #: Quotable on a support call.
    reference: str
    period_from: date
    period_to: date
    orders_count: int
    gross: Decimal
    commission: Decimal
    tax_on_commission: Decimal
    refunds: Decimal
    net: Decimal
    status: SettlementStatus
    paid_at: datetime | None
    #: Last four digits of the destination account and nothing more of it.
    account_last4: str | None
    created_at: datetime


class LedgerKind(enum.StrEnum):
    """What kind of thing a ledger line records.

    Not a database enum: no table stores it. The ledger is assembled from
    orders, refunds and settlements at read time, and this names which of them a
    row came from.
    """

    ORDER = "order"
    COMMISSION = "commission"
    REFUND = "refund"
    PAYOUT = "payout"


class LedgerEntry(BaseModel):
    """One line of the per-transaction trail behind the earnings figures."""

    model_config = ConfigDict(from_attributes=True)

    # "<kind>:<source row id>", e.g. "commission:412". A bare integer could not
    # be unique: a delivered order produces both an `order` line and a
    # `commission` line from the same orders.id, so the pair would collide and
    # any client keying a list by id would drop one of them. Prefixing with the
    # kind makes it unique and stable — the same order yields the same two ids
    # on every request, with no synthetic sequence to persist.
    id: str
    kind: LedgerKind
    occurred_at: datetime
    order_id: int | None
    settlement_id: int | None
    #: A human sentence, ready to render as-is.
    description: str
    # SIGNED, and the sign is the information: revenue is positive, and
    # commission, refunds and payouts are negative because each one is money
    # leaving the balance. Never send an absolute value with the direction
    # implied by `kind` — a client that ignores kind then reads a deduction as
    # income.
    amount: Decimal
