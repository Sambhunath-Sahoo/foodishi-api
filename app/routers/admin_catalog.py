"""Every kitchen on the platform, including the ones customers cannot see.

One route, and it exists because `GET /restaurants` cannot answer this question.
That endpoint is the customer's discovery listing: unauthenticated, and filtered
on `is_active` unconditionally. The operations console needs the opposite — the
whole catalogue, so it can open a kitchen precisely BECAUSE it is switched off.

That gap became an everyday one when self-serve onboarding landed. Approving an
application creates the restaurant dormant, which is deliberate (see
app/services/onboarding.approve), and the console then had nowhere to show it:
the application moved to "approved", and the restaurant it created appeared on
no screen in the platform's own console. The applicant could see their kitchen
in the partner app and Foodishi could not.

Guard on the router, as in routers/admin_platform.py: nothing here is
customer- or restaurant-scoped, so one decorator cannot be forgotten.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.repositories import catalog as repo
from app.schemas.catalog import RestaurantSummary

router = APIRouter(
    prefix="/admin",
    tags=["admin", "catalog-admin"],
    dependencies=[Depends(require_platform_role())],
)


@router.get(
    "/restaurants",
    response_model=Page[RestaurantSummary],
    responses={**UNAUTHENTICATED, **NOT_PLATFORM},
    summary="Every restaurant, trading or not",
)
async def list_all_restaurants(
    session: SessionDep,
    page: PageDep,
    is_active: Annotated[
        bool | None,
        Query(
            description=(
                "Only kitchens in this state. Omit for every kitchen, which is "
                "what this route exists for."
            )
        ),
    ] = None,
):
    """The catalogue as Foodishi sees it: trading, closed and never-opened alike.

    Answers the same `RestaurantSummary` as the customer listing, on purpose.
    The console renders one kind of row whichever call filled it, and a
    platform-only shape here would have meant a second card component that drifts
    from the first.

    `is_active` is a filter and never a default. Omitting it returns everything —
    the opposite of the customer route, where the filter is not optional and not
    a parameter.
    """
    statement = repo.platform_restaurants_statement(is_active=is_active)
    rows, total = await paginate(session, statement, page)
    return Page(
        items=[RestaurantSummary.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
