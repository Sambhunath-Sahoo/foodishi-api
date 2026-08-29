"""Read and write the one row of platform settings.

Two behaviours worth knowing before calling this:

  * `load` NEVER returns None. A database with no settings row is the ordinary
    state on a fresh deployment, and every caller wants the platform's defaults
    in that case rather than a null check. So the row is created on first read,
    with the column defaults the model declares — which are the values the
    Python constants already used, so introducing this table changes no price.

  * `save` validates. Every rule below is a mistake somebody can make in a form
    that would cost real money or break a real screen, and each is refused with
    the sentence that says what to change. The API's own `unprocessable` is
    used so the message reaches the operator verbatim, exactly as the coupon
    and cancellation refusals do.

Pure of the router: no HTTPException raised except through core.errors, no
FastAPI import, no knowledge of who is calling.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import not_found, unprocessable
from app.models.catalog import Restaurant
from app.models.settings import SINGLETON_ID, PlatformSettings

FULL_PERCENT = Decimal("100")
MAX_SETTLEMENT_DAYS = 60
MAX_SLA_HOURS = 720


async def load(session: AsyncSession) -> PlatformSettings:
    """The platform's settings, creating the row from its defaults if absent.

    ON CONFLICT DO NOTHING rather than a read-then-insert: two requests arriving
    together on a fresh database would both see no row and the second insert
    would die on the primary key. This way the loser is a no-op and both get the
    same row back.
    """
    row = await session.get(PlatformSettings, SINGLETON_ID)
    if row is not None:
        return row

    await session.execute(
        pg_insert(PlatformSettings)
        .values(id=SINGLETON_ID)
        .on_conflict_do_nothing(index_elements=[PlatformSettings.id])
    )
    await session.flush()
    created = await session.get(PlatformSettings, SINGLETON_ID)
    if created is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError("Platform settings row could not be created")
    return created


def _validate(values: dict[str, object]) -> None:
    """Refuse a combination that would cost money or break a screen.

    Reads from `values` rather than from the model so a partial update is judged
    on what it will become, not on what it was. The caller merges first.
    """
    percent_fields = (
        ("commission_default_percent", "Commission"),
        ("tax_gst_percent", "GST"),
        ("rule_late_cancellation_fee_percent", "The late cancellation fee"),
    )
    for field, label in percent_fields:
        value = values.get(field)
        if isinstance(value, Decimal) and not (0 <= value < FULL_PERCENT):
            raise unprocessable(
                f"{label} has to sit between 0 and 100%. "
                f"{value} is not a share of anything."
            )

    commission = values.get("commission_default_percent")
    if isinstance(commission, Decimal) and commission <= 0:
        raise unprocessable(
            "Commission has to be more than nothing — a platform on 0% earns "
            "nothing from a delivered order."
        )

    base = values.get("delivery_base_fee")
    free_above = values.get("delivery_free_above")
    if (
        isinstance(base, Decimal)
        and isinstance(free_above, Decimal)
        and free_above > 0
        and free_above <= base
    ):
        raise unprocessable(
            "Free delivery has to kick in above the base fee itself, or every "
            "order gets it free."
        )

    surge = values.get("delivery_surge_multiplier")
    if isinstance(surge, Decimal) and surge < 1:
        raise unprocessable(
            "A surge multiplier under 1 would discount delivery when the "
            "platform is already running behind."
        )

    radius = values.get("delivery_max_distance_km")
    if isinstance(radius, Decimal) and radius <= 0:
        raise unprocessable("The delivery radius has to be more than nothing.")

    settlement = values.get("commission_settlement_days")
    if isinstance(settlement, int) and not (1 <= settlement <= MAX_SETTLEMENT_DAYS):
        raise unprocessable(
            f"Settlement has to fall between a day and {MAX_SETTLEMENT_DAYS} "
            f"days after delivery."
        )

    sla = values.get("rule_refund_sla_hours")
    if isinstance(sla, int) and not (1 <= sla <= MAX_SLA_HOURS):
        raise unprocessable(
            f"The refund promise has to be between an hour and {MAX_SLA_HOURS} "
            f"hours. Every refund on the SLA watch is measured against it."
        )

    max_items = values.get("rule_max_items_per_order")
    if isinstance(max_items, int) and max_items < 1:
        raise unprocessable("An order has to be allowed at least one item.")

    auto_cancel = values.get("rule_auto_cancel_unconfirmed_minutes")
    if isinstance(auto_cancel, int) and auto_cancel < 1:
        raise unprocessable(
            "Auto-cancel needs a wait, or a kitchen loses every order it does "
            "not confirm in the same instant."
        )

    for field, label in (
        ("rule_free_cancellation_minutes", "The free cancellation window"),
        ("rule_prep_buffer_minutes", "The promise buffer"),
        ("delivery_surge_after_minutes", "The surge threshold"),
    ):
        value = values.get(field)
        if isinstance(value, int) and value < 0:
            raise unprocessable(f"{label} cannot run backwards.")

    minimum = values.get("rule_min_order_value")
    if isinstance(minimum, Decimal) and minimum < 0:
        raise unprocessable("A minimum order value cannot be negative.")


async def save(
    session: AsyncSession, changes: dict[str, object]
) -> PlatformSettings:
    """Apply a partial update, validated against the resulting whole.

    Partial on purpose: a form that saved every field would silently overwrite a
    setting somebody else changed while it was open. Only the keys present are
    written.
    """
    row = await load(session)
    if not changes:
        return row

    merged = {
        column.name: getattr(row, column.name)
        for column in PlatformSettings.__table__.columns
    }
    merged.update(changes)
    _validate(merged)

    for field, value in changes.items():
        setattr(row, field, value)
    await session.flush()
    await session.refresh(row)
    return row


async def reset(session: AsyncSession) -> PlatformSettings:
    """Back to what the platform ships.

    Deletes the row rather than writing the defaults back field by field, so
    "the default" has exactly one definition — the model's — and cannot drift
    from a second copy kept here. `load` recreates it on the next read.
    """
    row = await session.get(PlatformSettings, SINGLETON_ID)
    if row is not None:
        await session.delete(row)
        await session.flush()
    return await load(session)


async def set_restaurant_commission(
    session: AsyncSession, restaurant_id: int, percent: Decimal
) -> Decimal:
    """Put one kitchen on its own rate.

    Lives here rather than in the settings row because a negotiated rate is a
    commercial fact about one restaurant: `restaurants.commission_percent` is
    the rate that kitchen is actually on, and `services/settlements` reads it
    per order. The platform default is only what a new restaurant starts on.
    """
    if not (0 < percent < FULL_PERCENT):
        raise unprocessable(
            f"A negotiated rate has to sit between 0 and 100%. {percent} is not "
            f"a share of a sale."
        )

    restaurant = await session.get(Restaurant, restaurant_id)
    if restaurant is None:
        # 404, not 422: the route declares NOT_FOUND in its responses, so the
        # generated OpenAPI promised a 404 that could never occur and clients
        # branching on it mishandled a typo'd id.
        raise not_found("restaurant", restaurant_id)

    restaurant.commission_percent = percent
    try:
        await session.flush()
    except IntegrityError as exc:  # pragma: no cover - numeric(5,2) overflow
        raise unprocessable(
            "That rate does not fit in the commission column — it takes at most "
            "three digits before the decimal point."
        ) from exc
    # Refresh before echoing. numeric(5, 2) rounds on write, and with
    # expire_on_commit=False the in-memory attribute keeps whatever Python put
    # there -- so the response reported the requested rate rather than the stored
    # one. `save()` in this same module already refreshes for exactly this reason.
    await session.refresh(restaurant)
    return restaurant.commission_percent


async def negotiated_rates(session: AsyncSession) -> list[tuple[int, str, Decimal]]:
    """Kitchens whose rate differs from the platform default, with their names.

    Derived rather than stored: the overrides ARE the restaurants table, and a
    second list of them in the settings row would be the same fact twice with
    nothing keeping the two honest.
    """
    settings = await load(session)
    rows = await session.execute(
        select(Restaurant.id, Restaurant.name, Restaurant.commission_percent)
        .where(Restaurant.commission_percent != settings.commission_default_percent)
        .order_by(Restaurant.name)
    )
    return [(row[0], row[1], row[2]) for row in rows.all()]
