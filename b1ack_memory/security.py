from __future__ import annotations

import math
import os
import re
import stat
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\b\s*[:=]\s*[\"']?([^\s\"']{8,})"
    ),
)

_SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)(?:\b(?:diagnosis|medical|health|bank account|credit card|passport)\b|"
        r"身份证|银行卡|信用卡|病历|诊断|财务)"
    ),
    re.compile(r"\b\d{17}[0-9Xx]\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def contains_secret(text: str) -> bool:
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return True
    return any(_entropy(token) >= 4.2 for token in re.findall(r"[A-Za-z0-9_+/=-]{32,}", text))


def is_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def redact_secrets(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for pattern in _SECRET_PATTERNS:
        redacted, count = pattern.subn("[REDACTED_SECRET]", redacted)
        changed = changed or count > 0
    for token in set(re.findall(r"[A-Za-z0-9_+/=-]{32,}", redacted)):
        if _entropy(token) >= 4.2:
            redacted = redacted.replace(token, "[REDACTED_SECRET]")
            changed = True
    return redacted, changed


class SecretStore:
    """Tiny local secret file. It intentionally does not claim encryption at rest."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, str]:
        import json

        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def save(self, values: dict[str, str]) -> None:
        import json
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".secrets-", dir=self.path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(values, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temp_path, self.path)
            if os.name != "nt":
                self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        finally:
            temp_path.unlink(missing_ok=True)

    def masked_status(self, name: str) -> dict[str, object]:
        value = self.load().get(name, "")
        return {"configured": bool(value), "masked": f"••••{value[-4:]}" if value else ""}

    def permissions_safe(self) -> bool:
        if not self.path.exists() or os.name == "nt":
            return True
        return stat.S_IMODE(self.path.stat().st_mode) & 0o077 == 0
