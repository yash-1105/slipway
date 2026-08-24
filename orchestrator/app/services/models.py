"""The models seam, as a service.

Exists so the CLI can run `slipway models ping` without importing the seam --
an adapter that imports a seam is business logic wearing a command's clothes
(ADR 0002).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.base import Completion, ModelCatalogue, ModelClient, ModelSpec


@dataclass(frozen=True, slots=True)
class PingResult:
    ok: bool
    detail: str
    model_id: str = ""
    total_tokens: int = 0


class ModelsService:
    def __init__(
        self, client: ModelClient, catalogue: ModelCatalogue, *, timeout_seconds: float
    ) -> None:
        self._client = client
        self._catalogue = catalogue
        self._timeout = timeout_seconds

    async def ping(self) -> PingResult:
        """One real call. The provider-outage check from RUNBOOK.md."""
        result = await self._client.ping(timeout_seconds=self._timeout)
        if isinstance(result, Completion):
            return PingResult(
                ok=True,
                detail="ok",
                model_id=result.model_id,
                total_tokens=result.usage.total_tokens,
            )
        return PingResult(ok=False, detail=f"{result.reason}: {result.detail}")

    def catalogue(self) -> tuple[str, str, list[ModelSpec]]:
        """(source base URL, synced-at, specs) for `slipway models catalogue`."""
        return (
            self._catalogue.source_base_url,
            self._catalogue.synced_at,
            [self._catalogue.by_role[role] for role in sorted(self._catalogue.by_role)],
        )
