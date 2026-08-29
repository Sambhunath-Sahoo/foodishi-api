"""Photos for the seeded catalog: kitchen covers, dish galleries, avatars.

The bytes already live in Supabase Storage — this phase only writes the rows
that point at them, from the manifest in app/seed/data/images.json. That file
also records the licence, author and source page of every photo, because they
are Wikimedia Commons images and most are CC BY-SA: seeded data still has to
say where it came from.

DEVELOPMENT DATA. In production a restaurant uploads its own photos through
POST /menu-items/{id}/images-upload-url, which is why the dish paths here use
the very prefix that endpoint enforces — a seeded row the API would refuse is a
row nobody can reorder or delete through the product.

Idempotent and additive, like app/seed/platform.py: it SELECTs before every
INSERT and never truncates. It also takes NO draw from the shared rng — avatars
are assigned by user index, because the seeded emails come off that stream and
several live Supabase logins are matched to profiles by email.
"""

import json
import pathlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import MenuItem, Restaurant
from app.models.media import MenuItemImage
from app.models.user import User

DATA = pathlib.Path(__file__).parent / "data" / "images.json"

# Roughly one customer in eight keeps the placeholder, so every surface that
# renders an avatar is forced to handle the null case in development instead of
# only in production. Deterministic: it is the user's own id, not a dice roll.
NO_AVATAR_EVERY = 8


def _payload() -> dict:
    return json.loads(DATA.read_text())


async def build(session: AsyncSession) -> list[str]:
    """Attach photos to restaurants, dishes and users. Safe to run repeatedly."""
    data = _payload()
    notes: list[str] = []

    # ---- kitchen covers -------------------------------------------------
    # Keyed by NAME, not by id. Both maps in the manifest used to be keyed on
    # database ids, and a reseed that renumbered the catalog then matched nothing:
    # 333 dish photos and 25 covers silently became zero, with the phase still
    # reporting success. Ids are volatile here; the name is the stable identity.
    covers: dict[str, str] = data["covers"]
    restaurants = (await session.scalars(select(Restaurant))).all()
    attached = 0
    for restaurant in restaurants:
        url = covers.get(restaurant.name)
        if url is None or restaurant.image_url == url:
            continue
        restaurant.image_url = url
        attached += 1
    notes.append(f"kitchen covers · {attached} set, {len(restaurants)} restaurants")

    # ---- dish galleries -------------------------------------------------
    # Keyed "Restaurant Name::Dish Name" — see the note on covers above. The dish
    # name alone is not enough: "Paneer Tikka" is on several menus and each has
    # its own photo.
    dishes: dict[str, dict] = data["dishes"]
    existing = set(
        (await session.scalars(
            select(MenuItemImage.menu_item_id).where(MenuItemImage.sort_order == 0)
        )).all()
    )
    # The live catalog, as (restaurant name, dish name) -> id. Built from the
    # database rather than from the manifest, so a dish the catalog no longer has
    # simply never comes up — the manifest must not resurrect a deleted row.
    restaurant_names = {r.id: r.name for r in restaurants}
    live: dict[str, int] = {}
    for item in await session.scalars(select(MenuItem)):
        name = restaurant_names.get(item.restaurant_id)
        if name is not None:
            live[f"{name}::{item.name}"] = item.id

    created = 0
    unmatched = 0
    for key, item_id in live.items():
        dish = dishes.get(key)
        if dish is None:
            unmatched += 1
            continue
        if item_id in existing:
            continue
        session.add(
            MenuItemImage(
                menu_item_id=item_id,
                # NOTE: this path embeds the ORIGINAL restaurant and menu-item
                # ids, because that is where the bytes actually are in Supabase
                # Storage. It therefore does NOT match the prefix
                # POST /menu-items/{id}/images enforces for a NEW upload, so a
                # seeded row cannot be re-added through that endpoint after a
                # renumbering. Reading, rendering and deleting all work. Fixing
                # it properly means copying the objects to the new prefix.
                storage_path=dish["storage_path"],
                # Real alt text, not the filename. A screen reader announcing
                # "restaurants-4-menu-items-91-a3f.jpg" is worse than silence.
                alt_text=f"{dish['name']}, as served",
                sort_order=0,
            )
        )
        created += 1
    notes.append(
        f"dish photos · {created} created, {len(existing)} already had one, "
        f"{unmatched} with no photo in the manifest"
    )

    # ---- avatars --------------------------------------------------------
    avatars: list[str] = data["avatars"]
    if avatars:
        users = (await session.scalars(select(User).order_by(User.id))).all()
        given = 0
        for index, user in enumerate(users):
            # Operators and anyone already carrying a photo are left alone.
            if user.avatar_url is not None or user.email.endswith("@foodishi.internal"):
                continue
            if index % NO_AVATAR_EVERY == 0:
                continue
            user.avatar_url = avatars[index % len(avatars)]
            given += 1
        notes.append(f"avatars · {given} set from a pool of {len(avatars)}")

    await session.flush()
    return notes
