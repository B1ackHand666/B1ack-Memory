from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .version import __version__


class LlmError(RuntimeError):
    pass


@dataclass(slots=True)
class LlmResult:
    parsed: Any
    raw: dict[str, Any]
    input_tokens: int
    output_tokens: int


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        max_output_tokens: int = 1200,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model) and (bool(self.api_key) or self._is_local())

    def test(self) -> dict[str, Any]:
        result = self.chat_json(
            system="Return JSON only.",
            user='Return exactly this object: {"ok": true}',
        )
        return {"ok": bool(isinstance(result.parsed, dict) and result.parsed.get("ok")), "model": self.model}

    def chat_json(self, *, system: str, user: str) -> LlmResult:
        if not self.configured:
            raise LlmError("LLM is not configured")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        # DeepSeek V4 defaults to thinking mode. Dream extraction is structured,
        # repetitive work, so non-thinking mode is the lower-cost default.
        if "api.deepseek.com" in self.base_url and self.model.startswith("deepseek-v4"):
            body["thinking"] = {"type": "disabled"}
        try:
            response = self._post("/chat/completions", body)
        except LlmError as error:
            if "400" not in str(error) and "422" not in str(error):
                raise
            body.pop("response_format", None)
            body["messages"][0]["content"] += " Output one valid JSON object and no markdown."
            response = self._post("/chat/completions", body)
        try:
            content = response["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            parsed = self._parse_json(str(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise LlmError(f"Invalid JSON completion: {error}") from error
        usage = response.get("usage") or {}
        return LlmResult(
            parsed=parsed,
            raw=response,
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        )

    def embeddings(self, texts: list[str]) -> list[list[float]]:
        response = self._post("/embeddings", {"model": self.model, "input": texts})
        data = sorted(response.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in data]
        if len(vectors) != len(texts) or not all(isinstance(item, list) for item in vectors):
            raise LlmError("Invalid embeddings response")
        return vectors

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"B1ack-Memory/{__version__}",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise LlmError(f"HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise LlmError(str(error)) from error

    def _is_local(self) -> bool:
        return self.base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))

    @staticmethod
    def _parse_json(content: str) -> Any:
        value = content.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines)
        return json.loads(value)
