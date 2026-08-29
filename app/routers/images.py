"""Dish photo galleries.

Bytes never pass through this API. A client asks for an upload ticket, we
authorise it and hand back a short-lived signed URL, the client PUTs straight
to storage, then tells us the path to record. A 5 MB photo over hotel wifi must
not occupy a worker for the length of the upload.
"""

import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    BAD_GATEWAY,
    CONFLICT,
    NOT_FOUND,
    bad_gateway,
    conflict,
    not_found,
    unprocessable,
)
from app.db import SessionDep
from app.dependencies.identity import FORBIDDEN
from app.dependencies.scope import admin_of_menu_item
from app.models.catalog import MenuItem
from app.models.media import MAX_IMAGES_PER_ITEM, MenuItemImage
from app.schemas.media import (
    EXTENSIONS,
    ImageCreate,
    ImageRead,
    ImageReorder,
    UploadUrlRead,
    UploadUrlRequest,
)
from app.services.storage.base import StorageError, build_path
from app.services.storage.factory import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["images"])

ADMIN = [Depends(admin_of_menu_item)]


def _read(image: MenuItemImage) -> ImageRead:
    """Build the response from named fields only.

    Reading every column instead would touch updated_at, which onupdate expires
    after a flush — and refreshing an expired attribute from async code raises
    MissingGreenlet. The URL is derived here rather than stored, so swapping
    storage provider never means rewriting a row.
    """
    return ImageRead(
        id=image.id,
        menu_item_id=image.menu_item_id,
        storage_path=image.storage_path,
        url=get_storage().public_url(image.storage_path),
        alt_text=image.alt_text,
        sort_order=image.sort_order,
        width=image.width,
        height=image.height,
        created_at=image.created_at,
    )


@router.get(
    "/menu-items/{item_id}/images",
    response_model=list[ImageRead],
    responses=NOT_FOUND,
    summary="A dish's photos, cover first",
)
async def list_images(item_id: int, session: SessionDep):
    if await session.get(MenuItem, item_id) is None:
        raise not_found("menu item", item_id)
    rows = await session.execute(
        select(MenuItemImage)
        .where(MenuItemImage.menu_item_id == item_id)
        .order_by(MenuItemImage.sort_order)
    )
    return [_read(image) for image in rows.scalars()]


@router.post(
    "/menu-items/{item_id}/images/upload-url",
    response_model=UploadUrlRead,
    dependencies=ADMIN,
    responses={**NOT_FOUND, **FORBIDDEN, **CONFLICT, **BAD_GATEWAY},
    summary="Get a signed URL to upload one photo (managers only)",
)
async def create_upload_url(item_id: int, payload: UploadUrlRequest, session: SessionDep):
    item = await session.get(MenuItem, item_id)
    if item is None:
        raise not_found("menu item", item_id)

    # Checked before signing rather than after uploading: the alternative wastes
    # the customer's bandwidth and leaves an orphan object in the bucket.
    if await _count(session, item_id) >= MAX_IMAGES_PER_ITEM:
        raise conflict(
            f"{item.name} already has the maximum of {MAX_IMAGES_PER_ITEM} photos; "
            "delete one first"
        )

    storage_path = build_path(
        restaurant_id=item.restaurant_id,
        menu_item_id=item_id,
        extension=EXTENSIONS[payload.content_type],
    )
    try:
        ticket = await get_storage().create_upload_ticket(
            storage_path=storage_path, content_type=payload.content_type
        )
    except StorageError as exc:
        # 502, not 422: the caller's request was fine, the provider failed.
        # StorageError's docstring says exactly this. And the message is fixed --
        # it used to interpolate the exception, which carries 200 characters of
        # the provider's raw response body: bucket names, provider status codes
        # and request ids, handed to a restaurant admin.
        logger.exception("Storage refused an upload ticket for item %s", item_id)
        raise bad_gateway(
            "Image storage is unavailable right now — try again shortly"
        ) from exc
    return UploadUrlRead(**ticket.__dict__)


@router.post(
    "/menu-items/{item_id}/images",
    response_model=ImageRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN,
    responses={**NOT_FOUND, **FORBIDDEN, **CONFLICT},
    summary="Record a photo after it has been uploaded (managers only)",
)
async def add_image(item_id: int, payload: ImageCreate, session: SessionDep):
    item = await session.get(MenuItem, item_id)
    if item is None:
        raise not_found("menu item", item_id)

    # A path this API did not issue would let a caller point a dish at any object
    # in the bucket, including another restaurant's.
    expected_prefix = f"restaurants/{item.restaurant_id}/menu-items/{item_id}/"
    if not payload.storage_path.startswith(expected_prefix):
        raise unprocessable(
            "storage_path does not belong to this menu item; use the path returned "
            "by the upload-url endpoint"
        )

    used = await _count(session, item_id)
    if used >= MAX_IMAGES_PER_ITEM:
        raise conflict(
            f"{item.name} already has the maximum of {MAX_IMAGES_PER_ITEM} photos"
        )

    image = MenuItemImage(menu_item_id=item_id, sort_order=used, **payload.model_dump())
    session.add(image)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflict("That gallery position is already taken; refresh and retry") from exc
    await session.refresh(image)
    return _read(image)


@router.put(
    "/menu-items/{item_id}/images/order",
    response_model=list[ImageRead],
    dependencies=ADMIN,
    responses={**NOT_FOUND, **FORBIDDEN, 422: {"description": "Ids do not match the gallery"}},
    summary="Reorder a gallery; the first id becomes the cover (managers only)",
)
async def reorder_images(item_id: int, payload: ImageReorder, session: SessionDep):
    rows = list(
        (await session.execute(
            select(MenuItemImage).where(MenuItemImage.menu_item_id == item_id)
        )).scalars()
    )
    if not rows:
        raise not_found("menu item gallery", item_id)
    if {image.id for image in rows} != set(payload.image_ids):
        raise unprocessable("image_ids must list exactly the photos on this dish")

    # Written straight to the final positions. uq_menu_item_images_position is
    # DEFERRABLE INITIALLY DEFERRED, so the collisions every permutation passes
    # through are only checked at COMMIT.
    by_id = {image.id: image for image in rows}
    for position, image_id in enumerate(payload.image_ids):
        by_id[image_id].sort_order = position
    await session.flush()

    return [_read(by_id[image_id]) for image_id in payload.image_ids]


@router.delete(
    "/menu-items/{item_id}/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=ADMIN,
    responses={**NOT_FOUND, **FORBIDDEN},
    summary="Delete a photo and close the gap (managers only)",
)
async def delete_image(item_id: int, image_id: int, session: SessionDep):
    image = await session.get(MenuItemImage, image_id)
    if image is None or image.menu_item_id != item_id:
        raise not_found("image", image_id)

    storage_path = image.storage_path
    await session.delete(image)
    await session.flush()

    # Close the gap so positions stay 0..n-1 and position 0 is always the cover.
    remaining = list((await session.execute(
        select(MenuItemImage)
        .where(MenuItemImage.menu_item_id == item_id)
        .order_by(MenuItemImage.sort_order)
    )).scalars())
    for offset, row in enumerate(remaining):
        row.sort_order = offset
    await session.flush()

    try:
        await get_storage().delete(storage_path)
    except StorageError:
        # The row is already gone; an orphaned object is a cleanup job, not a
        # reason to fail the request and leave the gallery showing a dead photo.
        logger.exception("Could not remove %s from storage", storage_path)


async def _count(session: SessionDep, item_id: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(MenuItemImage)
        .where(MenuItemImage.menu_item_id == item_id)
    ) or 0)
