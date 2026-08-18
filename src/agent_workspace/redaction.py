"""Conservative redaction for all externally sourced text."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [^-\r\n]*(?:PRIVATE KEY|SECRET)[^-\r\n]*-----.*?"
            r"-----END [^-\r\n]*(?:PRIVATE KEY|SECRET)[^-\r\n]*-----",
            re.IGNORECASE | re.DOTALL,
        ),
        "[REDACTED_PRIVATE_MATERIAL]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:token|api[_-]?key|client[_-]?secret|password|passwd|credential|"
            r"private[_-]?key|oauth[_-]?profile)\b\s*[=:]\s*)([^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r'(?i)(["\'](?:token|api[_-]?key|client[_-]?secret|password|credential|'
            r'private[_-]?key|oauth[_-]?profile)["\']\s*:\s*["\'])[^"\']+(["\'])'
        ),
        r"\1[REDACTED]\2",
    ),
    (re.compile(r"\b(?:gh[oprsu]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"), "[REDACTED_TOKEN]"),
    (
        re.compile(r"\b(agent:[A-Za-z0-9_.-]+:(?:[^\s\"'<>]|:(?!//))+)", re.IGNORECASE),
        "[REDACTED_SESSION_KEY]",
    ),
    (
        re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
        r"\1[REDACTED]@",
    ),
)

_SENSITIVE_KEYS = re.compile(
    r"(?i)^(?:authorization|token|api[_-]?key|client[_-]?secret|password|passwd|"
    r"credential|private[_-]?key|oauth[_-]?profile|session[_-]?key)$"
)


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Path):
        return redact_text(str(value))
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEYS.match(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    return value
