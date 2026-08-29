"""What one person may do in one restaurant, decided in one place.

A role is the shorthand; a permission is the thing actually checked. That split
exists because two real questions are not role questions: "may this shift worker
turn an order away before the kitchen accepts it" and "may they cancel one the
kitchen already took on". One restaurant trusts its evening staff with both, the
next wants every refusal to go through a manager, and both are the same role.

So: a role grants a fixed floor, and an admin may hand a staff member extra
permissions from a short, deliberate list. **Nothing outside `GRANTABLE` can be
granted at all** — `validate_grants` is the only writer, so a row edited by hand
or by a future migration still cannot widen anybody past what an admin could
have given them, and no caller can name `payments.view` and be believed.

This module is the authority. The partner console carries a mirror of it in
apps/partner/lib/permissions.ts for drawing the UI, and that mirror is COSMETIC:
hiding a button saves a tap that would be refused anyway. If the two ever
disagree, this one is right.
"""

from app.models.enums import StaffRole

#: Everything the platform knows how to gate on. Kept as a flat tuple of dotted
#: strings rather than an enum: these are stored in a varchar[] column, read on
#: every scope check, and shared verbatim with a TypeScript mirror — an enum
#: would add a conversion at each of those boundaries and buy nothing.
ALL_PERMISSIONS: tuple[str, ...] = (
    "dashboard.view",
    "orders.view",
    "orders.accept",
    "orders.reject",
    "orders.status",
    "orders.cancel",
    "orders.history",
    "handover.view",
    "handover.mark",
    "menu.view",
    "menu.availability",
    "menu.edit",
    "menu.delete",
    "menu.categories",
    "menu.modifiers",
    "restaurant.view",
    "restaurant.edit",
    "staff.view",
    "staff.manage",
    "offers.view",
    "offers.manage",
    "reports.view",
    "payments.view",
)

#: What a shift worker can do the moment they are added, with nothing granted.
#:
#: Day-to-day service and nothing else: see the queue, take a ticket on, move it
#: through the kitchen, hand it over, and sell a dish out when it runs out.
#: Selling out is in here deliberately — a dish that ran out at eight in the
#: evening is queue work, and routing it through a manager means it stays on the
#: menu until somebody answers their phone.
STAFF_FLOOR: frozenset[str] = frozenset(
    {
        "dashboard.view",
        "orders.view",
        "orders.accept",
        "orders.status",
        "orders.history",
        "handover.view",
        "handover.mark",
        "menu.view",
        "menu.availability",
    }
)

#: The ONLY permissions an admin may hand to a staff member.
#:
#: Both turn a customer away, which is why they are a decision rather than a
#: default — and why the list is this short. Everything absent from it is a
#: business setting: the menu itself, the restaurant, the roster, promotions and
#: anything financial are structurally out of a staff member's reach, not merely
#: unticked. Adding to this list is a policy change, not a config tweak.
GRANTABLE: frozenset[str] = frozenset({"orders.reject", "orders.cancel"})

_ALL = frozenset(ALL_PERMISSIONS)


class InvalidGrant(ValueError):
    """A caller asked for a permission that cannot be granted to anybody."""


def resolve(role: StaffRole, granted: list[str] | None) -> frozenset[str]:
    """Everything this membership may actually do.

    An admin gets the lot and their grants are ignored — there is nothing left to
    grant. A staff member gets their floor plus whatever was granted from
    `GRANTABLE`, and anything else in `granted` is DROPPED rather than trusted:
    the column is data, and data can be wrong.
    """
    if role is StaffRole.ADMIN:
        return _ALL
    extra = frozenset(granted or []) & GRANTABLE
    return STAFF_FLOOR | extra


def validate_grants(role: StaffRole, requested: list[str]) -> list[str]:
    """The list to store, or a refusal naming exactly what was wrong.

    Deduplicated and sorted so the stored value is canonical — two rows granting
    the same pair should not differ by ordering, and a sorted array makes a diff
    in a log readable.

    Refuses rather than silently filtering. A manager who ticked something they
    cannot grant needs to be told, not to have the save appear to work and the
    permission quietly not be there.
    """
    if role is StaffRole.ADMIN:
        raise InvalidGrant(
            "An admin already holds every permission — there is nothing to grant. "
            "Change their role to staff first if their access should be narrower."
        )

    wanted = set(requested)
    unknown = sorted(wanted - _ALL)
    if unknown:
        raise InvalidGrant(f"Not permissions this platform knows about: {unknown}")

    # Told apart from `unknown` on purpose: "that is not a permission" and "that
    # one is a manager's" are different mistakes and need different answers.
    ungrantable = sorted(wanted - GRANTABLE)
    if ungrantable:
        raise InvalidGrant(
            f"These cannot be granted to a staff member: {ungrantable}. "
            f"Only {sorted(GRANTABLE)} can be; everything else is a manager's."
        )
    return sorted(wanted)
