"""Provider-neutral object storage.

Two rules keep this swappable:

1. The database stores a **path**, never a URL. Rows carry
   `restaurants/7/menu-items/91/a1b2.webp`, so moving from Supabase to S3 or
   Cloudinary rewrites configuration, not data.
2. Nothing outside this package imports a provider SDK. Callers depend on the
   StorageBackend protocol; the factory decides who implements it.

Adding a provider means implementing three methods.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class UploadTicket:
    """What the client needs to send bytes directly to the provider.

    The file never passes through this API: we authorise, the client uploads.
    A 5 MB photo over hotel wifi should not occupy a FastAPI worker.
    """

    url: str
    method: str
    headers: dict[str, str]
    storage_path: str
    expires_in: int


class StorageError(RuntimeError):
    """Provider rejected the operation. Surface as 502, never as 500."""


class StorageBackend(Protocol):
    async def create_upload_ticket(
        self, *, storage_path: str, content_type: str, expires_in: int = 900
    ) -> UploadTicket: ...

    def public_url(self, storage_path: str) -> str: ...

    async def delete(self, storage_path: str) -> None: ...


def build_path(*, restaurant_id: int, menu_item_id: int, extension: str) -> str:
    """Provider-neutral key. Flat enough for S3, nested enough to browse, and
    prefixed by restaurant so a future storage policy can scope on that prefix."""
    suffix = extension.lstrip(".").lower()
    return f"restaurants/{restaurant_id}/menu-items/{menu_item_id}/{uuid4().hex}.{suffix}"
