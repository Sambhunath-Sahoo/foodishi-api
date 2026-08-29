"""Who is calling, and what they are allowed to touch.

======================================================================
PHASE-1 ESCAPE HATCH — OFF BY DEFAULT, AND MUST STAY THAT WAY
======================================================================
AUTH_ENABLED now defaults to **TRUE**, so this module requires real Supabase
tokens unless somebody explicitly writes AUTH_ENABLED=false.

It used to default to false, which is the wrong direction to be wrong in: with
authentication off, every dependency here accepts an unsigned
`X-Dev-User-Id: <users.id>` header and trusts it completely, so any caller who
can reach the port can act as any customer — and as any restaurant's admin — by
guessing an integer. No signature, no password, no rate limit. A deployment that
simply forgot the variable ran in that state, and `.env` is gitignored, so `.env`
is not what a deployment gets. A misspelling is now a startup error rather than a
silent "false" (see app/config.py env_flag).

The escape hatch still exists, because the fixture-backed consoles need it on a
laptop. It now costs a deliberate AUTH_ENABLED=false.

Before this service is exposed to anything but a laptop, ALL of the following
must be true:

  1. AUTH_ENABLED is NOT set to false. It defaults to true; with it true, the
     dev header is ignored and only real tokens work.
  2. SUPABASE_URL and SUPABASE_JWKS_URL point at the real project.
  3. Every public.users row that needs to sign in has auth_user_id populated,
     otherwise those users get 404 "no profile linked" on their first request.
  4. `grep "DEV AUTH" <logs>` returns nothing. Every dev-identity request logs
     that marker at WARNING precisely so this is checkable.

Layering: app/services/auth.py verifies the JWT; this module maps a verified
identity onto public.users and public.restaurant_staff.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.config import is_auth_enabled
from app.db import SessionDep
from app.models.enums import PlatformRole, StaffRole
from app.models.platform import PlatformStaff
from app.models.staff import RestaurantStaff
from app.models.user import User
from app.services.auth import GENERIC_FAILURE, TokenError, verify_token

logger = logging.getLogger(__name__)

# The dev-mode identity header, and the synthetic claim that records that a set
# of claims came from it rather than from a signed token.
DEV_USER_HEADER = "X-Dev-User-Id"
DEV_USER_CLAIM = "dev_user_id"

# Where a signed-in caller with no public.users row is sent. Exported so the
# auth router and this message cannot drift apart.
PROFILE_LINK_PATH = "/auth/link"

# staff < manager < owner. Comparing StrEnum members directly would compare
# alphabetically ("manager" < "owner" < "staff"), which is wrong in a way that
# silently grants access, so ordering is explicit.
ROLE_RANK: dict[StaffRole, int] = {
    StaffRole.STAFF: 1,
    StaffRole.ADMIN: 2,
}

# One entry, and still a dict rather than a bare equality check: the moment a
# second platform role exists, comparing StrEnum members directly would rank
# them alphabetically — and "admin" sorts FIRST, so the most privileged role
# would read as the least. Keeping the ladder means that trap never reopens.
PLATFORM_ROLE_RANK: dict[PlatformRole, int] = {
    PlatformRole.ADMIN: 1,
}

# Shared OpenAPI response descriptions, matching the style of app/core/errors.py.
UNAUTHENTICATED = {401: {"description": "Missing or invalid access token"}}
FORBIDDEN = {403: {"description": "Not permitted for this restaurant"}}
NOT_PLATFORM = {403: {"description": "Requires Foodishi platform staff access"}}
NO_PROFILE = {404: {"description": "No user profile linked to this account"}}

# auto_error=False so this module words its own 401s and always attaches
# WWW-Authenticate; the scheme is still declared, so /docs grows an Authorize
# button and sends the header.
bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")

BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
DevUserDep = Annotated[int | None, Header(alias=DEV_USER_HEADER, ge=1)]


def unauthenticated(detail: str) -> HTTPException:
    # WWW-Authenticate is required by RFC 9110 on a 401 and is what tells a
    # client to refresh its Supabase session rather than retry blindly.
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def forbidden(detail: str) -> HTTPException:
    """403 for an authenticated caller acting outside their permissions.

    Distinct from 401: re-authenticating will not help, so the client must not
    bounce the user through a login screen.
    """
    return HTTPException(status.HTTP_403_FORBIDDEN, detail)


def _dev_claims(user_id: int) -> dict[str, Any]:
    """Claims for a request that was never signed.

    `sub` is None deliberately: there is no Supabase identity behind a dev
    request, and a plausible-looking fake uuid would leak into logs and rows as
    if it meant something. Read DEV_USER_CLAIM first; never index claims["sub"]
    without checking it is not None.
    """
    return {"sub": None, "aud": "authenticated", DEV_USER_CLAIM: user_id}


async def _verified_claims(credentials: HTTPAuthorizationCredentials) -> dict[str, Any]:
    try:
        return await verify_token(credentials.credentials)
    except TokenError as exc:
        # The specific reason stays here; the caller gets the vague version.
        logger.warning("Rejected access token: %s", exc)
        raise unauthenticated(GENERIC_FAILURE) from exc


async def current_claims(
    credentials: BearerDep = None,
    dev_user_id: DevUserDep = None,
) -> dict[str, Any]:
    """Verified JWT claims for the caller, or synthetic dev claims.

    Raises 401 when there is no usable identity at all.
    """
    if is_auth_enabled():
        if dev_user_id is not None:
            logger.warning(
                "Ignoring %s header: AUTH_ENABLED is true", DEV_USER_HEADER
            )
        if credentials is None:
            raise unauthenticated("Missing bearer token")
        return await _verified_claims(credentials)

    if dev_user_id is not None:
        logger.warning(
            "DEV AUTH: acting as users.id=%s from an unsigned %s header "
            "because AUTH_ENABLED is false",
            dev_user_id,
            DEV_USER_HEADER,
        )
        return _dev_claims(dev_user_id)

    # A real token still works with the flag off, so the auth flow can be
    # exercised end to end before it is switched on for everyone.
    if credentials is not None:
        return await _verified_claims(credentials)

    raise unauthenticated(
        f"Missing bearer token (or {DEV_USER_HEADER} while AUTH_ENABLED is false)"
    )


CurrentClaims = Annotated[dict[str, Any], Depends(current_claims)]


async def _load_profile(session: SessionDep, claims: dict[str, Any]) -> User | None:
    """Resolve claims to the public.users row, or None if there is not one."""
    dev_user_id = claims.get(DEV_USER_CLAIM)
    if dev_user_id is not None:
        return await session.get(User, dev_user_id)

    try:
        auth_user_id = UUID(claims["sub"])
    except (KeyError, TypeError, ValueError):
        # Supabase always issues a uuid sub, so this is a token from somewhere
        # else that nonetheless passed verification — worth a log line.
        logger.warning("Verified token carried a non-uuid sub: %r", claims.get("sub"))
        return None

    return await session.scalar(
        select(User).where(User.auth_user_id == auth_user_id)
    )


def _no_profile(claims: dict[str, Any]) -> HTTPException:
    dev_user_id = claims.get(DEV_USER_CLAIM)
    if dev_user_id is not None:
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No user with id {dev_user_id} ({DEV_USER_HEADER})",
        )
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        "No user profile is linked to this account. "
        f"POST {PROFILE_LINK_PATH} to create or link one.",
    )


async def current_user(session: SessionDep, claims: CurrentClaims) -> User:
    """The caller's public.users row. 401 unauthenticated, 404 if unlinked."""
    user = await _load_profile(session, claims)
    if user is None:
        raise _no_profile(claims)
    if not user.is_active:
        # Deactivation is how a customer is removed while their orders keep
        # their foreign keys, so it has to actually block requests.
        raise forbidden("This account is deactivated")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def optional_user(
    session: SessionDep,
    credentials: BearerDep = None,
    dev_user_id: DevUserDep = None,
) -> User | None:
    """The caller's profile if they are signed in, otherwise None.

    For endpoints that anyone may call but that personalise when they can —
    the catalog marking favourites, say. Every identity problem degrades to
    anonymous rather than raising, so an expired token never turns a public
    page into an error page.
    """
    try:
        claims = await current_claims(
            credentials=credentials, dev_user_id=dev_user_id
        )
    except HTTPException:
        return None

    user = await _load_profile(session, claims)
    return user if user is not None and user.is_active else None


OptionalUser = Annotated[User | None, Depends(optional_user)]


def _restaurant_id_from_path(request: Request) -> int:
    # Deferred: app.dependencies.scope imports this module, so the shared path
    # parser is pulled in at call time rather than at import time.
    from app.dependencies.scope import path_int

    # A missing segment is a wiring mistake and path_int raises RuntimeError
    # for it; a segment that is not an integer is the caller's mistake and
    # becomes the same 422 FastAPI would have produced itself, rather than the
    # unhandled ValueError — and 500 — that a bare int() gave.
    return path_int(request, "restaurant_id", caller="require_staff()")


def require_staff(
    restaurant_id: int | None = None,
    minimum_role: StaffRole = StaffRole.STAFF,
) -> Callable[..., Awaitable[RestaurantStaff]]:
    """Build a dependency that admits only staff of one restaurant.

    Pass restaurant_id to pin a route to a fixed restaurant; leave it None and
    the restaurant is read from the route's own {restaurant_id} path parameter,
    which is what CurrentStaff does.

    This runs against restaurant_staff even in dev mode — the AUTH_ENABLED
    escape hatch fakes identity, never permissions.
    """

    async def dependency(
        request: Request, session: SessionDep, user: CurrentUser
    ) -> RestaurantStaff:
        target_id = (
            restaurant_id
            if restaurant_id is not None
            else _restaurant_id_from_path(request)
        )
        staff = await session.scalar(
            select(RestaurantStaff).where(
                RestaurantStaff.user_id == user.id,
                RestaurantStaff.restaurant_id == target_id,
            )
        )
        # One message for absent, revoked and under-privileged alike: whether a
        # restaurant exists, and who works there, is not the caller's business.
        if (
            staff is None
            or not staff.is_active
            or ROLE_RANK[staff.role] < ROLE_RANK[minimum_role]
        ):
            logger.info(
                "Staff check failed: user=%s restaurant=%s needed=%s had=%s",
                user.id,
                target_id,
                minimum_role,
                staff.role if staff else None,
            )
            raise forbidden(
                f"Requires {minimum_role} access to restaurant {target_id}"
            )
        return staff

    return dependency


# The common case: a route with /restaurants/{restaurant_id}/... that any
# active staff member may use.
CurrentStaff = Annotated[RestaurantStaff, Depends(require_staff())]


async def _platform_staff_row(session: SessionDep, user_id: int) -> PlatformStaff | None:
    return await session.scalar(
        select(PlatformStaff).where(PlatformStaff.user_id == user_id)
    )


def require_platform_role(
    minimum_role: PlatformRole = PlatformRole.ADMIN,
) -> Callable[..., Awaitable[PlatformStaff]]:
    """Build a dependency that admits only Foodishi's own staff.

    The platform-scoped twin of require_staff. Restaurant membership grants
    nothing here and never stands in for a row in platform_staff: the owner of
    every kitchen on the platform still cannot mint a global coupon, read
    another customer's profile, or open the operations console.

    Like require_staff this runs against its table even in dev mode — the
    AUTH_ENABLED escape hatch fakes identity, never permissions.
    """

    async def dependency(session: SessionDep, user: CurrentUser) -> PlatformStaff:
        staff = await _platform_staff_row(session, user.id)
        if (
            staff is None
            or not staff.is_active
            or PLATFORM_ROLE_RANK[staff.role] < PLATFORM_ROLE_RANK[minimum_role]
        ):
            logger.info(
                "Platform check failed: user=%s needed=%s had=%s",
                user.id,
                minimum_role,
                staff.role if staff else None,
            )
            # One message for absent, revoked and under-privileged alike, and it
            # names no role: who Foodishi employs, and at what level, is not the
            # caller's business.
            raise forbidden("Requires Foodishi platform staff access")
        return staff

    return dependency


async def optional_platform_staff(
    session: SessionDep, user: CurrentUser
) -> PlatformStaff | None:
    """The caller's platform row if they have an active one, else None.

    For GET /me, which must answer for customers and restaurant staff too. A
    403 there would turn "you are not an operator" into "you are not signed
    in", and the consoles would bounce every customer to /login.
    """
    staff = await _platform_staff_row(session, user.id)
    return staff if staff is not None and staff.is_active else None


# Any active platform employee. Reach for require_platform_role(OPS) or (ADMIN)
# when the route spends Foodishi's money or edits who works here.
CurrentPlatformStaff = Annotated[PlatformStaff, Depends(require_platform_role())]
OptionalPlatformStaff = Annotated[PlatformStaff | None, Depends(optional_platform_staff)]


if not is_auth_enabled():  # pragma: no cover - startup notice only
    logger.warning(
        "DEV AUTH: AUTH_ENABLED is false — %s is trusted without a signature. "
        "Never run this configuration anywhere reachable.",
        DEV_USER_HEADER,
    )
