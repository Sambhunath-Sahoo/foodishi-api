import random
from datetime import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import (
    Cuisine,
    MenuCategory,
    MenuItem,
    Restaurant,
    RestaurantPolicy,
    restaurant_cuisines,
)
from app.models.enums import SpiceLevel
from app.seed import data
from app.services.money import money

#: At most this many dishes per restaurant.
#:
#: The source menus in data.py hold more than this for most cuisines (23 for
#: north-indian + biryani), and a short menu is the point: it fits on one screen,
#: it is quick to seed, and every dish on it is one somebody might actually look
#: at. Raise it when there is a reason to.
#:
#: The cap is applied ROUND-ROBIN across categories rather than by truncating the
#: dish list, because truncating would fill the quota from the first category and
#: leave a restaurant with ten starters and no mains -- which is not a menu, and
#: would leave the category navigation with nothing to navigate.
MENU_ITEM_LIMIT = 10


def _slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


async def build(session: AsyncSession, rng: random.Random) -> list[Restaurant]:
    cuisines = {}
    for name, slug in data.CUISINES:
        cuisine = Cuisine(name=name, slug=slug)
        session.add(cuisine)
        cuisines[slug] = cuisine
    await session.flush()

    restaurants = []
    for index, (name, cuisine_slugs) in enumerate(data.RESTAURANTS):
        city, area, lat, lon = data.LOCATIONS[index % len(data.LOCATIONS)]
        restaurant = Restaurant(
            name=name,
            slug=_slugify(name),
            description=f"{cuisines[cuisine_slugs[0]].name} favourites from {area}.",
            city=city,
            area=area,
            address_line=f"{rng.randint(1, 180)}, {area} Main Road, {city}",
            # Jitter so the restaurants are not stacked on exact LOCATIONS points.
            latitude=Decimal(str(round(lat + rng.uniform(-0.012, 0.012), 6))),
            longitude=Decimal(str(round(lon + rng.uniform(-0.012, 0.012), 6))),
            phone=f"9{rng.randint(100000000, 999999999)}",
            rating=Decimal(str(round(rng.uniform(3.4, 4.8), 1))),
            rating_count=rng.randint(80, 4200),
            price_for_two=money(rng.choice([300, 400, 500, 600, 800, 1000])),
            avg_prep_minutes=rng.randint(12, 40),
            opens_at=time(rng.choice([7, 8, 10, 11]), 0),
            closes_at=time(rng.choice([22, 23]), rng.choice([0, 30])),
            is_active=True,
        )
        session.add(restaurant)
        await session.flush()

        for slug in cuisine_slugs:
            await session.execute(
                restaurant_cuisines.insert().values(
                    restaurant_id=restaurant.id, cuisine_id=cuisines[slug].id
                )
            )

        session.add(_policy_for(restaurant.id, rng))
        await _add_menu(session, restaurant, cuisine_slugs, rng)
        restaurants.append(restaurant)

    await session.flush()
    return restaurants


def _policy_for(restaurant_id: int, rng: random.Random) -> RestaurantPolicy:
    """Varied on purpose. Uniform policies would make every support answer the
    same, and hide bugs where the wrong restaurant's rules get applied."""
    return RestaurantPolicy(
        restaurant_id=restaurant_id,
        cancellation_window_mins=rng.choice([3, 5, 5, 8, 10, 15]),
        cancellation_fee_percent=money(rng.choice([10, 15, 20, 25])),
        refund_sla_hours=rng.choice([24, 24, 48, 72]),
        delivery_fee_base=money(rng.choice([19, 25, 29, 35])),
        delivery_fee_per_km=money(rng.choice([4, 5, 6, 8])),
        free_delivery_above=(
            money(rng.choice([399, 499, 599])) if rng.random() < 0.55 else None
        ),
        packaging_fee=money(rng.choice([10, 15, 20, 25])),
        min_order_value=money(rng.choice([49, 79, 99, 149])),
        max_delivery_distance_km=Decimal(str(rng.choice([8.0, 10.0, 12.0, 15.0]))),
    )


async def _add_menu(
    session: AsyncSession, restaurant: Restaurant, cuisine_slugs: list[str], rng: random.Random
) -> None:
    """Attach at most MENU_ITEM_LIMIT dishes, spread across the categories.

    Collected first, then dealt out round-robin, so a five-category restaurant
    gets two dishes in each rather than ten in the first and none in the rest.
    Only the categories that actually receive a dish are created -- an empty
    category is a tab that opens onto nothing.
    """
    # (category name, [dishes]) in declaration order, de-duplicated by name the
    # way the original did: two cuisines can both offer "Breads".
    buckets: list[tuple[str, list]] = []
    seen: set[str] = set()
    for slug in cuisine_slugs:
        for category_name, dishes in data.MENUS[slug]:
            if category_name in seen:
                continue
            seen.add(category_name)
            buckets.append((category_name, list(dishes)))

    # Deal one dish per category per pass until the cap is reached.
    chosen: dict[str, list] = {name: [] for name, _ in buckets}
    taken = 0
    depth = 0
    while taken < MENU_ITEM_LIMIT:
        progressed = False
        for category_name, dishes in buckets:
            if depth >= len(dishes):
                continue
            chosen[category_name].append(dishes[depth])
            taken += 1
            progressed = True
            if taken >= MENU_ITEM_LIMIT:
                break
        if not progressed:
            break  # every category exhausted before the cap
        depth += 1

    sort_order = 0
    for category_name, _ in buckets:
        dishes = chosen[category_name]
        if not dishes:
            continue
        category = MenuCategory(
            restaurant_id=restaurant.id, name=category_name, sort_order=sort_order
        )
        session.add(category)
        sort_order += 1
        # Flushed here because each item needs the category's generated id.
        await session.flush()
        for dish, low, high, is_veg, spice in dishes:
            session.add(
                MenuItem(
                    restaurant_id=restaurant.id,
                    category_id=category.id,
                    name=dish,
                    description=f"{dish} prepared fresh at {restaurant.name}.",
                    price=money(rng.randint(low, high)),
                    is_veg=is_veg,
                    spice_level=SpiceLevel(spice),
                    serves=rng.choice([1, 1, 2]),
                    calories=rng.randint(180, 900),
                    # A handful genuinely out of stock, so the "unavailable"
                    # path has real data behind it.
                    is_available=rng.random() > 0.05,
                )
            )
