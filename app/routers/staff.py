"""Who may act for a restaurant, and as what.

Two audiences share this file. Restaurant admins manage their own roster under
/restaurants/{id}/staff and /staff/{id}; every signed-in caller asks
/me/restaurants which restaurants they may act for at all.

Read app/dependencies/identity.py first. Permissions are checked against
restaurant_staff on every request even while AUTH_ENABLED is false — the dev
escape hatch fakes identity, never authority — so an admin of restaurant 1
cannot reach restaurant 2 by editing the id in the URL.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    NO_PROFILE,
    UNAUTHENTICATED,
    CurrentUser,
    require_staff,
)
from app.dependencies.scope import assert_staff
from app.models.catalog import Restaurant
from app.models.enums import StaffRole
from app.models.staff import RestaurantStaff
from app.models.user import User
from app.schemas.staff import (
    MyRestaurantRead,
    StaffAccessReset,
    StaffCreate,
    StaffMemberRead,
    StaffPermissionsUpdate,
    StaffRead,
    StaffUpdate,
    StaffUserRead,
)
from app.services import permissions

logger = logging.getLogger(__name__)

# No prefix: the roster hangs off /restaurants/{id}, a single membership is
# addressed by its own id, and the picker lives under /me.
router = APIRouter(tags=["staff"])

# Every route here needs an identity; all but /me/restaurants also need a
# permission over one particular restaurant.
IDENTIFIED = {**UNAUTHENTICATED, **NO_PROFILE}
PERMITTED = {**IDENTIFIED, **FORBIDDEN}

LAST_ADMIN = {
    409: {"description": "Would leave the restaurant with no active admin"}
}

# Named so the insert below can tell a duplicate membership apart from a
# foreign key that no longer resolves.
UNIQUE_MEMBERSHIP = "uq_staff_user_restaurant"


async def _staff_row(session: AsyncSession, staff_id: int) -> RestaurantStaff:
    staff = await session.get(RestaurantStaff, staff_id)
    if staff is None:
        raise not_found("staff member", staff_id)
    return staff


async def _require_admin_of(
    request: Request, session: AsyncSession, user: User, restaurant_id: int
) -> RestaurantStaff:
    """require_staff's rules, for a restaurant only known after a lookup.

    /staff/{staff_id} carries no {restaurant_id} segment for the dependency to
    read, so the check runs in the handler once the row has named its
    restaurant. Delegating to scope.assert_staff — which itself delegates to
    require_staff — keeps one definition of what "admin" means, ladder and
    revocation included, and one wording of the refusal.

    Note the ordering this forces: a caller who is not an admin learns that the
    staff id exists (404 vs 403) but nothing about whose it is.
    """
    return await assert_staff(
        request, session, user, restaurant_id, StaffRole.ADMIN
    )


async def _active_admin_ids(
    session: AsyncSession, restaurant_id: int
) -> frozenset[int]:
    """Every active admin of this restaurant, locked for this transaction.

    SELECT ... FOR UPDATE over the whole admin set is what makes the last-admin
    rule hold under concurrency. Two admins resigning at the same instant would
    otherwise each see the other and both succeed, leaving a restaurant with no
    admin. Locking the same rows in the same order serialises the pair; the
    loser re-reads a set the winner has already left and gets the 409.

    ADMIN is compared for equality rather than by rank because it is the top of
    the ladder in app/models/enums.py — there is no tier above it to count.
    """
    rows = await session.execute(
        select(RestaurantStaff.id)
        .where(
            RestaurantStaff.restaurant_id == restaurant_id,
            RestaurantStaff.role == StaffRole.ADMIN,
            RestaurantStaff.is_active.is_(True),
        )
        .order_by(RestaurantStaff.id)  # stable lock order, so no deadlock
        .with_for_update()
    )
    return frozenset(rows.scalars())


async def _assert_not_last_admin(
    session: AsyncSession, staff: RestaurantStaff
) -> None:
    """Refuse a change that would leave a restaurant with nobody in charge.

    A restaurant with no admin cannot be repaired through this API at all: only
    an admin may add staff, so there would be no one left who could appoint one.

    Whether this row counts as an active admin is decided by the locked set,
    not by the copy the handler loaded a moment earlier. That copy is stale
    exactly when it matters: a row promoted to admin between that load and here
    still looks like a plain staff row, and deleting it while it is the
    restaurant's only admin is the one outcome this function exists to prevent.
    """
    admin_ids = await _active_admin_ids(session, staff.restaurant_id)
    if staff.id not in admin_ids:
        return  # not an active admin, so this change removes no admin
    if len(admin_ids) == 1:
        raise conflict(
            f"Staff member {staff.id} is the last active admin of restaurant "
            f"{staff.restaurant_id}. Appoint another admin first."
        )


def _drops_admin(staff: RestaurantStaff, changes: dict) -> bool:
    """True when the patched row would not be an active admin.

    Deliberately conservative: it answers "could this change cost the
    restaurant an admin", and _assert_not_last_admin then decides on fresh,
    locked rows whether it actually does. So a patch that leaves the row an
    active admin under either the current or the stale value of an untouched
    field still gets checked, and only a patch that plainly adds or keeps admin
    rights skips the query.
    """
    role = changes.get("role", staff.role)
    is_active = changes.get("is_active", staff.is_active)
    return role != StaffRole.ADMIN or not is_active


@router.get(
    "/restaurants/{restaurant_id}/staff",
    response_model=Page[StaffMemberRead],
    dependencies=[Depends(require_staff(minimum_role=StaffRole.ADMIN))],
    responses=PERMITTED,
    summary="List a restaurant's staff",
)
async def list_staff(
    restaurant_id: int,
    session: SessionDep,
    page: PageDep,
    is_active: bool | None = None,
):
    """The roster, newest membership last.

    Revoked members are included by default: the point of is_active over a
    delete is that the record survives, and an admin needs to see who once had
    access. Pass is_active=true for the working roster.
    """
    statement = select(RestaurantStaff).where(
        RestaurantStaff.restaurant_id == restaurant_id
    )
    if is_active is not None:
        statement = statement.where(RestaurantStaff.is_active.is_(is_active))

    items, total = await paginate(session, statement.order_by(RestaurantStaff.id), page)
    people = await _users_by_id(session, [row.user_id for row in items])
    return Page[StaffMemberRead](
        items=[_member(row, people[row.user_id]) for row in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


async def _users_by_id(
    session: AsyncSession, user_ids: Sequence[int]
) -> dict[int, User]:
    """One extra query for the whole page, rather than one per row."""
    if not user_ids:
        return {}
    rows = await session.execute(select(User).where(User.id.in_(set(user_ids))))
    return {user.id: user for user in rows.scalars()}


def _read(staff: RestaurantStaff) -> StaffRead:
    """One builder for every staff response.

    Built field by field rather than `model_validate(staff)` because
    `effective_permissions` is not a column — it is the role floor and the
    grants resolved together, and resolving it here means no client has to
    reimplement that rule and get it subtly wrong.
    """
    return StaffRead(
        id=staff.id,
        user_id=staff.user_id,
        restaurant_id=staff.restaurant_id,
        role=staff.role,
        is_active=staff.is_active,
        # An admin's stored grants are meaningless — their role already carries
        # everything — so they are reported empty rather than as leftovers from
        # before a promotion.
        permissions=[] if staff.role is StaffRole.ADMIN else sorted(staff.permissions or []),
        effective_permissions=sorted(permissions.resolve(staff.role, staff.permissions)),
        access_reset_at=staff.access_reset_at,
        created_at=staff.created_at,
        updated_at=staff.updated_at,
    )


def _member(staff: RestaurantStaff, user: User) -> StaffMemberRead:
    # user is never missing: restaurant_staff.user_id is ON DELETE CASCADE, so
    # a deleted person takes their memberships with them.
    return StaffMemberRead(
        **_read(staff).model_dump(),
        user=StaffUserRead.model_validate(user),
    )


async def _subject(session: AsyncSession, payload: StaffCreate) -> User:
    """The person being granted access, from whichever handle the caller had.

    Both branches end in one User row and share the deactivated check below, so
    an admin adding by email gets the same answers — and the same refusals — as
    an operator adding by id. Nothing about anybody else is disclosed either
    way: an address that matches nothing is a 404 on the address the caller
    typed, which they already knew.
    """
    if payload.user_id is not None:
        user = await session.get(User, payload.user_id)
        if user is None:
            raise not_found("user", payload.user_id)
        return user

    # POST and PATCH /users lowercase the address before storing it, so that
    # the unique index is effectively case-insensitive. Matching that here
    # keeps the lookup on the index, where wrapping the column in lower() would
    # not — and an admin who typed Priya@ still finds priya@.
    email = payload.email.lower()  # never None: see StaffCreate's validator
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No Foodishi account for {email}. They need to sign up before they "
            "can be given access to a restaurant.",
        )
    return user


@router.post(
    "/restaurants/{restaurant_id}/staff",
    response_model=StaffRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_staff(minimum_role=StaffRole.ADMIN))],
    responses={**PERMITTED, **NOT_FOUND, **CONFLICT},
    summary="Add a staff member (restaurant admins only)",
)
async def add_staff(restaurant_id: int, payload: StaffCreate, session: SessionDep):
    """Grant someone access to this restaurant.

    Name them by user_id or by email — see StaffCreate for why both exist. The
    restaurant is not one of the choices: it comes from the path and the
    caller's own admin row, never from the body, or an admin of restaurant 1
    could staff restaurant 2.
    """
    user = await _subject(session, payload)
    if not user.is_active:
        # current_user 403s a deactivated account, so granting one access
        # creates a permission the API will never honour — and which silently
        # becomes live the day the account is reactivated. Refuse it here.
        raise conflict(
            f"{user.email} is deactivated; reactivate the account before "
            "granting restaurant access"
        )

    # Read first, so the common mistake (re-adding someone who was revoked)
    # gets an answer that says what to do instead of a bare "already exists".
    existing = await session.scalar(
        select(RestaurantStaff).where(
            RestaurantStaff.user_id == user.id,
            RestaurantStaff.restaurant_id == restaurant_id,
        )
    )
    if existing is not None:
        raise _already_staff(existing)

    staff = RestaurantStaff(
        restaurant_id=restaurant_id, user_id=user.id, role=payload.role
    )
    session.add(staff)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _insert_failure(exc, restaurant_id, user.id) from exc
    await session.refresh(staff)  # picks up server-side defaults
    logger.info(
        "Added user %s to restaurant %s as %s",
        user.id,
        restaurant_id,
        payload.role,
    )
    return _read(staff)


def _already_staff(existing: RestaurantStaff) -> HTTPException:
    return conflict(
        f"User {existing.user_id} is already staff of restaurant "
        f"{existing.restaurant_id} as {existing.role} "
        f"(is_active={existing.is_active}). "
        f"PATCH /staff/{existing.id} to change their role or restore access."
    )


def _insert_failure(
    exc: IntegrityError, restaurant_id: int, user_id: int
) -> HTTPException:
    """Tell the two integrity failures this insert can hit apart.

    Matching on the constraint name rather than assuming: a foreign key that
    stopped resolving mid-request is a 404, and reporting it as a duplicate
    would send the owner looking for a membership that is not there.
    """
    if UNIQUE_MEMBERSHIP in str(exc.orig):
        # Lost a race with a concurrent add of the same person.
        return conflict(
            f"User {user_id} is already staff of restaurant {restaurant_id}"
        )
    # The user was verified a moment ago, so the restaurant is what vanished.
    return not_found("restaurant", restaurant_id)


@router.patch(
    "/staff/{staff_id}",
    response_model=StaffRead,
    responses={**PERMITTED, **NOT_FOUND, **LAST_ADMIN},
    summary="Change a staff member's role or access (restaurant admins only)",
)
async def update_staff(
    staff_id: int,
    payload: StaffUpdate,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
):
    """Promote, demote, revoke or restore one membership.

    An admin may do this to themselves — stepping down is legitimate — but not
    while they are the last active admin of the restaurant.
    """
    staff = await _staff_row(session, staff_id)
    await _require_admin_of(request, session, user, staff.restaurant_id)

    changes = payload.model_dump(exclude_unset=True)
    if _drops_admin(staff, changes):
        await _assert_not_last_admin(session, staff)

    # synchronize_session="fetch" because this row is already in the identity
    # map (loaded above, and possibly again by the owner check); without it the
    # in-session instance would keep its pre-update values.
    statement = (
        update(RestaurantStaff)
        .where(RestaurantStaff.id == staff_id)
        .values(**changes)
        .returning(RestaurantStaff)
        .execution_options(synchronize_session="fetch")
    )
    updated = (await session.execute(statement)).scalar_one_or_none()
    if updated is None:
        # It existed at the top of this handler, so it was removed mid-request.
        raise conflict("This staff member was removed while the update was in flight")

    logger.info("Updated staff %s: %s", staff_id, changes)
    return _read(updated)


@router.delete(
    "/staff/{staff_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**PERMITTED, **NOT_FOUND, **LAST_ADMIN},
    summary="Remove a staff member (restaurant admins only)",
)
async def remove_staff(
    staff_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    """Delete a membership outright.

    Prefer PATCH is_active=false when the person may come back or when the
    record of their access matters; this is for rows added by mistake.
    """
    staff = await _staff_row(session, staff_id)
    await _require_admin_of(request, session, user, staff.restaurant_id)
    await _assert_not_last_admin(session, staff)

    restaurant_id = staff.restaurant_id  # read before the row goes away
    statement = (
        delete(RestaurantStaff)
        .where(RestaurantStaff.id == staff_id)
        .returning(RestaurantStaff.id)
        .execution_options(synchronize_session="fetch")
    )
    deleted = (await session.execute(statement)).scalar_one_or_none()
    if deleted is None:
        # RETURNING distinguishes "removed one row" from "matched nothing", so
        # a repeat delete reports 404 rather than a silent success.
        raise not_found("staff member", staff_id)
    logger.info("Removed staff %s from restaurant %s", staff_id, restaurant_id)


@router.patch(
    "/staff/{staff_id}/permissions",
    response_model=StaffRead,
    responses={**PERMITTED, **NOT_FOUND},
    summary="Grant or withdraw a staff member's extra permissions (admins only)",
)
async def set_staff_permissions(
    staff_id: int,
    payload: StaffPermissionsUpdate,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
):
    """Replace the whole grant list for one membership.

    Its own route rather than a field on PATCH /staff/{id} because it is a
    different decision with a different shape: role and is_active are single
    values, this is a SET that has to be replaced wholesale for an unticked box
    to mean anything at all.

    What may be granted is app/services/permissions.py's decision, not this
    route's. Two refusals come back from it and they are told apart on purpose —
    "that is not a permission" and "that one is a manager's" are different
    mistakes needing different answers.

    An admin cannot be given grants: their role already carries everything, so a
    grant list on an admin row is noise that would become live the day somebody
    demotes them.
    """
    staff = await _staff_row(session, staff_id)
    await _require_admin_of(request, session, user, staff.restaurant_id)

    try:
        granted = permissions.validate_grants(staff.role, payload.granted)
    except permissions.InvalidGrant as exc:
        raise unprocessable(str(exc)) from exc

    staff.permissions = granted
    await session.flush()
    await session.refresh(staff)
    logger.info(
        "Set permissions on staff %s (restaurant %s) to %s",
        staff_id,
        staff.restaurant_id,
        granted,
    )
    return _read(staff)


@router.post(
    "/staff/{staff_id}/reset-access",
    response_model=StaffAccessReset,
    responses={**PERMITTED, **NOT_FOUND, **CONFLICT},
    summary="End a staff member's sessions and send a fresh sign-in link",
)
async def reset_staff_access(
    staff_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
):
    """Stamp the reset and report where the link would go.

    **No password is set, generated, mailed or returned by this route.** That is
    the security property worth guarding: an endpoint that handed back a
    credential would give every restaurant admin a way to take over a staff
    member's account — including an admin at another restaurant they happen to
    share a person with.

    There is no email provider and no Supabase admin call wired in, so what
    happens today is that `access_reset_at` is stamped and the address is echoed
    back. `sessions_revoked` is False to say exactly that: the console must not
    tell somebody their sessions ended when nothing ended them. When a provider
    is wired up it sets that flag and the response shape does not change.

    Refused for a revoked membership — there is no access to reset, and sending a
    sign-in link to somebody whose access was taken away is the opposite of what
    the admin meant.
    """
    staff = await _staff_row(session, staff_id)
    await _require_admin_of(request, session, user, staff.restaurant_id)

    if not staff.is_active:
        raise conflict(
            f"Staff member {staff_id} has no access to this restaurant, so there "
            "is nothing to reset. Restore their access first."
        )

    subject = await session.get(User, staff.user_id)
    if subject is None:  # pragma: no cover - user_id cascades on delete
        raise not_found("user", staff.user_id)

    reset_at = datetime.now(UTC)
    staff.access_reset_at = reset_at
    await session.flush()
    logger.info(
        "Access reset stamped for staff %s (user %s, restaurant %s) by user %s; "
        "no sessions revoked because no auth provider is configured",
        staff_id,
        staff.user_id,
        staff.restaurant_id,
        user.id,
    )
    return StaffAccessReset(
        staff_id=staff_id,
        email=subject.email,
        reset_at=reset_at,
        sessions_revoked=False,
    )


@router.get(
    "/me/restaurants",
    response_model=list[MyRestaurantRead],
    tags=["me"],
    responses=IDENTIFIED,
    summary="Restaurants the caller may act for",
)
async def list_my_restaurants(session: SessionDep, user: CurrentUser):
    """What the partner app calls on load to fill its restaurant picker.

    The subject is the token's, never a parameter, so there is no id to swap
    and no way to enumerate other people's restaurants. Revoked memberships are
    filtered out here rather than in the client: a picker entry the caller
    cannot actually use is a support ticket waiting to happen.

    Unpaginated by design — one person works at a handful of restaurants, and
    a picker that arrives in pages is worse than one that arrives whole.
    """
    rows = await session.execute(
        select(Restaurant, RestaurantStaff.role)
        .join(RestaurantStaff, RestaurantStaff.restaurant_id == Restaurant.id)
        .where(
            RestaurantStaff.user_id == user.id,
            RestaurantStaff.is_active.is_(True),
        )
        .order_by(Restaurant.name)
    )
    return [
        MyRestaurantRead(
            id=restaurant.id,
            name=restaurant.name,
            slug=restaurant.slug,
            city=restaurant.city,
            image_url=restaurant.image_url,
            is_active=restaurant.is_active,
            role=role,
        )
        for restaurant, role in rows
    ]
