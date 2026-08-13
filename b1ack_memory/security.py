from __future__ import annotations

import math
import os
import re
import stat
from pathlib import Path
from typing import Any

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
    return any(_entropy(token) >= 3.5 for token in _opaque_tokens(text))


def is_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def redact_secrets(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for pattern in _SECRET_PATTERNS:
        redacted, count = pattern.subn("[REDACTED_SECRET]", redacted)
        changed = changed or count > 0
    for token in set(_opaque_tokens(redacted)):
        if _entropy(token) >= 3.5:
            redacted = redacted.replace(token, "[REDACTED_SECRET]")
            changed = True
    return redacted, changed


def _opaque_tokens(text: str) -> list[str]:
    """Return bounded token candidates shared by detection and redaction."""
    return re.findall(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/=-]{16,}(?![A-Za-z0-9_+/=-])", text)


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def secure_file(path: Path) -> None:
    if path.exists() and os.name != "nt":
        path.chmod(0o600)


def permission_report(path: Path, *, directory: bool | None = None) -> dict[str, Any]:
    exists = path.exists()
    if os.name == "nt":
        return {
            "path": str(path), "exists": exists, "managed_by": "windows_acl",
            "safe": None, "mode": None,
            "recommendation": "Use Windows ACLs to restrict this path to the current user.",
        }
    if not exists:
        return {"path": str(path), "exists": False, "managed_by": "posix", "safe": True, "mode": None}
    mode = stat.S_IMODE(path.stat().st_mode)
    expected = 0o700 if (directory if directory is not None else path.is_dir()) else 0o600
    return {
        "path": str(path), "exists": True, "managed_by": "posix",
        "safe": mode & 0o077 == 0, "mode": f"{mode:04o}", "expected": f"{expected:04o}",
        "recommendation": None if mode & 0o077 == 0 else f"chmod {expected:04o} {path}",
    }


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
        return {"configured": bool(value)}

    def permissions_safe(self) -> bool:
        if not self.path.exists() or os.name == "nt":
            return True
        return stat.S_IMODE(self.path.stat().st_mode) & 0o077 == 0
