"""Turning a customer's option ids into answers a kitchen can cook from.

Pure: the caller loads the dish's groups (repositories.orders.
load_modifier_groups) and this module only judges them, so the quote and the
placement apply one set of rules and cannot disagree about what is a legal cart.

Every refusal is a PricingError, which the routers surface as 422 — the same
answer as an unavailable dish, because it is the same kind of problem: the cart
asks for something the menu does not currently sell.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.models.catalog import MenuItem
from app.models.modifiers import MenuItemModifierGroup, MenuItemModifierOption
from app.services.money import money
from app.services.pricing import PricingError


@dataclass(frozen=True)
class ChosenModifier:
    """One answer, already copied into the shape order_item_modifiers stores.

    The names and price are taken NOW, so the row written at placement says
    what the menu said when the customer tapped it.
    """

    option_id: int
    group_name: str
    option_name: str
    price_delta: Decimal


def resolve(
    menu_item: MenuItem,
    option_ids: Sequence[int],
    groups: Sequence[MenuItemModifierGroup],
) -> tuple[ChosenModifier, ...]:
    """Validate one line's answers and return them in menu order.

    `groups` is what this dish asks. An empty `option_ids` is always accepted,
    even for a dish with a required variant — see _check_bounds for why.
    """
    if not option_ids:
        return ()
    if len(set(option_ids)) != len(option_ids):
        raise PricingError(f"The same choice was sent twice for {menu_item.name!r}")

    by_option = _options_by_id(menu_item, groups)
    picked: list[tuple[MenuItemModifierGroup, MenuItemModifierOption]] = []
    for option_id in option_ids:
        found = by_option.get(option_id)
        if found is None:
            # Covers a made-up id, another dish's option and another
            # restaurant's: all three are "not a question this dish asks".
            raise PricingError(f"Choice {option_id} is not offered on {menu_item.name!r}")
        _, option = found
        if not option.is_available:
            raise PricingError(
                f"{option.name!r} is currently unavailable for {menu_item.name!r}"
            )
        picked.append(found)

    _check_bounds(menu_item, groups, picked)
    # Menu order — group, then option — so the rows are written in the order a
    # kitchen reads them and OrderItem.modifiers can read them back by id.
    picked.sort(key=lambda pair: (pair[0].sort_order, pair[0].id, pair[1].sort_order, pair[1].id))
    return tuple(
        ChosenModifier(
            option_id=option.id,
            group_name=group.name,
            option_name=option.name,
            price_delta=money(option.price_delta),
        )
        for group, option in picked
    )


def _options_by_id(
    menu_item: MenuItem, groups: Sequence[MenuItemModifierGroup]
) -> dict[int, tuple[MenuItemModifierGroup, MenuItemModifierOption]]:
    # A group is the restaurant's, not the dish's. The link table should never
    # attach another restaurant's group, but the price of trusting it is a
    # customer buying a surcharge from a kitchen that is not cooking their food.
    return {
        option.id: (group, option)
        for group in groups
        if group.restaurant_id == menu_item.restaurant_id
        for option in group.options
    }


def _check_bounds(
    menu_item: MenuItem,
    groups: Sequence[MenuItemModifierGroup],
    picked: Sequence[tuple[MenuItemModifierGroup, MenuItemModifierOption]],
) -> None:
    """Each group's min/max — applied only once the line carries any answers.

    The maximum always binds: three portions on one chai is never a real order.

    The minimum binds only when the client sent choices at all. The customer app
    has no way to answer a dish's questions yet, so every line it sends is
    empty; refusing those for a dish with a required "Portion" would make four
    dishes per restaurant impossible to order — a worse failure than the one
    being fixed. A client that does answer has seen the questions, so leaving a
    required one blank is a mistake worth a 422. Tighten this to always-on once
    every client offers the picker.
    """
    counts: dict[int, int] = {}
    for group, _ in picked:
        counts[group.id] = counts.get(group.id, 0) + 1

    for group in groups:
        chosen = counts.get(group.id, 0)
        if chosen > group.max_select:
            raise PricingError(
                f"{menu_item.name!r} allows at most {group.max_select} "
                f"{_choice_word(group.max_select)} for {group.name!r}; {chosen} were sent"
            )
        if chosen < group.min_select:
            raise PricingError(
                f"{menu_item.name!r} needs at least {group.min_select} "
                f"{_choice_word(group.min_select)} for {group.name!r}"
            )


def _choice_word(count: int) -> str:
    return "choice" if count == 1 else "choices"

