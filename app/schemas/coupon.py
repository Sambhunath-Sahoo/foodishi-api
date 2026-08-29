from datetime import datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.ids import DbId
from app.models.enums import CouponScope, DiscountType

CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
PERCENT_MAX = Decimal("100")

# Columns that are NOT NULL, so an explicit null in a PATCH body is a client
# mistake rather than a request to clear the column.
NON_NULLABLE_FIELDS = frozenset(
    {
        "code", "description", "discount_type", "discount_value", "min_order_value",
        "scope", "valid_from", "valid_until", "usage_limit_per_user", "is_active",
    }
)


def _check_valid_range(valid_from: datetime | None, valid_until: datetime | None) -> None:
    # Mirrors ck_coupons_valid_range: caught here it is a 422 naming the field,
    # caught by the database it is an opaque IntegrityError.
    if valid_from is not None and valid_until is not None and valid_until <= valid_from:
        raise ValueError("valid_until must be after valid_from")


def _check_percent_cap(
    discount_type: DiscountType | None,
    discount_value: Decimal | None,
    max_discount_amount: Decimal | None,
) -> None:
    if discount_type != DiscountType.PERCENT:
        return
    # Mirrors ck_coupons_percent_needs_cap — an uncapped percentage is an
    # unbounded liability.
    if max_discount_amount is None:
        raise ValueError("max_discount_amount is required for a percent coupon")
    if discount_value is not None and discount_value > PERCENT_MAX:
        raise ValueError("discount_value cannot exceed 100 for a percent coupon")


def _check_scope_target(
    scope: CouponScope | None, restaurant_id: int | None, cuisine_id: int | None
) -> None:
    # The service matches scope against exactly one target; a coupon carrying
    # the wrong one (or neither) would silently never apply.
    if scope == CouponScope.RESTAURANT and restaurant_id is None:
        raise ValueError("restaurant_id is required for a restaurant-scoped coupon")
    if scope == CouponScope.CUISINE and cuisine_id is None:
        raise ValueError("cuisine_id is required for a cuisine-scoped coupon")
    if scope == CouponScope.GLOBAL and (restaurant_id is not None or cuisine_id is not None):
        raise ValueError("A global coupon cannot target a restaurant or a cuisine")


class CouponCreate(BaseModel):
    # extra="forbid" stops clients setting server-owned columns — times_used,
    # is_active and the timestamps are not theirs to write.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=3, max_length=40, pattern=CODE_PATTERN)
    description: str = Field(min_length=3, max_length=500)

    discount_type: DiscountType
    discount_value: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    max_discount_amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    min_order_value: Decimal = Field(ge=0, max_digits=10, decimal_places=2)

    scope: CouponScope
    restaurant_id: int | None = Field(default=None, gt=0)
    cuisine_id: int | None = Field(default=None, gt=0)

    # Aware only: the columns are timestamptz and the service compares them
    # against an aware now(), which a naive value turns into a TypeError.
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    usage_limit_total: int | None = Field(default=None, ge=1)
    usage_limit_per_user: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def check_consistency(self):
        _check_valid_range(self.valid_from, self.valid_until)
        _check_percent_cap(self.discount_type, self.discount_value, self.max_discount_amount)
        _check_scope_target(self.scope, self.restaurant_id, self.cuisine_id)
        return self


class CouponUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str | None = Field(default=None, min_length=3, max_length=40, pattern=CODE_PATTERN)
    description: str | None = Field(default=None, min_length=3, max_length=500)

    discount_type: DiscountType | None = None
    discount_value: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    max_discount_amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    min_order_value: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)

    scope: CouponScope | None = None
    restaurant_id: int | None = Field(default=None, gt=0)
    cuisine_id: int | None = Field(default=None, gt=0)

    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    usage_limit_total: int | None = Field(default=None, ge=1)
    usage_limit_per_user: int | None = Field(default=None, ge=1)
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
        # None means "field omitted" for every NOT NULL column. The nullable
        # ones are left out: clearing a cap or a scope target is legitimate.
        nulls = sorted(
            f for f in self.model_fields_set
            if f in NON_NULLABLE_FIELDS and getattr(self, f) is None
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self

    @model_validator(mode="after")
    def check_consistency(self):
        # A partial body cannot be checked against the stored row, so each rule
        # fires only when this request carries the fields it needs. Changing
        # discount_type or scope therefore has to restate its companions —
        # otherwise the database CHECK is the first thing to notice.
        set_fields = self.model_fields_set
        if {"valid_from", "valid_until"} <= set_fields:
            _check_valid_range(self.valid_from, self.valid_until)
        if "discount_type" in set_fields:
            _check_percent_cap(self.discount_type, self.discount_value, self.max_discount_amount)
        if "scope" in set_fields:
            _check_scope_target(self.scope, self.restaurant_id, self.cuisine_id)
        return self


class CouponRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    description: str

    discount_type: DiscountType
    discount_value: Decimal
    max_discount_amount: Decimal | None
    min_order_value: Decimal

    scope: CouponScope
    restaurant_id: int | None
    cuisine_id: int | None

    valid_from: datetime
    valid_until: datetime
    usage_limit_total: int | None
    usage_limit_per_user: int
    times_used: int
    is_active: bool
    created_at: datetime


class CouponValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=3, max_length=40)
    restaurant_id: DbId
    user_id: DbId
    subtotal: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class CouponValidation(BaseModel):
    # from_attributes so the service's CouponOutcome dataclass serialises
    # directly, with no hand-copied dict in the router to drift from it.
    model_config = ConfigDict(from_attributes=True)

    applicable: bool
    discount: Decimal
    reason: str | None = None
