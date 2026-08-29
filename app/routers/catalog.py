from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, exists, or_, select

from app.core.errors import NOT_FOUND, not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.models.catalog import Cuisine, MenuItem, Restaurant, restaurant_cuisines
from app.models.enums import SpiceLevel
from app.repositories import catalog as repo
from app.repositories.catalog import RestaurantSort
from app.schemas.catalog import (
    CuisineRead,
    MenuCategoryRead,
    MenuItemRead,
    RestaurantDetail,
    RestaurantPolicyRead,
    RestaurantProfile,
    RestaurantSummary,
)

router = APIRouter(tags=["catalog"])

MAX_RATING = 5


# LIKE treats % and _ as wildcards, so a raw search term would let a caller
# widen the scan to the whole table. Escape them and match literally — the same
# helper, character for character, as _contains() in app/routers/users.py.
def _contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ilike(column, pattern: str) -> ColumnElement[bool]:
    # escape="\\" every time, or the backslashes _contains() just added would
    # be matched literally instead of doing their job.
    return column.ilike(pattern, escape="\\")


def _search_clause(term: str) -> ColumnElement[bool]:
    """Where a customer's search term is allowed to match.

    The kitchen's name alone was too narrow to keep the promise a search box
    makes: people type a neighbourhood ("Indiranagar"), a cuisine
    ("hyderabadi") or the dish they actually want ("dum biryani"), and none of
    those is a restaurant's name. Four more places to look, no full-text index:
    the whole catalog is a few hundred rows, and when it stops being that, this
    is one clause to replace rather than a search layer to unpick.

    EXISTS rather than joins, for the reason restaurants_statement gives about
    cuisines: a restaurant carries many cuisines and many dishes, and a join
    would duplicate its row — inflating both the page and the total beside it.

    Unavailable dishes are excluded. A kitchen that has taken biryani off the
    menu is not an answer to "biryani", and offering it is how a customer ends
    up on a menu that does not contain what they searched for.
    """
    pattern = _contains(term)
    return or_(
        _ilike(Restaurant.name, pattern),
        _ilike(Restaurant.area, pattern),
        _ilike(Restaurant.city, pattern),
        exists(
            select(1)
            .select_from(restaurant_cuisines)
            .join(Cuisine, Cuisine.id == restaurant_cuisines.c.cuisine_id)
            .where(
                restaurant_cuisines.c.restaurant_id == Restaurant.id,
                _ilike(Cuisine.name, pattern),
            )
        ),
        exists(
            select(1)
            .select_from(MenuItem)
            .where(
                MenuItem.restaurant_id == Restaurant.id,
                MenuItem.is_available.is_(True),
                _ilike(MenuItem.name, pattern),
            )
        ),
    )


@router.get("/cuisines", response_model=list[CuisineRead])
async def list_cuisines(session: SessionDep):
    # Unpaginated on purpose: the set is a fixed handful and every client needs
    # all of it to render a filter bar.
    return await repo.list_cuisines(session)


@router.get("/restaurants", response_model=Page[RestaurantSummary])
async def list_restaurants(
    session: SessionDep,
    page: PageDep,
    city: Annotated[str | None, Query(max_length=60)] = None,
    cuisine: Annotated[str | None, Query(max_length=60, description="Cuisine slug")] = None,
    q: Annotated[
        str | None,
        Query(
            max_length=160,
            description="Name, area, city, cuisine or dish contains",
        ),
    ] = None,
    open_now: Annotated[bool | None, Query(description="Serving at the current UTC time")] = None,
    min_rating: Annotated[Decimal | None, Query(ge=0, le=MAX_RATING)] = None,
    max_price_for_two: Annotated[Decimal | None, Query(ge=0)] = None,
    sort: RestaurantSort = "rating",
):
    statement = repo.restaurants_statement(
        city=city,
        cuisine=cuisine,
        # q is composed below instead of being handed to the repository: the
        # clause there matches the kitchen's name only and interpolates the term
        # into the pattern unescaped, and both had to change. Composed in the
        # router the way app/routers/users.py composes its own directory search,
        # so there is exactly one place a search term is turned into SQL for
        # this endpoint.
        open_now=open_now,
        min_rating=min_rating,
        max_price_for_two=max_price_for_two,
        sort=sort,
    )
    # Unauthenticated on purpose, like every route in this module: the customer
    # app searches before anyone signs in.
    term = q.strip() if q else ""
    if term:
        statement = statement.where(_search_clause(term))

    rows, total = await paginate(session, statement, page)
    return {"items": rows, "total": total, "limit": page.limit, "offset": page.offset}


@router.get(
    "/restaurants/{restaurant_id}",
    response_model=RestaurantDetail,
    responses=NOT_FOUND,
)
async def get_restaurant(restaurant_id: int, session: SessionDep):
    restaurant = await repo.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise not_found("restaurant", restaurant_id)

    cuisines = await repo.get_cuisines_for_restaurant(session, restaurant_id)
    policy = await repo.get_policy(session, restaurant_id)
    return RestaurantDetail(
        **RestaurantProfile.model_validate(restaurant).model_dump(),
        cuisines=[CuisineRead.model_validate(c) for c in cuisines],
        policy=RestaurantPolicyRead.model_validate(policy) if policy else None,
    )


@router.get(
    "/restaurants/{restaurant_id}/menu",
    response_model=list[MenuCategoryRead],
    responses=NOT_FOUND,
)
async def get_menu(restaurant_id: int, session: SessionDep):
    grouped = await repo.get_menu(session, restaurant_id)
    if not grouped and not await repo.restaurant_exists(session, restaurant_id):
        # Existence is only checked when the menu comes back empty, so the
        # normal path stays at the single grouped query.
        raise not_found("restaurant", restaurant_id)

    return [
        MenuCategoryRead(
            id=category.id,
            restaurant_id=category.restaurant_id,
            name=category.name,
            sort_order=category.sort_order,
            items=[MenuItemRead.model_validate(item) for item in items],
        )
        for category, items in grouped
    ]


@router.get(
    "/restaurants/{restaurant_id}/menu/search",
    response_model=Page[MenuItemRead],
    responses=NOT_FOUND,
)
async def search_menu(
    restaurant_id: int,
    session: SessionDep,
    page: PageDep,
    q: Annotated[str | None, Query(max_length=160, description="Name or description contains")] = None,
    is_veg: bool | None = None,
    max_price: Annotated[Decimal | None, Query(ge=0)] = None,
    spice_level: SpiceLevel | None = None,
):
    if not await repo.restaurant_exists(session, restaurant_id):
        # Without this an unknown restaurant reads as a restaurant with no
        # matching dishes, which sends clients hunting for the wrong bug.
        raise not_found("restaurant", restaurant_id)

    statement = repo.menu_search_statement(
        restaurant_id,
        q=q,
        is_veg=is_veg,
        max_price=max_price,
        spice_level=spice_level,
    )
    rows, total = await paginate(session, statement, page)
    return {"items": rows, "total": total, "limit": page.limit, "offset": page.offset}


@router.get(
    "/restaurants/{restaurant_id}/policy",
    response_model=RestaurantPolicyRead,
    responses=NOT_FOUND,
)
async def get_restaurant_policy(restaurant_id: int, session: SessionDep):
    policy = await repo.get_policy(session, restaurant_id)
    if policy is None:
        raise not_found("restaurant policy", restaurant_id)
    return policy


@router.get("/menu-items/{item_id}", response_model=MenuItemRead, responses=NOT_FOUND)
async def get_menu_item(item_id: int, session: SessionDep):
    item = await repo.get_menu_item(session, item_id)
    if item is None:
        raise not_found("menu item", item_id)
    return item
