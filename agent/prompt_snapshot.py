"""Build sanitized prompt snapshot metadata."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "password",
    "credential",
)

_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(access_token|refresh_token|id_token|api_key|token|secret|password)\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\btok_[A-Za-z0-9_-]{4,}\b"),
]


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def sanitize_text(value: str) -> str:
    sanitized = value
    sanitized = _SECRET_PATTERNS[0].sub(lambda match: f"{match.group(1)}=[REDACTED]", sanitized)
    for pattern in _SECRET_PATTERNS[1:]:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def _sanitize_input_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {str(k): _sanitize_input_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_input_value(key, item) for item in value]
    return value


def build_prompt_snapshot(resources: dict[str, Any], inputs: dict[str, Any], rendered_prompt: str) -> dict[str, Any]:
    frontmatter = resources.get("template_frontmatter") or {}
    return {
        "template_name": frontmatter.get("name"),
        "template_version": frontmatter.get("version"),
        "template_git_sha": resources.get("template_git_sha"),
        "persona": resources.get("persona_name", "jarvis"),
        "inputs_resolved": {str(k): _sanitize_input_value(str(k), v) for k, v in inputs.items()},
        "rendered_prompt": sanitize_text(rendered_prompt),
    }
