from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .version import __version__

try:
    import httpx
except ImportError:  # pragma: no cover - exercised by minimal plugin installs
    httpx = None  # type: ignore[assignment]


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
        model_id = self.model.lower().rsplit("/", 1)[-1]
        if model_id.startswith("deepseek-v4"):
            body["thinking"] = {"type": "disabled"}
        try:
            response = self._post("/chat/completions", body)
        except LlmError as error:
            if "400" not in str(error) and "422" not in str(error):
                raise
            body.pop("response_format", None)
            body["messages"][0]["content"] += " Output one valid JSON object and no markdown."
            response = self._post("/chat/completions", body)
        original_usage = response.get("usage") or {}
        repair_used = False
        try:
            parsed = self._parse_completion(response)
        except LlmError as original_error:
            if not str(original_error).startswith("Invalid JSON completion:"):
                raise
            malformed = self._completion_text(response)
            repair_body = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Repair the supplied malformed or truncated JSON. Return one compact valid "
                            "JSON object only. Preserve complete records, discard an incomplete trailing "
                            "record, never invent facts, keep at most 20 array items, and shorten long strings."
                        ),
                    },
                    {"role": "user", "content": malformed},
                ],
                "temperature": 0,
                "max_tokens": min(self.max_output_tokens, 4096),
                "stream": False,
                "response_format": {"type": "json_object"},
            }
            if model_id.startswith("deepseek-v4"):
                repair_body["thinking"] = {"type": "disabled"}
            repaired = self._post("/chat/completions", repair_body)
            try:
                parsed = self._parse_completion(repaired)
            except LlmError as repair_error:
                raise LlmError(
                    f"{original_error}; automatic JSON repair failed: {repair_error}"
                ) from repair_error
            response = repaired
            repair_used = True
        usage = response.get("usage") or {}
        return LlmResult(
            parsed=parsed,
            raw=response,
            input_tokens=(
                int(original_usage.get("prompt_tokens") or original_usage.get("input_tokens") or 0)
                + int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                if repair_used
                else int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            ),
            output_tokens=(
                int(original_usage.get("completion_tokens") or original_usage.get("output_tokens") or 0)
                + int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                if repair_used
                else int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            ),
        )

    def embeddings(self, texts: list[str]) -> list[list[float]]:
        response = self._post("/embeddings", {"model": self.model, "input": texts})
        data = sorted(response.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in data]
        if len(vectors) != len(texts) or not all(isinstance(item, list) for item in vectors):
            raise LlmError("Invalid embeddings response")
        return vectors

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"B1ack-Memory/{__version__}",
            **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
        }
        if httpx is not None:
            try:
                response = httpx.post(
                    url,
                    content=payload,
                    headers=headers,
                    timeout=self.timeout,
                    follow_redirects=True,
                )
            except httpx.HTTPError as error:
                raise LlmError(str(error)) from error
            raw = response.text
            if response.status_code >= 400:
                raise LlmError(f"HTTP {response.status_code}: {raw[:500]}")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                raise LlmError(str(error)) from error

        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers=headers,
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

    @classmethod
    def _parse_completion(cls, response: dict[str, Any]) -> Any:
        content = cls._completion_text(response)
        try:
            return cls._parse_json(content)
        except json.JSONDecodeError as error:
            raise LlmError(f"Invalid JSON completion: {error}") from error

    @staticmethod
    def _completion_text(response: dict[str, Any]) -> str:
        try:
            message = response["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LlmError(f"Invalid completion response: {error}") from error
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        rendered = str(content or "").strip()
        if not rendered:
            if message.get("reasoning_content"):
                raise LlmError(
                    "Model returned reasoning_content but no final content; "
                    "use non-thinking mode for structured Dream extraction"
                )
            raise LlmError("Model returned an empty completion")
        return rendered

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
