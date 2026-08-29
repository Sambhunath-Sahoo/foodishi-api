import random
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon
from app.models.enums import CouponScope, DiscountType
from app.services.money import money


async def build(session: AsyncSession, rng: random.Random, restaurant_ids: list[int],
                cuisine_ids: list[int]) -> list[Coupon]:
    """Ten coupons chosen to cover every branch of services.coupons.evaluate,
    including the two that must be refused."""
    now = datetime.now(UTC)
    specs = [
        # code, type, value, cap, min order, scope, valid, limits, active
        ("WELCOME50", DiscountType.FLAT, 50, None, 199, CouponScope.GLOBAL, (-30, 60), (None, 1), True),
        ("FOODISHI20", DiscountType.PERCENT, 20, 120, 299, CouponScope.GLOBAL, (-20, 40), (5000, 3), True),
        ("BIGSAVE", DiscountType.PERCENT, 30, 200, 599, CouponScope.GLOBAL, (-10, 30), (2000, 2), True),
        ("FLAT100", DiscountType.FLAT, 100, None, 499, CouponScope.GLOBAL, (-15, 45), (1000, 1), True),
        ("FIRSTBITE", DiscountType.FLAT, 75, None, 249, CouponScope.GLOBAL, (-60, 90), (None, 1), True),
        ("HOUSE15", DiscountType.PERCENT, 15, 150, 349, CouponScope.RESTAURANT, (-25, 35), (500, 2), True),
        ("BIRYANI10", DiscountType.PERCENT, 10, 80, 299, CouponScope.CUISINE, (-25, 35), (800, 3), True),
        ("LATENIGHT", DiscountType.FLAT, 60, None, 399, CouponScope.GLOBAL, (-5, 25), (300, 1), True),
        # Deliberately refusable — the two the acceptance checks target.
        ("EXPIRED25", DiscountType.PERCENT, 25, 150, 299, CouponScope.GLOBAL, (-90, -10), (1000, 2), True),
        ("SOLDOUT", DiscountType.FLAT, 150, None, 599, CouponScope.GLOBAL, (-30, 30), (200, 1), True),
    ]

    coupons = []
    for code, dtype, value, cap, min_order, scope, (start, end), (total, per_user), active in specs:
        coupon = Coupon(
            code=code,
            description=f"{code} promotional offer",
            discount_type=dtype,
            discount_value=money(value),
            max_discount_amount=money(cap) if cap is not None else None,
            min_order_value=money(min_order),
            scope=scope,
            restaurant_id=rng.choice(restaurant_ids) if scope == CouponScope.RESTAURANT else None,
            cuisine_id=rng.choice(cuisine_ids) if scope == CouponScope.CUISINE else None,
            valid_from=now + timedelta(days=start),
            valid_until=now + timedelta(days=end),
            usage_limit_total=total,
            usage_limit_per_user=per_user,
            # SOLDOUT starts at its limit so "coupon exhausted" has real data.
            times_used=total if code == "SOLDOUT" else rng.randint(0, 40),
            is_active=active,
        )
        session.add(coupon)
        coupons.append(coupon)

    await session.flush()
    return coupons
