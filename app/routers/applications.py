"""The applicant's side of joining Foodishi.

Two routes, and both take their subject from the verified token rather than from
a path or a body — the same rule as app/routers/me.py, and for the same reason:
an application names the person who will own a restaurant, so a caller who could
name somebody else could hand themselves a kitchen in their name.

Authenticated, not public. A form anybody on the internet could POST is a table
anybody on the internet can fill, and the operator's queue is the thing being
filled. Signing up is free (Supabase Auth, then POST /auth/link), so this costs
a legitimate applicant one screen and costs a script an account per submission.

The admin half — the queue, and the two decisions — is
app/routers/admin_applications.py.
"""

import logging

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import CONFLICT, conflict
from app.db import SessionDep
from app.dependencies.identity import (
    NO_PROFILE,
    UNAUTHENTICATED,
    CurrentUser,
)
from app.models.application import RestaurantApplication
from app.models.catalog import Restaurant
from app.models.enums import ApplicationStatus
from app.schemas.application import ApplicationRead, ApplicationSubmit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/restaurant-applications", tags=["applications"])

IDENTIFIED = {**UNAUTHENTICATED, **NO_PROFILE}

SLUG_TAKEN = {
    409: {"description": "A restaurant already trades under that web address"}
}


@router.post(
    "",
    response_model=ApplicationRead,
    status_code=201,
    responses={**IDENTIFIED, **CONFLICT, **SLUG_TAKEN},
    summary="Apply to put a restaurant on Foodishi",
)
async def submit_application(
    payload: ApplicationSubmit, session: SessionDep, user: CurrentUser
):
    """Ask for a restaurant. Nothing is created but the request itself.

    This route deliberately cannot produce a restaurant, a menu, or a login that
    reaches either. It writes one row in one table that no catalog query, scope
    check or report reads — approval is what mints a tenancy, and that is
    platform staff's to grant (app/routers/admin_applications.py).

    The slug is checked against live restaurants here as a courtesy, not as a
    guarantee. It is the one field an applicant cannot fix later without the
    address changing under their customers, so finding out at submission beats
    finding out in a rejection a day later — but two applicants can still
    propose the same unused slug and only the first approval gets it. The
    approval reports that; see services/onboarding.approve.
    """
    taken = await session.scalar(
        select(Restaurant.id).where(Restaurant.slug == payload.slug)
    )
    if taken is not None:
        raise conflict(
            f"A restaurant already trades under {payload.slug!r} on Foodishi — "
            "choose a different web address"
        )

    application = RestaurantApplication(
        applicant_user_id=user.id, **payload.model_dump()
    )
    session.add(application)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The partial unique index: this account already has one in the queue.
        # Read rather than raced — the index is what makes a double-tapped
        # submit button harmless, so the message names the state, not the error.
        raise conflict(
            "You already have an application waiting for review. Foodishi will "
            "answer that one before you can send another."
        ) from exc

    await session.refresh(application)  # picks up server-side defaults
    logger.info(
        "Application %s submitted by users.id=%s for %r",
        application.id,
        user.id,
        application.slug,
    )
    return application


@router.get(
    "/mine",
    response_model=list[ApplicationRead],
    responses=IDENTIFIED,
    summary="The caller's own applications",
)
async def list_my_applications(session: SessionDep, user: CurrentUser):
    """Every application this account has sent, newest first.

    Unpaginated, like GET /me/restaurants and for the same reason: one person
    applies a handful of times, and a list that arrives in pages is worse than
    one that arrives whole.

    This is what the partner console reads when an account has no restaurant
    yet. "Nobody has given you access to a kitchen" and "your application is
    with Foodishi" are different sentences, and only this route can tell them
    apart.
    """
    rows = await session.execute(
        select(RestaurantApplication)
        .where(RestaurantApplication.applicant_user_id == user.id)
        .order_by(
            # Pending first whatever the dates say: the one still open is the
            # one the applicant came to look at.
            (RestaurantApplication.status != ApplicationStatus.PENDING),
            RestaurantApplication.created_at.desc(),
        )
    )
    return list(rows.scalars().all())
