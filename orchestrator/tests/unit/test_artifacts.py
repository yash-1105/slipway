"""The artifacts seam's contract, exercised through the in-memory store."""

from __future__ import annotations

import hashlib

from app.artifacts.base import ArtifactFailure, StoredArtifact
from app.artifacts.impl.memory import InMemoryArtifactStore
from app.domain.ids import uuid7

TIMEOUT = 5.0


async def test_storing_bytes_returns_a_uri_and_their_digest() -> None:
    store = InMemoryArtifactStore(max_bytes=1024)
    run_id = uuid7()

    result = await store.put(run_id, "spec", b"the specification", timeout_seconds=TIMEOUT)

    assert isinstance(result, StoredArtifact)
    assert result.sha256 == hashlib.sha256(b"the specification").hexdigest()
    assert result.size_bytes == len(b"the specification")
    assert result.uri.startswith("memory://")


async def test_storing_the_same_bytes_twice_yields_the_same_uri() -> None:
    """Content addressing is what makes a retried job a no-op."""
    store = InMemoryArtifactStore(max_bytes=1024)
    run_id = uuid7()

    first = await store.put(run_id, "spec", b"same", timeout_seconds=TIMEOUT)
    second = await store.put(run_id, "spec", b"same", timeout_seconds=TIMEOUT)

    assert isinstance(first, StoredArtifact)
    assert isinstance(second, StoredArtifact)
    assert first.uri == second.uri


async def test_different_bytes_get_different_uris() -> None:
    store = InMemoryArtifactStore(max_bytes=1024)
    run_id = uuid7()

    first = await store.put(run_id, "spec", b"one", timeout_seconds=TIMEOUT)
    second = await store.put(run_id, "spec", b"two", timeout_seconds=TIMEOUT)

    assert isinstance(first, StoredArtifact)
    assert isinstance(second, StoredArtifact)
    assert first.uri != second.uri


async def test_oversized_content_is_refused_as_a_value() -> None:
    store = InMemoryArtifactStore(max_bytes=8)

    result = await store.put(uuid7(), "build_log", b"x" * 9, timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)
    assert result.reason == "too_large"


async def test_fetching_something_that_was_never_stored_is_a_value_not_a_raise() -> None:
    store = InMemoryArtifactStore(max_bytes=1024)

    result = await store.get("memory://nothing/here/abc", timeout_seconds=TIMEOUT)

    assert isinstance(result, ArtifactFailure)
    assert result.reason == "not_found"


async def test_a_stored_artifact_reads_back_byte_for_byte() -> None:
    store = InMemoryArtifactStore(max_bytes=1024)
    content = b"\x00\xff binary \n content"

    stored = await store.put(uuid7(), "bundle", content, timeout_seconds=TIMEOUT)
    assert isinstance(stored, StoredArtifact)

    assert await store.get(stored.uri, timeout_seconds=TIMEOUT) == content
    assert await store.exists(stored.uri, timeout_seconds=TIMEOUT)
