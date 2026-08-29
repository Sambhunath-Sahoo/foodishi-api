from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models.coupon import Coupon
from app.models.enums import CouponScope, DiscountType
from app.services.money import money


@dataclass(frozen=True)
class CouponOutcome:
    applicable: bool
    discount: Decimal
    reason: str | None = None


def evaluate(
    coupon: Coupon,
    *,
    subtotal: Decimal,
    restaurant_id: int,
    cuisine_ids: set[int],
    user_redemption_count: int,
    now: datetime,
) -> CouponOutcome:
    """Seven checks. Missing any one of them is how coupons get abused, so they
    all live here rather than being spread across the endpoints that need them.
    """
    if not coupon.is_active:
        return CouponOutcome(False, Decimal("0"), "Coupon is not active")

    if now < coupon.valid_from:
        return CouponOutcome(False, Decimal("0"), "Coupon is not valid yet")
    if now > coupon.valid_until:
        return CouponOutcome(False, Decimal("0"), "Coupon has expired")

    if subtotal < coupon.min_order_value:
        return CouponOutcome(
            False, Decimal("0"),
            f"Order must be at least {coupon.min_order_value} to use this coupon",
        )

    if coupon.scope == CouponScope.RESTAURANT and coupon.restaurant_id != restaurant_id:
        return CouponOutcome(False, Decimal("0"), "Coupon does not apply to this restaurant")
    if coupon.scope == CouponScope.CUISINE and coupon.cuisine_id not in cuisine_ids:
        return CouponOutcome(False, Decimal("0"), "Coupon does not apply to this cuisine")

    if coupon.usage_limit_total is not None and coupon.times_used >= coupon.usage_limit_total:
        return CouponOutcome(False, Decimal("0"), "Coupon has reached its usage limit")

    if user_redemption_count >= coupon.usage_limit_per_user:
        return CouponOutcome(False, Decimal("0"), "You have already used this coupon")

    return CouponOutcome(True, _discount_for(coupon, subtotal))


def _discount_for(coupon: Coupon, subtotal: Decimal) -> Decimal:
    if coupon.discount_type == DiscountType.FLAT:
        raw = coupon.discount_value
    else:
        raw = subtotal * coupon.discount_value / Decimal("100")
        if coupon.max_discount_amount is not None:
            # An uncapped percentage is an unbounded liability. The schema
            # already refuses to store one; this enforces the cap it guarantees.
            raw = min(raw, coupon.max_discount_amount)

    # ck_orders_discount_bounded: discount can never exceed the subtotal.
    return money(min(raw, subtotal))
