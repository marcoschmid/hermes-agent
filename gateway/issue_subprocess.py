"""Hardened subprocess spawn for Hermes issue-runs (Jarvis-OS Phase-4 4b-2).

Per Marco-Decision 4D-2 (Privilege-Separation) und 4D-8 (Hermes nutzt API-Pfad
nicht Direct-DB), spawnen wir Issue-Execution-Children mit:

- minimaler env-Allowlist (kein OPENAI_REFRESH_TOKEN, kein ANTHROPIC_API_KEY
  Leak vom Marco-Shell, kein CODEX_HOME, kein MC_AGENT_TOKEN)
- absoluten Argv-Listen (nie shell=True, nie shell-Interpolation)
- absolutem cwd unter erlaubtem Root (issuer-Project-Workspace)
- stdout/stderr Truncation + Secret-Redaction vor Run-Log-Persistierung

Diese Funktionen sind reine Helfer; der eigentliche Aufruf in
PaperclipIssueRunner kann gezielt darauf zugreifen oder weiterhin direkt
subprocess nutzen, solange er die gleichen Garantien einhaelt.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "HERMES_HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "PAPERCLIP_API_URL",
    "PAPERCLIP_API_TOKEN",
    "PAPERCLIP_ISSUE_RUN_TOKEN",
)

FORBIDDEN_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_REFRESH_TOKEN",
        "CODEX_HOME",
        "CODEX_API_KEY",
        "MC_AGENT_TOKEN",
        "MC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
    }
)

MAX_STREAM_BYTES = 64 * 1024

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("redacted:bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE)),
    ("redacted:openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("redacted:anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("redacted:authentication", re.compile(r"(?i)authorization:\s*\S+")),
    ("redacted:long-token", re.compile(r"\b[a-f0-9]{40,}\b")),
)


class SubprocessSpawnError(RuntimeError):
    """Raised when the spawn precondition fails (bad argv, bad cwd, etc.)."""


@dataclass(frozen=True)
class SpawnEnvPolicy:
    allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    extra_keys: tuple[str, ...] = ()

    def filter(self, source: Optional[dict[str, str]] = None) -> dict[str, str]:
        if source is None:
            source = dict(os.environ)
        keys = set(self.allowlist) | set(self.extra_keys)
        filtered = {k: source[k] for k in keys if k in source}
        forbidden_hits = FORBIDDEN_ENV_KEYS & filtered.keys()
        if forbidden_hits:
            raise SubprocessSpawnError(
                f"forbidden env keys leaked into filter result: {sorted(forbidden_hits)}"
            )
        return filtered


def assert_safe_argv(argv: Sequence[str]) -> None:
    """Validate the argv-list for the hardened spawn contract."""
    if not argv:
        raise SubprocessSpawnError("argv must not be empty")
    head, *rest = argv
    if not isinstance(head, str) or not head.startswith("/"):
        raise SubprocessSpawnError(f"argv[0] must be an absolute path, got {head!r}")
    for arg in rest:
        if not isinstance(arg, str):
            raise SubprocessSpawnError(f"argv item must be str, got {type(arg).__name__}")
    if not Path(head).is_file():
        raise SubprocessSpawnError(f"argv[0] is not an existing file: {head}")


def assert_safe_cwd(cwd: str, allowed_root: str) -> None:
    """Validate the cwd is an absolute path under allowed_root."""
    cwd_p = Path(cwd)
    root_p = Path(allowed_root)
    if not cwd_p.is_absolute():
        raise SubprocessSpawnError(f"cwd must be absolute, got {cwd!r}")
    if not cwd_p.is_dir():
        raise SubprocessSpawnError(f"cwd is not a directory: {cwd}")
    try:
        cwd_p.resolve().relative_to(root_p.resolve())
    except ValueError as exc:
        raise SubprocessSpawnError(f"cwd {cwd!r} not under allowed root {allowed_root!r}") from exc


def spawn_hardened(
    argv: Sequence[str],
    *,
    cwd: str,
    allowed_cwd_root: str,
    env_policy: Optional[SpawnEnvPolicy] = None,
    env_overrides: Optional[dict[str, str]] = None,
    stdout: Optional[int] = subprocess.PIPE,
    stderr: Optional[int] = subprocess.PIPE,
) -> subprocess.Popen[bytes]:
    """Spawn a child with the Phase-4 4b-2 hardening contract."""
    assert_safe_argv(argv)
    assert_safe_cwd(cwd, allowed_cwd_root)

    policy = env_policy or SpawnEnvPolicy()
    env = policy.filter()
    if env_overrides:
        for key, value in env_overrides.items():
            if key in FORBIDDEN_ENV_KEYS:
                raise SubprocessSpawnError(f"override key {key!r} is forbidden")
            env[key] = value

    return subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        shell=False,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
    )


def truncate_stream(data: bytes, *, max_bytes: int = MAX_STREAM_BYTES) -> bytes:
    """Truncate a captured stream blob to max_bytes with a marker."""
    if len(data) <= max_bytes:
        return data
    marker = f"\n…[truncated {len(data) - max_bytes} bytes]\n".encode("utf-8")
    return data[:max_bytes] + marker


def redact_secrets(text: str) -> str:
    """Best-effort secret-redaction for stdout/stderr / log lines."""
    redacted = text
    for replacement, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
