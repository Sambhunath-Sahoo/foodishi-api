"""Move an EXISTING database onto the Foodishi rebrand and the smaller dataset.

    uv run python -m app.seed.migrate_to_foodishi              # plan only
    uv run python -m app.seed.migrate_to_foodishi --apply      # do it

The alternative to this script is `--reset` on an empty database, which loses
every hand-made Supabase linkage. This exists so that is not the only option.

WHAT IT PRESERVES, WITHOUT EXCEPTION
------------------------------------
  * Every `users` row with a non-null `auth_user_id`. These are the only rows in
    the schema nothing here can recreate: the linkage was made by hand against a
    real Supabase account, and `app/main.py` calls losing it unrecoverable.
  * Every `platform_staff` grant, so the operations console stays reachable.
  * Every Supabase AUTH ACCOUNT. This script never calls a delete on the admin
    API. It renames one address; it removes nothing.

WHAT IT DELETES
---------------
Transactional and catalog data, all of which the seeder rebuilds:

  * every order and everything hanging off one (items, events, payments,
    refunds, deliveries, coupon redemptions, reviews, support threads)
  * every restaurant and its menu, policy, cuisines, coupons, modifier
    catalogue and payout statements
  * every `restaurant_staff` GRANT — see the note below, this is the one
    judgement call in the script
  * regenerable customers: `users` rows with no `auth_user_id` and no grant

THE ONE JUDGEMENT CALL: restaurant_staff
----------------------------------------
There are hand-made grants on this table, and five of the people holding them
are auth-linked. Their grants are still deleted, and their ACCOUNTS and `users`
rows are not — so they remain customers who can sign in, and they stop being
restaurant staff.

The reason is the new roster. `app/seed/staff.py` defines at most three people
per restaurant, and two of the surviving kitchens already have three hand-made
staff between them. Keeping both sets would put six people on one kitchen and
break the cap the roster exists to express. Keeping the hand-made ones instead
would mean the roster never applies and the permission cases it was written to
cover (a lone admin, a revoked membership, a staff member with granted
permissions) stay untested.

So the grants are rebuilt from the roster, on documented logins with a shared
dev password, and partner-console access ends up better than it was rather than
worse. If that is the wrong call for your data, run with `--keep-staff-grants`
and reconcile the roster by hand afterwards.

ORDER MATTERS. Deletes run children-first because the schema uses RESTRICT in
the places that matter: `orders.user_id`, `orders.restaurant_id` and
`order_items.menu_item_id` all refuse to let a parent go while a child points at
it. That is a feature, and the ordering below respects it rather than reaching
for CASCADE.
"""

import argparse
import asyncio
import logging

import httpx
from sqlalchemy import text

from app.db import engine
from app.models.registry import Base  # noqa: F401 - registers every table
from app.seed import data
from app.seed.auth_accounts import DEV_PASSWORD, STAFF_DOMAIN, admin_api

logger = logging.getLogger(__name__)

#: The domain being migrated away from. Historical, and deliberately not
#: rebranded: this script exists to find rows that still carry it.
LEGACY_STAFF_DOMAIN = "tadka.internal"

#: Deleted children-first. Every table here is rebuilt by `app/seed/run.py`.
#:
#: `users`, `platform_staff` and `addresses` are absent on purpose. The first two
#: are identity; addresses belong to preserved customers and cost nothing to
#: keep, and the seeder only adds addresses for customers it creates.
TRANSACTIONAL_TABLES = (
    "messages",
    "conversations",
    "order_item_modifiers",
    "order_items",
    "order_status_events",
    "refunds",
    "payments",
    "deliveries",
    "coupon_redemptions",
    "reviews",
    "restaurant_settlements",
    "orders",
)

#: Catalog, also children-first. Emptied entirely rather than trimmed to the new
#: five: a partial trim would leave menu items whose prices came from the old
#: draw order mixed with new ones, and "which of these is current" is not a
#: question anybody should have to answer about seed data.
CATALOG_TABLES = (
    "menu_item_modifier_links",
    "menu_item_modifier_options",
    "menu_item_modifier_groups",
    "menu_item_images",
    "menu_items",
    "menu_categories",
    "restaurant_policies",
    "restaurant_cuisines",
    "coupons",
    "restaurants",
    "cuisines",
    "delivery_partners",
)


async def _scalar(connection, sql: str) -> int:
    return int(await connection.scalar(text(sql)) or 0)


async def survey() -> dict[str, int]:
    """Read-only. What is here, and what the plan would touch."""
    async with engine.connect() as connection:
        counts = {
            "users_total": await _scalar(connection, "select count(*) from users"),
            "users_auth_linked": await _scalar(
                connection, "select count(*) from users where auth_user_id is not null"
            ),
            "users_regenerable": await _scalar(
                connection,
                """
                select count(*) from users u
                 where u.auth_user_id is null
                   and not exists (select 1 from platform_staff p where p.user_id = u.id)
                   and not exists (select 1 from restaurant_staff s where s.user_id = u.id)
                """,
            ),
            "platform_staff": await _scalar(
                connection, "select count(*) from platform_staff"
            ),
            "restaurant_staff": await _scalar(
                connection, "select count(*) from restaurant_staff"
            ),
            "restaurants": await _scalar(connection, "select count(*) from restaurants"),
            "menu_items": await _scalar(connection, "select count(*) from menu_items"),
            "orders": await _scalar(connection, "select count(*) from orders"),
            "legacy_domain_users": await _scalar(
                connection,
                f"select count(*) from users where email like '%@{LEGACY_STAFF_DOMAIN}'",
            ),
        }
    return counts


async def rename_legacy_domain(*, apply: bool) -> list[str]:
    """Move `*@tadka.internal` addresses onto the new domain, Supabase included.

    Both sides or neither. Renaming only `users.email` would leave the profile
    pointing at an auth account with the old address, and `POST /auth/link`
    matches on the verified email claim — so the operator would keep their
    `auth_user_id` and still be unable to sign in. Supabase is renamed FIRST for
    that reason: if it fails, the database is untouched and the state is
    unchanged rather than half-migrated.
    """
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "select id, email, auth_user_id from users "
                    f"where email like '%@{LEGACY_STAFF_DOMAIN}' order by id"
                )
            )
        ).all()

    if not rows:
        return ["no addresses on the legacy domain"]

    api = admin_api()
    notes: list[str] = []

    for user_id, email, auth_user_id in rows:
        new_email = email.replace(f"@{LEGACY_STAFF_DOMAIN}", f"@{STAFF_DOMAIN}")

        if not apply:
            notes.append(f"would rename users.id={user_id}: {email} -> {new_email}")
            continue

        if auth_user_id is not None:
            if api is None:
                notes.append(
                    f"SKIPPED users.id={user_id}: {email} is auth-linked but "
                    "SUPABASE_URL/SUPABASE_SECRET_KEY are unset. Renaming the "
                    "profile alone would break its sign-in."
                )
                continue
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    await api.rename(client, str(auth_user_id), new_email)
                    # And the password, in the same breath. A rename leaves the
                    # old credential in place -- Supabase stores a hash -- so
                    # without this the account answers to the pre-rebrand
                    # password while every comment in the repo names the new one.
                    await api.set_password(client, str(auth_user_id), DEV_PASSWORD)
            except (httpx.HTTPError, ValueError) as exc:
                notes.append(
                    f"SKIPPED users.id={user_id}: Supabase rename failed ({exc}). "
                    "Database left unchanged for this row."
                )
                continue

        async with engine.begin() as connection:
            await connection.execute(
                text("update users set email = :new where id = :id"),
                {"new": new_email, "id": user_id},
            )
        notes.append(f"renamed users.id={user_id}: {email} -> {new_email}")

    return notes


async def clear_rebuildable(*, apply: bool, keep_staff_grants: bool) -> list[str]:
    """Delete everything the seeder can rebuild. Never touches identity."""
    tables = list(TRANSACTIONAL_TABLES)
    if not keep_staff_grants:
        # After the transactional rows and before the catalog: the grant points
        # at a restaurant, and the restaurant cannot go while it does.
        tables.append("restaurant_staff")
    tables.extend(CATALOG_TABLES)

    notes: list[str] = []
    async with engine.connect() as connection:
        for table in tables:
            n = await _scalar(connection, f"select count(*) from {table}")
            if n:
                notes.append(f"{'delete' if apply else 'would delete'} {n:>6} from {table}")

    if apply:
        # ONE transaction. A half-cleared database is not a state the seeder can
        # start from, and DELETE respects the RESTRICT constraints that would
        # otherwise let a partial order survive its parent.
        async with engine.begin() as connection:
            for table in tables:
                await connection.execute(text(f"delete from {table}"))

    return notes


async def trim_regenerable_customers(
    *, apply: bool, staff_grants_survive: bool
) -> list[str]:
    """Delete customers that carry nothing irreplaceable.

    A row survives if it has an `auth_user_id`, a platform grant, or a restaurant
    grant. Anything else is a generated customer the seeder will make again — and
    by this point its orders are already gone.

    Addresses follow automatically: `addresses.user_id` is CASCADE, and the clear
    step has already removed the orders that would otherwise hold them (
    `orders.address_id` is RESTRICT).

    `staff_grants_survive` exists so the DRY RUN does not lie. The restaurant
    grants are normally deleted by the step before this one, which frees the
    people holding them — but in a dry run nothing has actually been deleted, so
    counting with the grant clause still in place reported 73 where the real
    figure was 143. The predicate used for the count matches what WILL be true
    when this step runs, not what is true while planning it.
    """
    grant_clause = (
        "and not exists (select 1 from restaurant_staff s where s.user_id = u.id)"
        if staff_grants_survive
        else ""
    )
    predicate = f"""
         where u.auth_user_id is null
           and not exists (select 1 from platform_staff p where p.user_id = u.id)
           {grant_clause}
    """
    async with engine.connect() as connection:
        n = await _scalar(connection, f"select count(*) from users u {predicate}")
    if apply and n:
        async with engine.begin() as connection:
            await connection.execute(text(f"delete from users u {predicate}"))

    qualifier = "" if staff_grants_survive else " (once the grants above are gone)"
    return [
        f"{'delete' if apply else 'would delete'} {n} regenerable customer(s)"
        f"{'' if apply else qualifier}"
    ]


async def main(*, apply: bool, keep_staff_grants: bool) -> None:
    before = await survey()

    print()
    print("── current database " + "─" * 41)
    for key, value in before.items():
        print(f"  {key:22} {value:>6}")

    print()
    print("── target " + "─" * 51)
    print(f"  restaurants            {len(data.RESTAURANTS):>6}")
    print("  menu items each        <=  10")
    print("  customers                 100  (plus preserved auth-linked rows)")
    print("  orders                    150")
    print("  restaurant_staff           11  (<= 3 per restaurant, from staff.ROSTER)")

    print()
    print("── PRESERVED, always " + "─" * 40)
    print(f"  auth-linked users      {before['users_auth_linked']:>6}  never deleted")
    print(f"  platform grants        {before['platform_staff']:>6}  never deleted")
    print("  Supabase accounts         all  never deleted, one renamed")

    print()
    print("── plan " + "─" * 53)
    for note in await rename_legacy_domain(apply=apply):
        print(f"  1. {note}")
    for note in await clear_rebuildable(
        apply=apply, keep_staff_grants=keep_staff_grants
    ):
        print(f"  2. {note}")
    for note in await trim_regenerable_customers(
        apply=apply, staff_grants_survive=keep_staff_grants
    ):
        print(f"  3. {note}")

    print()
    if apply:
        after = await survey()
        print("── after " + "─" * 52)
        for key, value in after.items():
            print(f"  {key:22} {value:>6}")
        print()
        print("DONE. Now rebuild the dataset — WITHOUT --reset:")
        print()
        print("    uv run python -m app.seed.run")
        print()
        print("The seeder is idempotent for users and staff, so it will reuse the")
        print("rows preserved above rather than colliding with them.")
    else:
        print("PLAN ONLY — nothing was changed. Re-run with --apply to execute.")
    print()

    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Migrate an existing database onto the Foodishi dataset."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the migration. Without this, only the plan is printed.",
    )
    parser.add_argument(
        "--keep-staff-grants",
        action="store_true",
        help=(
            "Keep the existing restaurant_staff rows instead of rebuilding them "
            "from staff.ROSTER. You will need to reconcile the roster by hand: a "
            "kitchen may end up with more than the three members the roster allows."
        ),
    )
    args = parser.parse_args()
    asyncio.run(
        main(apply=args.apply, keep_staff_grants=args.keep_staff_grants)
    )
