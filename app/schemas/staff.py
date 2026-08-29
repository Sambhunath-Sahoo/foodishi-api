from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.enums import StaffRole


class StaffCreate(BaseModel):
    # extra="forbid" rejects unknown fields, and stops a caller setting
    # server-owned columns like id, restaurant_id or created_at. The restaurant
    # comes from the path, never from the body: accepting one here would let an
    # admin of restaurant 1 grant access to restaurant 2.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Exactly one of these two names the person. user_id is what the operations
    # console already holds; email is the only handle a restaurant admin has,
    # because GET /users is platform-only and GET /users/{id} admits the caller
    # themselves — so without it this route could not be reached from the
    # partner app at all, whatever the roster screen looked like.
    #
    # An address and nothing else, on purpose: an exact match tells the admin
    # only what they already typed, where a partial-match lookup would hand
    # every restaurant a searchable copy of the customer directory.
    user_id: int | None = Field(default=None, ge=1)
    email: EmailStr | None = Field(default=None, max_length=200)

    # No default. Granting a permission level is the whole point of this call,
    # so it is stated rather than inherited from whatever the default happens
    # to be on the day.
    role: StaffRole

    @model_validator(mode="after")
    def exactly_one_subject(self):
        # Both would need a rule for which wins, and one of them would be
        # ignored silently; neither identifies anybody.
        if (self.user_id is None) == (self.email is None):
            raise ValueError("Provide exactly one of user_id or email")
        return self


class StaffUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional so the PATCH shape stays uniform with the rest of the API; the
    # validators below make an empty body and an explicit null both errors.
    role: StaffRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        # An empty body would otherwise reach the database as a no-op UPDATE
        # and report success without changing anything.
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # None means "field omitted", never "set this column to null" — both
        # restaurant_staff.role and .is_active are NOT NULL.
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class StaffPermissionsUpdate(BaseModel):
    """The whole grant list, replaced.

    Not a patch and not a pair of add/remove lists: an unticked box has to be a
    real removal, and a merge would make it impossible to take a permission away
    at all. Send [] to strip somebody back to their role's floor.

    Which values are accepted is app/services/permissions.py's decision, not
    this schema's — a hardcoded Literal here would be a second list to keep in
    step, and it would drift.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    granted: list[str] = Field(max_length=32)


class StaffAccessReset(BaseModel):
    """What a reset actually did.

    Deliberately thin. No token, no link, no password — nothing here could be
    used to sign in as the person, because an API that returned one would hand
    every restaurant admin a way to take over a staff member's account.
    """

    model_config = ConfigDict(from_attributes=True)

    staff_id: int
    #: Where the fresh sign-in link was sent. The one thing a manager standing
    #: at a tablet can actually act on ("check your email, it went to that one").
    email: EmailStr
    reset_at: datetime
    #: True only when an auth provider was configured and actually asked to
    #: invalidate the sessions. False means the row was stamped and nothing else
    #: happened — see the route, and do not tell the user otherwise.
    sessions_revoked: bool


class StaffRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    restaurant_id: int
    role: StaffRole
    is_active: bool
    #: What an admin granted on top of the role. Empty for an admin, whose role
    #: already carries everything — see services/permissions.resolve.
    permissions: list[str]
    #: Everything this membership may actually do, role floor and grants
    #: combined. Sent so a client never has to recompute the resolution rule and
    #: get it subtly wrong; `permissions` above is what is editable.
    effective_permissions: list[str]
    access_reset_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StaffUserRead(BaseModel):
    """Just enough of the person to render a staff row.

    Deliberately not UserRead: a restaurant's manager has no business seeing
    another restaurant's customer's city or account status.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    phone: str
    # Enough to draw the person rather than their initials. Null is normal and
    # the clients already render a placeholder for it.
    avatar_url: str | None = None


class StaffMemberRead(StaffRead):
    """A staff row with the person attached, for the staff-management list.

    Ids alone cannot be rendered, and letting the client fetch each user
    separately would be an N+1 over an endpoint that already knows the answer.
    """

    user: StaffUserRead


class MyRestaurantRead(BaseModel):
    """One entry in the partner app's restaurant picker.

    Flattened on purpose — the picker needs the restaurant's identity and the
    caller's own role over it, and nothing about the other staff.
    """

    id: int
    name: str
    slug: str
    city: str
    image_url: str | None
    # The restaurant's own flag, not the membership's: a deactivated restaurant
    # still belongs in the picker so its owner can see why it is dark.
    is_active: bool
    role: StaffRole
