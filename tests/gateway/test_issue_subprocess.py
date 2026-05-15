"""Tests for gateway.issue_subprocess (Jarvis-OS Phase-4 4b-2)."""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

import pytest

from gateway.issue_subprocess import (
    DEFAULT_ENV_ALLOWLIST,
    FORBIDDEN_ENV_KEYS,
    MAX_STREAM_BYTES,
    SpawnEnvPolicy,
    SubprocessSpawnError,
    assert_safe_argv,
    assert_safe_cwd,
    redact_secrets,
    spawn_hardened,
    truncate_stream,
)


# ----- env-allowlist -------------------------------------------------------


def test_env_policy_filters_unknown_keys():
    src = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "FOO_BAR": "leak",
        "RANDOM_KEY": "drop",
    }
    out = SpawnEnvPolicy().filter(src)
    assert "PATH" in out
    assert "HOME" in out
    assert "FOO_BAR" not in out
    assert "RANDOM_KEY" not in out


def test_env_policy_drops_forbidden_keys_silently():
    src = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-leak",
        "ANTHROPIC_API_KEY": "sk-ant-leak",
    }
    out = SpawnEnvPolicy().filter(src)
    assert out == {"PATH": "/usr/bin"}


def test_env_policy_extra_keys():
    policy = SpawnEnvPolicy(extra_keys=("CUSTOM_X",))
    out = policy.filter({"PATH": "/usr/bin", "CUSTOM_X": "v"})
    assert out["CUSTOM_X"] == "v"


def test_default_allowlist_includes_paperclip_keys():
    assert "PAPERCLIP_API_URL" in DEFAULT_ENV_ALLOWLIST
    assert "PAPERCLIP_API_TOKEN" in DEFAULT_ENV_ALLOWLIST
    assert "PAPERCLIP_ISSUE_RUN_TOKEN" in DEFAULT_ENV_ALLOWLIST


def test_forbidden_set_covers_known_leak_keys():
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_REFRESH_TOKEN",
                "MC_AGENT_TOKEN", "GITHUB_TOKEN"):
        assert key in FORBIDDEN_ENV_KEYS


# ----- argv validation -----------------------------------------------------


def test_argv_must_be_non_empty():
    with pytest.raises(SubprocessSpawnError, match="empty"):
        assert_safe_argv([])


def test_argv_first_must_be_absolute(tmp_path):
    with pytest.raises(SubprocessSpawnError, match="absolute"):
        assert_safe_argv(["ls"])


def test_argv_first_must_exist(tmp_path):
    with pytest.raises(SubprocessSpawnError, match="not an existing file"):
        assert_safe_argv(["/does/not/exist/binary"])


def test_argv_accepts_valid_absolute_path():
    binary = sys.executable
    assert_safe_argv([binary, "--version"])


def test_argv_rejects_non_string_item():
    with pytest.raises(SubprocessSpawnError, match="must be str"):
        assert_safe_argv([sys.executable, 42])  # type: ignore[list-item]


# ----- cwd validation ------------------------------------------------------


def test_cwd_must_be_absolute(tmp_path):
    with pytest.raises(SubprocessSpawnError, match="absolute"):
        assert_safe_cwd("relative/path", str(tmp_path))


def test_cwd_must_exist(tmp_path):
    with pytest.raises(SubprocessSpawnError, match="not a directory"):
        assert_safe_cwd(str(tmp_path / "nope"), str(tmp_path))


def test_cwd_must_be_under_root(tmp_path):
    sibling = tmp_path / "sibling"
    other = tmp_path / "other"
    sibling.mkdir()
    other.mkdir()
    with pytest.raises(SubprocessSpawnError, match="not under allowed root"):
        assert_safe_cwd(str(sibling), str(other))


def test_cwd_accepts_valid(tmp_path):
    sub = tmp_path / "ws"
    sub.mkdir()
    assert_safe_cwd(str(sub), str(tmp_path))


# ----- spawn_hardened ------------------------------------------------------


def test_spawn_hardened_runs_command_with_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    monkeypatch.setenv("HOME", str(tmp_path))

    proc = spawn_hardened(
        [sys.executable, "-c", "import os; print(sorted(os.environ.keys()))"],
        cwd=str(tmp_path),
        allowed_cwd_root=str(tmp_path),
    )
    stdout, _ = proc.communicate(timeout=10)
    text = stdout.decode("utf-8")
    assert "OPENAI_API_KEY" not in text
    assert "PATH" in text
    assert "HOME" in text


def test_spawn_hardened_rejects_forbidden_override(tmp_path):
    with pytest.raises(SubprocessSpawnError, match="forbidden"):
        spawn_hardened(
            [sys.executable, "--version"],
            cwd=str(tmp_path),
            allowed_cwd_root=str(tmp_path),
            env_overrides={"OPENAI_API_KEY": "sk-leak"},
        )


def test_spawn_hardened_passes_extra_env_override(tmp_path):
    proc = spawn_hardened(
        [sys.executable, "-c", "import os; print(os.environ.get('CUSTOM_VAR', 'MISSING'))"],
        cwd=str(tmp_path),
        allowed_cwd_root=str(tmp_path),
        env_overrides={"CUSTOM_VAR": "ok"},
    )
    stdout, _ = proc.communicate(timeout=10)
    assert b"ok" in stdout


def test_spawn_hardened_runs_with_shell_false_implicitly(tmp_path):
    proc = spawn_hardened(
        [sys.executable, "-c", "print('hello')"],
        cwd=str(tmp_path),
        allowed_cwd_root=str(tmp_path),
    )
    stdout, _ = proc.communicate(timeout=10)
    assert b"hello" in stdout


# ----- truncate_stream -----------------------------------------------------


def test_truncate_stream_passthrough_below_limit():
    data = b"x" * 100
    assert truncate_stream(data) == data


def test_truncate_stream_truncates_above_limit():
    data = b"a" * (MAX_STREAM_BYTES + 100)
    out = truncate_stream(data)
    assert len(out) <= MAX_STREAM_BYTES + 64
    assert out.startswith(b"a")
    assert b"truncated" in out


def test_truncate_stream_custom_max():
    data = b"x" * 50
    out = truncate_stream(data, max_bytes=10)
    assert out.startswith(b"x" * 10)
    assert b"truncated 40" in out


# ----- redact_secrets ------------------------------------------------------


def test_redact_bearer_token():
    text = "Authorization: Bearer eyJhbGciAAAAAAAAAAAAAA"
    out = redact_secrets(text)
    assert "eyJhbGci" not in out
    assert "redacted" in out


def test_redact_openai_key():
    text = "key is sk-1234567890abcdefghijklmnopqrstuv"
    out = redact_secrets(text)
    assert "sk-1234567890" not in out
    assert "redacted:openai-key" in out


def test_redact_anthropic_key():
    text = "got sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaa back"
    out = redact_secrets(text)
    assert "api03-aaa" not in out
    assert "redacted:anthropic-key" in out


def test_redact_long_hex_token():
    text = "session abc 1234567890abcdef1234567890abcdef1234567890 was created"
    out = redact_secrets(text)
    assert "1234567890abcdef" not in out
    assert "redacted:long-token" in out


def test_redact_preserves_innocent_text():
    text = "plain message with no secrets"
    assert redact_secrets(text) == text
