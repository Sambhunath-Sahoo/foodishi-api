from datetime import time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    Time,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import SpiceLevel

# A real import, not a TYPE_CHECKING one: cover_image's primaryjoin names
# MenuItemImage as a string, and SQLAlchemy resolves that only once the class is
# registered. Deferred, this module could not configure its own mappers, and any
# script importing app.models.catalog without app.models.registry died on
# "name 'MenuItemImage' is not defined". media.py imports nothing from here, so
# there is no cycle to avoid.
from app.models.media import MenuItemImage
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum

# Association table — no model class, because it carries no data of its own.
restaurant_cuisines = Table(
    "restaurant_cuisines",
    Base.metadata,
    Column("restaurant_id", ForeignKey("restaurants.id", ondelete="CASCADE"), primary_key=True),
    # index=True because the composite PK is (restaurant_id, cuisine_id) and
    # cuisine_id is not its leading column, so a cuisine delete or a
    # filter-by-cuisine cannot use it.
    Column(
        "cuisine_id",
        ForeignKey("cuisines.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)


class Cuisine(Base):
    __tablename__ = "cuisines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    slug: Mapped[str] = mapped_column(String(60), unique=True)


class Restaurant(Base, TimestampMixin):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(180), unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    city: Mapped[str] = mapped_column(String(60), index=True)
    area: Mapped[str] = mapped_column(String(80))
    address_line: Mapped[str] = mapped_column(String(240))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    phone: Mapped[str] = mapped_column(String(20))

    rating: Mapped[Decimal] = mapped_column(Numeric(2, 1), default=Decimal("0.0"))
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    price_for_two: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # The input that makes a promised ETA defensible rather than invented.
    avg_prep_minutes: Mapped[int] = mapped_column(Integer)

    # Null is normal: a restaurant that has not uploaded a cover renders a
    # generated placeholder rather than a broken image.
    image_url: Mapped[str | None] = mapped_column(Text)

    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # The platform's share of every delivered order, as a percentage.
    #
    # Deliberately NOT in RestaurantPolicy: that table is the contract with the
    # CUSTOMER — what they are charged, how long they have to change their mind —
    # and GET /restaurants/{id}/policy is public. What Foodishi takes is between
    # Foodishi and the kitchen, and putting it there would publish it.
    commission_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), server_default=text("18.00"), default=Decimal("18.00")
    )


class RestaurantPolicy(Base, TimestampMixin):
    """Cancellation, refund and fee rules. One row per restaurant, PK is the FK.

    Read at order placement and frozen onto the order — never re-read to judge
    an existing order, or a policy edit would retroactively change what a
    customer was promised.
    """

    __tablename__ = "restaurant_policies"

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), primary_key=True
    )
    cancellation_window_mins: Mapped[int] = mapped_column(Integer)
    cancellation_fee_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    refund_sla_hours: Mapped[int] = mapped_column(Integer)

    delivery_fee_base: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    delivery_fee_per_km: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    free_delivery_above: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    packaging_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    min_order_value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    max_delivery_distance_km: Mapped[Decimal] = mapped_column(Numeric(4, 1))


class MenuCategory(Base):
    __tablename__ = "menu_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MenuItem(Base, TimestampMixin):
    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("menu_categories.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_veg: Mapped[bool] = mapped_column(Boolean, default=True)
    spice_level: Mapped[SpiceLevel] = mapped_column(
        pg_enum(SpiceLevel, "spice_level"), default=SpiceLevel.NONE
    )
    serves: Mapped[int] = mapped_column(Integer, default=1)
    calories: Mapped[int | None] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)

    # Position 0 of the gallery, eager-loaded. lazy="selectin" rather than a
    # per-query option because MenuItem is read from three places (the grouped
    # menu, the menu search and the single-item read) and an option that has to
    # be remembered in each is an option that gets forgotten in one — which
    # shows up as a dish with no photo rather than as an error.
    # viewonly: the gallery is written through app/routers/images.py, which owns
    # sort_order and its deferred uniqueness constraint.
    cover_image: Mapped[MenuItemImage | None] = relationship(
        "MenuItemImage",
        primaryjoin=(
            "and_(MenuItem.id == MenuItemImage.menu_item_id,"
            " MenuItemImage.sort_order == 0)"
        ),
        uselist=False,
        viewonly=True,
        lazy="selectin",
    )

    @property
    def image_url(self) -> str | None:
        """Public URL of the cover photo, or None.

        Derived rather than stored, the same way ImageRead.url is: the CDN host
        can change without rewriting a row. The import is local and the failure
        is swallowed on purpose — get_storage() raises when SUPABASE_* is
        unset and is lru_cached, so without this a misconfigured environment
        would turn the customer app's landing path into 500s instead of a page
        with no photos.
        """
        if self.cover_image is None:
            return None
        from app.services.storage.base import StorageError
        from app.services.storage.factory import get_storage

        try:
            return get_storage().public_url(self.cover_image.storage_path)
        except (RuntimeError, StorageError):
            return None
