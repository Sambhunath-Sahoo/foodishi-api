"""What a restaurant asking to join sends, and what the two sides read back.

Three audiences, three shapes, and the split is the point:

  * ApplicationSubmit — what the applicant may declare. The restaurant's own
    details and nothing else: not who they are (that is their signed-in
    profile), not whether they are accepted, not what Foodishi's cut will be.
  * ApplicationRead — what the applicant may see of their own application.
  * AdminApplicationRow — the same row plus who is behind it, for the operator
    working the queue. Kept apart because the applicant's email and phone are
    the operator's business and the applicant already knows them, while the
    reviewer's identity is nobody else's.
"""

from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import ApplicationStatus
from app.schemas.catalog_admin import RestaurantDetails

#: A rejection has to be actionable, so the reason is not allowed to be a
#: shrug. Long enough to say what was wrong, bounded because it is a Text
#: column reached by an authenticated route and an unbounded body is an
#: unbounded row.
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 2000

NOTE_MAX_LENGTH = 2000


class ApplicationSubmit(RestaurantDetails):
    """The details of the restaurant, from the person who runs it.

    Every field the `restaurants` table needs, validated exactly as the
    operator-facing create route validates it — same base model, so an applicant
    cannot get a slug or a latitude past this that POST /restaurants would have
    refused, and an approval can never fail on a value this accepted.
    """

    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class ApplicationRead(BaseModel):
    """An application as its own applicant sees it.

    reviewed_by_user_id is absent deliberately: which operator said no is not
    something the person told no gets to know. decision_note IS present — it is
    written for them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: ApplicationStatus

    name: str
    slug: str
    description: str | None
    city: str
    area: str
    address_line: str
    latitude: Decimal
    longitude: Decimal
    phone: str
    price_for_two: Decimal
    avg_prep_minutes: int
    opens_at: time
    closes_at: time
    note: str | None

    decision_note: str | None
    reviewed_at: datetime | None
    #: Set the moment an approval mints the restaurant. This is the applicant's
    #: signal that their partner console now has something in it.
    restaurant_id: int | None
    created_at: datetime
    updated_at: datetime


class AdminApplicationRow(ApplicationRead):
    """The queue's row: the application, and the person behind it.

    Read from the joined users row rather than copied onto the application when
    it was submitted, so an operator writing back uses the address that works
    today.
    """

    applicant_user_id: int
    applicant_name: str
    applicant_email: EmailStr
    applicant_phone: str
    #: Whether the applicant's own account is still usable. An approval on a
    #: deactivated account would mint an owner login the API refuses, so the
    #: queue shows it before the operator clicks.
    is_applicant_active: bool


class ApplicationApprove(BaseModel):
    """Optionally, a line about why — for the record, not for the applicant.

    Approval needs no input at all: everything the restaurant will be was
    settled when the application was submitted, and asking the operator to
    retype any of it is asking them to introduce a typo.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class ApplicationReject(BaseModel):
    """Why not, in words the applicant will read.

    Required, and that is the whole design of this model. A refusal with no
    reason produces an applicant who resubmits the same form and waits again,
    and an operator who answers the same application twice.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=REASON_MIN_LENGTH, max_length=REASON_MAX_LENGTH)
