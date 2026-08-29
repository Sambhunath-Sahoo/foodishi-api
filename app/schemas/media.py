from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.media import MAX_IMAGES_PER_ITEM

ALLOWED_TYPES = {"image/webp", "image/jpeg", "image/png"}
EXTENSIONS = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png"}


class UploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: str = Field(description="One of image/webp, image/jpeg, image/png")

    @model_validator(mode="after")
    def known_type(self):
        if self.content_type not in ALLOWED_TYPES:
            raise ValueError(
                f"content_type must be one of {', '.join(sorted(ALLOWED_TYPES))}"
            )
        return self


class UploadUrlRead(BaseModel):
    """Everything the client needs to PUT the file straight to storage."""

    url: str
    method: str
    headers: dict[str, str]
    storage_path: str
    expires_in: int


class ImageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Echoed back from the upload ticket, so the row can only ever point at a
    # path this API issued.
    #
    # The pattern admits exactly what storage.build_path produces and nothing
    # else. The router's startswith() prefix check is the tenancy half and stays,
    # but startswith alone was satisfied by
    # "restaurants/7/menu-items/91/../../../9/menu-items/44/cover.webp" -- a
    # traversal that pointed one restaurant's dish at another's photo and, on
    # DELETE, removed another restaurant's object from the shared bucket. A
    # pattern with no dots and no slashes in the final segment cannot express
    # that. Anchored at both ends, so a prefix match is not enough.
    storage_path: str = Field(
        max_length=400,
        pattern=r"^restaurants/\d+/menu-items/\d+/[0-9a-f]{32}\.[a-z0-9]{2,5}$",
    )
    alt_text: str | None = Field(default=None, max_length=200)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    bytes: int | None = Field(default=None, ge=1)


class ImageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    menu_item_id: int
    storage_path: str
    url: str
    alt_text: str | None
    sort_order: int
    width: int | None
    height: int | None
    created_at: datetime


class ImageReorder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The full desired order. Partial reorders would need a second pass to
    # detect gaps, and the gallery is at most seven items.
    image_ids: list[int] = Field(min_length=1, max_length=MAX_IMAGES_PER_ITEM)

    @model_validator(mode="after")
    def no_duplicates(self):
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError("image_ids must not repeat")
        return self
