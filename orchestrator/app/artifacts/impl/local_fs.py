"""artifacts seam: content-addressed files under a configured root.

URI scheme is `file://`, and the path is derived from the sha256 of the
content, so storing the same bytes twice is a no-op and every artifact is
immutable.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from app.artifacts.base import (
    ArtifactFailure,
    ArtifactStore,
    FetchResult,
    StoredArtifact,
    StoreResult,
)


class LocalFilesystemArtifactStore(ArtifactStore):
    def __init__(self, *, root: Path, max_bytes: int) -> None:
        self._root = root
        self._max_bytes = max_bytes

    async def put(
        self, run_id: UUID, kind: str, content: bytes, *, timeout_seconds: float
    ) -> StoreResult:
        if len(content) > self._max_bytes:
            return ArtifactFailure(
                "too_large", f"{len(content)} bytes exceeds cap of {self._max_bytes}"
            )

        digest = hashlib.sha256(content).hexdigest()
        path = self._path_for(run_id, kind, digest)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_write_atomically, path, content), timeout=timeout_seconds
            )
        except TimeoutError:
            return ArtifactFailure("write_failed", f"timed out after {timeout_seconds}s")
        except OSError as exc:
            return ArtifactFailure("write_failed", f"{path}: {exc}")

        return StoredArtifact(uri=path.as_uri(), sha256=digest, size_bytes=len(content))

    async def get(self, uri: str, *, timeout_seconds: float) -> FetchResult:
        path = self._resolve(uri)
        if path is None:
            return ArtifactFailure("not_found", f"not a local artifact URI: {uri}")
        try:
            content: bytes = await asyncio.wait_for(
                asyncio.to_thread(path.read_bytes), timeout=timeout_seconds
            )
        except FileNotFoundError:
            return ArtifactFailure("not_found", uri)
        except TimeoutError:
            return ArtifactFailure("not_found", f"timed out after {timeout_seconds}s reading {uri}")

        expected = path.name.split(".", 1)[0]
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            return ArtifactFailure("checksum_mismatch", f"{uri}: expected {expected}, got {actual}")
        return content

    async def exists(self, uri: str, *, timeout_seconds: float) -> bool:
        path = self._resolve(uri)
        if path is None:
            return False
        return await asyncio.wait_for(asyncio.to_thread(path.is_file), timeout=timeout_seconds)

    def _path_for(self, run_id: UUID, kind: str, digest: str) -> Path:
        return self._root / str(run_id) / kind / f"{digest}.bin"

    def _resolve(self, uri: str) -> Path | None:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        path = Path(parsed.path).resolve()
        root = self._root.resolve()
        if not path.is_relative_to(root):
            # A URI pointing outside the artifact root is either a bug or an
            # attempt to read the orchestrator's disk. Refuse either way.
            return None
        return path


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
