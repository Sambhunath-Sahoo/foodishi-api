"""Add-ons and variants: the questions a dish asks before it can be ordered.

Three tables and one association, and the split is what makes them safe:

  * a GROUP is the question ("Portion", "Goes with the biryani") and how it
    behaves — one-of or any-of, with bounds;
  * an OPTION is an answer, with what it adds to the price;
  * the ASSOCIATION says which dishes ask that question, so "Extra raita" is
    written once and offered on four biryanis;
  * ORDER ITEM MODIFIERS are the answers a customer actually gave, with the
    name and price COPIED onto the row.

That last copy is the important one. `order_items` already freezes `item_name`
and `unit_price` for the same reason: a receipt has to keep saying what was
bought at what price, however the menu changes afterwards. A modifier that
pointed only at a live option would let renaming "Half plate" rewrite history.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ModifierKind
from app.models.mixins import TimestampMixin
from app.models.types import pg_enum

if TYPE_CHECKING:  # pragma: no cover - resolved by the mapper at runtime
    from app.models.catalog import MenuItem

#: Which dishes offer which group. A plain association table — there is nothing
#: to say about the pairing itself beyond that it exists.
menu_item_modifier_links = Table(
    "menu_item_modifier_links",
    Base.metadata,
    Column(
        "group_id",
        ForeignKey("menu_item_modifier_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "menu_item_id",
        ForeignKey("menu_items.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_modifier_link_item", "menu_item_id"),
)


class MenuItemModifierGroup(Base, TimestampMixin):
    __tablename__ = "menu_item_modifier_groups"
    __table_args__ = (
        # A group that requires more answers than it permits can never be
        # satisfied, so an order for that dish becomes impossible to place. The
        # constraint is here because the failure is silent and far from the edit.
        CheckConstraint("min_select <= max_select", name="ck_modifier_group_bounds"),
        CheckConstraint("min_select >= 0 and max_select >= 1", name="ck_modifier_group_range"),
        UniqueConstraint("restaurant_id", "name", name="uq_modifier_group_name"),
        Index("ix_modifier_group_restaurant", "restaurant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The group belongs to the RESTAURANT, not to one dish — that is what lets
    #: it be attached to several, and what scopes every permission check on it.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[ModifierKind] = mapped_column(pg_enum(ModifierKind, "modifier_kind"))
    #: A variant group is always exactly 1/1. The API pins those rather than
    #: trusting a caller, but the columns stay general so an "any 2 of 5" group
    #: needs no migration.
    min_select: Mapped[int] = mapped_column(Integer, default=0)
    max_select: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    options: Mapped[list["MenuItemModifierOption"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="MenuItemModifierOption.sort_order",
        lazy="selectin",
    )
    items: Mapped[list["MenuItem"]] = relationship(
        "MenuItem",
        secondary=menu_item_modifier_links,
        lazy="selectin",
        viewonly=True,
    )


class MenuItemModifierOption(Base, TimestampMixin):
    __tablename__ = "menu_item_modifier_options"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_modifier_option_name"),
        CheckConstraint("price_delta >= 0", name="ck_modifier_option_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("menu_item_modifier_groups.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    #: Added to the dish price. "0.00" for a choice that costs nothing, which is
    #: most of them — never negative, so a modifier cannot discount a dish
    #: behind the pricing service's back.
    price_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    #: Sold out at the choice level: "no raita tonight" without taking the whole
    #: group off four dishes.
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    group: Mapped["MenuItemModifierGroup"] = relationship(back_populates="options")


class OrderItemModifier(Base):
    """One answer a customer gave, frozen at the moment they ordered."""

    __tablename__ = "order_item_modifiers"
    __table_args__ = (Index("ix_order_item_modifier_line", "order_item_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE")
    )
    #: SET NULL, not CASCADE: deleting a choice from the menu must not delete
    #: what somebody ate. The frozen name and price below are what the receipt
    #: actually renders.
    option_id: Mapped[int | None] = mapped_column(
        ForeignKey("menu_item_modifier_options.id", ondelete="SET NULL"),
        index=True,
    )
    group_name: Mapped[str] = mapped_column(String(80))
    option_name: Mapped[str] = mapped_column(String(80))
    price_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
