"""R3-1/R3-2 regression: the secret-store guard must cover ALL HERMES_HOME
credential stores (not just auth.json/.env/mcp-tokens), via one central
registry shared by the read and write guards — and search filtering must
resolve relative parent (`..`) match paths correctly.

Credential stores discovered in the codebase:
  auth.json                       (hermes_cli/auth.py, auxiliary_client.py)
  .env                            (hermes_cli/config.py)
  mcp-tokens/                     (tools/mcp_oauth.py)
  .anthropic_oauth.json           (agent/anthropic_adapter.py:1019)
  auth/google_oauth.json          (agent/google_oauth.py:157)
  slack_tokens.json               (gateway/platforms/slack.py:398)
  secrets/notify-token            (gateway/paperclip_notify.py:37)
  whatsapp/session/creds.json     (hermes_cli/gateway.py:2838)
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import get_hermes_home

from agent.file_safety import get_read_block_error, is_write_denied
from tools.file_tools import search_tool, _read_tracker
from tools.file_operations import SearchResult, SearchMatch


READ_BLOCKED_STORES = [
    "auth.json",
    ".env",
    "mcp-tokens/some-server.json",
    ".anthropic_oauth.json",
    "auth/google_oauth.json",
    "slack_tokens.json",
    "secrets/notify-token",
    "whatsapp/session/creds.json",
    "weixin/accounts/acct123.json",
]

SECRET_STORES = [*READ_BLOCKED_STORES, "config.yaml"]

# Secret directory roots themselves must be write-denied (else a write at the
# dir path on a fresh profile blocks future credential persistence).
SECRET_DIR_ROOTS = ["mcp-tokens", "secrets", "weixin/accounts"]

BENIGN_STORES = [
    "models_dev_cache.json",
    "channel_directory.json",
    "sticker_cache.json",
    "sessions/sessions.json",
    "feishu_comment_rules.json",
]

READABLE_STORES = ["config.yaml", *BENIGN_STORES]


class TestSecretStoreRegistryRead:
    @pytest.mark.parametrize("rel", READ_BLOCKED_STORES)
    def test_read_blocked(self, rel):
        path = str(get_hermes_home() / rel)
        assert get_read_block_error(path) is not None, rel


class TestSecretStoreRegistryWrite:
    @pytest.mark.parametrize("rel", SECRET_STORES)
    def test_write_denied(self, rel):
        path = str(get_hermes_home() / rel)
        assert is_write_denied(path) is True, rel


class TestSecretDirRootsWriteDenied:
    @pytest.mark.parametrize("rel", SECRET_DIR_ROOTS)
    def test_dir_root_write_denied(self, rel):
        # Exact directory path (no trailing slash) must be write-denied too.
        path = str(get_hermes_home() / rel)
        assert is_write_denied(path) is True, rel


class TestSecretStoreRegistryAllowed:
    @pytest.mark.parametrize("rel", READABLE_STORES)
    def test_read_allowed(self, rel):
        path = str(get_hermes_home() / rel)
        assert get_read_block_error(path) is None, rel

    @pytest.mark.parametrize("rel", BENIGN_STORES)
    def test_write_allowed(self, rel):
        path = str(get_hermes_home() / rel)
        assert is_write_denied(path) is False, rel


def _fake_ops(result):
    fake = MagicMock()
    fake.search = MagicMock(return_value=result)
    return fake


class TestSearchParentPathResolution:
    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_parent_relative_match_filtered(self, mock_ops, monkeypatch):
        # cwd = HERMES_HOME/sub, search path='..' -> rg reports a hit in
        # HERMES_HOME/auth.json as '../auth.json' relative to the command cwd.
        home = get_hermes_home()
        monkeypatch.setenv("TERMINAL_CWD", str(home / "sub"))
        result = SearchResult(
            matches=[SearchMatch(path="../auth.json", line_number=1,
                                 content='"token":"PARENTLEAK0001"')],
            total_count=1,
        )
        mock_ops.return_value = _fake_ops(result)
        out = json.loads(search_tool(pattern=".", path="..", target="content",
                                     task_id="s_parent"))
        assert "PARENTLEAK0001" not in json.dumps(out)
