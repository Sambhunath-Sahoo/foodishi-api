"""Which restaurant's data a partner request is allowed to touch.

app/dependencies/identity.py answers two questions: who is calling, and are
they staff of restaurant N. This module answers the question the partner
endpoints actually ask, which is almost never phrased as a restaurant id. The
caller names a menu item, a menu category, an order or a delivery, and the
restaurant that governs that row has to be looked up in the database before
anyone is let through.

Why it exists: every endpoint wired to this module previously took the
restaurant — or the row that implies it — straight from the client and trusted
it. A dashboard that only shows a manager their own restaurant is not a
control; the id in the URL is editable, and so is the one in the JSON body.

Three rules hold throughout:

  * The restaurant is always re-derived server-side. Nothing here trusts a
    client-supplied restaurant_id beyond using it as a lookup key.
  * An authenticated caller reaching outside their restaurants gets 403 with a
    message that says so — never a 404, never a silently empty list.
  * A row that genuinely does not exist is still 404, exactly as before.
    Menu items and orders are already discoverable elsewhere, so masking a
    missing id as a permission problem would only mislead honest clients.

One exception, and it arrived with platform_staff: Foodishi's own staff reach
outside every restaurant by definition, and an operator staffs none of them, so
the membership filter used to hand the operations console a 403 for the board
that is its whole purpose. They are admitted to the order *listing*, and only to
read it — see platform_reader. Nothing else here consults it: staff_of_order,
admin_of_restaurant, admin_of_menu_* and RestaurantScope.require are
untouched, so a support agent still cannot advance an order's status or edit a
kitchen's menu.

Every restaurant check ultimately runs through identity.require_staff, so the
membership rule, the 403 wording and the audit log line have a single
definition. Nothing here is weakened by AUTH_ENABLED=false: the dev escape hatch
fakes identity, never permissions, so flipping the flag is the only change
needed to go live.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import not_found
from app.db import SessionDep
from app.dependencies.identity import (
    PLATFORM_ROLE_RANK,
    CurrentUser,
    forbidden,
    optional_platform_staff,
    require_staff,
)
from app.models.catalog import MenuCategory, MenuItem, Restaurant
from app.models.delivery import Delivery
from app.models.enums import PlatformRole, StaffRole
from app.models.order import Order
from app.models.platform import PlatformStaff
from app.models.staff import RestaurantStaff
from app.models.user import User
from app.services import permissions

logger = logging.getLogger(__name__)

# Where a customer's own orders live. GET /orders is the partner queue; a
# caller who works for no restaurant and is not platform staff is pointed here
# rather than being handed an empty page they cannot explain.
CUSTOMER_ORDERS_PATH = "/me/orders"

# session, row id -> the restaurant that owns the row, or None if there is no
# such row.
RestaurantResolver = Callable[[AsyncSession, int], Awaitable[int | None]]

# Every id these dependencies look up is a PostgreSQL `integer` primary key.
# A value outside that range is not a row that is missing, it is a value the
# column cannot hold — asyncpg raises DataError on the bind, which surfaces as
# a 500 rather than the 422 the caller deserves.
PG_INT_MAX = 2**31 - 1


def path_int(request: Request, name: str, *, caller: str) -> int:
    """Read an integer id straight off the path, or refuse the request as 422.

    These dependencies run before FastAPI has coerced the handler's own
    `order_id: int` annotation, so they see the raw path segment. A bare
    int() on it turns `/deliveries/abc` into an unhandled ValueError and a
    500; raising RequestValidationError instead produces exactly the 422 body
    FastAPI would have produced for the path parameter on its own.
    """
    raw = request.path_params.get(name)
    if raw is None:
        # A wiring mistake, not a bad request: this dependency was attached to
        # a route that has no such segment.
        raise RuntimeError(
            f"{caller} found no {name} path parameter on {request.url.path}."
        )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad_path_int(name, raw, "unable to parse string as an integer") from None
    if not 0 < value <= PG_INT_MAX:
        raise _bad_path_int(name, raw, "id is out of range")
    return value


def _bad_path_int(name: str, raw: object, msg: str) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "int_parsing",
                "loc": ("path", name),
                "msg": f"Input should be a valid integer, {msg}",
                "input": raw,
            }
        ]
    )


async def assert_staff(
    request: Request,
    session: AsyncSession,
    user: User,
    restaurant_id: int,
    minimum_role: StaffRole = StaffRole.STAFF,
) -> RestaurantStaff:
    """The single membership check behind everything in this module.

    Delegates to identity.require_staff rather than re-querying
    restaurant_staff, so there is exactly one implementation of the role ladder
    and one wording of the refusal. The Request is only along for the ride:
    require_staff reads {restaurant_id} off the path when no restaurant is
    pinned, and here one always is.
    """
    check = require_staff(restaurant_id=restaurant_id, minimum_role=minimum_role)
    return await check(request, session, user)


# The rung at which a platform_staff row starts admitting platform-wide reads,
# shared with app/dependencies/ownership.py. Support is the bottom of the ladder
# and reading is what the support tier does, so every active operator clears it.
# It is still written as a rank comparison rather than as "has a platform_staff
# row at all", so that a rung added *below* support later — an outsourced queue,
# a read-only auditor — does not inherit the platform's order history merely by
# existing.
PLATFORM_READ_FLOOR = PlatformRole.ADMIN


async def platform_reader(session: AsyncSession, user: User) -> PlatformStaff | None:
    """The caller's platform row, if it admits them to platform-wide reads.

    Returns None rather than raising, which is the whole reason this is not
    identity.require_platform_role: every check that consults this has other
    ways to pass — the customer owns the row, the manager staffs the restaurant
    — so a refusal here has to be something the caller can fall through.
    require_platform_role is still the right dependency for a route that is only
    ever an operator's.

    Read-only by construction: this answers "may they see it", never "may they
    change it". No write path in this module calls it.
    """
    staff = await optional_platform_staff(session, user)
    if staff is None:
        return None
    if PLATFORM_ROLE_RANK[staff.role] < PLATFORM_ROLE_RANK[PLATFORM_READ_FLOOR]:
        return None
    return staff


async def _order_restaurant(session: AsyncSession, order_id: int) -> int | None:
    return await session.scalar(
        select(Order.restaurant_id).where(Order.id == order_id)
    )


async def _delivery_restaurant(session: AsyncSession, delivery_id: int) -> int | None:
    # A delivery has no restaurant of its own; it inherits the one on its order.
    return await session.scalar(
        select(Order.restaurant_id)
        .join(Delivery, Delivery.order_id == Order.id)
        .where(Delivery.id == delivery_id)
    )


async def _menu_category_restaurant(
    session: AsyncSession, category_id: int
) -> int | None:
    return await session.scalar(
        select(MenuCategory.restaurant_id).where(MenuCategory.id == category_id)
    )


async def _menu_item_restaurant(session: AsyncSession, item_id: int) -> int | None:
    return await session.scalar(
        select(MenuItem.restaurant_id).where(MenuItem.id == item_id)
    )


def staff_of_row(
    *,
    path_param: str,
    resolve: RestaurantResolver,
    resource: str,
    minimum_role: StaffRole = StaffRole.STAFF,
) -> Callable[..., Awaitable[RestaurantStaff]]:
    """Build a dependency for a route whose restaurant is one hop off the path.

    `path_param` names the row's id in the route, `resolve` turns that id into
    a restaurant id, and `resource` is how a missing row is described in the
    404 — matched to the wording the handlers already used so this dependency
    running first does not change what clients see for a bad id.
    """

    async def dependency(
        request: Request, session: SessionDep, user: CurrentUser
    ) -> RestaurantStaff:
        row_id = path_int(request, path_param, caller="staff_of_row()")

        restaurant_id = await resolve(session, row_id)
        if restaurant_id is None:
            raise not_found(resource, row_id)
        return await assert_staff(
            request, session, user, restaurant_id, minimum_role
        )

    return dependency


# --- Route dependencies -------------------------------------------------
# Attached with dependencies=[Depends(...)] where the handler does not need the
# staff row, and through the Annotated aliases below where it does.

# Routes that carry {restaurant_id} themselves. Catalog writes are a manager's
# job: a shift worker marking an item unavailable is a different feature.
admin_of_restaurant = require_staff(minimum_role=StaffRole.ADMIN)

staff_of_order = staff_of_row(
    path_param="order_id", resolve=_order_restaurant, resource="order"
)
staff_of_delivery = staff_of_row(
    path_param="delivery_id", resolve=_delivery_restaurant, resource="delivery"
)
admin_of_menu_category = staff_of_row(
    path_param="category_id",
    resolve=_menu_category_restaurant,
    resource="menu category",
    minimum_role=StaffRole.ADMIN,
)
admin_of_menu_item = staff_of_row(
    path_param="item_id",
    resolve=_menu_item_restaurant,
    resource="menu item",
    minimum_role=StaffRole.ADMIN,
)

StaffOfOrder = Annotated[RestaurantStaff, Depends(staff_of_order)]


# --- Platform WRITE admission ------------------------------------------------
#
# ownership.py's platform widening is confined to SAFE_METHODS on purpose, so
# that "the operator console can open an order or a profile but cannot cancel,
# refund or edit through the same dependency". That boundary is right and is left
# alone: the two dependencies below are NEW and NARROW, not a loosening of it.
#
# They exist because the operations console has screens that write, and every one
# of them was answering 403: the restaurant drawer's Save details and its
# Taking-orders switch, and deactivating a customer. A console that renders an
# edit form it cannot submit is worse than one that does not render it.
#
# Deliberately NOT extended to anything else. Cancelling an order, issuing a
# refund and editing a menu all stay closed to platform staff, because each has a
# restaurant-side actor whose accountability the audit trail depends on.


async def _is_platform_admin(session: AsyncSession, user: User) -> bool:
    staff = await optional_platform_staff(session, user)
    if staff is None:
        return False
    return PLATFORM_ROLE_RANK[staff.role] >= PLATFORM_ROLE_RANK[PlatformRole.ADMIN]


async def admin_of_restaurant_or_platform(
    request: Request, session: SessionDep, user: CurrentUser
) -> None:
    """The restaurant's own admin, or a Foodishi platform admin.

    Used only by PATCH /restaurants/{id} and PUT /restaurants/{id}/availability.
    catalog_admin.py notes that "Foodishi's operators staff no restaurant, so
    require_staff refuses them here" -- true, and the consequence was that the
    operator console could not close a kitchen that was causing a problem, which
    is one of the few genuinely urgent things an operations team does.

    Platform admin is checked FIRST and cheaply: an operator staffs no restaurant,
    so running the membership check on them only to discard its 403 would log a
    refusal on every legitimate write.
    """
    if await _is_platform_admin(session, user):
        return
    restaurant_id = path_int(
        request, "restaurant_id", caller="admin_of_restaurant_or_platform"
    )
    await assert_staff(request, session, user, restaurant_id, StaffRole.ADMIN)


async def writable_user(
    request: Request, session: SessionDep, user: CurrentUser
) -> None:
    """The caller's own profile, or any profile if they are a platform admin.

    PATCH /users/{id} was guarded by `readable_user`, whose platform branch is
    read-only -- so an operator could open a customer and not deactivate one,
    which is the entire point of the customers screen.

    Note what this does NOT admit: restaurant staff. A kitchen can see the
    customer on an order it is cooking and has no business editing them.
    """
    user_id = path_int(request, "user_id", caller="writable_user")
    if user_id == user.id:
        return
    if await _is_platform_admin(session, user):
        return
    raise forbidden("You do not have access to this resource")


def staff_of_order_with(permission: str) -> Callable[..., Awaitable[RestaurantStaff]]:
    """staff_of_order, then the per-person permission on top of the role.

    app/services/permissions.py calls itself "the authority" on what a staff
    member may do and was consulted by NOTHING: resolve() built a response body
    and validate_grants() checked a stored list, and no route dependency ever
    asked. So the two permissions deliberately withheld from STAFF_FLOOR --
    orders.reject and orders.cancel, the two that "turn a customer away" -- were
    held by every shift worker regardless of what an admin granted. The partner
    console hid the buttons; the API answered anyway, which made the hiding
    cosmetic and the grant list decorative.

    Layered on top of staff_of_order rather than replacing it, so membership and
    permission stay two separate questions with two separate answers.
    """

    async def dependency(
        staff: StaffOfOrder,
    ) -> RestaurantStaff:
        if permission not in permissions.resolve(staff.role, staff.permissions):
            raise forbidden(
                f"Your access at this restaurant does not include {permission}"
            )
        return staff

    return dependency
StaffOfDelivery = Annotated[RestaurantStaff, Depends(staff_of_delivery)]


async def _staffed_restaurant_ids(
    session: AsyncSession, user_id: int
) -> frozenset[int]:
    rows = await session.execute(
        select(RestaurantStaff.restaurant_id).where(
            RestaurantStaff.user_id == user_id,
            RestaurantStaff.is_active,
        )
    )
    return frozenset(rows.scalars())


async def _all_restaurant_ids(session: AsyncSession) -> frozenset[int]:
    # Every restaurant, active or not: a kitchen that has been switched off
    # still has order history, and hiding it from ops would hide exactly the
    # orders someone is most likely to be asking about.
    rows = await session.execute(select(Restaurant.id))
    return frozenset(rows.scalars())


async def order_list_restaurants(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    restaurant_id: int | None = Query(
        default=None,
        description="Restrict to one restaurant. Must be one you are staff of, "
        "unless you are Foodishi platform staff. Omit it to get every restaurant "
        "you work for — or, for platform staff, the whole platform.",
    ),
) -> frozenset[int]:
    """The restaurants GET /orders may return to this caller.

    Naming a restaurant asks for that one and is checked against membership;
    omitting it means "every restaurant I work for", which is a filter read out
    of restaurant_staff rather than off the query string. Either way the handler
    is handed a set of ids that it must filter by, so there is no path through
    the listing that is unscoped — dropping the query parameter widens nothing.

    Platform staff are the exception. They staff no restaurant, so the
    membership filter refused them the platform-wide board that is the whole
    point of the operations console; for them the set is enumerated from
    `restaurants` instead. Note what that deliberately is NOT: it is not
    "return no filter". Returning an id set keeps the handler's
    `Order.restaurant_id.in_(...)` the only shape this dependency can produce,
    so the worst a bug in here can do is list the wrong restaurants — it can
    never drop the WHERE clause, which is the failure mode a sentinel meaning
    "unscoped" would have introduced. The price is an IN list that grows with
    the platform, and a set that is empty when `restaurants` is empty. That
    last case is the only way the result is ever empty, it is reachable only by
    platform staff, and it is the truthful answer: a platform with no
    restaurants has no restaurant orders. For restaurant staff the set is
    non-empty exactly as it always was.
    """
    # Resolved before the membership check rather than after, because
    # assert_staff raises: an operator naming a restaurant they do not staff —
    # which is all of them — must not be turned away by it. One indexed lookup
    # on platform_staff, on a listing that already runs two queries.
    platform = await platform_reader(session, user)

    if restaurant_id is not None:
        if platform is None:
            await assert_staff(request, session, user, restaurant_id)
        return frozenset({restaurant_id})

    if platform is not None:
        return await _all_restaurant_ids(session)

    staffed = await _staffed_restaurant_ids(session, user.id)
    if not staffed:
        logger.info(
            "Order listing refused: user=%s staffs no restaurant and is not "
            "platform staff",
            user.id,
        )
        raise forbidden(
            "This listing returns restaurant orders and you are not staff of "
            f"any restaurant. Your own orders are at {CUSTOMER_ORDERS_PATH}."
        )
    return staffed


OrderListRestaurants = Annotated[frozenset[int], Depends(order_list_restaurants)]


@dataclass(frozen=True)
class RestaurantScope:
    """A membership check the handler runs itself, once it has the body.

    POST /menu-items names its restaurant in the payload. A dependency cannot
    read that without either parsing the body a second time or declaring the
    same body model twice — and the second one makes FastAPI nest the request
    under a key, changing the wire format for every existing client. So for
    body-addressed writes the check is one awaited line at the top of the
    handler, while the rule it enforces still lives in this module.
    """

    request: Request
    session: AsyncSession
    user: User

    async def require(
        self, restaurant_id: int, minimum_role: StaffRole = StaffRole.STAFF
    ) -> RestaurantStaff:
        return await assert_staff(
            self.request, self.session, self.user, restaurant_id, minimum_role
        )

    async def admin_restaurant_ids(self) -> frozenset[int]:
        """Restaurants this caller administers. Empty when they administer none.

        For a listing that has to be narrowed to "mine" rather than refused
        outright — the coupon board, where a restaurant admin sees their own
        kitchens' discounts. `require` is still the right call for a write
        against one named restaurant; this is only for building a filter.

        Returns a set rather than raising: an empty result and a 403 are
        different answers, and the caller decides which one its route owes.
        """
        rows = await self.session.scalars(
            select(RestaurantStaff.restaurant_id).where(
                RestaurantStaff.user_id == self.user.id,
                RestaurantStaff.is_active,
                RestaurantStaff.role == StaffRole.ADMIN,
            )
        )
        return frozenset(rows)


async def restaurant_scope(
    request: Request, session: SessionDep, user: CurrentUser
) -> RestaurantScope:
    return RestaurantScope(request=request, session=session, user=user)


RestaurantScopeDep = Annotated[RestaurantScope, Depends(restaurant_scope)]
