"""GitHub, scoped to the scratch org.

The token comes from configuration and is never inherited from a local `gh`
session -- an agent pushing with a developer's credentials would be able to
write to every repository that developer can. Every call has a timeout.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from app.domain.errors import ConfigError

log = structlog.get_logger(__name__)

_API = "https://api.github.com"


@dataclass(frozen=True, slots=True)
class Repository:
    full_name: str
    clone_url: str
    private: bool


@dataclass(frozen=True, slots=True)
class GitHubFailure:
    status: int
    detail: str


CreateRepoResult = Repository | GitHubFailure


class GitHubClient:
    def __init__(self, *, token: str, org: str, timeout_seconds: float = 30.0) -> None:
        if not token or not org:
            raise ConfigError(
                "SLIPWAY_GITHUB_TOKEN and SLIPWAY_GITHUB_ORG are both required; "
                "the token must be scoped to the scratch org and must not be a "
                "developer's personal session"
            )
        self._org = org
        self._timeout = timeout_seconds
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def create_repo(self, name: str, *, description: str = "") -> CreateRepoResult:
        """Create a private repository in the scratch org. Idempotent."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{_API}/orgs/{self._org}/repos",
                headers=self._headers,
                json={"name": name, "private": True, "description": description},
            )

            if response.status_code == 422:
                # Already exists. Fetch it rather than failing the retry.
                existing = await client.get(
                    f"{_API}/repos/{self._org}/{name}", headers=self._headers
                )
                if existing.status_code == 200:
                    return _to_repository(existing.json())
                return GitHubFailure(existing.status_code, existing.text[:500])

            if response.status_code not in (200, 201):
                return GitHubFailure(response.status_code, response.text[:500])

            return _to_repository(response.json())


def _to_repository(payload: dict[str, object]) -> Repository:
    return Repository(
        full_name=str(payload["full_name"]),
        clone_url=str(payload["clone_url"]),
        private=bool(payload["private"]),
    )
