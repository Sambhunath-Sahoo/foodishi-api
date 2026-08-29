import os
from functools import lru_cache

from app.services.storage.base import StorageBackend
from app.services.storage.supabase_backend import SupabaseStorage

# Set STORAGE_PROVIDER to switch. Adding "s3" or "cloudinary" means writing one
# class against StorageBackend and adding a line here — no router, schema or
# database row changes, because rows hold paths rather than URLs.
_PROVIDERS: dict[str, type[StorageBackend]] = {"supabase": SupabaseStorage}


@lru_cache
def get_storage() -> StorageBackend:
    name = os.environ.get("STORAGE_PROVIDER", "supabase").lower()
    # Resolve first, construct second. Wrapping both in one try turns a missing
    # environment variable into "unknown provider", which sends you looking in
    # entirely the wrong place.
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise RuntimeError(
            f"Unknown STORAGE_PROVIDER {name!r}. Known: {', '.join(sorted(_PROVIDERS))}"
        )
    try:
        return provider()
    except KeyError as exc:
        raise RuntimeError(
            f"Storage provider {name!r} is missing required configuration: {exc}"
        ) from exc
