from __future__ import annotations

from typing import Any, Protocol


class JudgeProvider(Protocol):
    @property
    def identity(self) -> str: ...

    def complete_json(self, *, system: str, payload: dict[str, Any],
                      schema: dict[str, Any]) -> dict[str, Any]: ...

