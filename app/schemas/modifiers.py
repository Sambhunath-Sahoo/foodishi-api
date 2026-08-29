"""Add-ons and variants: the questions a dish asks before it can be ordered."""

from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ModifierKind

NAME_MAX = 80
#: A group with more than this many options is a menu, not a question.
MAX_OPTIONS_PER_GROUP = 24


class ModifierOptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    group_id: int
    name: str
    #: Added to the dish price. "0.00" for most choices, and never negative — a
    #: modifier may not discount a dish behind the pricing service's back.
    price_delta: Decimal
    # Included even when false, rather than filtered out of the list. A customer
    # should see that the raita is off tonight; silently dropping it makes them
    # wonder whether they misremembered the menu.
    is_available: bool
    sort_order: int


class ModifierGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    restaurant_id: int
    name: str
    kind: ModifierKind
    min_select: int
    max_select: int
    sort_order: int
    options: list[ModifierOptionRead]
    #: Which dishes ask this question. Populated on the restaurant-facing list
    #: and left empty on the per-dish read, where the dish is already known.
    menu_item_ids: list[int] = Field(default_factory=list)


class ModifierGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=NAME_MAX)
    kind: ModifierKind
    # Sent, but only honoured for an addon group: the router pins a variant to
    # 1/1 whatever arrives, because "pick one" with any other bounds is not a
    # variant. Kept on the schema rather than rejected so a client can send one
    # shape for both kinds.
    min_select: int = Field(default=0, ge=0, le=MAX_OPTIONS_PER_GROUP)
    max_select: int = Field(default=1, ge=1, le=MAX_OPTIONS_PER_GROUP)
    #: Replacing this list replaces the attachments wholesale — see the note on
    #: ModifierGroupUpdate.
    menu_item_ids: list[int] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def bounds_are_satisfiable(self):
        # A group requiring more answers than it permits can never be satisfied,
        # which makes every dish it is attached to impossible to order. The
        # database has the same constraint; catching it here turns a 500 into a
        # sentence naming the two numbers.
        if self.kind is ModifierKind.ADDON and self.min_select > self.max_select:
            raise ValueError(
                f"min_select ({self.min_select}) cannot exceed max_select "
                f"({self.max_select}) — no choice would satisfy this group"
            )
        return self


class ModifierGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=NAME_MAX)
    kind: ModifierKind | None = None
    min_select: int | None = Field(default=None, ge=0, le=MAX_OPTIONS_PER_GROUP)
    max_select: int | None = Field(default=None, ge=1, le=MAX_OPTIONS_PER_GROUP)
    sort_order: int | None = Field(default=None, ge=0)
    # WHOLESALE REPLACEMENT, not a merge. An id absent from this list is a real
    # detachment — that is the only way a client can ever take a group off a
    # dish, and a PATCH that merged would make detaching impossible to express.
    # Omit the field entirely to leave attachments alone; send [] to detach all.
    menu_item_ids: list[int] | None = Field(default=None, max_length=200)

    #: menu_item_ids is the one field where null is distinct from absent -- see
    #: the WHOLESALE REPLACEMENT note above -- so it is exempt from the guard.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"menu_item_ids"})

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # Eight other PATCH schemas in app/schemas/ carry this guard and these
        # two did not. Without it an explicit null passes validation and fails
        # downstream: {"min_select": null} reached `None > max_select` and raised
        # TypeError as a bare 500, and {"name": null} reached the NOT NULL column
        # and came back quoting the raw Postgres error including
        # "DETAIL: Failing row contains (...)" -- the whole row, to the client.
        nulls = sorted(
            name
            for name in self.model_fields_set
            if getattr(self, name) is None and name not in self.NULLABLE_FIELDS
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class ModifierOptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=NAME_MAX)
    # ge=0: the check is here as well as in the database because an
    # IntegrityError escaping as a 500 tells a manager nothing, where "a choice
    # cannot cost less than nothing" tells them what they typed wrong.
    price_delta: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=10, decimal_places=2)
    is_available: bool = True
    sort_order: int | None = Field(default=None, ge=0)


class ModifierOptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    price_delta: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    #: The mid-service control: "no raita tonight" without taking the group off
    #: four dishes.
    is_available: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    #: Every field here maps to a NOT NULL column, so no null is meaningful.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # See ModifierGroupUpdate.reject_explicit_nulls.
        nulls = sorted(
            name
            for name in self.model_fields_set
            if getattr(self, name) is None and name not in self.NULLABLE_FIELDS
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self
