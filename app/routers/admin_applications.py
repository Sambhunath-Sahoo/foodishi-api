"""The operator's queue of restaurants asking to join, and the two answers.

Under /admin because everything here spans the platform rather than one
restaurant: an application belongs to no kitchen — granting it is what creates
one. Guard on the router, not the routes, for the reason routers/metrics.py and
routers/admin_platform.py give: four decorators are four chances to forget one,
and the fifth route somebody adds inherits the guard for free.

The rules live in app/services/onboarding.py, not here. Approval writes three
tables and can refuse for three different reasons, and a router that knew any of
that would eventually disagree with the next caller that approves an application.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, NOT_FOUND, not_found
from app.core.pagination import Page, PageDep, paginate
from app.db import SessionDep
from app.dependencies.identity import (
    NOT_PLATFORM,
    UNAUTHENTICATED,
    require_platform_role,
)
from app.models.application import RestaurantApplication
from app.models.enums import ApplicationStatus
from app.models.platform import PlatformStaff
from app.models.user import User
from app.schemas.application import (
    AdminApplicationRow,
    ApplicationApprove,
    ApplicationRead,
    ApplicationReject,
)
from app.services import onboarding

logger = logging.getLogger(__name__)

# ONE dependency object, used both as the router's guard and as the parameter
# the two decision routes read the reviewer from. require_platform_role() builds
# a NEW function on every call, and FastAPI caches a dependency's result per
# request by the callable — so calling it twice would run the platform check
# twice per request, for one answer. Built once, cached once.
_require_ops = require_platform_role()

router = APIRouter(
    prefix="/admin/restaurant-applications",
    tags=["admin", "applications"],
    dependencies=[Depends(_require_ops)],
)

OpsStaff = Annotated[PlatformStaff, Depends(_require_ops)]

ADMIN_RESPONSES = {**UNAUTHENTICATED, **NOT_PLATFORM}
DECISION_RESPONSES = {**ADMIN_RESPONSES, **NOT_FOUND, **CONFLICT}


def _row(application: RestaurantApplication, applicant: User) -> AdminApplicationRow:
    """One builder for every response in this router.

    The applicant's details come from their CURRENT users row rather than from
    anything copied onto the application, so an operator reading this has the
    address that works today — which is the one they will write to if they need
    to ask a question before deciding.
    """
    # Validated as the PARENT shape and widened, never as AdminApplicationRow
    # itself: the applicant columns do not exist on the application row, so
    # validating the child against it would refuse every row in the queue.
    return AdminApplicationRow(
        **ApplicationRead.model_validate(application).model_dump(),
        applicant_user_id=applicant.id,
        applicant_name=applicant.name,
        applicant_email=applicant.email,
        applicant_phone=applicant.phone,
        is_applicant_active=applicant.is_active,
    )


async def _load(
    session: AsyncSession, application_id: int
) -> tuple[RestaurantApplication, User]:
    """The application and the person behind it, or 404.

    An inner join: applicant_user_id is ON DELETE CASCADE, so an application
    whose applicant is gone does not exist either.
    """
    row = (
        await session.execute(
            select(RestaurantApplication, User)
            .join(User, User.id == RestaurantApplication.applicant_user_id)
            .where(RestaurantApplication.id == application_id)
        )
    ).one_or_none()
    if row is None:
        raise not_found("restaurant application", application_id)
    return row[0], row[1]


@router.get(
    "",
    response_model=Page[AdminApplicationRow],
    responses=ADMIN_RESPONSES,
    summary="Restaurants waiting to join",
)
async def list_applications(
    session: SessionDep,
    page: PageDep,
    status: Annotated[
        ApplicationStatus | None,
        Query(description="Only applications in this state. Omit for every state."),
    ] = None,
):
    """The queue, oldest first.

    Oldest first and not newest: this is a worklist, not a feed. Sorting the
    newest to the top means the application that has been waiting longest sinks
    out of sight, which is the one failure mode a queue must not have.

    `status` defaults to every state rather than to pending. A default filter
    that hides the answered ones would make "did we already turn these people
    down" unanswerable from the screen that has to answer it, and the console
    asks for `pending` explicitly on the tab that wants the queue.
    """
    statement = select(RestaurantApplication)
    if status is not None:
        statement = statement.where(RestaurantApplication.status == status)
    statement = statement.order_by(
        RestaurantApplication.created_at.asc(), RestaurantApplication.id.asc()
    )

    applications, total = await paginate(session, statement, page)
    applicants = await _applicants_by_id(
        session, [application.applicant_user_id for application in applications]
    )
    return Page(
        items=[
            _row(application, applicants[application.applicant_user_id])
            for application in applications
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


async def _applicants_by_id(
    session: AsyncSession, user_ids: list[int]
) -> dict[int, User]:
    """One extra query for the whole page, rather than one per row."""
    if not user_ids:
        return {}
    rows = await session.execute(select(User).where(User.id.in_(set(user_ids))))
    return {user.id: user for user in rows.scalars()}


@router.get(
    "/{application_id}",
    response_model=AdminApplicationRow,
    responses={**ADMIN_RESPONSES, **NOT_FOUND},
    summary="One application in full",
)
async def get_application(application_id: int, session: SessionDep):
    return _row(*await _load(session, application_id))


@router.post(
    "/{application_id}/approve",
    response_model=AdminApplicationRow,
    responses=DECISION_RESPONSES,
    summary="Approve an application and create the restaurant",
)
async def approve_application(
    application_id: int,
    payload: ApplicationApprove,
    session: SessionDep,
    operator: OpsStaff,
):
    """Say yes: the restaurant is created, dormant, and handed to the applicant.

    What the applicant has afterwards is an admin membership on a restaurant
    that customers cannot see — because is_active is false and discovery filters
    on it. They write their policy and their menu through the partner console,
    then turn the kitchen on themselves with PUT
    /restaurants/{id}/availability. Approving is not publishing; see
    services/onboarding.approve for why those are two decisions.

    A POST rather than a PATCH with a status field: this is not an edit to a row,
    it is an act that writes three tables, and a client that could PATCH the
    status could mark an application approved without any of it happening.
    """
    application, applicant = await _load(session, application_id)
    await onboarding.approve(
        session,
        application,
        reviewer_user_id=operator.user_id,
        note=payload.note,
    )
    return _row(application, applicant)


@router.post(
    "/{application_id}/reject",
    response_model=AdminApplicationRow,
    responses=DECISION_RESPONSES,
    summary="Turn an application down, with a reason",
)
async def reject_application(
    application_id: int,
    payload: ApplicationReject,
    session: SessionDep,
    operator: OpsStaff,
):
    """Say no. The reason is required and the applicant reads it verbatim.

    Nothing is deleted: the row stays, so a resubmission arrives beside the
    answer it already had.
    """
    application, applicant = await _load(session, application_id)
    await onboarding.reject(
        session,
        application,
        reviewer_user_id=operator.user_id,
        reason=payload.reason,
    )
    return _row(application, applicant)
