"""What one restaurant sold, and how well it cooked and delivered it.

Operational rather than financial: every figure here is the kitchen's own trade
— dishes out of the pass, promises kept — not what Foodishi owes it. The payout
side of the same period lives in app/schemas/finance.py and answers to a
restaurant admin; these answer to anyone on shift.

Revenue in this module means DELIVERED orders only, without exception. A basket
that was cancelled or is still being cooked has sold nothing, so it contributes
nothing to revenue or discount anywhere below.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class SalesDay(BaseModel):
    """One local calendar day of trade.

    Days with no trade are present at zero rather than omitted. A chart that
    skips them draws a straight line across a Monday the restaurant was open
    and sold nothing, which reads as "we did fine" instead of "we did nothing".
    """

    model_config = ConfigDict(from_attributes=True)

    date: date
    # Every order placed that day, whatever became of it, so the three counts
    # can be read together: orders - delivered - cancelled is what was still in
    # flight when the report was run.
    orders: int
    delivered: int
    cancelled: int
    # DELIVERED orders only, and Decimal rather than float because these are
    # rupees: a binary float cannot hold 0.10 and a day's takings that do not
    # add up is a support call.
    revenue: Decimal
    # Discount given away on those same delivered orders — the cost of the
    # coupons that produced the revenue beside it, which is the only way to
    # judge whether a promotion paid for itself.
    discount: Decimal


class PopularItem(BaseModel):
    """One dish's share of the period, counted over delivered orders only."""

    model_config = ConfigDict(from_attributes=True)

    menu_item_id: int
    #: Frozen at order time, from order_items.item_name — the name the dish
    #: actually sold under, which is what a receipt from that week says.
    name: str
    # Live values, read from the menu row as it stands now, so the report can
    # answer "this is selling and it is switched off". None on both means the
    # dish is no longer on the menu at all, which is NOT the same as
    # is_available=False: false is a dish the kitchen can turn back on today.
    category_name: str | None
    is_available: bool | None
    quantity: int
    #: Distinct orders the dish appeared on. Always <= quantity, and the gap
    #: between them is how many people bought more than one.
    orders: int
    revenue: Decimal


class PerformanceReport(BaseModel):
    """How well the kitchen kept its promises over the period.

    Every ratio and average is guarded: a period with no trade returns zeros
    rather than failing, because a restaurant's first week has to render.
    """

    model_config = ConfigDict(from_attributes=True)

    orders: int
    delivered: int
    cancelled: int
    # Cancelled orders that never reached `confirmed` — the kitchen turned them
    # away before accepting them. Counted separately from `cancelled` because
    # the two are different failures: a rejection is a busy kitchen saying no
    # up front, which costs the customer a few minutes, while a cancellation
    # after acceptance is a promise broken on food somebody was already waiting
    # for. A subset of `cancelled`, never added to it.
    rejected: int
    # Delivered on or before promised_at, over everything delivered. 0..1, and
    # 0.0 when nothing was delivered — read it beside `delivered` before
    # printing it, since "0% on time" and "nothing to be on time about" are the
    # same number here.
    on_time_rate: float
    # placed_at -> the first ready_for_pickup event on the order's trail.
    # Orders that never got there are excluded from the average rather than
    # counted as zero, which would drag the mean towards a kitchen that looks
    # faster the more orders it abandons.
    avg_prep_minutes: int
    #: placed_at -> delivered_at, so it spans the cooking as well as the ride.
    avg_delivery_minutes: int
    #: Delivered revenue over delivered orders — a basket that was actually
    #: paid for, not one that was merely placed.
    avg_order_value: Decimal
    # The platform's rolling aggregate off the restaurants row, not something
    # recomputed from reviews in the window: it is the number shown to
    # customers, so a partner reading their own report must see the same one.
    # It therefore spans the restaurant's whole history and ignores the period.
    rating: Decimal
    rating_count: int
