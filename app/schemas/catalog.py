from datetime import time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import SpiceLevel


class CuisineRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class RestaurantSummary(BaseModel):
    """The card view: enough to render a discovery list, nothing more."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    city: str
    area: str
    rating: Decimal
    rating_count: int
    price_for_two: Decimal
    avg_prep_minutes: int
    opens_at: time
    closes_at: time
    is_active: bool
    # Null is normal — a kitchen that has not uploaded a cover renders a
    # placeholder rather than a broken image (see the column's own comment).
    image_url: str | None = None


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


class RestaurantProfile(RestaurantSummary):
    """Summary plus the fields only worth shipping on a single-restaurant read.

    Split out from RestaurantDetail so the restaurant row can be validated on
    its own, before the cuisines and policy fetched separately are attached.
    """

    description: str | None
    address_line: str
    latitude: Decimal
    longitude: Decimal
    phone: str


class RestaurantDetail(RestaurantProfile):
    cuisines: list[CuisineRead]
    # Nullable because the policy row is a separate table and seeding can lag
    # behind the restaurant; the detail read stays useful either way.
    policy: RestaurantPolicyRead | None


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
    # Cover photo, read off MenuItem.image_url. Derived at response time from
    # menu_item_images position 0, never stored on the dish.
    image_url: str | None = None


class MenuCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    restaurant_id: int
    name: str
    sort_order: int
    items: list[MenuItemRead]
