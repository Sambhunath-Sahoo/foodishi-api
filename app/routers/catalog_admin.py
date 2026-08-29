import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    NOT_PLATFORM,
    UNAUTHENTICATED,
    CurrentUser,
    require_platform_role,
)
from app.dependencies.scope import (
    RestaurantScopeDep,
    admin_of_menu_category,
    admin_of_menu_item,
    admin_of_restaurant,
    admin_of_restaurant_or_platform,
)
from app.models.catalog import MenuCategory, MenuItem, Restaurant, RestaurantPolicy
from app.models.enums import PlatformRole, StaffRole
from app.models.platform import PlatformStaff
from app.models.staff import RestaurantStaff
from app.models.user import User
from app.schemas.catalog_admin import (
    MenuCategoryCreate,
    MenuCategoryRead,
    MenuCategoryUpdate,
    MenuItemCreate,
    MenuItemRead,
    MenuItemUpdate,
    RestaurantAvailabilityRead,
    RestaurantAvailabilityUpdate,
    RestaurantCreate,
    RestaurantPolicyRead,
    RestaurantPolicyUpsert,
    RestaurantRead,
    RestaurantUpdate,
)

# No prefix: this router spans three resource trees (/restaurants,
# /menu-categories, /menu-items) that share one owner — the catalog admin.
#
# Every route here is a write, and all but one are scoped: the caller must be
# an active admin of the restaurant the row belongs to. For the
# /menu-categories and /menu-items trees the restaurant is not in the URL, so
# it is resolved from the row itself (app/dependencies/scope.py) — the path id
# is a lookup key, never a claim about who the caller works for.
#
# POST /restaurants is the exception: there is no restaurant yet to be scoped
# to, so it answers to Foodishi's own operators instead.
logger = logging.getLogger(__name__)

router = APIRouter(tags=["catalog-admin"])

SLUG_TAKEN = {409: {"description": "Slug is already taken"}}

# Every restaurant-scoped route can refuse an unauthenticated or out-of-scope
# caller, so the two responses are declared once and spread into each route.
# POST /restaurants declares its own 403 — NOT_PLATFORM, not FORBIDDEN.
SCOPED = {**UNAUTHENTICATED, **FORBIDDEN}

# The same check as dependencies=[Depends(admin_of_restaurant)], taken as a
# parameter instead, for the route that logs who acted: the dependency already
# returns the membership row, so naming it costs nothing extra.
RestaurantAdmin = Annotated[RestaurantStaff, Depends(admin_of_restaurant)]


async def _assert_category_in_restaurant(
    session: AsyncSession, category_id: int, restaurant_id: int
) -> None:
    """A menu item's category must belong to the item's own restaurant.

    The two foreign keys are independent, so nothing in the schema stops an
    item pointing at another restaurant's category — that is checked here.
    """
    owner_id = await session.scalar(
        select(MenuCategory.restaurant_id).where(MenuCategory.id == category_id)
    )
    if owner_id != restaurant_id:
        raise unprocessable(
            f"Menu category {category_id} does not belong to restaurant {restaurant_id}"
        )


async def _assert_can_own(session: AsyncSession, user_id: int) -> None:
    """The named owner must be an account that can actually sign in and act.

    A membership pointing at a missing or deactivated person is one the API
    will never honour — current_user 403s a deactivated account — which would
    leave the new restaurant with an owner row and still nobody able to edit
    it. POST /restaurants/{id}/staff refuses the same thing for the same
    reason.
    """
    owner = await session.get(User, user_id)
    if owner is None:
        raise unprocessable(f"No user with id {user_id} to own this restaurant")
    if not owner.is_active:
        raise unprocessable(
            f"{owner.email} is deactivated; reactivate the account before "
            "making them the owner of a restaurant"
        )


class RestaurantOnboard(RestaurantCreate):
    """RestaurantCreate plus the one field only this call needs.

    owner_user_id describes the onboarding act rather than the restaurant — it
    names the partner who will run the place, which is not the operator filling
    in the form — so it is declared beside the route instead of among the
    resource's own columns in app/schemas/catalog_admin.py.
    """

    # Optional, so an operator who has not been handed the partner's account id
    # yet can still onboard; see the route docstring for what that costs.
    owner_user_id: int | None = Field(default=None, ge=1)

    # The DEFAULT is overridden, not the field: an operator who means to open a
    # kitchen the moment it is created may still say so, and one who says
    # nothing gets a restaurant customers cannot see yet.
    #
    # False because discovery filters on this column alone
    # (app/repositories/catalog.py), so the old default published a restaurant
    # with no menu and no policy the instant this route returned — findable in
    # search, and failing at the customer's checkout, until somebody remembered
    # to come back and finish it. Same reason approval creates a dormant
    # restaurant; see app/services/onboarding.approve.
    is_active: bool = False


# Onboarding mints a tenancy: a restaurant plus the first login that can staff
# and edit it, and through it reach every restaurant-scoped route below. That
# is Foodishi's decision to make, not a customer's — until this dependency landed
# the only check was "is signed in", so anyone with an account could create
# unlimited restaurants and appoint themselves owner of each.
OpsStaff = Annotated[PlatformStaff, Depends(require_platform_role(PlatformRole.ADMIN))]


@router.post(
    "/restaurants",
    response_model=RestaurantRead,
    status_code=201,
    responses={**SLUG_TAKEN, **UNAUTHENTICATED, **NOT_PLATFORM},
)
async def create_restaurant(
    payload: RestaurantOnboard, session: SessionDep, operator: OpsStaff
):
    """Onboard a restaurant and appoint its first owner. Platform ops only.

    The owner row is not a convenience. Every other write in this router, and
    POST /restaurants/{id}/staff, demand an existing membership, so a
    restaurant created without one could never be edited or staffed by anyone —
    it would need a hand-written INSERT to become usable.

    So one is always written, and it names payload.owner_user_id: the partner
    who will run the restaurant, never the operator who typed the form. Omit
    the field and the operator is seeded as the owner instead, which keeps the
    restaurant reachable but leaves them holding a live owner login on somebody
    else's kitchen until they hand it over — POST /restaurants/{id}/staff to
    appoint the partner, then PATCH /staff/{id} to revoke themselves.

    The restaurant is created DORMANT unless the body says otherwise: customers
    cannot see it until somebody turns it on with PUT
    /restaurants/{id}/availability. Creating and publishing are two decisions,
    and the second one belongs after the policy and the menu exist.

    This is the operator-driven path. A restaurant that asked to join arrives
    instead through POST /restaurant-applications and is created by an approval
    — app/routers/admin_applications.py — which mints the same two rows this
    route does.
    """
    owner_user_id = payload.owner_user_id
    if owner_user_id is None:
        # The operator's own row needs no check: current_user already refused a
        # missing or deactivated account before this handler ran.
        owner_user_id = operator.user_id
    else:
        await _assert_can_own(session, owner_user_id)

    restaurant = Restaurant(**payload.model_dump(exclude={"owner_user_id"}))
    session.add(restaurant)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflict(f"Slug {restaurant.slug!r} is already taken") from exc

    session.add(
        RestaurantStaff(
            user_id=owner_user_id, restaurant_id=restaurant.id, role=StaffRole.ADMIN
        )
    )
    await session.flush()
    logger.info(
        "Restaurant %s onboarded by platform users.id=%s; users.id=%s owns it",
        restaurant.id,
        operator.user_id,
        owner_user_id,
    )
    await session.refresh(restaurant)  # picks up server-side defaults
    return restaurant


@router.patch(
    "/restaurants/{restaurant_id}",
    response_model=RestaurantRead,
    responses={**NOT_FOUND, **SLUG_TAKEN, **SCOPED},
    # The restaurant's own admin, OR a platform admin. The operations console
    # edits a kitchen's details and had no way to: require_staff consults
    # restaurant_staff only, and an operator staffs nothing.
    dependencies=[Depends(admin_of_restaurant_or_platform)],
)
async def update_restaurant(
    restaurant_id: int, payload: RestaurantUpdate, session: SessionDep
):
    changes = payload.model_dump(exclude_unset=True)

    # A single UPDATE ... RETURNING rather than fetch-then-mutate: one round
    # trip, and no half-modified entity left in the session if it fails.
    statement = (
        update(Restaurant)
        .where(Restaurant.id == restaurant_id)
        .values(**changes)
        .returning(Restaurant)
    )
    try:
        restaurant = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        # slug is the only unique column today, but the payload may not carry
        # it — never index into changes here.
        slug = changes.get("slug")
        detail = (
            f"Slug {slug!r} is already taken"
            if slug
            else "Update conflicts with an existing record"
        )
        raise conflict(detail) from exc

    if restaurant is None:
        raise not_found("restaurant", restaurant_id)
    return restaurant


@router.put(
    "/restaurants/{restaurant_id}/availability",
    response_model=RestaurantAvailabilityRead,
    responses={**NOT_FOUND, **SCOPED},
    summary="Open or close the restaurant for new orders",
    # The restaurant's own admin, OR a platform admin. Closing a kitchen that is
    # causing a problem is one of the few genuinely urgent things an operations
    # team does, and the console's Taking-orders switch was answering 403.
    #
    # A dependency rather than a parameter now: the handler never read the staff
    # row, only its refusal, and a platform admin has no such row to hand it.
    dependencies=[Depends(admin_of_restaurant_or_platform)],
)
async def set_restaurant_availability(
    restaurant_id: int,
    payload: RestaurantAvailabilityUpdate,
    session: SessionDep,
    # The CALLER, not their staff row. The audit line below records who closed
    # the kitchen, and that has to work for a platform admin too -- they have no
    # restaurant_staff row to read a user_id off. Authorization is the dependency
    # above; this is only for the log.
    user: CurrentUser,
):
    """Stop or resume taking orders, right now.

    This writes restaurants.is_active, and that choice is the whole design of
    the route. It is the only column the placement path consults: pricing a
    cart refuses with "<name> is not currently accepting orders" when it is
    false (app/services/ordering.py), so it is the only one that can make "we
    are closed" true for a customer. opens_at/closes_at look like the natural home for a close
    button and are not: nothing reads them but the optional open_now filter on
    the customer listing, so an order placed at 3am is accepted today, and
    overwriting closes_at to shut early would destroy the real trading hours
    with nowhere left to remember them. Hours stay editable as hours, through
    PATCH /restaurants/{restaurant_id}.

    Nor is is_active the platform's own suspension switch, which is the thing
    to check before letting a partner flip it: no platform route writes it
    after onboarding. This route and that PATCH are both admin_of_restaurant,
    and Foodishi's operators staff no restaurant, so require_staff refuses them
    here. The column belongs to the restaurant, and this is a restaurant admin
    acting on their own kitchen.

    What closing does NOT mean:

      * Not a scheduled pause. There is no "until", and nothing reopens the
        restaurant except another call to this route.
      * Not "shown as closed". Discovery filters on is_active
        (app/repositories/catalog.py), so a closed restaurant disappears from
        search rather than greying out. Its own pages still load, which is what
        lets its admin reopen it.
      * Not a stop on work in hand. Orders already placed still have to be
        cooked or cancelled one at a time.
      * Not an audit trail. The reason and the author live in this API's log
        line and nowhere a client can read; the column has room for neither.
    """
    # UPDATE ... RETURNING for the same reason as the PATCH above: one round
    # trip, and the response is the row as the database now holds it.
    statement = (
        update(Restaurant)
        .where(Restaurant.id == restaurant_id)
        .values(is_active=payload.is_active)
        .returning(Restaurant)
    )
    restaurant = (await session.execute(statement)).scalar_one_or_none()
    if restaurant is None:
        raise not_found("restaurant", restaurant_id)

    # Logged at info because this is the one catalog write a customer feels
    # immediately, and the column keeps no record of who turned it.
    logger.info(
        "Restaurant %s is %s for orders; set by users.id=%s",
        restaurant_id,
        "open" if payload.is_active else "closed",
        user.id,
    )
    return restaurant


@router.delete(
    "/restaurants/{restaurant_id}",
    status_code=204,
    responses={
        **NOT_FOUND,
        409: {"description": "Restaurant still has orders"},
        **SCOPED,
    },
    dependencies=[Depends(admin_of_restaurant)],
)
async def delete_restaurant(restaurant_id: int, session: SessionDep):
    # RETURNING the id distinguishes "deleted one row" from "matched nothing",
    # so a repeat delete reports 404 rather than a silent success.
    statement = (
        delete(Restaurant).where(Restaurant.id == restaurant_id).returning(Restaurant.id)
    )
    try:
        deleted = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        # orders.restaurant_id is ON DELETE RESTRICT: past orders must stay
        # readable. Deactivate the restaurant instead of deleting it.
        raise conflict(
            f"Restaurant {restaurant_id} is referenced by existing orders; "
            "set is_active to false instead"
        ) from exc
    if deleted is None:
        raise not_found("restaurant", restaurant_id)


@router.put(
    "/restaurants/{restaurant_id}/policy",
    response_model=RestaurantPolicyRead,
    responses={**NOT_FOUND, **SCOPED},
    dependencies=[Depends(admin_of_restaurant)],
)
async def upsert_restaurant_policy(
    restaurant_id: int, payload: RestaurantPolicyUpsert, session: SessionDep
):
    """Create the policy row, or replace it if the restaurant already has one.

    The primary key is the foreign key, so there is no separate create and
    update — one INSERT ... ON CONFLICT keeps concurrent callers from racing
    into a duplicate-key error.
    """
    exists = await session.scalar(
        select(Restaurant.id).where(Restaurant.id == restaurant_id)
    )
    if exists is None:
        raise not_found("restaurant", restaurant_id)

    values = payload.model_dump()
    statement = (
        pg_insert(RestaurantPolicy)
        .values(restaurant_id=restaurant_id, **values)
        # ON CONFLICT bypasses the ORM's Python-side onupdate, so updated_at is
        # bumped explicitly or an edited policy would keep its original stamp.
        .on_conflict_do_update(
            index_elements=[RestaurantPolicy.restaurant_id],
            set_={**values, "updated_at": func.now()},
        )
        .returning(RestaurantPolicy)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()


@router.post(
    "/restaurants/{restaurant_id}/menu-categories",
    response_model=MenuCategoryRead,
    status_code=201,
    responses={**NOT_FOUND, **SCOPED},
    dependencies=[Depends(admin_of_restaurant)],
)
async def create_menu_category(
    restaurant_id: int, payload: MenuCategoryCreate, session: SessionDep
):
    exists = await session.scalar(
        select(Restaurant.id).where(Restaurant.id == restaurant_id)
    )
    if exists is None:
        raise not_found("restaurant", restaurant_id)

    category = MenuCategory(restaurant_id=restaurant_id, **payload.model_dump())
    session.add(category)
    await session.flush()
    return category


@router.patch(
    "/menu-categories/{category_id}",
    response_model=MenuCategoryRead,
    responses={**NOT_FOUND, **SCOPED},
    dependencies=[Depends(admin_of_menu_category)],
)
async def update_menu_category(
    category_id: int, payload: MenuCategoryUpdate, session: SessionDep
):
    statement = (
        update(MenuCategory)
        .where(MenuCategory.id == category_id)
        .values(**payload.model_dump(exclude_unset=True))
        .returning(MenuCategory)
    )
    category = (await session.execute(statement)).scalar_one_or_none()
    if category is None:
        raise not_found("menu category", category_id)
    return category


@router.delete(
    "/menu-categories/{category_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT, **SCOPED},
    dependencies=[Depends(admin_of_menu_category)],
)
async def delete_menu_category(category_id: int, session: SessionDep):
    statement = (
        delete(MenuCategory)
        .where(MenuCategory.id == category_id)
        .returning(MenuCategory.id)
    )
    try:
        deleted = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        # menu_items cascades from the category, but order_items is RESTRICT —
        # so a category holding an already-ordered item cannot be dropped.
        raise conflict(
            f"Menu category {category_id} holds items referenced by existing orders"
        ) from exc
    if deleted is None:
        raise not_found("menu category", category_id)


@router.post(
    "/menu-items",
    response_model=MenuItemRead,
    status_code=201,
    responses={**SCOPED},
)
async def create_menu_item(
    payload: MenuItemCreate, session: SessionDep, scope: RestaurantScopeDep
):
    """Add an item to a menu. Admins of that restaurant only.

    The restaurant is named in the body here rather than the path, so the check
    is the handler's first statement instead of a route dependency — the rule
    is the same one, borrowed from app/dependencies/scope.py. It runs before
    the category check so an outsider probing ids learns nothing about which
    categories exist.
    """
    await scope.require(payload.restaurant_id, StaffRole.ADMIN)

    # Also covers an unknown restaurant_id: no category can belong to one.
    await _assert_category_in_restaurant(
        session, payload.category_id, payload.restaurant_id
    )
    item = MenuItem(**payload.model_dump())
    session.add(item)
    await session.flush()
    await session.refresh(item)  # picks up server-side defaults
    return item


@router.patch(
    "/menu-items/{item_id}",
    response_model=MenuItemRead,
    responses={**NOT_FOUND, **SCOPED},
    dependencies=[Depends(admin_of_menu_item)],
)
async def update_menu_item(item_id: int, payload: MenuItemUpdate, session: SessionDep):
    changes = payload.model_dump(exclude_unset=True)

    if "category_id" in changes:
        # The new category is validated against the item's own restaurant,
        # which the payload cannot change.
        restaurant_id = await session.scalar(
            select(MenuItem.restaurant_id).where(MenuItem.id == item_id)
        )
        if restaurant_id is None:
            raise not_found("menu item", item_id)
        await _assert_category_in_restaurant(
            session, changes["category_id"], restaurant_id
        )

    statement = (
        update(MenuItem)
        .where(MenuItem.id == item_id)
        .values(**changes)
        .returning(MenuItem)
    )
    item = (await session.execute(statement)).scalar_one_or_none()
    if item is None:
        raise not_found("menu item", item_id)
    return item


@router.delete(
    "/menu-items/{item_id}",
    status_code=204,
    responses={
        **NOT_FOUND,
        409: {"description": "Menu item appears on existing orders"},
        **SCOPED,
    },
    dependencies=[Depends(admin_of_menu_item)],
)
async def delete_menu_item(item_id: int, session: SessionDep):
    statement = delete(MenuItem).where(MenuItem.id == item_id).returning(MenuItem.id)
    try:
        deleted = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        # order_items.menu_item_id is ON DELETE RESTRICT: a past receipt keeps
        # its own name and price, but the link must survive for analytics.
        raise conflict(
            f"Menu item {item_id} appears on existing orders; "
            "set is_available to false instead"
        ) from exc
    if deleted is None:
        raise not_found("menu item", item_id)
