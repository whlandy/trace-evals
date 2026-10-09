"""OpenAI Responses API provider，只有调用时才读取 SDK/API key。"""

from __future__ import annotations

import json
from typing import Any


class OpenAIProvider:
    def __init__(self, model: str, *, reasoning_effort: str = "low", client=None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._client = client

    @property
    def identity(self) -> str:
        return f"openai:{self.model}:{self.reasoning_effort}"

    def complete_json(self, *, system: str, payload: dict[str, Any],
                      schema: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        response = self._client.responses.create(
            model=self.model,
            instructions=system,
            input=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            reasoning={"effort": self.reasoning_effort},
            text={"format": {"type": "json_schema", "name": "trace_evaluation",
                              "strict": True, "schema": schema}},
        )
        return json.loads(response.output_text)

