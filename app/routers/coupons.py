import enum
from datetime import UTC, datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    FORBIDDEN,
    UNAUTHENTICATED,
    OptionalUser,
    forbidden,
    optional_platform_staff,
)
from app.dependencies.scope import RestaurantScopeDep
from app.models.catalog import Restaurant, restaurant_cuisines
from app.models.coupon import Coupon, CouponRedemption
from app.models.enums import CouponScope, StaffRole
from app.models.platform import PlatformStaff
from app.schemas.coupon import (
    CouponCreate,
    CouponRead,
    CouponUpdate,
    CouponValidateRequest,
    CouponValidation,
)
from app.services.coupons import evaluate

router = APIRouter(prefix="/coupons", tags=["coupons"])

# Who may write a coupon, and over whose money.
#
# Two kinds of caller, and the difference is whose margin pays for the discount:
#   · Foodishi platform admin — any coupon, any scope. A GLOBAL or CUISINE coupon
#     comes out of the platform's own take, so nobody else can mint one.
#   · a restaurant admin   — only a RESTAURANT-scoped coupon, and only for a
#     kitchen they actually admin. That discount is theirs to give.
#
# The check has to run in the handler rather than as a route dependency because
# it reads the body: which restaurant a coupon targets is in the payload, not
# the path. Same reason POST /menu-items does its scope check as its first
# statement (see catalog_admin.py).
async def _assert_may_write(
    *,
    scope: RestaurantScopeDep,
    platform: PlatformStaff | None,
    coupon_scope: CouponScope,
    restaurant_id: int | None,
) -> None:
    if platform is not None:
        return
    if coupon_scope is not CouponScope.RESTAURANT or restaurant_id is None:
        # Deliberately does not say "you could do this if it were restaurant
        # scoped": whether a platform tier exists at all is not the caller's
        # business, and the schema already rejects a scope/target mismatch.
        raise forbidden(
            "Only Foodishi staff may create a coupon outside a single restaurant"
        )
    await scope.require(restaurant_id, StaffRole.ADMIN)



UNIQUE_VIOLATION = "23505"
UNPROCESSABLE = {422: {"description": "Violates a coupon rule"}}


async def _by_code(session: AsyncSession, code: str) -> Coupon | None:
    # Case-insensitive: a customer types a code, they do not copy it. Writes
    # here store it upper-cased, so first() only ever has one row to pick from
    # unless a legacy mixed-case pair exists — and then a 500 helps nobody.
    statement = select(Coupon).where(func.upper(Coupon.code) == code.upper())
    return (await session.scalars(statement)).first()


def _raise_write_failure(exc: IntegrityError, code: str | None) -> NoReturn:
    """Split a rejected write into 409 (code taken) and 422 (rule violated).

    A PATCH can move a coupon past a CHECK the partial payload could not be
    validated against — a percent coupon losing its cap, an inverted validity
    range — and that is a business-rule failure, not a conflict.
    """
    if getattr(exc.orig, "sqlstate", None) == UNIQUE_VIOLATION:
        raise conflict(f"Coupon code {code!r} already exists") from exc
    raise unprocessable(
        "Coupon violates a constraint: percent coupons need a cap, and "
        "valid_until must be after valid_from"
    ) from exc


# Two boards off one route, because the row shape is identical and only the
# visible set differs:
#   · Foodishi admin      -> every live coupon on the platform
#   · restaurant admin -> the live coupons scoped to kitchens they admin
# Enumerating every live discount tells a reader what Foodishi is willing to give
# away, so a caller who is neither gets nothing at all rather than an empty page
# — an empty page reads as "there are no coupons", which is a different claim.
class CouponState(enum.StrEnum):
    """Which slice of the coupon table a listing wants.

    A StrEnum rather than a bare bool so a third state (say 'expired' on its own)
    is an added member rather than a second flag that can contradict the first.
    """

    #: Active, started, and not yet expired. What a customer could redeem now.
    REDEEMABLE = "redeemable"
    #: Every code, whatever its state. For a management screen.
    ALL = "all"


@router.get(
    "",
    response_model=Page[CouponRead],
    responses={**UNAUTHENTICATED, **FORBIDDEN},
)
async def list_coupons(
    session: SessionDep,
    params: PageDep,
    scope: RestaurantScopeDep,
    platform: Annotated[PlatformStaff | None, Depends(optional_platform_staff)],
    state: Annotated[
        CouponState,
        Query(
            description=(
                "Which codes to list. 'redeemable' (the default) is what a "
                "customer could use right now. 'all' includes expired, "
                "not-yet-started and switched-off codes."
            )
        ),
    ] = CouponState.REDEEMABLE,
):
    now = datetime.now(UTC)
    statement = select(Coupon).order_by(
        Coupon.valid_until, Coupon.id  # soonest to expire first
    )
    if state is CouponState.REDEEMABLE:
        # The default, because expired and switched-off codes are noise on a
        # board whose question is "what can a customer use".
        statement = statement.where(
            Coupon.is_active, Coupon.valid_from <= now, Coupon.valid_until >= now
        )
    # 'all' filters nothing, and that is the point. This filter used to be
    # unconditional with no way to widen it, which made a whole class of screen
    # impossible: a console that switches a code OFF then watches the row vanish
    # on the next refetch cannot offer switching it back ON, and its
    # "expired or switched off" panel is permanently zero. A management surface
    # has to be able to see the thing it manages.
    if platform is None:
        mine = await scope.admin_restaurant_ids()
        if not mine:
            raise forbidden("Requires Foodishi platform staff or restaurant admin access")
        # Restaurant-scoped only: a GLOBAL coupon applies to this kitchen's
        # orders too, but it is not theirs to see, edit or reason about.
        statement = statement.where(
            Coupon.scope == CouponScope.RESTAURANT,
            Coupon.restaurant_id.in_(sorted(mine)),
        )
    items, total = await paginate(session, statement, params)
    return Page[CouponRead](
        items=items, total=total, limit=params.limit, offset=params.offset
    )


# DELIBERATELY PUBLIC — do not add a guard here or to GET /{code} below. The
# customer app checks a typed code before the customer has signed in, so the
# only identity it can offer is the user_id in the payload. Both routes read a
# code the caller already knows and neither enumerates anything; the listing
# that does is GET "", which is guarded.
@router.post("/validate", response_model=CouponValidation, responses=NOT_FOUND)
async def validate_coupon(
    payload: CouponValidateRequest, session: SessionDep, caller: OptionalUser
):
    coupon = await _by_code(session, payload.code)
    if coupon is None:
        raise not_found("coupon", payload.code)

    # The restaurant is still checked, because an unknown one would look like "no
    # cuisines" and turn a bad request into a confidently wrong answer. It leaks
    # nothing: the catalog is public.
    if not await session.scalar(select(exists().where(Restaurant.id == payload.restaurant_id))):
        raise not_found("restaurant", payload.restaurant_id)

    # The USER is not, and that is the fix. This route is unauthenticated by
    # design, and it used to answer `404 "No user with id N"` for a free id and
    # 200 for a real one -- a clean unauthenticated map of which users.id values
    # exist, i.e. the size and shape of the customer base, from one guessable
    # coupon code. Worse, with a real id the `applicable`/`reason` fields reported
    # whether that specific stranger had already spent that coupon.
    #
    # An unknown or absent user now means "no redemptions on record", which is
    # both true and unremarkable, so every id returns the same shape of answer.
    #
    # And when the caller IS signed in, the subject must be them: a signed-in
    # customer naming somebody else's id is either a bug or probing, exactly as
    # orders.order_owner_id argues. Pre-sign-in callers keep working unchanged,
    # which is the whole reason this route has no guard.
    if caller is not None and payload.user_id != caller.id:
        raise forbidden(
            "A coupon is always checked for the signed-in caller "
            f"(users.id={caller.id}); send that id"
        )
    subject_id = caller.id if caller is not None else payload.user_id

    cuisine_ids = set(
        await session.scalars(
            select(restaurant_cuisines.c.cuisine_id).where(
                restaurant_cuisines.c.restaurant_id == payload.restaurant_id
            )
        )
    )
    redemptions = await session.scalar(
        select(func.count())
        .select_from(CouponRedemption)
        .where(
            CouponRedemption.coupon_id == coupon.id,
            CouponRedemption.user_id == subject_id,
        )
    )

    # The service owns all seven checks; this endpoint only assembles inputs.
    return evaluate(
        coupon,
        subtotal=payload.subtotal,
        restaurant_id=payload.restaurant_id,
        cuisine_ids=cuisine_ids,
        user_redemption_count=int(redemptions or 0),
        now=datetime.now(UTC),
    )


@router.get("/{code}", response_model=CouponRead, responses=NOT_FOUND)
async def get_coupon(code: str, session: SessionDep):
    coupon = await _by_code(session, code)
    if coupon is None:
        raise not_found("coupon", code)
    return coupon


# A coupon is spent out of Foodishi's own margin, not the restaurant's, so minting
# and editing one is a platform act unless it belongs to a single kitchen — see
# _assert_may_write for who may do what, and why the check reads the body.
@router.post(
    "",
    response_model=CouponRead,
    status_code=201,
    responses={**UNAUTHENTICATED, **FORBIDDEN, **CONFLICT, **UNPROCESSABLE},
)
async def create_coupon(
    payload: CouponCreate,
    session: SessionDep,
    scope: RestaurantScopeDep,
    platform: Annotated[PlatformStaff | None, Depends(optional_platform_staff)],
):
    await _assert_may_write(
        scope=scope,
        platform=platform,
        coupon_scope=payload.scope,
        restaurant_id=payload.restaurant_id,
    )
    data = payload.model_dump()
    data["code"] = data["code"].upper()  # so lookups can match case-insensitively
    coupon = Coupon(**data)
    session.add(coupon)
    try:
        await session.flush()
    except IntegrityError as exc:
        _raise_write_failure(exc, data["code"])
    await session.refresh(coupon)  # created_at is a server default
    return coupon


@router.patch(
    "/{coupon_id}",
    response_model=CouponRead,
    responses={
        **UNAUTHENTICATED,
        **FORBIDDEN,
        **NOT_FOUND,
        **CONFLICT,
        **UNPROCESSABLE,
    },
)
async def update_coupon(
    coupon_id: int,
    payload: CouponUpdate,
    session: SessionDep,
    scope: RestaurantScopeDep,
    platform: Annotated[PlatformStaff | None, Depends(optional_platform_staff)],
):
    # The existing row decides who may edit it, not the payload: otherwise a
    # restaurant admin could widen their own coupon to GLOBAL by sending a new
    # scope, and the check would approve it against the value they just chose.
    existing = await session.get(Coupon, coupon_id)
    if existing is None:
        raise not_found("coupon", coupon_id)
    await _assert_may_write(
        scope=scope,
        platform=platform,
        coupon_scope=existing.scope,
        restaurant_id=existing.restaurant_id,
    )
    changes = payload.model_dump(exclude_unset=True)
    # A non-platform caller must not move a coupon out of their own kitchen.
    if platform is None and (
        ("scope" in changes and changes["scope"] is not CouponScope.RESTAURANT)
        or ("restaurant_id" in changes and changes["restaurant_id"] != existing.restaurant_id)
    ):
        raise forbidden("Only Foodishi staff may change which restaurants a coupon covers")
    if "code" in changes:
        changes["code"] = changes["code"].upper()

    # One UPDATE ... RETURNING rather than fetch-then-mutate: a single round
    # trip, and nothing half-modified left in the session if it fails.
    statement = (
        update(Coupon).where(Coupon.id == coupon_id).values(**changes).returning(Coupon)
    )
    try:
        coupon = (await session.execute(statement)).scalar_one_or_none()
    except IntegrityError as exc:
        _raise_write_failure(exc, changes.get("code"))

    if coupon is None:
        raise not_found("coupon", coupon_id)
    return coupon
