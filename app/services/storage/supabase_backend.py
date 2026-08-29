import os

import httpx

from app.services.storage.base import StorageBackend, StorageError, UploadTicket

BUCKET = "menu-images"


class SupabaseStorage(StorageBackend):
    """Supabase Storage over its REST API.

    Deliberately httpx rather than the supabase-py client: three endpoints do not
    justify a dependency, and a thin implementation is easier to mirror when a
    second backend arrives.
    """

    def __init__(self, *, base_url: str | None = None, secret_key: str | None = None,
                 bucket: str = BUCKET) -> None:
        self._base = (base_url or os.environ["SUPABASE_URL"]).rstrip("/")
        self._key = secret_key or os.environ["SUPABASE_SECRET_KEY"]
        self._bucket = bucket

    @property
    def _auth(self) -> dict[str, str]:
        return {"apikey": self._key, "Authorization": f"Bearer {self._key}"}

    async def create_upload_ticket(
        self, *, storage_path: str, content_type: str, expires_in: int = 900
    ) -> UploadTicket:
        endpoint = f"{self._base}/storage/v1/object/upload/sign/{self._bucket}/{storage_path}"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                endpoint, headers=self._auth, json={"expiresIn": expires_in}
            )
        if response.status_code >= 400:
            raise StorageError(f"Could not sign upload: {response.status_code} {response.text[:200]}")

        # Supabase returns a relative signed URL carrying the token.
        signed = response.json()["url"].lstrip("/")
        return UploadTicket(
            url=f"{self._base}/storage/v1/{signed}",
            method="PUT",
            headers={"Content-Type": content_type},
            storage_path=storage_path,
            expires_in=expires_in,
        )

    def public_url(self, storage_path: str) -> str:
        return f"{self._base}/storage/v1/object/public/{self._bucket}/{storage_path}"

    async def delete(self, storage_path: str) -> None:
        endpoint = f"{self._base}/storage/v1/object/{self._bucket}/{storage_path}"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.delete(endpoint, headers=self._auth)
        if response.status_code >= 400 and response.status_code != 404:
            raise StorageError(f"Could not delete: {response.status_code} {response.text[:200]}")
