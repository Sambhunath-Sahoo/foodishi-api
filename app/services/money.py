from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def money(value: Decimal | int | float | str) -> Decimal:
    """Round to 2 decimal places, half-up.

    Every monetary value crosses this function exactly once before it reaches
    the database. ck_orders_total_reconciles compares the stored total against
    the stored parts, so the parts must already be rounded when they are summed
    -- rounding the sum instead lets it drift by a paisa and fails the CHECK.
    """
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)
