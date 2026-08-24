"""The local filesystem artifact store. Touches a real disk, so not a unit test."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.artifacts.base import ArtifactFailure, StoredArtifact
from app.artifacts.impl.local_fs import LocalFilesystemArtifactStore
from app.domain.ids import uuid7

TIMEOUT = 10.0


def store(root: Path, *, max_bytes: int = 1 << 20) -> LocalFilesystemArtifactStore:
    return LocalFilesystemArtifactStore(root=root, max_bytes=max_bytes)


async def test_content_is_written_and_read_back(tmp_path: Path) -> None:
    subject = store(tmp_path)
    content = b"the specification\n"

    stored = await subject.put(uuid7(), "spec", content, timeout_seconds=TIMEOUT)

    assert isinstance(stored, StoredArtifact)
    assert stored.uri.startswith("file://")
    assert stored.sha256 == hashlib.sha256(content).hexdigest()
    assert await subject.get(stored.uri, timeout_seconds=TIMEOUT) == content


async def test_storing_the_same_bytes_twice_is_idempotent(tmp_path: Path) -> None:
    """A retried job must not create a second copy."""
    subject = store(tmp_path)
    run_id = uuid7()

    first = await subject.put(run_id, "spec", b"same bytes", timeout_seconds=TIMEOUT)
    second = await subject.put(run_id, "spec", b"same bytes", timeout_seconds=TIMEOUT)

    assert isinstance(first, StoredArtifact)
    assert isinstance(second, StoredArtifact)
    assert first.uri == second.uri
    assert len(list(tmp_path.rglob("*.bin"))) == 1


async def test_a_corrupted_file_is_detected_rather_than_returned(tmp_path: Path) -> None:
    """Content addressing is only worth having if the checksum is verified."""
    subject = store(tmp_path)
    stored = await subject.put(uuid7(), "spec", b"original", timeout_seconds=TIMEOUT)
    assert isinstance(stored, StoredArtifact)

    on_disk = next(tmp_path.rglob("*.bin"))
    on_disk.write_bytes(b"tampered")

    result = await subject.get(stored.uri, timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)
    assert result.reason == "checksum_mismatch"


async def test_a_uri_outside_the_root_is_refused(tmp_path: Path) -> None:
    """A URI pointing at the orchestrator's disk is a bug or an attack."""
    subject = store(tmp_path / "artifacts")

    result = await subject.get("file:///etc/passwd", timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)
    assert result.reason == "not_found"


async def test_traversal_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (tmp_path / "secret.bin").write_bytes(b"not yours")
    subject = store(root)

    result = await subject.get(f"file://{root}/../secret.bin", timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)


async def test_oversized_content_is_refused_before_it_is_written(tmp_path: Path) -> None:
    subject = store(tmp_path, max_bytes=16)

    result = await subject.put(uuid7(), "build_log", b"x" * 17, timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)
    assert result.reason == "too_large"
    assert list(tmp_path.rglob("*.bin")) == []


async def test_a_missing_artifact_is_a_value_not_a_raise(tmp_path: Path) -> None:
    subject = store(tmp_path)
    missing = (tmp_path / "nothing" / f"{'0' * 64}.bin").as_uri()

    result = await subject.get(missing, timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)
    assert result.reason == "not_found"
    assert not await subject.exists(missing, timeout_seconds=TIMEOUT)


async def test_no_partial_file_survives_a_write(tmp_path: Path) -> None:
    """Writes go through a temporary file and a rename."""
    subject = store(tmp_path)
    await subject.put(uuid7(), "spec", b"content", timeout_seconds=TIMEOUT)

    assert list(tmp_path.rglob("*.tmp")) == []
