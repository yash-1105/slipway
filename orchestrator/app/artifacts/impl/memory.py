"""artifacts seam: an in-memory store for tests/unit."""

from __future__ import annotations

import hashlib
from uuid import UUID

from app.artifacts.base import (
    ArtifactFailure,
    ArtifactStore,
    FetchResult,
    StoredArtifact,
    StoreResult,
)


class InMemoryArtifactStore(ArtifactStore):
    def __init__(self, *, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._blobs: dict[str, bytes] = {}

    async def put(
        self, run_id: UUID, kind: str, content: bytes, *, timeout_seconds: float
    ) -> StoreResult:
        if len(content) > self._max_bytes:
            return ArtifactFailure(
                "too_large", f"{len(content)} bytes exceeds cap of {self._max_bytes}"
            )
        digest = hashlib.sha256(content).hexdigest()
        uri = f"memory://{run_id}/{kind}/{digest}"
        self._blobs[uri] = content
        return StoredArtifact(uri=uri, sha256=digest, size_bytes=len(content))

    async def get(self, uri: str, *, timeout_seconds: float) -> FetchResult:
        content = self._blobs.get(uri)
        if content is None:
            return ArtifactFailure("not_found", uri)
        return content

    async def exists(self, uri: str, *, timeout_seconds: float) -> bool:
        return uri in self._blobs
