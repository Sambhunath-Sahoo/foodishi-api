"""Turning an accepted application into a restaurant somebody can run.

One module because approval is one indivisible act with four parts — the
restaurant row, the owner's membership, the stamp on the application, and the
refusals that stop any of it happening wrongly. Split across a router and a
repository, the router would end up holding the rules, and the next route that
approves an application (a bulk action, a support tool) would hold them again,
differently.

Nothing here commits. The session's transaction is opened per request in
app/db.py and commits when the handler returns, so a slug clash on the second
statement cannot leave a restaurant with no owner.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import conflict, unprocessable
from app.models.application import RestaurantApplication
from app.models.catalog import Restaurant
from app.models.enums import ApplicationStatus, StaffRole
from app.models.staff import RestaurantStaff
from app.models.user import User

logger = logging.getLogger(__name__)

#: The columns copied from an application onto the restaurant it becomes.
#:
#: Named explicitly rather than derived from the model's columns. A column added
#: to `restaurant_applications` for the operator's benefit — an internal note, a
#: score, a source — must not silently become a column written to `restaurants`
#: because both tables happened to spell it the same way.
DETAIL_COLUMNS = (
    "name",
    "slug",
    "description",
    "city",
    "area",
    "address_line",
    "latitude",
    "longitude",
    "phone",
    "price_for_two",
    "avg_prep_minutes",
    "opens_at",
    "closes_at",
)


def assert_pending(application: RestaurantApplication) -> None:
    """Refuse a second decision on an application that already has one.

    Both decisions are terminal, so this is not a race to be retried: it is two
    operators working the same queue, and the second one needs to know the
    answer was already given rather than to overwrite it. An approval that ran
    twice would mint two restaurants from one application.
    """
    if application.status == ApplicationStatus.PENDING:
        return
    when = (
        f" on {application.reviewed_at:%Y-%m-%d}"
        if application.reviewed_at is not None
        else ""
    )
    raise conflict(
        f"Application {application.id} was already {application.status}{when}"
    )


async def _assert_applicant_can_own(
    session: AsyncSession, application: RestaurantApplication
) -> User:
    """The applicant must still be an account that can sign in and act.

    Same rule, and the same reason, as POST /restaurants and POST
    /restaurants/{id}/staff: current_user 403s a deactivated account, so
    approving one would create a restaurant with an owner row and nobody able
    to edit it. Checked before anything is written, because the alternative is
    a restaurant that exists and cannot be reached.
    """
    applicant = await session.get(User, application.applicant_user_id)
    if applicant is None:
        # The FK is ON DELETE CASCADE, so this is a row deleted between the read
        # that filled the operator's screen and this write.
        raise unprocessable(
            f"The applicant's account no longer exists, so application "
            f"{application.id} cannot be approved"
        )
    if not applicant.is_active:
        raise unprocessable(
            f"{applicant.email} is deactivated; reactivate the account before "
            "approving their application"
        )
    return applicant


async def approve(
    session: AsyncSession,
    application: RestaurantApplication,
    *,
    reviewer_user_id: int,
    note: str | None = None,
) -> Restaurant:
    """Mint the restaurant, hand it to the applicant, and stamp the application.

    The restaurant is created DORMANT — is_active is false — and that is the
    substance of this function rather than a detail of it. Discovery filters on
    that column alone (app/repositories/catalog.py), so a restaurant approved at
    11am does not appear to customers until its own owner turns it on, by which
    time they have had the chance to write a policy and a menu. Approving and
    publishing are two different decisions made by two different people, and
    collapsing them means every approval publishes a kitchen with no food on it
    and no cancellation terms, which fails at the customer's checkout rather
    than here.
    """
    assert_pending(application)
    applicant = await _assert_applicant_can_own(session, application)

    restaurant = Restaurant(
        **{column: getattr(application, column) for column in DETAIL_COLUMNS},
        is_active=False,
    )
    session.add(restaurant)
    try:
        await session.flush()
    except IntegrityError as exc:
        # restaurants.slug is unique and an application's is not, so this is the
        # expected collision: two applicants proposed the same one, or a
        # restaurant already trades under it. Reported as the applicant's own
        # field rather than as a database error, because the fix is a different
        # slug and somebody has to be told which value to change.
        raise conflict(
            f"Slug {application.slug!r} is already taken by another restaurant "
            "— reject this application and ask for a different one"
        ) from exc

    # The applicant becomes an admin of their own restaurant, which is what
    # makes every restaurant-scoped route reachable for them. Without this row
    # the approval produces a restaurant nobody on earth can edit.
    session.add(
        RestaurantStaff(
            user_id=applicant.id,
            restaurant_id=restaurant.id,
            role=StaffRole.ADMIN,
        )
    )

    application.status = ApplicationStatus.APPROVED
    application.reviewed_by_user_id = reviewer_user_id
    application.reviewed_at = datetime.now(UTC)
    application.decision_note = note
    application.restaurant_id = restaurant.id

    await session.flush()
    # Refreshed, not just flushed. `updated_at` carries onupdate=func.now(), so
    # the UPDATE leaves that attribute EXPIRED — and reading an expired
    # attribute on an async session is a lazy load from sync context, which
    # raises MissingGreenlet rather than returning a stale value. The router
    # serialises this object immediately afterwards, so it has to be whole.
    await session.refresh(application)
    logger.info(
        "Application %s approved by users.id=%s: restaurant %s created dormant, "
        "owned by users.id=%s",
        application.id,
        reviewer_user_id,
        restaurant.id,
        applicant.id,
    )
    return restaurant


async def reject(
    session: AsyncSession,
    application: RestaurantApplication,
    *,
    reviewer_user_id: int,
    reason: str,
) -> RestaurantApplication:
    """Answer no, in words the applicant will read.

    Nothing is deleted. The row stays, carrying its reason, because the
    applicant's next screen is this answer and because "did we already turn
    these people down, and why" is a question an operator asks about a
    resubmission.
    """
    assert_pending(application)

    application.status = ApplicationStatus.REJECTED
    application.reviewed_by_user_id = reviewer_user_id
    application.reviewed_at = datetime.now(UTC)
    application.decision_note = reason

    await session.flush()
    await session.refresh(application)  # see approve(): updated_at expires
    logger.info(
        "Application %s rejected by users.id=%s", application.id, reviewer_user_id
    )
    return application


async def pending_count(session: AsyncSession) -> int:
    """How many applications are waiting, for the operator console's navigation."""
    total = await session.scalar(
        select(func.count(RestaurantApplication.id)).where(
            RestaurantApplication.status == ApplicationStatus.PENDING
        )
    )
    return int(total or 0)
