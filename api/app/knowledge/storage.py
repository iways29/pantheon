"""Where original files are kept: a private Supabase Storage bucket.

Uploads go to `POST {SUPABASE_URL}/storage/v1/object/{bucket}/{path}`
(Supabase Storage API, read 2026-09-26) with the server-side key. Supabase is
moving from the legacy `service_role` JWT to `sb_secret_...` keys; both are
sent in the `apikey` header, and a JWT-style key also as a bearer token.
"""

from typing import Protocol

import httpx

BUCKET = "documents"


class StorageError(RuntimeError):
    pass


class FileStore(Protocol):
    def put(self, path: str, content: bytes, content_type: str) -> None: ...


class SupabaseStorage:
    def __init__(
        self,
        project_url: str,
        key: str,
        *,
        bucket: str = BUCKET,
        client: httpx.Client | None = None,
    ) -> None:
        if not project_url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        self._base = f"{project_url.rstrip('/')}/storage/v1/object/{bucket}"
        self._key = key
        self._client = client or httpx.Client(timeout=30.0)

    def put(self, path: str, content: bytes, content_type: str) -> None:
        headers = {"apikey": self._key, "content-type": content_type, "x-upsert": "true"}
        if self._key.startswith("eyJ"):
            headers["authorization"] = f"Bearer {self._key}"
        try:
            response = self._client.post(f"{self._base}/{path}", content=content, headers=headers)
        except httpx.HTTPError as error:
            raise StorageError(f"Storage upload failed: {error}") from error
        if response.status_code >= 400:
            raise StorageError(f"Storage returned {response.status_code}: {response.text[:300]}")


class MemoryStore:
    """For tests and local runs without Supabase."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}

    def put(self, path: str, content: bytes, content_type: str) -> None:
        self.files[path] = (content, content_type)
