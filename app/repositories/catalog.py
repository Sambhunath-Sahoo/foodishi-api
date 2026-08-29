from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Literal

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.search import ESCAPE_CHARACTER, contains, escape_like
from app.models.catalog import (
    Cuisine,
    MenuCategory,
    MenuItem,
    Restaurant,
    RestaurantPolicy,
    restaurant_cuisines,
)
from app.models.enums import SpiceLevel

RestaurantSort = Literal["rating", "price_for_two", "name", "avg_prep_minutes"]

# Sort key -> ORDER BY terms. Rating leads descending because "best first" is
# what a discovery list means; price and name read naturally ascending.
_SORTS = {
    "rating": (Restaurant.rating.desc(), Restaurant.rating_count.desc()),
    "price_for_two": (Restaurant.price_for_two.asc(),),
    "name": (Restaurant.name.asc(),),
    # "Fastest first". Ordered in the query, not in the page: sorting one page
    # of twelve would reorder a twelfth of the list and call it the ranking.
    "avg_prep_minutes": (Restaurant.avg_prep_minutes.asc(), Restaurant.rating.desc()),
}


def _open_now_clause(now: time):
    """True when `now` falls inside the restaurant's serving window.

    A window that closes before it opens (22:00 -> 02:00) crosses midnight, so
    it matches the union of the two sides rather than an empty intersection.
    """
    within_day = and_(
        Restaurant.opens_at < Restaurant.closes_at,
        Restaurant.opens_at <= now,
        Restaurant.closes_at > now,
    )
    overnight = and_(
        Restaurant.closes_at < Restaurant.opens_at,
        or_(Restaurant.opens_at <= now, Restaurant.closes_at > now),
    )
    return or_(within_day, overnight)


def restaurants_statement(
    *,
    city: str | None = None,
    cuisine: str | None = None,
    q: str | None = None,
    open_now: bool | None = None,
    min_rating: Decimal | None = None,
    max_price_for_two: Decimal | None = None,
    sort: RestaurantSort = "rating",
) -> Select:
    statement = select(Restaurant).where(Restaurant.is_active.is_(True))

    if city:
        # Escaped and anchored: `?city=%` used to make this match every city
        # while still reporting itself as a city filter, so a client narrowing to
        # one city silently got the whole platform. An exact city name needs no
        # wildcards at all, so the pattern is the escaped term itself.
        statement = statement.where(
            Restaurant.city.ilike(escape_like(city), escape=ESCAPE_CHARACTER)
        )
    if q:
        statement = statement.where(
            Restaurant.name.ilike(contains(q), escape=ESCAPE_CHARACTER)
        )
    if min_rating is not None:
        statement = statement.where(Restaurant.rating >= min_rating)
    if max_price_for_two is not None:
        statement = statement.where(Restaurant.price_for_two <= max_price_for_two)
    if open_now is not None:
        clause = _open_now_clause(datetime.now(UTC).time())
        statement = statement.where(clause if open_now else ~clause)
    if cuisine:
        # EXISTS rather than a join: a restaurant carries several cuisines and
        # a join would duplicate its row, inflating both the page and the count.
        statement = statement.where(
            exists(
                select(1)
                .select_from(restaurant_cuisines)
                .join(Cuisine, Cuisine.id == restaurant_cuisines.c.cuisine_id)
                .where(
                    restaurant_cuisines.c.restaurant_id == Restaurant.id,
                    Cuisine.slug == cuisine,
                )
            )
        )

    # id breaks ties so paging is stable across requests.
    return statement.order_by(*_SORTS[sort], Restaurant.id.asc())


def platform_restaurants_statement(*, is_active: bool | None = None) -> Select:
    """Every restaurant on the platform, whatever state it is in.

    The deliberate twin of restaurants_statement above, and separate from it
    rather than a flag on it. That function opens with
    `where(is_active.is_(True))` and is reached by an UNAUTHENTICATED route; a
    parameter that could switch the filter off would put "show customers the
    kitchens that are closed" one wrong argument away, on the one endpoint where
    that mistake is public.

    So this one is only ever called from a route behind require_platform_role,
    and it is the answer to a question only Foodishi asks: which kitchens exist.
    An approved application creates a restaurant with is_active false, and until
    this existed the operations console — which reads the customer listing —
    could not see the restaurant it had just created anywhere.

    Ordered by name because this is a directory somebody looks a kitchen up in,
    not a ranking. `rating desc` is right for discovery and wrong here.
    """
    statement = select(Restaurant)
    if is_active is not None:
        statement = statement.where(Restaurant.is_active.is_(is_active))
    # id breaks ties so paging is stable across requests, as in the listing above.
    return statement.order_by(Restaurant.name.asc(), Restaurant.id.asc())


def menu_search_statement(
    restaurant_id: int,
    *,
    q: str | None = None,
    is_veg: bool | None = None,
    max_price: Decimal | None = None,
    spice_level: SpiceLevel | None = None,
) -> Select:
    statement = select(MenuItem).where(MenuItem.restaurant_id == restaurant_id)

    if q:
        # This is the clause app/routers/catalog.py:115 says "interpolates the
        # term into the pattern unescaped, and both had to change" -- only the
        # other one did. Reachable unauthenticated via
        # GET /restaurants/{id}/menu/search?q=%, which returned the entire menu.
        pattern = contains(q)
        statement = statement.where(
            or_(
                MenuItem.name.ilike(pattern, escape=ESCAPE_CHARACTER),
                MenuItem.description.ilike(pattern, escape=ESCAPE_CHARACTER),
            )
        )
    if is_veg is not None:
        statement = statement.where(MenuItem.is_veg.is_(is_veg))
    if max_price is not None:
        statement = statement.where(MenuItem.price <= max_price)
    if spice_level is not None:
        statement = statement.where(MenuItem.spice_level == spice_level)

    return statement.order_by(MenuItem.name.asc(), MenuItem.id.asc())


async def list_cuisines(session: AsyncSession) -> list[Cuisine]:
    rows = await session.execute(select(Cuisine).order_by(Cuisine.name.asc()))
    return list(rows.scalars().all())


async def get_restaurant(session: AsyncSession, restaurant_id: int) -> Restaurant | None:
    return await session.get(Restaurant, restaurant_id)


async def restaurant_exists(session: AsyncSession, restaurant_id: int) -> bool:
    found = await session.scalar(
        select(exists().where(Restaurant.id == restaurant_id))
    )
    return bool(found)


async def get_policy(
    session: AsyncSession, restaurant_id: int
) -> RestaurantPolicy | None:
    return await session.get(RestaurantPolicy, restaurant_id)


async def get_cuisines_for_restaurant(
    session: AsyncSession, restaurant_id: int
) -> list[Cuisine]:
    statement = (
        select(Cuisine)
        .join(restaurant_cuisines, restaurant_cuisines.c.cuisine_id == Cuisine.id)
        .where(restaurant_cuisines.c.restaurant_id == restaurant_id)
        .order_by(Cuisine.name.asc())
    )
    rows = await session.execute(statement)
    return list(rows.scalars().all())


async def get_menu(
    session: AsyncSession, restaurant_id: int
) -> list[tuple[MenuCategory, list[MenuItem]]]:
    """Every category with its items in a single round trip.

    The models declare no relationships, so an outer join plus grouping in
    Python is what keeps this off the N+1 path — one SELECT regardless of how
    many categories the restaurant has. LEFT join so an empty category still
    appears.
    """
    statement = (
        select(MenuCategory, MenuItem)
        .outerjoin(MenuItem, MenuItem.category_id == MenuCategory.id)
        .where(MenuCategory.restaurant_id == restaurant_id)
        .order_by(
            MenuCategory.sort_order.asc(),
            MenuCategory.id.asc(),
            MenuItem.name.asc(),
        )
    )
    rows = (await session.execute(statement)).all()

    categories: dict[int, MenuCategory] = {}
    items: dict[int, list[MenuItem]] = {}
    for category, item in rows:
        categories.setdefault(category.id, category)
        bucket = items.setdefault(category.id, [])
        if item is not None:
            bucket.append(item)

    # dicts preserve insertion order, which is the ORDER BY order above.
    return [(category, items[cid]) for cid, category in categories.items()]


async def get_menu_item(session: AsyncSession, item_id: int) -> MenuItem | None:
    return await session.get(MenuItem, item_id)
