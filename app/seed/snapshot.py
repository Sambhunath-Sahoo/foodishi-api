"""Dump every public table to JSON, so a destructive migration has an undo.

    uv run python -m app.seed.snapshot                     # write a snapshot
    uv run python -m app.seed.snapshot --out /path/dir      # somewhere specific

There is no pg_dump on this machine and Supabase's MCP surface has no backup
call, so this is the available undo. It is a data snapshot, not a schema one:
it captures rows, not DDL, which is what matters here because
`migrate_to_foodishi.py` only ever deletes rows.

WHAT IT IS GOOD FOR. Restoring the one thing nothing can recreate — the
`users.auth_user_id` linkage, the `platform_staff` grant and the hand-made
`restaurant_staff` rows. Every other table it captures is regenerable by the
seeder, and is included only because a complete snapshot is easier to reason
about than a partial one.

WHAT IT IS NOT. It is not a point-in-time backup: it reads table by table in one
transaction, so it is consistent, but it does not preserve sequence positions and
restoring it wholesale would collide with rows written since. Treat it as
evidence to restore FROM by hand, not as a one-command rollback.

It contains real seeded email addresses and phone numbers. It is written to a
local path and should not leave this machine.
"""

import argparse
import asyncio
import datetime
import json
import pathlib
from decimal import Decimal

from sqlalchemy import text

from app.db import engine

#: Every table in the public schema, parents first so a human restoring by hand
#: can replay the file top to bottom without tripping a foreign key.
TABLES = (
    "platform_settings",
    "users",
    "addresses",
    "cuisines",
    "restaurants",
    "restaurant_cuisines",
    "restaurant_policies",
    "restaurant_staff",
    "platform_staff",
    "restaurant_applications",
    "menu_categories",
    "menu_items",
    "menu_item_images",
    "menu_item_modifier_groups",
    "menu_item_modifier_options",
    "menu_item_modifier_links",
    "delivery_partners",
    "coupons",
    "orders",
    "order_items",
    "order_item_modifiers",
    "order_status_events",
    "payments",
    "refunds",
    "deliveries",
    "coupon_redemptions",
    "reviews",
    "restaurant_settlements",
    "conversations",
    "messages",
)


def _encode(value: object) -> object:
    """Make a value JSON-safe WITHOUT flattening the types JSON already has.

    The catch-all at the bottom used to swallow int, float and bool as well,
    so every id in the snapshot came back as a string and anything reading it
    had to know to cast. Native types pass through untouched now; only the ones
    JSON genuinely cannot represent are converted.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        # JSON represents all five natively, so there is nothing to convert and
        # nothing for a reader to cast back.
        return value
    if isinstance(value, Decimal):
        # str, never float: a float would silently change a money value, which is
        # the one thing a backup must not do.
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return str(value)


async def snapshot(out_dir: pathlib.Path) -> pathlib.Path:

    # handler, so a blocking mkdir/write cannot stall an event loop that is
    # serving anybody. Reaching for anyio.Path here would add a dependency to
    # make a script that runs alone marginally more correct.
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    # Named from the database clock rather than the laptop's, so two snapshots
    # taken either side of a migration sort correctly.
    async with engine.connect() as probe:
        stamp = (await probe.scalar(text("select now()"))).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"snapshot-{stamp}.json"

    payload: dict[str, object] = {
        "taken_at": stamp,
        "note": "Row snapshot before migrate_to_foodishi. Data only, no DDL.",
        "tables": {},
    }
    total = 0

    # ONE transaction for the whole read, so the snapshot is internally
    # consistent rather than a set of reads taken at different moments.
    async with engine.connect() as connection:
        for table in TABLES:
            result = await connection.execute(text(f"select * from {table}"))
            columns = list(result.keys())
            rows = [
                {c: (None if v is None else _encode(v)) for c, v in zip(columns, row, strict=True)}
                for row in result.all()
            ]
            payload["tables"][table] = {"columns": columns, "rows": rows}
            total += len(rows)
            print(f"  {table:32} {len(rows):>6}")

    path.write_text(json.dumps(payload, indent=1))
    print()
    print(f"{total} rows -> {path}  ({path.stat().st_size / 1024:.0f} KB)")
    return path


async def main(out: str | None) -> None:
    target = pathlib.Path(out) if out else pathlib.Path.cwd() / "snapshots"
    try:
        await snapshot(target)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dump all public tables to JSON.")
    parser.add_argument("--out", default=None, help="Directory to write into.")
    asyncio.run(main(parser.parse_args().out))
