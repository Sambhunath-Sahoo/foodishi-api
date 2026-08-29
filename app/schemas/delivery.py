from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import DeliveryStatus


class DeliveryPartnerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    vehicle_type: str = Field(min_length=2, max_length=30)
    # A fleet is onboarded before it rides, so a partner may start off-duty.
    is_available: bool = True


class DeliveryPartnerRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    vehicle_type: str
    is_available: bool


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    partner_id: int
    distance_km: Decimal
    eta_minutes: int
    status: DeliveryStatus
    assigned_at: datetime
    picked_up_at: datetime | None
    delivered_at: datetime | None


class DeliveryDetail(DeliveryRead):
    """A delivery plus who is carrying it.

    eta_minutes here is recomputed against the order's promised_at on every
    read — the stored column is the value at assignment time, which goes stale
    the moment it is written.
    """

    partner: DeliveryPartnerRead


class DeliveryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Optional so the PATCH shape stays uniform with the rest of the API; the
    # validators below make an empty body and an explicit null both errors.
    status: DeliveryStatus | None = None

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        # An empty body would otherwise reach the database as a no-op UPDATE
        # and report success without changing anything.
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # None means "field omitted", never "set this column to null" —
        # deliveries.status is NOT NULL.
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self
