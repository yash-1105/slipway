"""artifacts seam: storing specs, build logs, bundles and images.

Artifacts are content-hashed and immutable, and addressed by URI with an
explicit scheme -- `file://` in V1, `s3://` in V2 -- so old rows stay valid
across the migration and resolution is a scheme lookup inside the seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    uri: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactFailure:
    reason: Literal["not_found", "write_failed", "checksum_mismatch", "too_large"]
    detail: str


StoreResult = StoredArtifact | ArtifactFailure
FetchResult = bytes | ArtifactFailure


class ArtifactStore(Protocol):
    """The artifacts seam."""

    async def put(
        self, run_id: UUID, kind: str, content: bytes, *, timeout_seconds: float
    ) -> StoreResult:
        """Store bytes. Idempotent: the same bytes yield the same URI."""

    async def get(self, uri: str, *, timeout_seconds: float) -> FetchResult: ...

    async def exists(self, uri: str, *, timeout_seconds: float) -> bool: ...
