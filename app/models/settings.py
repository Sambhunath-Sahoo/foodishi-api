"""The platform's own numbers, in one row.

Until now every figure below was a Python constant or a per-restaurant policy
column: `services/pricing.TAX_RATE`, `services/settlements.COMMISSION_GST_RATE`,
`restaurant_policies.delivery_fee_base`. That is fine for a number nobody
changes and wrong for a number an operator is supposed to own — a tax rate that
needs a deploy to move is not configuration, it is a hard-coded value with a
comment above it.

Two rules about precedence, because this table does NOT make the per-restaurant
policy rows redundant:

  * `restaurant_policies` still wins wherever it has an opinion. A kitchen that
    sets its own delivery fee, packaging fee or cancellation window keeps it.
    These are the platform's defaults and the floor for a kitchen that has said
    nothing.
  * Commission is the exception and deliberately so. `restaurants.commission_percent`
    is the rate a kitchen is actually on and it is never derived from here;
    `commission_default_percent` is only what a NEW restaurant is created with.
    A negotiated rate is a commercial fact about one kitchen, so it lives on that
    kitchen — see `services/settlements.commission_percent_for`.

Single row, enforced rather than assumed: `ck_platform_settings_singleton` pins
the primary key to 1. A second row of platform settings is not a state this
system should be able to reach, and "the newest one wins" is the kind of rule
that is discovered during an incident.
"""

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin

#: The only primary key this table may hold.
SINGLETON_ID = 1


class PlatformSettings(Base, TimestampMixin):
    __tablename__ = "platform_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_platform_settings_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SINGLETON_ID)

    # --- delivery charges ------------------------------------------------
    # Money is Numeric, never float: these are the figures a customer is
    # charged and a kitchen is paid against, and a binary float cannot hold
    # 0.10. Same reasoning as every other money column in this schema.
    delivery_base_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), server_default="25.00"
    )
    delivery_per_km_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), server_default="5.00"
    )
    #: Order value at which delivery costs the customer nothing.
    delivery_free_above: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), server_default="599.00"
    )
    #: What the delivery fee is multiplied by when the platform is behind.
    delivery_surge_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), server_default="1.40"
    )
    #: How far behind its promises the platform must be before that applies.
    delivery_surge_after_minutes: Mapped[int] = mapped_column(
        Integer, server_default="20"
    )
    delivery_max_distance_km: Mapped[Decimal] = mapped_column(
        Numeric(5, 1), server_default="14.0"
    )
    delivery_packaging_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), server_default="20.00"
    )

    # --- commission ------------------------------------------------------
    #: What a NEW restaurant is created on. Never read for an existing one.
    commission_default_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), server_default="18.00"
    )
    #: How long after delivery a kitchen is paid what it is owed.
    commission_settlement_days: Mapped[int] = mapped_column(
        Integer, server_default="7"
    )

    # --- tax -------------------------------------------------------------
    #: GST on the food. The default matches services/pricing.TAX_RATE (5%), so
    #: introducing this table changes no price on day one.
    tax_gst_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), server_default="5.00"
    )
    tax_packaging_taxable: Mapped[bool] = mapped_column(
        Boolean, server_default="true"
    )
    tax_delivery_taxable: Mapped[bool] = mapped_column(
        Boolean, server_default="false"
    )
    #: Printed on every customer receipt and on each kitchen's statement.
    tax_gstin: Mapped[str] = mapped_column(String(20), server_default="")

    # --- order rules -----------------------------------------------------
    rule_min_order_value: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), server_default="79.00"
    )
    rule_max_items_per_order: Mapped[int] = mapped_column(
        Integer, server_default="30"
    )
    #: Cancel inside this and the customer pays nothing. The window is frozen
    #: onto each order at checkout, so changing it never moves an existing one.
    rule_free_cancellation_minutes: Mapped[int] = mapped_column(
        Integer, server_default="5"
    )
    rule_late_cancellation_fee_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), server_default="15.00"
    )
    #: What the customer is promised their money back within. Every refund on
    #: the SLA watch is measured against the value frozen onto it, not this.
    rule_refund_sla_hours: Mapped[int] = mapped_column(Integer, server_default="24")
    #: An order a kitchen has not accepted by then is cancelled on its behalf.
    rule_auto_cancel_unconfirmed_minutes: Mapped[int] = mapped_column(
        Integer, server_default="12"
    )
    #: Added to every promise on top of prep and the ride, so a kitchen that is
    #: on time is not recorded as a minute late.
    rule_prep_buffer_minutes: Mapped[int] = mapped_column(Integer, server_default="5")
