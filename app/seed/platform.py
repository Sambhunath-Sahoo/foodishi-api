"""Foodishi's own staff — the rows that make the operations console reachable.

Named fixtures, not random ones, for the same reason edge_cases.py exists: the
console gate reads platform_staff, so an account that only sometimes has a grant
is an account that only sometimes gets in.

This phase is the answer to a bootstrap deadlock. platform_staff is what
require_platform_role checks, and the only route that could create a grant would
itself need a grant — so the first rows can only come from here.

Idempotent on purpose, and additive only. It SELECTs before every INSERT and it
truncates nothing: the live database holds hand-made restaurant_staff rows and
hand-linked auth profiles that no code in this repo can recreate. It also takes
no draw from the shared rng — the seeded customer emails are derived from that
stream, and several real Supabase logins are matched to profiles by email, so a
single extra draw would orphan them.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PlatformRole
from app.models.platform import PlatformStaff
from app.models.user import User
from app.seed.auth_accounts import STAFF_DOMAIN, resolve_auth_ids

logger = logging.getLogger(__name__)

# One admin, which is the whole platform side today (see PlatformRole).
#
# The domain comes from app/seed/auth_accounts.py and is never example.com: the
# seeded customers occupy that, and a collision there would hand a platform grant
# to a customer.
OPERATORS: tuple[tuple[str, str, PlatformRole], ...] = (
    (f"ops.admin@{STAFF_DOMAIN}", "Asha Menon", PlatformRole.ADMIN),
)

# Every operator is reachable on the same desk line. Not a real number: 5550000
# is inside the fictional range, so nobody's phone rings during a demo.
OPERATOR_PHONE = "9000055500"
OPERATOR_CITY = "Bengaluru"


async def build(session: AsyncSession) -> list[str]:
    """Create or repair the operator accounts. Safe to run repeatedly."""
    auth_ids = await resolve_auth_ids(tuple(email for email, _, _ in OPERATORS))
    notes: list[str] = []

    for email, name, role in OPERATORS:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                name=name,
                email=email,
                phone=OPERATOR_PHONE,
                city=OPERATOR_CITY,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            state = "created"
        else:
            state = "exists"

        # Link on every run, not only on creation: an earlier run may have landed
        # the profile while Supabase was unreachable.
        auth_id = auth_ids.get(email)
        if auth_id is not None and str(user.auth_user_id) != auth_id:
            user.auth_user_id = auth_id
        linked = "auth linked" if user.auth_user_id is not None else "NO AUTH — cannot sign in"

        staff = await session.scalar(
            select(PlatformStaff).where(PlatformStaff.user_id == user.id)
        )
        if staff is None:
            session.add(PlatformStaff(user_id=user.id, role=role, is_active=True))
        else:
            # Repair rather than skip. A revoked or downgraded grant would leave
            # the console locked, and this phase exists to guarantee a way in.
            staff.role = role
            staff.is_active = True
            state = f"{state}, grant repaired"

        notes.append(f"{email} -> {role.value} ({state}, {linked})")

    await session.flush()
    return notes
