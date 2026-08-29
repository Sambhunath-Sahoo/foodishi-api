import random
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import Address
from app.models.delivery import DeliveryPartner
from app.models.user import User
from app.seed import data

#: 100 customers. The de-duplication below can drop a couple when two draws
#: collide on the same name-and-index, so the table lands at or just under this.
USER_COUNT = 100

#: Six riders for five restaurants. Enough that the dispatch board has choices
#: and that "no partner available" is reachable by claiming them all, which it
#: needs to be now that app/routers/delivery.py actually marks a rider busy.
PARTNER_COUNT = 6


async def build_users(session: AsyncSession, rng: random.Random) -> list[User]:
    """Seeded customers, REUSING any row that already exists for the email.

    Idempotent on purpose: it is what makes `python -m app.seed.run` WITHOUT
    `--reset` safe to run on a database that already has people in it. A plain
    INSERT would die on the unique index on `users.email` the moment a generated
    address collided with an existing row, and some of those rows carry a
    hand-made Supabase linkage on `auth_user_id` that nothing here can recreate.

    Existing rows are left otherwise untouched — no name, phone, city or
    is_active is overwritten, and no extra addresses are added. A hand-corrected
    row stays hand-corrected.

    WHAT THIS DOES NOT PROMISE. The generated emails are stable only for a given
    catalog: `catalog.build` runs before this phase and draws from the SAME shared
    rng, so changing the number of restaurants or the size of a menu shifts every
    address generated here. Cutting the catalog from twenty-five restaurants to
    five did exactly that, which means the `@example.com` rows already in a live
    database will NOT be re-matched by a later run — they are preserved as
    existing customers and a fresh set is generated alongside them.

    So: reproducible on a FRESH database, which is the guarantee that is worth
    something. Not reproducible as a top-up, and nothing should depend on a
    specific generated address. The two demo logins the customer app names in
    `DEV_HINT_ACCOUNTS` are preserved ROWS, not regenerated ones, which is why
    they keep working across this change.
    """
    users, seen_emails = [], set()
    for index in range(USER_COUNT):
        first = rng.choice(data.FIRST_NAMES)
        last = rng.choice(data.LAST_NAMES)
        email = f"{first}.{last}{index}".lower() + "@example.com"
        if email in seen_emails:
            continue
        seen_emails.add(email)

        # area/lat/lon are drawn for the customer's own city and used by the
        # address loop below via a second lookup; only `city` is needed here.
        city = rng.choice(data.LOCATIONS)[0]

        # Drawn before the lookup so these two, at least, cost the same number of
        # draws whether or not the row exists. The address loop below still only
        # runs on the create path, so a skipped row does shift what follows -- see
        # the docstring. Keeping these two unconditional is not a full fix; it is
        # the part that is free.
        phone = f"9{rng.randint(100000000, 999999999)}"
        is_active = rng.random() > 0.04

        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            # Addresses are not topped up for an existing customer: they already
            # have some, and adding more on every run would grow the table without
            # bound.
            users.append(existing)
            continue

        user = User(
            name=f"{first} {last}",
            email=email,
            phone=phone,
            city=city,
            is_active=is_active,
        )
        session.add(user)
        await session.flush()

        for slot in range(rng.choice([1, 1, 2, 2, 3])):
            # Addresses stay in the user's own city and near a real neighbourhood,
            # so distances to restaurants land inside plausible delivery radii.
            _, a_area, a_lat, a_lon = rng.choice(
                [loc for loc in data.LOCATIONS if loc[0] == city]
            )
            session.add(
                Address(
                    user_id=user.id,
                    label=data.ADDRESS_LABELS[slot % len(data.ADDRESS_LABELS)],
                    line1=f"{rng.randint(1, 400)}, {a_area}",
                    line2=f"Near {a_area} Metro" if rng.random() < 0.4 else None,
                    city=city,
                    pincode=f"5{rng.randint(60000, 69999)}",
                    latitude=Decimal(str(round(a_lat + rng.uniform(-0.02, 0.02), 6))),
                    longitude=Decimal(str(round(a_lon + rng.uniform(-0.02, 0.02), 6))),
                    is_default=(slot == 0),
                )
            )
        users.append(user)

    await session.flush()
    return users


async def build_partners(session: AsyncSession, rng: random.Random) -> list[DeliveryPartner]:
    """Delivery riders, topped up to PARTNER_COUNT rather than duplicated.

    Keyed on how many already exist rather than on a name: two riders can share a
    name, so there is no natural key to match on, and a second run would otherwise
    double the fleet every time.
    """
    already = list(await session.scalars(select(DeliveryPartner)))
    partners = list(already)
    for _ in range(max(0, PARTNER_COUNT - len(already))):
        partner = DeliveryPartner(
            name=f"{rng.choice(data.FIRST_NAMES)} {rng.choice(data.LAST_NAMES)}",
            phone=f"9{rng.randint(100000000, 999999999)}",
            vehicle_type=rng.choice(data.VEHICLES),
            is_available=rng.random() > 0.3,
        )
        session.add(partner)
        partners.append(partner)
    await session.flush()
    return partners
