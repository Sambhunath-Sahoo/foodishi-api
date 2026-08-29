import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    NOT_PLATFORM,
    UNAUTHENTICATED,
    CurrentUser,
    require_platform_role,
)
from app.dependencies.ownership import readable_user
from app.dependencies.scope import path_int, writable_user
from app.models.enums import PlatformRole
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

NOT_FOUND = {404: {"description": "No user with that id"}}
EMAIL_TAKEN = {409: {"description": "Email is already registered"}}


def _not_found(user_id: int) -> HTTPException:
    return HTTPException(404, f"No user with id {user_id}")


# The one route here that answers to nobody in particular, and deliberately:
# registration happens before the caller has an identity to present — the
# Supabase account may not exist yet, and POST /auth/link claims this row
# afterwards. Every route below names an existing account and is guarded.
@router.post("", response_model=UserRead, status_code=201, responses=EMAIL_TAKEN)
async def create_user(payload: UserCreate, session: SessionDep):
    data = payload.model_dump()
    data["email"] = data["email"].lower()  # so the unique index is case-insensitive
    user = User(**data)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, f"Email {user.email!r} is already registered") from exc
    await session.refresh(user)
    return user


@router.get(
    "/{user_id}",
    response_model=UserRead,
    dependencies=[Depends(readable_user)],
    responses={**NOT_FOUND, **UNAUTHENTICATED, **FORBIDDEN},
)
async def get_user(user_id: int, session: SessionDep):
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found(user_id)
    return user


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    # writable_user, not readable_user. readable_user's platform branch is
    # confined to SAFE_METHODS by design, so an operator could OPEN a customer
    # and not deactivate one — which is the entire point of the customers
    # screen. writable_user admits the customer themselves or a platform admin,
    # and still refuses restaurant staff.
    dependencies=[Depends(writable_user)],
    responses={**NOT_FOUND, **EMAIL_TAKEN, **UNAUTHENTICATED, **FORBIDDEN},
)
async def update_user(user_id: int, payload: UserUpdate, session: SessionDep):
    changes = payload.model_dump(exclude_unset=True)
    if "email" in changes:
        changes["email"] = changes["email"].lower()  # match the unique index

    # A single UPDATE ... RETURNING rather than fetch-then-mutate: one round
    # trip, and no half-modified entity left in the session if it fails.
    statement = (
        update(User).where(User.id == user_id).values(**changes).returning(User)
    )
    try:
        user = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        # email is the only unique column today, but the payload may not carry
        # it — never index into changes here.
        email = changes.get("email")
        detail = (
            f"Email {email!r} is already registered"
            if email
            else "Update conflicts with an existing record"
        )
        raise HTTPException(409, detail) from exc

    if user is None:
        raise _not_found(user_id)
    return user


# Built once at import rather than per request: the factory only needs the two
# arguments the dependency below already has, so it is called straight through
# instead of being resolved by FastAPI a second time.
_require_admin = require_platform_role(PlatformRole.ADMIN)


async def deletable_user(
    request: Request, session: SessionDep, user: CurrentUser
) -> User:
    """readable_user's either-or twin: your own account, or an admin's doing.

    Closing your own account is self-service, and Foodishi support closes accounts
    for the people who ask them to — but nobody else may. readable_user raises
    403 on its own, so listing it beside require_platform_role(ADMIN) would
    refuse the operator before the admin check ever ran; an either-or has to be
    decided inside one dependency.

    Returns the caller, exactly as readable_user does — the route itself works
    from the path id.

    Lives here rather than in ownership.py because DELETE /users/{user_id} is
    its only caller; move it next to readable_user when a second one appears.
    """
    user_id = path_int(request, "user_id", caller="deletable_user")
    if user_id == user.id:
        return user
    # Somebody else's account, so this is an operator action or nothing.
    # require_platform_role words the refusal and logs the near miss.
    await _require_admin(session=session, user=user)
    return user


class UserActiveUpdate(BaseModel):
    """Deactivate or restore a customer's account."""

    model_config = ConfigDict(extra="forbid")

    is_active: bool


class UserActiveRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool


@router.put(
    "/{user_id}/active",
    response_model=UserActiveRead,
    dependencies=[Depends(require_platform_role(PlatformRole.ADMIN))],
    responses={**NOT_PLATFORM, **UNAUTHENTICATED, **FORBIDDEN},
    summary="Deactivate or restore a customer (Foodishi staff only)",
)
async def set_user_active(
    user_id: int, payload: UserActiveUpdate, session: SessionDep, actor: CurrentUser
):
    """Turn a customer's account off, or back on.

    A SEPARATE route rather than a field on UserUpdate, and the reason is the
    comment already on that schema: is_active and created_at "are the server's to
    set, not the client's". Adding it there would have let a customer deactivate
    themselves through the same PATCH they use to fix their own phone number,
    because that route admits the account's owner.

    So this is the same shape of decision as PUT /restaurants/{id}/availability:
    one column, one route, one guard. Platform admin ONLY -- not the customer,
    and not restaurant staff, who see the customer on an order they are cooking
    and have no business closing their account.

    Deactivating deletes nothing. identity.py refuses a deactivated account at
    sign-in with "This account is deactivated", and the row, its orders and its
    addresses all stay where they were. That is the difference between this and
    DELETE below.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found(user_id)

    user.is_active = payload.is_active
    # Logged because the column keeps no record of who turned it, and "why can
    # this customer not sign in" is a support question somebody will ask.
    logger.info(
        "users.id=%s is now %s; set by users.id=%s",
        user_id,
        "active" if payload.is_active else "deactivated",
        actor.id,
    )
    await session.flush()
    return user


# Guarded for two reasons rather than one: closing an account is destructive to
# the customer, and platform_staff.user_id is ON DELETE CASCADE, so deleting an
# operator's profile also revokes their grant — with no row left behind to say
# the access ever existed.
@router.delete(
    "/{user_id}",
    status_code=204,
    dependencies=[Depends(deletable_user)],
    responses={
        **NOT_FOUND,
        409: {"description": "User still has orders"},
        **UNAUTHENTICATED,
        **NOT_PLATFORM,
    },
)
async def delete_user(user_id: int, session: SessionDep):
    # RETURNING the id distinguishes "deleted one row" from "matched nothing",
    # so a repeat delete reports 404 rather than a silent success.
    statement = delete(User).where(User.id == user_id).returning(User.id)
    try:
        deleted = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        # orders.user_id is ON DELETE RESTRICT, so a customer with order
        # history cannot be removed. Deactivate them instead.
        raise HTTPException(
            409, f"User {user_id} still has orders and cannot be deleted"
        ) from exc
    if deleted is None:
        raise _not_found(user_id)


# LIKE treats % and _ as wildcards, so a raw search term would let a caller
# widen the scan to the whole table. Escape them and match literally.
def _contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# The whole customer directory — name, email, phone, city — in one page-able
# call. readable_user cannot express this: there is no single {user_id} to own,
# so the only caller who may ask is Foodishi itself.
@router.get(
    "",
    response_model=Page[UserRead],
    dependencies=[Depends(require_platform_role())],
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
)
async def list_users(
    session: SessionDep,
    params: PageDep,
    city: Annotated[str | None, Query(max_length=60)] = None,
    is_active: bool | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
):
    statement = select(User).order_by(User.id)
    if city is not None:
        statement = statement.where(User.city == city)
    if is_active is not None:
        statement = statement.where(User.is_active.is_(is_active))
    if q is not None:
        pattern = _contains(q)
        statement = statement.where(
            or_(
                User.name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
            )
        )

    items, total = await paginate(session, statement, params)
    return Page[UserRead](
        items=items, total=total, limit=params.limit, offset=params.offset
    )
