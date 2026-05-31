"""Regression guard for tools.mcp_oauth._write_json — the OAuth token store
must be created with 0o600 from the start (atomic), never transiently
world/group readable, even under a permissive umask."""

import os
import stat

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
