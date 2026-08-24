"""Loading prompt templates from prompts/.

Prompts are files, not string literals, so a change to one shows up in a diff
as a change to a prompt and can be reviewed as such.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.errors import ConfigError


class PromptLibrary:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        path = self._root / f"{name}.md"
        if not path.is_file():
            raise ConfigError(f"prompt {name!r} not found at {path}")
        text = path.read_text()
        self._cache[name] = text
        return text

    def render(self, name: str, **values: str) -> str:
        template = self.get(name)
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", value)
        return template
