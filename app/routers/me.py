"""The signed-in caller's own profile and orders.

Every route here derives the subject from the verified token, never from a path
segment or a query parameter, which is what separates it from /users/{id} and
/orders?user_id=. Read app/dependencies/identity.py first: while AUTH_ENABLED is
false, "the verified token" may be an unsigned X-Dev-User-Id header.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import CONFLICT, conflict, unprocessable
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    DEV_USER_CLAIM,
    DEV_USER_HEADER,
    FORBIDDEN,
    NO_PROFILE,
    PROFILE_LINK_PATH,
    UNAUTHENTICATED,
    CurrentClaims,
    CurrentUser,
    OptionalPlatformStaff,
)
from app.models.enums import OrderStatus
from app.models.user import User
from app.repositories import orders as repo
from app.schemas.me import MeProfile, MeUpdate, ProfileLink
from app.schemas.order import OrderRead
from app.schemas.user import UserRead

logger = logging.getLogger(__name__)

router = APIRouter(tags=["me"])

EMAIL_MAX_LENGTH = 200  # users.email is String(200)

# Supabase's own validation is not ours: claims are external data, so the
# address is validated before it becomes a row.
_EMAIL_ADAPTER = TypeAdapter(EmailStr)

ALREADY_LINKED = {
    409: {"description": "That email already belongs to another account"}
}
NOT_LINKABLE = {
    422: {"description": "This identity carries no email address to link with"}
}

# postgres SQLSTATEs. users.auth_user_id is a real foreign key into auth.users,
# so writing one is a different failure from clashing on the unique email index
# and must not be reported as though it were.
FOREIGN_KEY_VIOLATION = "23503"


def _reject_missing_identity(exc: IntegrityError) -> None:
    """422 when the write failed because auth.users no longer holds this id.

    A verified token outlives the account it names: delete the auth user and
    its still-unexpired tokens keep passing signature checks. Reporting that as
    a 409 "retry" would send the client round a loop that can never succeed, so
    name the real cause instead — the fix is to sign in again, not to retry.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == FOREIGN_KEY_VIOLATION:
        raise unprocessable(
            "This sign-in no longer corresponds to an account — sign in again"
        ) from exc


def _auth_user_id(claims: dict[str, Any]) -> UUID:
    """The Supabase identity to link, or 422 if there is not a real one."""
    if claims.get(DEV_USER_CLAIM) is not None:
        # A dev request already names a profile by id; there is no Supabase
        # identity behind it, so there is nothing to link it to.
        raise unprocessable(
            f"{DEV_USER_HEADER} already names a profile — linking needs a real "
            "Supabase access token"
        )
    try:
        return UUID(claims["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Link attempt with a non-uuid sub: %r", claims.get("sub"))
        raise unprocessable("This access token carries no usable account id") from exc


def _account_email(claims: dict[str, Any]) -> str:
    """The verified address, lowercased to match the unique index on users.email."""
    raw = claims.get("email") or (claims.get("user_metadata") or {}).get("email")
    if not raw:
        raise unprocessable(
            "This account has no email address, so it cannot be linked to a "
            "profile. Add an email in Supabase Auth and try again."
        )
    try:
        email = _EMAIL_ADAPTER.validate_python(raw.strip()).lower()
    except ValidationError as exc:
        logger.warning("Verified token carried an unusable email claim")
        raise unprocessable("This account's email address is not usable") from exc
    if len(email) > EMAIL_MAX_LENGTH:
        # Reported apart from "no email": an address that is merely too long to
        # store is not an account missing one, and telling the caller to go add
        # an email in Supabase would send them after the wrong fix.
        logger.warning("Verified token carried an email longer than the column")
        raise unprocessable("This account's email address is too long to store")
    return email


def _reject_deactivated(user: User) -> None:
    """409 for a profile that is switched off.

    Enforced on every path through POST /auth/link, not only when claiming an
    unlinked row: a deactivated profile 403s on GET /me and everywhere else, so
    handing its owner a 200 and a full profile body here would tell them the
    sign-in worked seconds before the next request says it did not.
    """
    if not user.is_active:
        raise conflict(
            f"The profile for {user.email!r} is deactivated — contact support"
        )


@router.post(
    PROFILE_LINK_PATH,
    response_model=UserRead,
    tags=["auth"],
    responses={**UNAUTHENTICATED, **ALREADY_LINKED, **NOT_LINKABLE},
    summary="Link this signed-in account to a profile",
)
async def link_profile(
    payload: ProfileLink, session: SessionDep, claims: CurrentClaims
):
    """Attach the caller's Supabase identity to a public.users row.

    Three outcomes, in this order:

    1. Already linked -> the same profile comes back. Idempotent on purpose:
       a client that retries a dropped response, or a frontend that calls this
       on every sign-in, must not end up with two profiles.
    2. An unlinked profile carries the token's email -> that row is claimed.
       This is the case worth the code. The 150 seeded users already own
       orders, addresses and coupon redemptions keyed to their integer id;
       when the real person signs up with the same address, matching on email
       hands them their history instead of an empty account beside it. The
       address is safe to match on because Supabase verified it, and because
       public.users.email is unique so at most one row can match.
    3. Nothing matches -> a fresh profile.

    Always 200, never 201: the caller cannot tell which of the three happened
    and does not need to, and a status code that changes on retry would defeat
    the idempotency this endpoint exists to provide.
    """
    auth_user_id = _auth_user_id(claims)

    linked = await session.scalar(
        select(User).where(User.auth_user_id == auth_user_id)
    )
    if linked is not None:
        _reject_deactivated(linked)
        return linked

    email = _account_email(claims)
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        return await _claim_profile(session, existing, auth_user_id, payload)

    return await _create_profile(session, email, auth_user_id, payload)


async def _claim_profile(
    session: SessionDep, existing: User, auth_user_id: UUID, payload: ProfileLink
) -> User:
    """Point an unclaimed profile at this identity, or 409 if it is spoken for."""
    if existing.auth_user_id is not None:
        # Unique on both columns, and we already know it is not ours.
        raise conflict(
            f"Email {existing.email!r} is already linked to a different account"
        )
    # Linking would produce an account that 403s on every other route.
    _reject_deactivated(existing)

    # auth_user_id IS NULL in the WHERE clause, not just in the check above:
    # two sign-ins racing on the same seeded email would otherwise both pass
    # the read and the second would silently steal the row. The loser matches
    # nothing and gets the 409.
    #
    # The name, phone and city the caller just typed win over the seeded
    # placeholders — they came from the person themselves, minutes ago.
    statement = (
        update(User)
        .where(User.id == existing.id, User.auth_user_id.is_(None))
        .values(auth_user_id=auth_user_id, **payload.model_dump())
        .returning(User)
        .execution_options(synchronize_session="fetch")
    )
    claimed = await _execute_returning_user(session, statement)
    if claimed is None:
        raise conflict(
            f"Email {existing.email!r} is already linked to a different account"
        )
    logger.info("Linked auth identity to existing profile %s", claimed.id)
    return claimed


async def _create_profile(
    session: SessionDep, email: str, auth_user_id: UUID, payload: ProfileLink
) -> User:
    user = User(email=email, auth_user_id=auth_user_id, **payload.model_dump())
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        _reject_missing_identity(exc)
        # Otherwise it is the unique index: two concurrent first-time links,
        # and one inserted between our read and this flush. The transaction is
        # dead, so we cannot re-read and return the winner — the client retries
        # and takes the idempotent path.
        raise conflict(
            "This account was linked to a profile concurrently — retry"
        ) from exc
    await session.refresh(user)
    logger.info("Created profile %s for a new auth identity", user.id)
    return user


@router.get(
    "/me",
    response_model=MeProfile,
    # No 403 for "not platform staff": OptionalPlatformStaff answers null, so
    # the response set is the same one every other /me route declares.
    responses={**UNAUTHENTICATED, **NO_PROFILE, **FORBIDDEN},
    summary="The caller's own profile",
)
async def get_my_profile(
    user: CurrentUser, platform_staff: OptionalPlatformStaff
):
    """The profile, plus whether this person may act for Foodishi itself.

    Every frontend calls this on boot, which is why the platform role rides
    along here instead of behind a second request: the operations console has to
    know before it renders that a restaurant-scoped account is not an operator.
    """
    # CurrentUser raises the 404 that points at POST /auth/link, so an unlinked
    # caller is told what to do rather than just refused.
    #
    # Named fields rather than from_attributes over the row: reading every
    # column touches updated_at, which onupdate expires after a flush, and
    # refreshing an expired attribute from async code raises MissingGreenlet
    # (same reason as _read in app/routers/images.py).
    return MeProfile(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        city=user.city,
        is_active=user.is_active,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        platform_role=platform_staff.role if platform_staff is not None else None,
    )


@router.patch(
    "/me",
    response_model=UserRead,
    responses={**UNAUTHENTICATED, **NO_PROFILE, **FORBIDDEN, **CONFLICT},
    summary="Update the caller's own profile",
)
async def update_my_profile(
    payload: MeUpdate, session: SessionDep, user: CurrentUser
):
    """The caller maintaining their own name, phone, city, email and avatar.

    Same field rules as PATCH /users/{id} — MeUpdate subclasses that route's
    schema — plus avatar_url, which only makes sense on a route whose subject is
    the caller. The difference that matters is the subject: it is the token's,
    so there is no id to pass and no way to aim the update at somebody else.

    Nothing here can grant a privilege. MeUpdate forbids unknown fields, so a
    body carrying platform_role, is_active or id is a 422 rather than a silent
    no-op — which matters because the payload is dumped straight into the UPDATE
    below. The role a caller holds is read from platform_staff by GET /me and is
    writable only through the staff routes.
    """
    # exclude_unset, so an absent field is left alone and is not confused with
    # avatar_url=null — the one value here that is a real instruction ("clear my
    # photo") rather than "field omitted". See MeUpdate.reject_explicit_nulls.
    changes = payload.model_dump(exclude_unset=True)
    if "email" in changes:
        changes["email"] = changes["email"].lower()  # match the unique index

    # synchronize_session="fetch", unlike in users.py: CurrentUser has already
    # loaded this row into the identity map, and without it the in-session
    # instance would keep the pre-update values.
    statement = (
        update(User)
        .where(User.id == user.id)
        .values(**changes)
        .returning(User)
        .execution_options(synchronize_session="fetch")
    )
    updated = await _execute_returning_user(session, statement)
    if updated is None:
        # The row existed a moment ago (CurrentUser loaded it), so it was
        # deleted mid-request rather than never having been there.
        raise conflict("This profile was removed while the update was in flight")
    return updated


async def _execute_returning_user(session: SessionDep, statement) -> User | None:
    """Run an UPDATE ... RETURNING User, turning a unique clash into a 409."""
    try:
        return (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        _reject_missing_identity(exc)
        # email is the only unique column a caller can set here.
        raise conflict("That email is already registered to another profile") from exc


@router.get(
    "/me/orders",
    response_model=Page[OrderRead],
    # CurrentUser can also 404 (no profile linked yet) and 403 (deactivated),
    # so both belong in the contract the frontends generate their client from.
    responses={**UNAUTHENTICATED, **NO_PROFILE, **FORBIDDEN},
    summary="The caller's own orders",
)
async def list_my_orders(
    session: SessionDep,
    page: PageDep,
    user: CurrentUser,
    restaurant_id: int | None = None,
    status: OrderStatus | None = None,
    live: bool = Query(
        default=False, description="Only orders not delivered or cancelled"
    ),
    placed_from: datetime | None = None,
    placed_to: datetime | None = None,
):
    """GET /orders with the owner pinned to the caller.

    user_id is not a parameter of this route at all — not one defaulting to the
    caller, which a later edit could make overridable. FastAPI drops undeclared
    query parameters, so ?user_id=7 changes nothing about the rows returned.
    """
    statement = repo.order_query(
        user_id=user.id,
        restaurant_id=restaurant_id,
        status=status,
        live=live,
        placed_from=placed_from,
        placed_to=placed_to,
    )
    items, total = await paginate(session, statement, page)
    return Page[OrderRead](
        items=items, total=total, limit=page.limit, offset=page.offset
    )
