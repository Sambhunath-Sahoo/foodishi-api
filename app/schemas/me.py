from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.models.enums import PlatformRole
from app.schemas.user import UserRead, UserUpdate

# users.avatar_url is Text, so the column sets no ceiling. This one is ours: an
# avatar is a URL somebody hands us, and an unbounded string in a JSON body is
# an unbounded row.
AVATAR_URL_MAX_LENGTH = 500

# Validated through an adapter with the field left a plain str, for the reason
# app/routers/me.py validates the token's email claim the same way: the value
# has to survive model_dump() as something SQLAlchemy can write to a Text
# column, so the adapter is only ever asked "is this a URL", never to replace
# it. HttpUrl also refuses schemes that are not http(s), which is what keeps a
# javascript: or data: string out of the <img src> every frontend puts it in.
_AVATAR_URL_ADAPTER = TypeAdapter(HttpUrl)


class ProfileLink(BaseModel):
    """What a newly signed-in account tells us about itself.

    There is deliberately no email field. The address comes from the verified
    token and nowhere else — accepting one from the body would let a caller
    claim someone else's seeded profile just by typing their address.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    city: str = Field(min_length=2, max_length=60)


class MeUpdate(UserUpdate):
    """PATCH /me's body: what PATCH /users/{id} accepts, plus the avatar.

    Subclassed rather than retyped so the name, email, phone and city rules
    cannot drift from the ones the staff-facing route enforces — there is one
    definition of "a phone number is 7 to 20 characters", and it is the parent's.

    The avatar lives here and not in UserUpdate because this is the only route
    whose subject is the caller themselves: a customer maintaining their own
    photo needs no privilege, while letting platform staff rewrite any
    customer's avatar through PATCH /users/{id} is a moderation feature nobody
    has asked for.

    platform_role stays absent, exactly as it is absent from UserUpdate — see
    the comment on MeProfile below for why a writable role here would be
    self-service privilege escalation. is_active and created_at stay absent for
    the reason UserUpdate gives: they are the server's.
    """

    # The one field on this model a caller may legitimately clear: the column is
    # nullable, and "remove my photo" has no other spelling.
    avatar_url: str | None = Field(default=None, max_length=AVATAR_URL_MAX_LENGTH)

    @field_validator("avatar_url")
    @classmethod
    def require_absolute_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            _AVATAR_URL_ADAPTER.validate_python(value)
        except ValidationError as exc:
            # Reported as one plain sentence rather than pydantic's URL error
            # tree, because this reaches a person editing their profile.
            raise ValueError(
                "avatar_url must be an absolute http(s) URL"
            ) from exc
        return value

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # Overrides UserUpdate's check rather than adding to it. Every column
        # that one guards is still NOT NULL, so an explicit null there is still
        # a write the database would refuse — but avatar_url is nullable, and
        # null is how a caller says "clear it".
        nulls = sorted(
            field
            for field in self.model_fields_set
            if field != "avatar_url" and getattr(self, field) is None
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class MeProfile(UserRead):
    """The caller's profile plus the one thing only they may be told about it.

    Separate from UserRead because this answer is nobody else's: GET /users/{id}
    and the staff list have no business publishing who works for Foodishi.
    """

    # Read-only, and only ever set from the platform_staff row the server just
    # read. It is absent from UserUpdate and from every other write model on
    # purpose: PATCH /me dumps its payload straight into an UPDATE, so a
    # writable role field here would be self-service privilege escalation —
    # any customer could promote themselves to admin.
    #
    # Null is the normal answer. Customers and restaurant staff are not platform
    # staff, and the consoles read this to decide whether to render at all, so
    # "no" has to arrive as a 200 rather than as a 403 the client reads as
    # "signed out".
    platform_role: PlatformRole | None = None
