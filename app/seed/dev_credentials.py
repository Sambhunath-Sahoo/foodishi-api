"""Emit the seeded logins as a TypeScript module the consoles can import.

    uv run python -m app.seed.dev_credentials

Writes `foodishi-web/packages/api-client/src/dev-credentials.ts`, which the
`/creds` page in each app renders.

WHY GENERATED, AND WHY FROM THE DATABASE. The three consoles cannot import a
Python constant, so the alternative was hand-copying two dozen addresses into
TypeScript — which is the drift this project has already been bitten by twice.
Reading the live database instead means the list cannot claim a login that does
not exist, and grants come out right without anybody maintaining a second copy of
the roster.

WHAT IT DELIBERATELY DOES NOT DO. It never reads or emits a password from
Supabase (it could not — Supabase stores a hash). It emits the seeder's own
`DEV_PASSWORD`, and it is only true because every seeded account is created with
it and `app/seed/migrate_to_foodishi.py` resets it on rename. If somebody changes
an account's password by hand, this file will be wrong about that account and
there is no way for it to know.

DEVELOPMENT ONLY. The output is a list of working credentials for a development
Supabase project. The page that renders it is gated on NODE_ENV, and this script
refuses to run against anything that does not look like a development database.
"""

import asyncio
import json
import pathlib

from sqlalchemy import select

from app.db import Session, engine
from app.models.catalog import Restaurant
from app.models.platform import PlatformStaff
from app.models.staff import RestaurantStaff
from app.models.user import User
from app.seed.auth_accounts import DEV_PASSWORD, STAFF_DOMAIN

#: Written relative to this repo, into the package all three apps already import.
OUT = (
    pathlib.Path(__file__).resolve().parents[3]
    / "foodishi-web"
    / "packages"
    / "api-client"
    / "src"
    / "dev-credentials.ts"
)

HEADER = '''/**
 * Seeded logins for development. GENERATED — do not edit by hand.
 *
 *     cd foodishi-api && uv run python -m app.seed.dev_credentials
 *
 * Every account below is created by the seeder against a development Supabase
 * project and shares one password, which is committed in this repository on
 * purpose: a development door nobody can find is a wasted morning, not a
 * security control.
 *
 * The `/creds` page in each console renders this. That page is gated on
 * NODE_ENV, so a production build ships nothing.
 *
 * Regenerate after reseeding. The list is read from the live database rather
 * than from the seeder's constants, so it cannot claim a login that does not
 * exist — but it cannot know about a password somebody changed by hand.
 */

export interface DevCredential {
  readonly email: string;
  readonly name: string;
  /** Which console this login is for. */
  readonly audience: "operator" | "restaurant" | "customer";
  /** Platform role, restaurant + role, or what makes this customer interesting. */
  readonly grant: string;
  /** False for a membership that has been revoked but kept on record. */
  readonly isActive: boolean;
}

/** Shared by every account below. */
export const DEV_PASSWORD = __PASSWORD__;

export const DEV_CREDENTIALS: readonly DevCredential[] = __ROWS__;
'''


async def collect() -> list[dict]:
    async with Session() as session:
        # Auth-linked only. An account with no Supabase identity cannot be signed
        # into, so listing it would be listing a credential that does not work.
        users = list(
            await session.scalars(
                select(User)
                .where(User.auth_user_id.is_not(None))
                .order_by(User.id)
            )
        )
        platform = {
            row.user_id: row
            for row in await session.scalars(select(PlatformStaff))
        }
        memberships: dict[int, list[tuple[RestaurantStaff, str]]] = {}
        rows = await session.execute(
            select(RestaurantStaff, Restaurant.name).join(
                Restaurant, Restaurant.id == RestaurantStaff.restaurant_id
            )
        )
        for staff, restaurant_name in rows.all():
            memberships.setdefault(staff.user_id, []).append((staff, restaurant_name))

    out: list[dict] = []
    for user in users:
        grant_rows = memberships.get(user.id, [])
        if user.id in platform:
            out.append(
                {
                    "email": user.email,
                    "name": user.name,
                    "audience": "operator",
                    "grant": f"platform {platform[user.id].role.value}",
                    "isActive": bool(platform[user.id].is_active),
                }
            )
        elif grant_rows:
            for staff, restaurant_name in grant_rows:
                extra = (
                    f" +{','.join(staff.permissions)}" if staff.permissions else ""
                )
                out.append(
                    {
                        "email": user.email,
                        "name": user.name,
                        "audience": "restaurant",
                        "grant": f"{restaurant_name} · {staff.role.value}{extra}",
                        "isActive": bool(staff.is_active),
                    }
                )
        else:
            # A signable-in account with no grant is a CUSTOMER, and the only
            # ones here are the hand-linked demo shoppers the customer app names
            # on its own sign-in screen.
            out.append(
                {
                    "email": user.email,
                    "name": user.name,
                    "audience": "customer",
                    "grant": "customer" + ("" if user.is_active else " · deactivated"),
                    "isActive": bool(user.is_active),
                }
            )
    # Operator first, then kitchens, then customers: the order somebody scanning
    # the page actually wants.
    order = {"operator": 0, "restaurant": 1, "customer": 2}
    out.sort(key=lambda r: (order[r["audience"]], r["grant"], r["email"]))
    return out


async def main() -> None:
    try:
        rows = await collect()
        if not rows:
            print("No auth-linked accounts found — nothing to write.")
            return
        if not any(r["email"].endswith(STAFF_DOMAIN) for r in rows):
            print(
                f"Refusing to write: no address on {STAFF_DOMAIN}, so this does "
                "not look like a seeded development database."
            )
            return

        # Explicit replacement rather than str.format: the template contains a
        # TypeScript interface, and every brace in it would have to be doubled.
        OUT.write_text(
            HEADER.replace("__PASSWORD__", json.dumps(DEV_PASSWORD)).replace(
                "__ROWS__", json.dumps(rows, indent=2)
            )
        )
        by_audience: dict[str, int] = {}
        for r in rows:
            by_audience[r["audience"]] = by_audience.get(r["audience"], 0) + 1
        print(f"{len(rows)} credential(s) -> {OUT}")
        for audience, count in sorted(by_audience.items()):
            print(f"  {audience:12} {count}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
