"""Restaurant staff — the rows that make the PARTNER console reachable.

Nothing seeded this table before. `restaurant_staff` is the authorization
boundary for the whole partner app (`require_staff`, `staff_of_order`,
`admin_of_restaurant` all read it), so with it empty there was no way into that
console except rows somebody had made by hand — which is exactly why
app/main.py calls those hand-made rows unrecoverable.

Now it is seeded, and the roster is FIXED rather than random, for the same reason
app/seed/platform.py's is: a login that only sometimes has a grant is a login
that only sometimes gets in, and "can a manager see the roster screen" stops
being answerable.

WHAT THIS CREATES OUTSIDE POSTGRES: a Supabase auth account per address, through
app/seed/auth_accounts.py. Read that module's docstring before changing anything
here — it fails soft, so a profile can exist without an account and simply not be
signable-into yet.
"""

import logging
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Restaurant
from app.models.enums import StaffRole
from app.models.staff import RestaurantStaff
from app.models.user import User
from app.seed.auth_accounts import STAFF_DOMAIN, resolve_auth_ids

logger = logging.getLogger(__name__)

#: At most three people per restaurant, and deliberately NOT three everywhere.
#:
#: (slug-ish key, [(local part, display name, role, extra permissions)])
#:
#: The shape of this roster is the point, so each row below is a case some screen
#: has to handle:
#:
#:   * a restaurant with an admin and two staff  -> the full roster screen
#:   * a restaurant with an admin and one staff  -> the common small kitchen
#:   * a restaurant with ONE admin and no staff  -> the "no team yet" empty state,
#:     and the last-admin guard in app/routers/staff.py, which cannot be
#:     exercised at all unless some restaurant has exactly one admin
#:   * one staff member with granted permissions and one WITHOUT
#:
#: The permission lists matter more than they look. app/services/permissions.py
#: withholds `orders.reject` and `orders.cancel` from the staff floor and only an
#: admin may grant them, so a roster where every staff member has them (or none
#: does) leaves the newly-added permission gate in
#: app/dependencies/scope.py untested against real data.
ROSTER: tuple[tuple[str, tuple[tuple[str, str, StaffRole, list[str]], ...]], ...] = (
    (
        "Tandoori Nights",
        (
            ("tandoori.admin", "Rohan Verma", StaffRole.ADMIN, []),
            ("tandoori.staff1", "Meera Iyer", StaffRole.STAFF, ["orders.reject"]),
            ("tandoori.staff2", "Imran Shaikh", StaffRole.STAFF, []),
        ),
    ),
    (
        "Dakshin Diaries",
        (
            ("dakshin.admin", "Lata Rao", StaffRole.ADMIN, []),
            (
                "dakshin.staff1",
                "Suresh Kumar",
                StaffRole.STAFF,
                ["orders.reject", "orders.cancel"],
            ),
        ),
    ),
    (
        "Wok This Way",
        (
            ("wok.admin", "Kevin Dsouza", StaffRole.ADMIN, []),
            ("wok.staff1", "Anita Bose", StaffRole.STAFF, []),
        ),
    ),
    (
        # ONE admin, no staff. Keeps the empty-team state and the last-admin
        # guard reachable.
        "Chai Point Cafe",
        (("chai.admin", "Farah Khan", StaffRole.ADMIN, []),),
    ),
    (
        "Cheese Republic",
        (
            ("cheese.admin", "Nikhil Menon", StaffRole.ADMIN, []),
            ("cheese.staff1", "Priya Nair", StaffRole.STAFF, ["orders.cancel"]),
            # Deactivated on purpose: `is_active=False` is what "access revoked
            # but the record kept" looks like, and require_staff filters on it.
            # Without one, nothing proves the filter is applied.
            ("cheese.former", "Deepak Shetty", StaffRole.STAFF, []),
        ),
    ),
)

#: Which addresses get is_active=False. Kept beside ROSTER rather than as a
#: fourth tuple field, because it applies to the membership, not the person.
INACTIVE = frozenset({"cheese.former"})

STAFF_CITY = "Bengaluru"


def _email(local_part: str) -> str:
    return f"{local_part}@{STAFF_DOMAIN}"


async def build(session: AsyncSession, rng: random.Random) -> list[str]:
    """Create or repair the restaurant staff roster. Safe to run repeatedly.

    Takes NO draw from `rng` beyond phone numbers, and takes those last, for the
    reason app/seed/platform.py gives: the seeded customer emails come from the
    same stream, and several real Supabase logins are matched to profiles by
    email, so a draw inserted earlier would shift them all and orphan the lot.
    The parameter is accepted so this phase reads like its siblings in run.py.
    """
    emails = tuple(
        _email(local) for _, members in ROSTER for local, _, _, _ in members
    )
    auth_ids = await resolve_auth_ids(emails)

    # One lookup for every restaurant named in the roster, by name, because the
    # ids are generated and the roster cannot know them.
    wanted = [name for name, _ in ROSTER]
    rows = await session.scalars(
        select(Restaurant).where(Restaurant.name.in_(wanted))
    )
    by_name = {r.name: r for r in rows}

    missing = [name for name in wanted if name not in by_name]
    if missing:
        # Loud, not silent: a roster entry for a restaurant that is not in the
        # catalog means data.py and this file have drifted apart, and the
        # symptom would otherwise be a console nobody can sign into.
        logger.warning(
            "No restaurant row for %s — staff for it were skipped. ROSTER and "
            "data.RESTAURANTS have drifted apart.",
            ", ".join(missing),
        )

    notes: list[str] = []
    for restaurant_name, members in ROSTER:
        restaurant = by_name.get(restaurant_name)
        if restaurant is None:
            continue

        for local_part, display_name, role, granted in members:
            email = _email(local_part)

            user = await session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    name=display_name,
                    email=email,
                    phone=f"9{rng.randint(100000000, 999999999)}",
                    city=STAFF_CITY,
                    is_active=True,
                )
                session.add(user)
                await session.flush()

            # Linked on EVERY run, not only on creation: an earlier run may have
            # written the profile while Supabase was unreachable.
            auth_id = auth_ids.get(email)
            if auth_id is not None and str(user.auth_user_id) != auth_id:
                user.auth_user_id = auth_id

            is_active = local_part not in INACTIVE
            membership = await session.scalar(
                select(RestaurantStaff).where(
                    RestaurantStaff.user_id == user.id,
                    RestaurantStaff.restaurant_id == restaurant.id,
                )
            )
            if membership is None:
                session.add(
                    RestaurantStaff(
                        user_id=user.id,
                        restaurant_id=restaurant.id,
                        role=role,
                        is_active=is_active,
                        permissions=list(granted),
                    )
                )
            else:
                # Repair rather than skip, matching platform.py: a membership
                # left revoked or downgraded by a half-finished run would lock
                # the console, and this phase exists to guarantee a way in.
                membership.role = role
                membership.is_active = is_active
                membership.permissions = list(granted)

            linked = "auth linked" if user.auth_user_id is not None else "NO AUTH"
            state = "active" if is_active else "revoked"
            notes.append(
                f"{email} -> {restaurant_name} / {role.value} ({state}, {linked})"
            )

    await session.flush()
    return notes
