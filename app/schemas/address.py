from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Mirrors Numeric(9, 6) on the model: six decimals is ~11 cm, plenty for a
# doorstep, and the bounds stop a swapped lat/lng pair reaching the ETA maths.
COORD_DECIMALS = 6
MAX_LATITUDE = Decimal("90")
MAX_LONGITUDE = Decimal("180")

PINCODE_PATTERN = r"^\d{4,10}$"

# line2 is the only nullable column, so it is the only field a PATCH may null.
NULLABLE_FIELDS = frozenset({"line2"})


class AddressCreate(BaseModel):
    # extra="forbid" keeps server-owned columns out of reach — notably
    # is_default, which only PUT /addresses/{id}/default may change because
    # flipping it has to clear the previous default in the same transaction.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=40)
    line1: str = Field(min_length=3, max_length=240)
    line2: str | None = Field(default=None, max_length=240)
    city: str = Field(min_length=2, max_length=60)
    pincode: str = Field(pattern=PINCODE_PATTERN)
    latitude: Decimal = Field(
        ge=-MAX_LATITUDE, le=MAX_LATITUDE, decimal_places=COORD_DECIMALS
    )
    longitude: Decimal = Field(
        ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, decimal_places=COORD_DECIMALS
    )


class AddressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, min_length=1, max_length=40)
    line1: str | None = Field(default=None, min_length=3, max_length=240)
    line2: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, min_length=2, max_length=60)
    pincode: str | None = Field(default=None, pattern=PINCODE_PATTERN)
    latitude: Decimal | None = Field(
        default=None, ge=-MAX_LATITUDE, le=MAX_LATITUDE, decimal_places=COORD_DECIMALS
    )
    longitude: Decimal | None = Field(
        default=None, ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, decimal_places=COORD_DECIMALS
    )

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        # An empty body would otherwise reach the database as a no-op UPDATE
        # and report success without changing anything.
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # For every field but line2, None means "omitted", never "set null" —
        # those columns are NOT NULL, so an explicit null would fail in the
        # database instead of here.
        nulls = sorted(
            f
            for f in self.model_fields_set
            if getattr(self, f) is None and f not in NULLABLE_FIELDS
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class AddressRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    label: str
    line1: str
    line2: str | None
    city: str
    pincode: str
    latitude: Decimal
    longitude: Decimal
    is_default: bool
    created_at: datetime
