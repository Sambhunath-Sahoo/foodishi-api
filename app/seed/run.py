"""Seed the database with a realistic dataset.

    uv run python -m app.seed.run --reset

Content is deterministic: a fixed RNG seed means the same restaurants, menus,
prices and carts every run, so anything you verify today still holds tomorrow.
Timestamps are not — they are relative to when you run it, because "an order
still inside its cancellation window" is only useful if the window is open now.

Deliberately imports models and seed modules only, never app.main, so seeding
does not depend on the router layer being importable.
"""

import argparse
import asyncio
import random

from sqlalchemy import text

from app.db import Session, engine
from app.models.registry import Base  # noqa: F401 - registers every table
from app.seed import (
    catalog,
    edge_cases,
    images,
    modifiers,
    orders,
    people,
    platform,
    promos,
    reviews,
    settlements,
    staff,
    support,
)
from app.services import platform_settings

RNG_SEED = 42

#: The rows nothing here can rebuild.
#:
#: app/main.py states the constraint: "The seed cannot rebuild the hand-made
#: restaurant_staff rows or the Supabase auth linkage on users.auth_user_id, so
#: losing either is unrecoverable." `--reset` destroyed exactly those, and CASCADE
#: silently took `platform_staff` too — locking every operator out of the console,
#: which is the one row COUNTED_ONLY below believed it was protecting.
#:
#: THESE ARE NOT EXCLUDED FROM THE TRUNCATE, and cannot be: restaurant_staff has
#: foreign keys into BOTH restaurants and users, so excluding it while truncating
#: restaurants makes TRUNCATE fail outright — Postgres refuses on the CONSTRAINT
#: existing, not on rows existing. Excluding it and keeping the catalog would be
#: worse than either: RESTART IDENTITY reuses restaurant ids, so the hand-made
#: staff rows would silently come to point at DIFFERENT restaurants.
#:
#: So the protection is the GUARD, not the list. assert_reset_is_safe() refuses
#: the whole operation while any of these holds a single row, and `--reset`
#: becomes what it always should have been: something you run on an empty
#: database, never on one carrying real identity.
IRREPLACEABLE = ("users", "restaurant_staff", "platform_staff")

# Child-first for readability only — TRUNCATE takes them in one statement and
# empties them atomically, so the order does not affect correctness.
#
# CLOSED UNDER REFERENCES: every table in the public schema that holds a foreign
# key into any table named here is itself named here. That is what lets the
# TRUNCATE drop CASCADE, and it is checked against pg_constraint rather than
# guessed. platform_settings is deliberately absent — it is configuration, not
# data, and nothing references it.
TABLES = [
    "messages",
    "order_item_modifiers", "order_items", "order_status_events",
    "refunds", "payments", "deliveries",
    "coupon_redemptions", "conversations", "reviews",
    "restaurant_settlements", "orders",
    "menu_item_modifier_links", "menu_item_images",
    "menu_item_modifier_options", "menu_item_modifier_groups",
    "menu_items", "menu_categories",
    "restaurant_policies", "restaurant_cuisines", "coupons",
    # restaurant_applications holds foreign keys into BOTH restaurants and
    # users, so leaving it out does not merely skip a table — it makes the
    # whole TRUNCATE fail, because there is no CASCADE to reach it. Listed for
    # the same reason restaurant_staff is, and it is emptied for the same
    # reason: an application is seed data, not a hand-made row.
    "restaurant_applications",
    "restaurant_staff", "platform_staff", "addresses",
    "restaurants", "cuisines", "delivery_partners", "users",
]


async def reset() -> None:
    async with engine.begin() as connection:
        # No CASCADE, and TABLES is closed under references so none is needed.
        #
        # CASCADE truncated every table holding a foreign key into a named one
        # regardless of that key's ON DELETE action, so it reached eight tables
        # beyond the twenty listed — platform_staff, reviews,
        # restaurant_settlements, menu_item_images and the four modifier tables —
        # none of which anybody had enumerated. Without it, a child table missing
        # from TABLES is a loud error naming the constraint instead of a silent
        # deletion, which is the point: this list is an audited inventory now.
        #
        # One statement so it is atomic, and RESTART IDENTITY so ids start at 1
        # and the dataset really is reproducible.
        await connection.execute(
            text(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY")
        )


async def seed() -> dict[str, int]:
    """One transaction per phase.

    expire_on_commit=False on the sessionmaker means objects stay usable after a
    commit, so each phase can land independently instead of risking the whole
    dataset on a single long transaction against a remote database.
    """
    rng = random.Random(RNG_SEED)
    async with Session() as session:
        print("  catalog…", flush=True)
        restaurants = await catalog.build(session, rng)
        await session.commit()

        print("  users and partners…", flush=True)
        users = await people.build_users(session, rng)
        partners = await people.build_partners(session, rng)
        await session.commit()

        print("  coupons…", flush=True)
        cuisine_ids = [
            row[0] for row in await session.execute(text("select id from cuisines"))
        ]
        coupons = await promos.build(
            session, rng, [r.id for r in restaurants], cuisine_ids
        )
        await session.commit()

        print("  orders…", flush=True)
        order_count = await orders.build(session, rng)

        print("  edge cases…", flush=True)
        edges = await edge_cases.build(session, rng)
        await session.commit()

        # Last on purpose. This phase takes no draw from `rng`, so its position
        # cannot shift the seeded emails — but appending rather than inserting is
        # what keeps that true for whoever adds the next one.
        print("  platform staff…", flush=True)
        operators = await platform.build(session)
        await session.commit()

        # ------------------------------------------------------------------
        # Everything below was NOT seeded before, and each of these tables was
        # empty: restaurant_staff (the partner console's whole authorization
        # boundary), the four modifier tables, reviews, restaurant_settlements,
        # conversations and messages, and the platform_settings singleton.
        #
        # All appended AFTER the phases above rather than slotted in among them.
        # The seeded customer emails are derived from the shared `rng` stream and
        # several real Supabase logins are matched to profiles by email, so a
        # phase inserted earlier would shift every later draw and orphan them.
        # Append, never insert.
        # ------------------------------------------------------------------

        print("  platform settings…", flush=True)
        # Self-healing at runtime (services.platform_settings.load creates the
        # row on first read), but seeded explicitly so a fresh dataset has it
        # deterministically rather than on whoever opens the settings screen
        # first.
        await platform_settings.load(session)
        await session.commit()

        print("  restaurant staff…", flush=True)
        kitchen_staff = await staff.build(session, rng)
        await session.commit()

        print("  dish options…", flush=True)
        modifier_counts = await modifiers.build(session, rng)
        await session.commit()

        print("  reviews…", flush=True)
        review_counts = await reviews.build(session, rng)
        await session.commit()

        print("  payout statements…", flush=True)
        settlement_counts = await settlements.build(session, rng)
        await session.commit()

        print("  support threads…", flush=True)
        support_counts = await support.build(session, rng)
        await session.commit()

        print("  images…", flush=True)
        media = await images.build(session)
        await session.commit()

    return {
        "restaurants": len(restaurants),
        "users": len(users),
        "partners": len(partners),
        "coupons": len(coupons),
        "orders": order_count,
        "edge_cases": len(edges),
        "platform_staff": len(operators),
        "restaurant_staff": len(kitchen_staff),
        "modifier_groups": modifier_counts["groups"],
        "modifier_options": modifier_counts["options"],
        "modifier_links": modifier_counts["links"],
        "order_modifiers": modifier_counts["answers"],
        "reviews": review_counts["reviews"],
        "review_replies": review_counts["replies"],
        "settlements": settlement_counts["statements"],
        "conversations": support_counts["conversations"],
        "messages": support_counts["messages"],
        "image_notes": len(media),
        "_notes": edges,
        "_operators": operators,
        "_staff": kitchen_staff,
        "_media": media,
    }


# Counted in the summary but not part of TABLES. menu_item_images used to be
# here and was NOT in fact protected — CASCADE reached it through menu_items — so
# it is in TABLES now, where it is honestly truncated. platform_settings is the
# only genuine non-data table left.
COUNTED_ONLY = ["platform_settings"]


class ResetRefused(RuntimeError):
    """--reset would have destroyed data this seeder cannot rebuild."""


async def assert_reset_is_safe() -> None:
    """Refuse --reset on a database carrying real identity rows.

This is the ONLY thing standing between `--reset` and unrecoverable data, so it
    runs before a single row is touched. See IRREPLACEABLE for why the truncate
    list cannot exclude these tables instead.
    """
    async with engine.connect() as connection:
        populated = {
            table: int(
                await connection.scalar(text(f"select count(*) from {table}"))
            )
            for table in IRREPLACEABLE
        }
    occupied = {table: n for table, n in populated.items() if n}
    if occupied:
        rows = ", ".join(f"{table}={n}" for table, n in sorted(occupied.items()))
        raise ResetRefused(
            f"Refusing --reset: {rows}.\n"
            "\n"
            "--reset TRUNCATES these tables, and nothing in this repository can\n"
            "rebuild them: users.auth_user_id carries the Supabase auth linkage,\n"
            "and restaurant_staff and platform_staff were made by hand. Losing any\n"
            "of them locks every partner and every operator out of their console\n"
            "permanently.\n"
            "\n"
            "Run without --reset to seed on top of what is there. To start from\n"
            "nothing, point DATABASE_URL at an empty database."
        )


async def counts() -> list[tuple[str, int]]:
    async with engine.connect() as connection:
        return [
            (table, int(await connection.scalar(text(f"select count(*) from {table}"))))
            for table in [*reversed(TABLES), *COUNTED_ONLY]
        ]


async def main(do_reset: bool) -> None:
    if do_reset:
        # Checked before anything is written, so a refusal costs nothing.
        await assert_reset_is_safe()
        print("truncating…")
        await reset()

    print("seeding…")
    summary = await seed()
    for note in summary.pop("_notes"):
        print(f"  edge case · {note}")
    for note in summary.pop("_operators"):
        print(f"  platform · {note}")
    for note in summary.pop("_media"):
        print(f"  image · {note}")

    print("\nrow counts")
    for table, n in await counts():
        if n:
            print(f"  {table:<22} {n:>6}")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="truncate before seeding")
    asyncio.run(main(parser.parse_args().reset))
