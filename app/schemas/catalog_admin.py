from datetime import datetime, time
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import SpiceLevel

# A slug is part of a public URL, so it is restricted here rather than left to
# whatever the admin typed: lowercase words joined by single hyphens.
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

# A cover photo is served back to browsers as an img src, so only the two
# schemes that fetch an image are accepted here.
IMAGE_URL_PATTERN = r"^https?://\S+$"


class _WriteBase(BaseModel):
    # extra="forbid" rejects unknown fields, and stops clients setting
    # server-owned columns like id, rating or created_at.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _PatchBase(_WriteBase):
    """Shared PATCH semantics: no empty body, no null on a NOT NULL column."""

    # Columns that genuinely accept NULL. For every other field None means
    # "omitted", never "set this column to null".
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        # An empty body would otherwise reach the database as a no-op UPDATE
        # and report success without changing anything.
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # Without this check an explicit null passes validation and fails in
        # the database instead, as a 500 rather than a 422.
        nulls = sorted(
            name
            for name in self.model_fields_set
            if getattr(self, name) is None and name not in self.NULLABLE_FIELDS
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class RestaurantCreate(_WriteBase):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=2, max_length=180, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=2000)

    city: str = Field(min_length=2, max_length=60)
    area: str = Field(min_length=2, max_length=80)
    address_line: str = Field(min_length=4, max_length=240)
    latitude: Decimal = Field(ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal = Field(ge=-180, le=180, max_digits=9, decimal_places=6)
    phone: str = Field(min_length=7, max_length=20)

    # rating and rating_count are absent on purpose: they are derived from
    # customer feedback, never declared by whoever onboards the restaurant.
    price_for_two: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    avg_prep_minutes: int = Field(ge=1, le=240)

    opens_at: time
    closes_at: time

    is_active: bool = True


class RestaurantUpdate(_PatchBase):
    # image_url is nullable so a restaurant can drop a cover it no longer wants
    # and fall back to the generated placeholder, rather than being stuck with
    # a dead link because null was the one value it could not send.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"description", "image_url"})

    name: str | None = Field(default=None, min_length=2, max_length=160)
    slug: str | None = Field(default=None, min_length=2, max_length=180, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=2000)

    city: str | None = Field(default=None, min_length=2, max_length=60)
    area: str | None = Field(default=None, min_length=2, max_length=80)
    address_line: str | None = Field(default=None, min_length=4, max_length=240)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, max_digits=9, decimal_places=6)
    phone: str | None = Field(default=None, min_length=7, max_length=20)

    price_for_two: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    avg_prep_minutes: int | None = Field(default=None, ge=1, le=240)

    # The cover photo is a plain URL, matching the column and how it is read
    # everywhere else. The scheme is pinned because three apps render this
    # straight into an img src: a javascript: or data: value stored here would
    # be script the restaurant chose and the platform served.
    image_url: str | None = Field(default=None, max_length=1000, pattern=IMAGE_URL_PATTERN)

    # Advertised trading hours. Editing these does not open or close the
    # kitchen — see PUT /restaurants/{restaurant_id}/availability, which is the
    # switch order placement actually reads.
    opens_at: time | None = None
    closes_at: time | None = None
    is_active: bool | None = None


class RestaurantRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None
    city: str
    area: str
    address_line: str
    latitude: Decimal
    longitude: Decimal
    phone: str
    rating: Decimal
    rating_count: int
    price_for_two: Decimal
    avg_prep_minutes: int
    image_url: str | None
    opens_at: time
    closes_at: time
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RestaurantAvailabilityUpdate(_WriteBase):
    """Whether the restaurant is taking orders right now.

    A PUT body rather than a PATCH one: the state is a single boolean, so
    "closed" has to be said out loud. An omitted field here would mean nothing
    at all.
    """

    is_active: bool


class RestaurantAvailabilityRead(BaseModel):
    """The switch, plus the hours it is not.

    opens_at and closes_at ride along because the partner app shows them beside
    the control and would otherwise need a second request for them — and
    because seeing both together is what stops "we close at 23:00" being
    mistaken for "we stop taking orders at 23:00". Nothing enforces the hours;
    is_active is the only field on this response that decides anything.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_active: bool
    opens_at: time
    closes_at: time


class RestaurantPolicyUpsert(_WriteBase):
    """Full replacement of a restaurant's policy row.

    Every field is required even though the row may already exist: PUT means
    "the policy is now exactly this", so an omitted fee cannot silently keep an
    old value that the caller believes they replaced.
    """

    cancellation_window_mins: int = Field(ge=0, le=1440)
    cancellation_fee_percent: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    refund_sla_hours: int = Field(ge=0, le=720)

    delivery_fee_base: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    delivery_fee_per_km: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    free_delivery_above: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    packaging_fee: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    min_order_value: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    max_delivery_distance_km: Decimal = Field(gt=0, max_digits=4, decimal_places=1)


class RestaurantPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    restaurant_id: int
    cancellation_window_mins: int
    cancellation_fee_percent: Decimal
    refund_sla_hours: int
    delivery_fee_base: Decimal
    delivery_fee_per_km: Decimal
    free_delivery_above: Decimal | None
    packaging_fee: Decimal
    min_order_value: Decimal
    max_delivery_distance_km: Decimal
    updated_at: datetime


class MenuCategoryCreate(_WriteBase):
    # restaurant_id comes from the path, so it is deliberately not a body field.
    name: str = Field(min_length=2, max_length=80)
    sort_order: int = Field(default=0, ge=0, le=9999)


class MenuCategoryUpdate(_PatchBase):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class MenuCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    restaurant_id: int
    name: str
    sort_order: int


class MenuItemCreate(_WriteBase):
    restaurant_id: int = Field(gt=0)
    category_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    is_veg: bool = True
    spice_level: SpiceLevel = SpiceLevel.NONE
    serves: int = Field(default=1, ge=1, le=50)
    calories: int | None = Field(default=None, ge=0, le=10000)
    is_available: bool = True


class MenuItemUpdate(_PatchBase):
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"description", "calories"})

    # restaurant_id is not updatable: moving an item to another restaurant
    # would leave its category — and every past order line — pointing elsewhere.
    category_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    is_veg: bool | None = None
    spice_level: SpiceLevel | None = None
    serves: int | None = Field(default=None, ge=1, le=50)
    calories: int | None = Field(default=None, ge=0, le=10000)
    is_available: bool | None = None


class MenuItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    restaurant_id: int
    category_id: int
    name: str
    description: str | None
    price: Decimal
    is_veg: bool
    spice_level: SpiceLevel
    serves: int
    calories: int | None
    is_available: bool
    created_at: datetime
    updated_at: datetime
