"""Dish options — the questions a dish asks before it can be ordered.

Nothing seeded any of these four tables before, so
`menu_item_modifier_groups`, `..._options`, `..._links` and
`order_item_modifiers` were all empty. That left the partner console's whole
modifier editor, `GET /menu-items/{id}/modifier-groups`, and every receipt line
that renders a customer's choices with no data behind them.

ONE THING TO KNOW BEFORE READING THE `order_item_modifiers` PART.

When this seeder was written the API could not produce those rows. It can now:
`OrderItemIn.option_ids` carries the answers, `pricing.quote` folds each
`price_delta` into the line's unit price, and `ordering.place` writes the frozen
copies. This seeder still does NOT go through that path — it builds historic
orders by calling `quote()` without modifiers and attaches answers afterwards —
so everything below about seeded answers still holds.

Seeding the answers anyway is deliberate, and it is a trade worth stating:

  * FOR — the kitchen ticket, the order detail and the receipt all have a
    modifier line to render, so those screens can be built and reviewed against
    real rows rather than against nothing.
  * AGAINST — the seeded `price_delta` values are NOT in the order's
    `total_amount`. `ck_orders_total_reconciles` checks the stored parts against
    the stored total, and a modifier is not one of those parts, so nothing
    catches the difference.

So a seeded order carrying modifier answers is priced as though they were free.
That is a fixture, not a claim about how pricing works, and the `price_delta` on
these rows is set to **0.00** rather than a real surcharge precisely so nobody
reconciles a receipt against a total and finds it short. Paid add-ons are
represented in the CATALOGUE (where the price is real and visible in the editor)
and answered only with free options on orders. To seed paid answers, pick
them BEFORE pricing and pass them to `quote(modifiers=...)`, then drop
`FREE_ANSWERS_ONLY` — mind the rng draw order (people.build_users comes first).
"""

import logging
import random
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import MenuItem, Restaurant
from app.models.enums import ModifierKind, OrderStatus
from app.models.modifiers import (
    MenuItemModifierGroup,
    MenuItemModifierOption,
    OrderItemModifier,
    menu_item_modifier_links,
)
from app.models.order import Order, OrderItem
from app.services.money import money

logger = logging.getLogger(__name__)

#: A VARIANT group is pinned to exactly one answer by the API
#: (`VARIANT_BOUNDS = (1, 1)` in app/routers/modifiers.py). Matched here so a
#: seeded group is one the editor would also accept.
VARIANT_BOUNDS = (1, 1)

#: Only zero-cost options are attached to orders. See the module docstring.
FREE_ANSWERS_ONLY = True

#: (group name, kind, min, max, [(option, price_delta)])
#:
#: Two groups per restaurant, one of each kind, because the two behave
#: differently everywhere they are read: a variant must be answered and an addon
#: may be skipped, and a screen that only ever saw one of them would look
#: finished while half the logic went unexercised.
#:
#: Every group carries at least one FREE option, which is what the order-answer
#: pass below is allowed to choose from, and at least one PAID one, so the
#: catalogue shows a real surcharge in the editor.
GROUP_TEMPLATES: tuple[tuple[str, ModifierKind, int, int, tuple[tuple[str, str], ...]], ...] = (
    (
        "Portion",
        ModifierKind.VARIANT,
        *VARIANT_BOUNDS,
        (("Half plate", "0.00"), ("Full plate", "60.00")),
    ),
    (
        "Add-ons",
        ModifierKind.ADDON,
        0,
        3,
        (
            ("No onion", "0.00"),
            ("Extra gravy", "25.00"),
            ("Extra cheese", "35.00"),
        ),
    ),
)

#: How many of a restaurant's dishes a group is attached to. Not all of them:
#: a dish with no options is the normal case and the UI has to handle it.
LINKED_DISHES_PER_GROUP = 4

#: Roughly how many delivered orders get modifier answers on their lines.
ANSWERED_ORDER_SHARE = 0.35


async def build(session: AsyncSession, rng: random.Random) -> dict[str, int]:
    """Build the modifier catalogue, then answer it on some past orders."""
    restaurants = list(await session.scalars(select(Restaurant)))
    if not restaurants:
        logger.warning("No restaurants — skipping modifiers.")
        return {"groups": 0, "options": 0, "links": 0, "answers": 0}

    counts = {"groups": 0, "options": 0, "links": 0, "answers": 0}
    # group_id -> (group name, [(option_id, option name, price_delta)])
    catalogue: dict[int, tuple[str, list[tuple[int, str, Decimal]]]] = {}
    # menu_item_id -> [group_id]
    by_item: dict[int, list[int]] = {}

    for restaurant in restaurants:
        dishes = list(
            await session.scalars(
                select(MenuItem)
                .where(MenuItem.restaurant_id == restaurant.id)
                .order_by(MenuItem.id)
            )
        )
        if not dishes:
            continue

        for sort_order, (name, kind, low, high, options) in enumerate(GROUP_TEMPLATES):
            group = MenuItemModifierGroup(
                restaurant_id=restaurant.id,
                name=name,
                kind=kind,
                min_select=low,
                max_select=high,
                sort_order=sort_order,
            )
            session.add(group)
            await session.flush()  # the options need the generated group id
            counts["groups"] += 1

            built: list[tuple[int, str, Decimal]] = []
            for option_order, (option_name, delta) in enumerate(options):
                option = MenuItemModifierOption(
                    group_id=group.id,
                    name=option_name,
                    price_delta=money(Decimal(delta)),
                    # One choice sold out per addon group, so "no raita tonight"
                    # has a row behind it. Never on a variant: a variant must be
                    # answerable, and turning off one of two options would leave
                    # a required question with a single button.
                    is_available=not (
                        kind is ModifierKind.ADDON and option_name == "Extra cheese"
                    ),
                    sort_order=option_order,
                )
                session.add(option)
                await session.flush()
                built.append((option.id, option_name, money(Decimal(delta))))
                counts["options"] += 1
            catalogue[group.id] = (name, built)

            # Attach to a slice of the menu rather than all of it.
            for dish in dishes[:LINKED_DISHES_PER_GROUP]:
                await session.execute(
                    menu_item_modifier_links.insert().values(
                        group_id=group.id, menu_item_id=dish.id
                    )
                )
                by_item.setdefault(dish.id, []).append(group.id)
                counts["links"] += 1

    await session.flush()
    counts["answers"] = await _answer_on_orders(session, rng, catalogue, by_item)
    await session.flush()
    return counts


async def _answer_on_orders(
    session: AsyncSession,
    rng: random.Random,
    catalogue: dict[int, tuple[str, list[tuple[int, str, Decimal]]]],
    by_item: dict[int, list[int]],
) -> int:
    """Record choices on the lines of some already-delivered orders.

    Delivered only. An answer is something a customer gave at checkout, so
    putting one on a pending order would imply this seeder had placed it through
    the API, which it did not.

    The name and price are COPIED onto the row rather than joined, which is the
    whole point of the table: deleting a choice from the menu must not change
    what somebody ate. `option_id` is kept as well, and is `SET NULL` on delete.
    """
    if not catalogue or not by_item:
        return 0

    lines = list(
        await session.scalars(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.status == OrderStatus.DELIVERED,
                OrderItem.menu_item_id.in_(list(by_item)),
            )
            .order_by(OrderItem.id)
        )
    )

    written = 0
    for line in lines:
        if rng.random() > ANSWERED_ORDER_SHARE:
            continue
        for group_id in by_item.get(line.menu_item_id, []):
            group_name, options = catalogue[group_id]
            choosable = [
                option
                for option in options
                if not FREE_ANSWERS_ONLY or option[2] == Decimal("0.00")
            ]
            if not choosable:
                continue
            option_id, option_name, price_delta = rng.choice(choosable)
            session.add(
                OrderItemModifier(
                    order_item_id=line.id,
                    option_id=option_id,
                    group_name=group_name,
                    option_name=option_name,
                    price_delta=price_delta,
                )
            )
            written += 1
    return written
