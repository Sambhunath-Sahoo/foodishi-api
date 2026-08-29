"""What the platform charges, and where the work is.

Two unrelated things in one router because both are the console's chrome rather
than any one section's data: the navigation reads `/workload` on every page, and
`/settings` is the one screen that decides numbers every other screen reports.

The guard hangs off the router, not the routes. Nothing under `/admin` is
customer- or restaurant-scoped — every figure spans the platform — so five
decorators would be five chances to forget one, and the sixth route somebody
adds inherits the guard for free. Same reasoning as routers/metrics.py.
"""

from fastapi import APIRouter, Depends

from app.core.errors import NOT_FOUND
from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.models.catalog import Restaurant
from app.models.settings import PlatformSettings
from app.schemas.admin import (
    CommissionSettingsRead,
    DeliverySettingsRead,
    NegotiatedRate,
    OrderRuleSettingsRead,
    PlatformSettingsRead,
    PlatformSettingsUpdate,
    RestaurantCommissionUpdate,
    TaxSettingsRead,
    Workload,
)
from app.services import admin_insights, platform_settings

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_role())],
)

ADMIN_RESPONSES = {**UNAUTHENTICATED, **NOT_PLATFORM}


async def _to_read(
    session: SessionDep, row: PlatformSettings
) -> PlatformSettingsRead:
    """The flat table as the four groups the API speaks in.

    The only place the two shapes meet. Grouping is not decoration: pricing, the
    commercial deal, tax and what an order may be are four different
    conversations, and a flat wall of twenty fields invites somebody to change
    the GST rate while looking for the delivery fee.
    """
    negotiated = await platform_settings.negotiated_rates(session)
    return PlatformSettingsRead(
        delivery=DeliverySettingsRead(
            base_fee=row.delivery_base_fee,
            per_km_fee=row.delivery_per_km_fee,
            free_delivery_above=row.delivery_free_above,
            surge_multiplier=row.delivery_surge_multiplier,
            surge_after_minutes_late=row.delivery_surge_after_minutes,
            max_distance_km=row.delivery_max_distance_km,
            packaging_fee=row.delivery_packaging_fee,
        ),
        commission=CommissionSettingsRead(
            default_percent=row.commission_default_percent,
            settlement_days=row.commission_settlement_days,
            negotiated=[
                NegotiatedRate(restaurant_id=rid, name=name, percent=percent)
                for rid, name, percent in negotiated
            ],
        ),
        tax=TaxSettingsRead(
            gst_percent=row.tax_gst_percent,
            is_packaging_taxable=row.tax_packaging_taxable,
            is_delivery_taxable=row.tax_delivery_taxable,
            gstin=row.tax_gstin,
        ),
        order_rules=OrderRuleSettingsRead(
            min_order_value=row.rule_min_order_value,
            max_items_per_order=row.rule_max_items_per_order,
            free_cancellation_minutes=row.rule_free_cancellation_minutes,
            late_cancellation_fee_percent=row.rule_late_cancellation_fee_percent,
            refund_sla_hours=row.rule_refund_sla_hours,
            auto_cancel_unconfirmed_minutes=row.rule_auto_cancel_unconfirmed_minutes,
            prep_buffer_minutes=row.rule_prep_buffer_minutes,
        ),
        updated_at=row.updated_at,
    )


@router.get("/workload", response_model=Workload, responses=ADMIN_RESPONSES)
async def get_workload(session: SessionDep):
    """One figure per section of the console that has something to say.

    Read on every page, because it is what the navigation reports. Sections with
    nothing to report are absent rather than zero: a badge on a section nobody
    has to act on teaches the reader to ignore the ones that matter.
    """
    counts = await admin_insights.workload(session)
    return Workload(
        live_orders=counts.live_orders,
        orders_late=counts.orders_late,
        deliveries_out=counts.deliveries_out,
        deliveries_late=counts.deliveries_late,
        restaurants_slipping=counts.restaurants_slipping,
        coupons_exhausted=counts.coupons_exhausted,
        payments_failed=counts.payments_failed,
        refunds_breached=counts.refunds_breached,
        refunds_owed=counts.refunds_owed,
    )


@router.get("/settings", response_model=PlatformSettingsRead, responses=ADMIN_RESPONSES)
async def get_settings(session: SessionDep):
    """The platform's own numbers.

    Never 404s. A database with no settings row is the ordinary state of a fresh
    deployment, and the answer there is the platform's defaults rather than an
    error the reader can do nothing about — see services/platform_settings.load.
    """
    return await _to_read(session, await platform_settings.load(session))


@router.put("/settings", response_model=PlatformSettingsRead, responses=ADMIN_RESPONSES)
async def put_settings(payload: PlatformSettingsUpdate, session: SessionDep):
    """Change some of them.

    PUT rather than PATCH because the body is the whole settings document as far
    as the client is concerned, and partial rather than total because a form that
    submitted all twenty fields would silently overwrite a setting somebody else
    changed while it was open.

    Every rule that can refuse this lives in the service, not here — a router
    that validated the same combinations would eventually disagree with it.
    """
    row = await platform_settings.save(session, payload.to_columns())
    return await _to_read(session, row)


@router.post(
    "/settings/reset", response_model=PlatformSettingsRead, responses=ADMIN_RESPONSES
)
async def reset_settings(session: SessionDep):
    """Back to what the platform ships.

    A POST and not a DELETE: nothing is removed as far as the caller is
    concerned — the settings still exist afterwards, at their defaults. Orders
    already placed keep the rules frozen onto them either way.
    """
    return await _to_read(session, await platform_settings.reset(session))


@router.put(
    "/restaurants/{restaurant_id}/commission",
    response_model=NegotiatedRate,
    responses={**ADMIN_RESPONSES, **NOT_FOUND},
)
async def put_restaurant_commission(
    restaurant_id: int, payload: RestaurantCommissionUpdate, session: SessionDep
):
    """Put one kitchen on its own rate.

    Not part of the settings document, and that is the design: a negotiated rate
    is a commercial fact about one restaurant, so it lives on that restaurant's
    row where services/settlements reads it per order. The settings document
    only carries what a NEW restaurant starts on.
    """
    percent = await platform_settings.set_restaurant_commission(
        session, restaurant_id, payload.percent
    )
    # Read the name from the restaurant, not from negotiated_rates: a rate set
    # back TO the platform default is no longer an override, so it is absent
    # from that list and the name would come back empty on the one call that
    # most needs to echo it.
    restaurant = await session.get(Restaurant, restaurant_id)
    return NegotiatedRate(
        restaurant_id=restaurant_id,
        name=restaurant.name if restaurant is not None else "",
        percent=percent,
    )
