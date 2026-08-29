from fastapi import APIRouter, Depends
from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, conflict, not_found
from app.db import SessionDep
from app.dependencies.identity import FORBIDDEN, UNAUTHENTICATED
from app.dependencies.ownership import readable_address, readable_user
from app.models.address import Address
from app.models.order import Order
from app.models.user import User
from app.schemas.address import AddressCreate, AddressRead, AddressUpdate
from app.services.order_state import TERMINAL

# An address is reachable two ways — scoped to its owner when creating or
# listing, by its own id once the client has it — so the module carries two
# prefixed routers and exports a single aggregate for main.py to include.
owned = APIRouter(prefix="/users/{user_id}/addresses", tags=["addresses"])
standalone = APIRouter(prefix="/addresses", tags=["addresses"])

# order_state.TERMINAL is the single definition of "finished".
TERMINAL_STATUSES = tuple(TERMINAL)

DEFAULT_RACE = "Another address was made default at the same time; retry the request"


async def _load(session: AsyncSession, address_id: int) -> Address:
    address = await session.get(Address, address_id)
    if address is None:
        raise not_found("address", address_id)
    return address


async def _require_user(session: AsyncSession, user_id: int) -> None:
    if not await session.get(User, user_id):
        raise not_found("user", user_id)


async def _clear_default(session: AsyncSession, user_id: int) -> None:
    """Drop the user's current default.

    ix_addresses_one_default_per_user is a partial unique index, so two rows
    may never carry is_default at once — not even momentarily inside the
    transaction. Every promotion has to run this first.
    """
    await session.execute(
        update(Address)
        .where(Address.user_id == user_id, Address.is_default.is_(True))
        .values(is_default=False)
    )


async def _promote_newest(session: AsyncSession, user_id: int) -> None:
    """Hand the vacated default to the most recently added address, if any."""
    successor = await session.scalar(
        select(Address.id)
        .where(Address.user_id == user_id)
        .order_by(Address.created_at.desc(), Address.id.desc())
        .limit(1)
    )
    if successor is not None:
        await session.execute(
            update(Address).where(Address.id == successor).values(is_default=True)
        )


# Guarded like the listing below it, and for a sharper reason: the body below
# hands is_default to a customer's first address, so a write from anyone but its
# owner does not merely add a row — it picks where their next order is delivered.
@owned.post(
    "", response_model=AddressRead, status_code=201,
    dependencies=[Depends(readable_user)],
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN},
)
async def create_address(user_id: int, payload: AddressCreate, session: SessionDep):
    await _require_user(session, user_id)

    # A user's first address is their default — otherwise checkout would have
    # nowhere to deliver until they picked one explicitly.
    has_address = await session.scalar(
        select(exists().where(Address.user_id == user_id))
    )
    address = Address(
        user_id=user_id, is_default=not has_address, **payload.model_dump()
    )
    session.add(address)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two concurrent first addresses both saw an empty list and both
        # claimed the default; the partial index rejected the loser.
        raise conflict(DEFAULT_RACE) from exc
    await session.refresh(address)
    return address


@owned.get(
    "",
    response_model=list[AddressRead],
    dependencies=[Depends(readable_user)],
    responses={**NOT_FOUND, **UNAUTHENTICATED, **FORBIDDEN},
)
async def list_addresses(user_id: int, session: SessionDep):
    # Unpaginated by design: a user keeps a handful of addresses, and the
    # picker at checkout wants all of them at once, default first.
    await _require_user(session, user_id)
    rows = await session.execute(
        select(Address)
        .where(Address.user_id == user_id)
        .order_by(
            Address.is_default.desc(), Address.created_at.desc(), Address.id.desc()
        )
    )
    return list(rows.scalars().all())


@standalone.get(
    "/{address_id}",
    response_model=AddressRead,
    dependencies=[Depends(readable_address)],
    responses={**NOT_FOUND, **UNAUTHENTICATED, **FORBIDDEN},
)
async def get_address(address_id: int, session: SessionDep):
    return await _load(session, address_id)


@standalone.patch(
    "/{address_id}",
    response_model=AddressRead,
    dependencies=[Depends(readable_address)],
    responses={**NOT_FOUND, **UNAUTHENTICATED, **FORBIDDEN},
)
async def update_address(address_id: int, payload: AddressUpdate, session: SessionDep):
    statement = (
        update(Address)
        .where(Address.id == address_id)
        .values(**payload.model_dump(exclude_unset=True))
        .returning(Address)
    )
    address = (await session.execute(statement)).scalar_one_or_none()
    if address is None:
        raise not_found("address", address_id)
    return address


@standalone.delete(
    "/{address_id}", status_code=204,
    dependencies=[Depends(readable_address)],
    responses={**NOT_FOUND, **CONFLICT, **UNAUTHENTICATED, **FORBIDDEN}
)
async def delete_address(address_id: int, session: SessionDep):
    address = await _load(session, address_id)

    # A live order still has to be delivered somewhere. Terminal orders keep
    # the reference too (orders.address_id is ON DELETE RESTRICT), so the
    # database may still refuse — that is caught below.
    in_use = await session.scalar(
        select(
            exists().where(
                Order.address_id == address_id,
                Order.status.not_in(TERMINAL_STATUSES),
            )
        )
    )
    if in_use:
        raise conflict(f"Address {address_id} is used by an order in progress")

    user_id, was_default = address.user_id, address.is_default
    try:
        await session.execute(delete(Address).where(Address.id == address_id))
    except IntegrityError as exc:
        raise conflict(
            f"Address {address_id} is referenced by past orders and cannot be deleted"
        ) from exc

    if was_default:
        await _promote_newest(session, user_id)


@standalone.put(
    "/{address_id}/default", response_model=AddressRead,
    dependencies=[Depends(readable_address)],
    responses={**NOT_FOUND, **UNAUTHENTICATED, **FORBIDDEN}
)
async def set_default_address(address_id: int, session: SessionDep):
    address = await _load(session, address_id)
    if address.is_default:
        return address

    await _clear_default(session, address.user_id)
    await session.execute(
        update(Address).where(Address.id == address_id).values(is_default=True)
    )
    await session.refresh(address)
    return address


# Each sub-router already baked its prefix into its routes, so the aggregate
# adopts them directly. include_router() would work too, but it defers
# resolution and leaves router.routes holding placeholders instead of routes.
router = APIRouter()
router.routes.extend(owned.routes)
router.routes.extend(standalone.routes)
