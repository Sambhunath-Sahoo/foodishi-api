"""Add-ons and variants: the questions a dish asks before it can be ordered.

Two audiences, and that is what shapes this file.

A RESTAURANT ADMIN builds the groups. Those routes are admin-gated, and the two
that are keyed by a group or option id rather than by a restaurant get their
gate from `scope.staff_of_row` with a resolver defined below — the same factory
the menu-item and category routes already use, so "admin of this thing's
restaurant" has one definition and one wording of the refusal.

A CUSTOMER has to be able to READ the groups on a dish, unauthenticated, exactly
like GET /restaurants/{id}/menu. Without that read the whole feature is
decoration: a manager can build a "Half plate / Full plate" variant and nobody
can ever order one. That route is the most important thing in this module.

What is deliberately NOT here: applying a selection to a price. Nothing in this
file touches pricing. `order_item_modifiers` freezes the chosen name and price
onto the order line at placement, and extending app/services/pricing.py to add
`price_delta` into a line total is a separate change with its own tests — until
it lands, a selection is recorded and costs nothing, which is the honest
behaviour rather than a total the customer was never quoted.
"""

import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found, unprocessable
from app.db import SessionDep
from app.dependencies.identity import FORBIDDEN, UNAUTHENTICATED
from app.dependencies.scope import admin_of_restaurant, staff_of_row
from app.models.catalog import MenuItem
from app.models.enums import ModifierKind, StaffRole
from app.models.modifiers import (
    MenuItemModifierGroup,
    MenuItemModifierOption,
    menu_item_modifier_links,
)
from app.schemas.modifiers import (
    MAX_OPTIONS_PER_GROUP,
    ModifierGroupCreate,
    ModifierGroupRead,
    ModifierGroupUpdate,
    ModifierOptionCreate,
    ModifierOptionRead,
    ModifierOptionUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["modifiers"])

SCOPED = {**UNAUTHENTICATED, **FORBIDDEN}
ADMIN_ERRORS = {**SCOPED, **NOT_FOUND}

# A variant group is exactly one choice, by definition. Pinned on both create
# and patch whatever the caller sent — see the module docstring on why a variant
# with other bounds is not a variant.
VARIANT_BOUNDS = (1, 1)

# The smallest either/or worth offering. Deleting below this leaves a "pick one"
# with one answer, which is not a choice — the customer would be shown a
# question with a single button.
MIN_VARIANT_OPTIONS = 2

UNIQUE_GROUP_NAME = "uq_modifier_group_name"
UNIQUE_OPTION_NAME = "uq_modifier_option_name"


# --- Restaurant resolvers, for staff_of_row ------------------------------


async def _group_restaurant(session: AsyncSession, group_id: int) -> int | None:
    return await session.scalar(
        select(MenuItemModifierGroup.restaurant_id).where(
            MenuItemModifierGroup.id == group_id
        )
    )


async def _option_restaurant(session: AsyncSession, option_id: int) -> int | None:
    return await session.scalar(
        select(MenuItemModifierGroup.restaurant_id)
        .join(
            MenuItemModifierOption,
            MenuItemModifierOption.group_id == MenuItemModifierGroup.id,
        )
        .where(MenuItemModifierOption.id == option_id)
    )


admin_of_group = staff_of_row(
    path_param="group_id",
    resolve=_group_restaurant,
    resource="modifier group",
    minimum_role=StaffRole.ADMIN,
)
admin_of_option = staff_of_row(
    path_param="option_id",
    resolve=_option_restaurant,
    resource="modifier option",
    minimum_role=StaffRole.ADMIN,
)


# --- Reads ---------------------------------------------------------------


def _to_read(
    group: MenuItemModifierGroup, *, menu_item_ids: list[int] | None = None
) -> ModifierGroupRead:
    return ModifierGroupRead(
        id=group.id,
        restaurant_id=group.restaurant_id,
        name=group.name,
        kind=group.kind,
        min_select=group.min_select,
        max_select=group.max_select,
        sort_order=group.sort_order,
        options=[
            ModifierOptionRead.model_validate(option) for option in group.options
        ],
        menu_item_ids=menu_item_ids or [],
    )


@router.get(
    "/menu-items/{item_id}/modifier-groups",
    response_model=list[ModifierGroupRead],
    responses=NOT_FOUND,
    summary="The choices a dish offers (public)",
)
async def list_groups_for_item(item_id: int, session: SessionDep):
    """Unauthenticated, like the menu itself.

    A customer app calls this to build the ordering form, so it cannot require a
    token — and there is nothing private here: it is the same information printed
    on a menu card. Options are returned in sort order INCLUDING unavailable
    ones, flagged; see the note on ModifierOptionRead.
    """
    exists = await session.scalar(select(MenuItem.id).where(MenuItem.id == item_id))
    if exists is None:
        raise not_found("menu item", item_id)

    rows = await session.execute(
        select(MenuItemModifierGroup)
        .join(
            menu_item_modifier_links,
            menu_item_modifier_links.c.group_id == MenuItemModifierGroup.id,
        )
        .where(menu_item_modifier_links.c.menu_item_id == item_id)
        .order_by(MenuItemModifierGroup.sort_order, MenuItemModifierGroup.id)
    )
    # No menu_item_ids: the caller asked about one dish and already knows which.
    return [_to_read(group) for group in rows.scalars().unique()]


@router.get(
    "/restaurants/{restaurant_id}/modifier-groups",
    response_model=list[ModifierGroupRead],
    dependencies=[Depends(admin_of_restaurant)],
    responses=SCOPED,
    summary="Every group this restaurant has built",
)
async def list_groups(restaurant_id: int, session: SessionDep):
    """The management view: every group, each carrying the dishes it is on.

    Unpaginated. A restaurant has a handful of these — "Portion", "Heat", "Goes
    with the biryani" — and a paged editor for six rows is worse than a list.
    """
    rows = await session.execute(
        select(MenuItemModifierGroup)
        .where(MenuItemModifierGroup.restaurant_id == restaurant_id)
        .order_by(MenuItemModifierGroup.sort_order, MenuItemModifierGroup.id)
    )
    groups = list(rows.scalars().unique())
    attachments = await _attachments_for(session, [group.id for group in groups])
    return [
        _to_read(group, menu_item_ids=attachments.get(group.id, [])) for group in groups
    ]


async def _attachments_for(
    session: AsyncSession, group_ids: list[int]
) -> dict[int, list[int]]:
    """One query for the whole page rather than one per group."""
    if not group_ids:
        return {}
    rows = await session.execute(
        select(
            menu_item_modifier_links.c.group_id,
            menu_item_modifier_links.c.menu_item_id,
        )
        .where(menu_item_modifier_links.c.group_id.in_(group_ids))
        .order_by(menu_item_modifier_links.c.menu_item_id)
    )
    found: dict[int, list[int]] = {}
    for group_id, menu_item_id in rows:
        found.setdefault(group_id, []).append(menu_item_id)
    return found


# --- Writes --------------------------------------------------------------


def _bounds_for(kind: ModifierKind, min_select: int, max_select: int) -> tuple[int, int]:
    return VARIANT_BOUNDS if kind is ModifierKind.VARIANT else (min_select, max_select)


async def _assert_items_belong(
    session: AsyncSession, restaurant_id: int, menu_item_ids: list[int]
) -> None:
    """Every dish named must belong to THIS restaurant.

    Checked rather than assumed: without it an admin of restaurant 1 could
    attach their group to restaurant 2's dishes, and the customer-facing read
    above would then serve one kitchen's add-ons on another kitchen's menu.
    """
    if not menu_item_ids:
        return
    wanted = set(menu_item_ids)
    rows = await session.execute(
        select(MenuItem.id).where(
            MenuItem.id.in_(wanted), MenuItem.restaurant_id == restaurant_id
        )
    )
    mine = set(rows.scalars())
    stray = sorted(wanted - mine)
    if stray:
        raise unprocessable(
            f"Menu items {stray} do not belong to restaurant {restaurant_id}"
        )


async def _replace_attachments(
    session: AsyncSession, group_id: int, menu_item_ids: list[int]
) -> None:
    """Wholesale replacement — see the note on ModifierGroupUpdate.

    Delete-then-insert rather than a diff: the set is tiny, and a diff is three
    times the code for the same two statements.
    """
    await session.execute(
        delete(menu_item_modifier_links).where(
            menu_item_modifier_links.c.group_id == group_id
        )
    )
    if menu_item_ids:
        await session.execute(
            menu_item_modifier_links.insert(),
            [
                {"group_id": group_id, "menu_item_id": item_id}
                for item_id in sorted(set(menu_item_ids))
            ],
        )


async def _load_group(session: AsyncSession, group_id: int) -> MenuItemModifierGroup:
    group = await session.get(MenuItemModifierGroup, group_id)
    if group is None:
        raise not_found("modifier group", group_id)
    return group


@router.post(
    "/restaurants/{restaurant_id}/modifier-groups",
    response_model=ModifierGroupRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(admin_of_restaurant)],
    responses={**SCOPED, **CONFLICT},
    summary="Create a group of choices",
)
async def create_group(
    restaurant_id: int, payload: ModifierGroupCreate, session: SessionDep
):
    """A group starts empty. It has no effect until it has options AND a dish.

    Both halves are worth stating: a group with no options never renders, and a
    group attached to nothing is never reached — so an admin who creates one and
    stops has changed nothing a customer can see.
    """
    await _assert_items_belong(session, restaurant_id, payload.menu_item_ids)
    min_select, max_select = _bounds_for(
        payload.kind, payload.min_select, payload.max_select
    )

    # Placed after every existing group, so a new question lands at the bottom
    # of the ordering form rather than jumping to the top of one.
    highest = await session.scalar(
        select(MenuItemModifierGroup.sort_order)
        .where(MenuItemModifierGroup.restaurant_id == restaurant_id)
        .order_by(MenuItemModifierGroup.sort_order.desc())
        .limit(1)
    )

    group = MenuItemModifierGroup(
        restaurant_id=restaurant_id,
        name=payload.name,
        kind=payload.kind,
        min_select=min_select,
        max_select=max_select,
        sort_order=(highest or 0) + 1,
    )
    session.add(group)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _write_failure(exc, payload.name) from exc

    await _replace_attachments(session, group.id, payload.menu_item_ids)
    await session.refresh(group)
    logger.info(
        "Created modifier group %s (%s) for restaurant %s",
        group.id,
        payload.kind,
        restaurant_id,
    )
    return _to_read(group, menu_item_ids=sorted(set(payload.menu_item_ids)))


@router.patch(
    "/modifier-groups/{group_id}",
    response_model=ModifierGroupRead,
    dependencies=[Depends(admin_of_group)],
    responses={**ADMIN_ERRORS, **CONFLICT},
    summary="Change a group",
)
async def update_group(
    group_id: int, payload: ModifierGroupUpdate, session: SessionDep
):
    group = await _load_group(session, group_id)
    changes = payload.model_dump(exclude_unset=True)
    menu_item_ids = changes.pop("menu_item_ids", None)

    kind = changes.get("kind", group.kind)
    min_select, max_select = _bounds_for(
        kind,
        changes.get("min_select", group.min_select),
        changes.get("max_select", group.max_select),
    )
    if min_select > max_select:
        raise unprocessable(
            f"min_select ({min_select}) cannot exceed max_select ({max_select})"
        )
    # A group cannot require more answers than it has to give. Checked against
    # the options it actually holds, because lowering max_select and deleting
    # options are two separate calls and the order they arrive in is the
    # caller's choice.
    option_count = len(group.options)
    if option_count and max_select > option_count:
        raise unprocessable(
            f"{group.name} has {option_count} option(s), so max_select cannot be "
            f"{max_select} — every dish it is attached to would be unorderable"
        )

    for field, value in changes.items():
        setattr(group, field, value)
    group.kind = kind
    group.min_select = min_select
    group.max_select = max_select

    if menu_item_ids is not None:
        await _assert_items_belong(session, group.restaurant_id, menu_item_ids)
        await _replace_attachments(session, group.id, menu_item_ids)

    try:
        await session.flush()
    except IntegrityError as exc:
        raise _write_failure(exc, changes.get("name", group.name)) from exc

    await session.refresh(group)
    attachments = await _attachments_for(session, [group.id])
    logger.info("Updated modifier group %s: %s", group_id, sorted(changes))
    return _to_read(group, menu_item_ids=attachments.get(group.id, []))


@router.delete(
    "/modifier-groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(admin_of_group)],
    responses=ADMIN_ERRORS,
    summary="Delete a group and its choices",
)
async def delete_group(group_id: int, session: SessionDep):
    """The options and the attachments go with it — both cascade.

    Past orders keep what the customer actually chose: order_item_modifiers
    stores the option id as ON DELETE SET NULL beside a frozen copy of the name
    and price, so a receipt still reads correctly after this.
    """
    group = await _load_group(session, group_id)
    await session.delete(group)
    logger.info("Deleted modifier group %s", group_id)


@router.post(
    "/modifier-groups/{group_id}/options",
    response_model=ModifierOptionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(admin_of_group)],
    responses={**ADMIN_ERRORS, **CONFLICT},
    summary="Add a choice to a group",
)
async def add_option(
    group_id: int, payload: ModifierOptionCreate, session: SessionDep
):
    group = await _load_group(session, group_id)
    if len(group.options) >= MAX_OPTIONS_PER_GROUP:
        raise conflict(
            f"{group.name} already has {MAX_OPTIONS_PER_GROUP} choices, which is "
            "the most one question can offer. Split it into two groups."
        )

    option = MenuItemModifierOption(
        group_id=group_id,
        name=payload.name,
        price_delta=payload.price_delta,
        is_available=payload.is_available,
        sort_order=(
            payload.sort_order
            if payload.sort_order is not None
            else len(group.options) + 1
        ),
    )
    session.add(option)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _write_failure(exc, payload.name) from exc
    await session.refresh(option)
    return option


@router.patch(
    "/modifier-options/{option_id}",
    response_model=ModifierOptionRead,
    dependencies=[Depends(admin_of_option)],
    responses={**ADMIN_ERRORS, **CONFLICT},
    summary="Change a choice, or turn it off for tonight",
)
async def update_option(
    option_id: int, payload: ModifierOptionUpdate, session: SessionDep
):
    option = await session.get(MenuItemModifierOption, option_id)
    if option is None:
        raise not_found("modifier option", option_id)

    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(option, field, value)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _write_failure(exc, changes.get("name", option.name)) from exc
    await session.refresh(option)
    return option


@router.delete(
    "/modifier-options/{option_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(admin_of_option)],
    responses={**ADMIN_ERRORS, **CONFLICT},
    summary="Remove a choice",
)
async def delete_option(option_id: int, session: SessionDep):
    """Refused when it would leave an either/or with one answer.

    A `variant` group is a question the customer MUST answer, so one remaining
    option is a form with a single button and no decision. The refusal names the
    two ways out — turn it off, or delete the whole group — because "cannot
    delete" on its own leaves somebody stuck.
    """
    option = await session.get(MenuItemModifierOption, option_id)
    if option is None:
        raise not_found("modifier option", option_id)

    group = await _load_group(session, option.group_id)
    if group.kind is ModifierKind.VARIANT and len(group.options) <= MIN_VARIANT_OPTIONS:
        raise conflict(
            f"{group.name} is a pick-one choice and needs at least "
            f"{MIN_VARIANT_OPTIONS} options. Turn this one off instead, or delete "
            "the whole group."
        )
    # An addon group can legitimately empty out, but max_select must not be left
    # above what remains — the dish would become unorderable.
    remaining = len(group.options) - 1
    if remaining and group.max_select > remaining:
        group.max_select = remaining
        # min_select has to come down too, or ck_modifier_group_bounds
        # (min_select <= max_select) is violated at COMMIT -- outside this
        # handler, so an unconditional 500 and the delete silently not
        # happening. Reachable today: an addon group with min_select=2,
        # max_select=2 and two options, delete one, and max drops to 1 while min
        # stays 2. update_group already checks this invariant; the delete path
        # did not.
        group.min_select = min(group.min_select, group.max_select)

    await session.delete(option)
    logger.info("Deleted modifier option %s from group %s", option_id, group.id)


def _write_failure(exc: IntegrityError, name: str):
    """Turn the two unique constraints into sentences a manager can act on."""
    detail = str(exc.orig)
    if UNIQUE_GROUP_NAME in detail:
        return conflict(f"This restaurant already has a group called {name}")
    if UNIQUE_OPTION_NAME in detail:
        return conflict(f"This group already has a choice called {name}")
    # ck_modifier_option_price and ck_modifier_group_bounds are both checked
    # above, so reaching here means something new was added to the table.
    #
    # The Postgres text goes to the log, NOT to the client. It used to be
    # interpolated into the response, which for a NOT NULL or CHECK violation
    # includes "DETAIL: Failing row contains (...)" -- the entire row, plus the
    # table and constraint names. Every other refusal in this codebase keeps one
    # fixed sentence for the caller and the reason in the log; this one leaked.
    logger.warning("Unmapped integrity error on a modifier write: %s", detail)
    return unprocessable(
        "That group or choice was rejected. Check the name, the price and the "
        "pick-one bounds, then try again."
    )
