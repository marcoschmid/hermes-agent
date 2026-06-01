"""Regression guard for tools.mcp_oauth._write_json — the OAuth token store
must be created with 0o600 from the start (atomic), never transiently
world/group readable, even under a permissive umask.

F4: _write_json must also use a unique per-write temp file (not a shared
fixed-name temp that a concurrent writer for the same path can unlink out from
under another writer), and replace atomically."""

import json
import os
import stat
import threading

from tools.mcp_oauth import _write_json


def test_write_json_is_0600_under_permissive_umask(tmp_path):
    old = os.umask(0o000)
    try:
        path = tmp_path / "sub" / "tokens.json"
        _write_json(path, {"access_token": "secret"})
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)
        # No leftover temp file with loose permissions.
        assert not (tmp_path / "sub" / "tokens.tmp").exists()
    finally:
        os.umask(old)


def test_write_json_no_fixed_shared_temp_leftover(tmp_path):
    """The fixed-name `<stem>.tmp` sibling must never be the temp used, so a
    concurrent writer cannot unlink it mid-write."""
    path = tmp_path / "tokens.json"
    _write_json(path, {"access_token": "secret"})
    assert not (tmp_path / "tokens.tmp").exists()
    assert path.exists()
    # No stray *.tmp left behind from a successful write.
    assert not list(tmp_path.glob("*.tmp"))


def test_write_json_concurrent_writers_same_path(tmp_path):
    """Many threads writing the SAME path must all complete without a writer
    deleting another's temp file (FileNotFoundError) or corrupting the store."""
    path = tmp_path / "tokens.json"
    errors = []

    def writer(i):
        try:
            for _ in range(40):
                _write_json(path, {"access_token": f"tok-{i}"})
        except Exception as exc:  # noqa: BLE001 — record, assert later
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # Final store must be valid JSON written by exactly one of the writers.
    data = json.loads(path.read_text())
    assert data["access_token"].startswith("tok-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_write_json_crash_midwrite_preserves_prior(tmp_path, monkeypatch):
    """If serialization fails mid-write, the existing store is untouched and no
    temp file is left behind (atomic replace, not in-place truncation)."""
    path = tmp_path / "tokens.json"
    _write_json(path, {"access_token": "original"})

    class _Unserializable:
        pass

    # json.dumps(default=str) stringifies unknown objects, so force a failure
    # with a key that cannot be encoded.
    try:
        _write_json(path, {object(): _Unserializable()})
    except (TypeError, ValueError):
        pass

    assert json.loads(path.read_text())["access_token"] == "original"
    assert not list(tmp_path.glob("*.tmp"))
