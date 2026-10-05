from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.catalog import MenuItem, Restaurant, RestaurantPolicy
from app.services import eta
from app.services.money import money

if TYPE_CHECKING:  # pragma: no cover - modifier_choices imports PricingError from here
    from app.services.modifier_choices import ChosenModifier

# GST on restaurant food delivery.
TAX_RATE = Decimal("0.05")


@dataclass(frozen=True)
class QuoteLine:
    menu_item_id: int
    item_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    #: The customer's answers for this line, in menu order. Their price_delta is
    #: already inside unit_price, so unit_price x quantity == line_total holds
    #: exactly as it did before options were priced.
    modifiers: tuple["ChosenModifier", ...] = ()


@dataclass(frozen=True)
class Quote:
    lines: tuple[QuoteLine, ...]
    subtotal: Decimal
    packaging_fee: Decimal
    delivery_fee: Decimal
    tax_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    distance_km: Decimal
    promised_at: datetime
    cancellable_until: datetime


class PricingError(ValueError):
    """A business-rule failure the caller should surface as 422."""


def quote(
    *,
    restaurant: Restaurant,
    policy: RestaurantPolicy,
    items: list[tuple[MenuItem, int]],
    latitude: Decimal,
    longitude: Decimal,
    discount: Decimal = Decimal("0"),
    placed_at: datetime,
    modifiers: Sequence[Sequence["ChosenModifier"]] | None = None,
) -> Quote:
    """The single source of truth for what an order costs.

    Pure: no database access, no writes. The API calls it to quote, calls it
    again to place, and the seeder calls it to build 800 historic orders. One
    implementation means the three can never disagree.

    `modifiers` runs parallel to `items` — entry i is line i's already-validated
    answers (services/modifier_choices.resolve). Optional, and keyword-only, so
    the seeder's calls are untouched and price exactly as they always did.
    """
    if not items:
        raise PricingError("An order must contain at least one item")
    if modifiers is not None and len(modifiers) != len(items):
        # A programming error, not a customer one — but a silent zip() would
        # price line 3's "Full plate" onto line 2.
        raise ValueError("modifiers must have one entry per item")

    lines = []
    for index, (menu_item, quantity) in enumerate(items):
        if quantity <= 0:
            raise PricingError(f"Quantity for {menu_item.name!r} must be positive")
        if menu_item.restaurant_id != restaurant.id:
            raise PricingError(f"{menu_item.name!r} is not on this restaurant's menu")
        if not menu_item.is_available:
            raise PricingError(f"{menu_item.name!r} is currently unavailable")
        chosen = tuple(modifiers[index]) if modifiers is not None else ()
        unit_price = _unit_price(menu_item, chosen)
        lines.append(
            QuoteLine(
                menu_item_id=menu_item.id,
                item_name=menu_item.name,
                unit_price=unit_price,
                quantity=quantity,
                line_total=money(unit_price * quantity),
                modifiers=chosen,
            )
        )

    subtotal = money(sum(line.line_total for line in lines))
    if subtotal < policy.min_order_value:
        raise PricingError(
            f"Minimum order value for this restaurant is {policy.min_order_value}"
        )

    # No delivery-radius rule and no real distance: see eta.nominal_* for why.
    # The address still seeds both, so two addresses price differently and one
    # address prices the same every time.
    seed = (restaurant.id, latitude, longitude)
    distance_km = eta.nominal_distance_km(*seed)
    eta_minutes = eta.nominal_eta_minutes(*seed)

    delivery_fee = _delivery_fee(policy, subtotal, distance_km)
    packaging_fee = money(policy.packaging_fee)

    # The discount is computed BEFORE the tax and reduces the taxable base: a
    # coupon is a reduction in the price of the supply, so GST is charged on what
    # the customer actually pays for the food, not on the list price. Taxing the
    # undiscounted subtotal instead overcharged 5% of every coupon -- 5.00 on a
    # 100.00 discount -- consistently enough that ck_orders_total_reconciles
    # still held, because that CHECK verifies the parts sum to the total and says
    # nothing about the tax base.
    #
    # packaging_fee and delivery_fee stay outside the base, unchanged.
    discount_amount = money(min(discount, subtotal))
    tax_amount = money((subtotal - discount_amount) * TAX_RATE)

    # Sum of already-rounded parts, so the stored total matches the stored
    # components exactly and ck_orders_total_reconciles holds.
    total_amount = money(
        subtotal + packaging_fee + delivery_fee + tax_amount - discount_amount
    )

    return Quote(
        lines=tuple(lines),
        subtotal=subtotal,
        packaging_fee=packaging_fee,
        delivery_fee=delivery_fee,
        tax_amount=tax_amount,
        discount_amount=discount_amount,
        total_amount=total_amount,
        distance_km=distance_km,
        promised_at=placed_at + timedelta(minutes=eta_minutes),
        cancellable_until=placed_at
        + timedelta(minutes=policy.cancellation_window_mins),
    )


def _delivery_fee(
    policy: RestaurantPolicy, subtotal: Decimal, distance_km: Decimal
) -> Decimal:
    if policy.free_delivery_above is not None and subtotal >= policy.free_delivery_above:
        return money(0)
    return money(policy.delivery_fee_base + policy.delivery_fee_per_km * distance_km)


def _unit_price(menu_item: MenuItem, chosen: Sequence["ChosenModifier"]) -> Decimal:
    """The dish price plus every answer's surcharge, per unit.

    Folded into unit_price rather than kept as a separate column because
    order_items has no column for it, and a receipt line whose unit price times
    quantity did not equal its total would be the first thing anyone audited.
    """
    return money(menu_item.price + sum((c.price_delta for c in chosen), Decimal("0")))
