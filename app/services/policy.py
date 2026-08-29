from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.enums import OrderStatus
from app.services.money import money

# Once food is on its way, cancelling is a support decision, not a self-serve one.
CANCELLABLE_STATUSES = frozenset(
    {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING}
)


@dataclass(frozen=True)
class CancellationOutcome:
    allowed: bool
    within_window: bool
    fee: Decimal
    refund_amount: Decimal
    reason: str | None = None


def evaluate_cancellation(
    *,
    status: OrderStatus,
    cancellable_until: datetime,
    total_amount: Decimal,
    captured_amount: Decimal,
    refunded_amount: Decimal,
    cancellation_fee_percent: Decimal,
    now: datetime,
) -> CancellationOutcome:
    """Decide whether an order can be cancelled and what comes back.

    cancellable_until is read from the *order*, never recomputed from the
    restaurant's current policy. A policy edited tomorrow must not change what
    a customer was promised today.
    """
    zero = money(0)

    if status == OrderStatus.CANCELLED:
        return CancellationOutcome(False, False, zero, zero, "Order is already cancelled")
    if status == OrderStatus.DELIVERED:
        return CancellationOutcome(False, False, zero, zero, "Order has already been delivered")
    if status not in CANCELLABLE_STATUSES:
        return CancellationOutcome(
            False, False, zero, zero,
            f"Order is {status.value} and can no longer be cancelled",
        )

    # What is still ours to give back: collected, less anything already returned.
    # Computed before the fee so the fee cannot be charged against money that has
    # already gone back to the customer.
    headroom = money(max(money(captured_amount) - money(refunded_amount), zero))

    within_window = now <= cancellable_until
    if within_window:
        fee = zero
    else:
        fee = money(total_amount * cancellation_fee_percent / Decimal("100"))
        fee = min(fee, headroom)

    # Nothing captured means nothing to refund -- cash on delivery, or a payment
    # that never went through.
    #
    # refunded_amount is subtracted because captured_amount does NOT shrink when
    # a refund is booked: captured_total sums payments in CAPTURED and
    # PARTIALLY_REFUNDED at their full amount, and nothing in the API ever writes
    # PARTIALLY_REFUNDED anyway. Without this term a goodwill refund followed by
    # a cancellation refunds the full captured amount a second time -- 700 paid
    # back on a 500 order, no concurrency required. The refund route's own
    # headroom guard already counts prior refunds; this is the same arithmetic on
    # the path that was missing it.
    refund_amount = money(max(headroom - fee, zero))
    return CancellationOutcome(True, within_window, fee, refund_amount)


def refund_sla_due(initiated_at: datetime, refund_sla_hours: int) -> datetime:
    """Frozen at initiation, so 'is my refund late?' stays one comparison."""
    return initiated_at + timedelta(hours=refund_sla_hours)


def cancellable_until(placed_at: datetime, cancellation_window_mins: int) -> datetime:
    return placed_at + timedelta(minutes=cancellation_window_mins)
