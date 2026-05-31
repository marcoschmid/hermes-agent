"""R2-F2 regression: write_file_tool must apply the write-deny guard to the
TASK-RESOLVED path, not the raw caller string.

write_file_tool resolves the target with _resolve_path_for_task but only uses
it for locking/staleness; it passed the original relative path to
file_ops.write_file, whose _is_write_denied resolves against the Python PROCESS
cwd. From a terminal cwd of HERMES_HOME, write_file('auth.json', ...) could
overwrite the secret store."""

import json
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home

from tools.file_tools import write_file_tool, _read_tracker


def _fake_write_ops():
    fake = MagicMock()
    fake.write_file = MagicMock(
        return_value=MagicMock(to_dict=lambda: {"success": True, "bytes_written": 3})
    )
    return fake


class TestWriteFileCwdGuard:
    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_relative_auth_json_write_blocked_via_terminal_cwd(self, mock_ops, monkeypatch):
        fake = _fake_write_ops()
        mock_ops.return_value = fake
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(write_file_tool("auth.json", "{}", task_id="w_auth"))
        assert "error" in result
        fake.write_file.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_relative_env_write_blocked_via_terminal_cwd(self, mock_ops, monkeypatch):
        fake = _fake_write_ops()
        mock_ops.return_value = fake
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(write_file_tool(".env", "X=1\n", task_id="w_env"))
        assert "error" in result
        fake.write_file.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_relative_mcp_token_write_blocked_via_terminal_cwd(self, mock_ops, monkeypatch):
        fake = _fake_write_ops()
        mock_ops.return_value = fake
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(write_file_tool("mcp-tokens/srv.json", "{}", task_id="w_tok"))
        assert "error" in result
        fake.write_file.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_absolute_secret_write_blocked(self, mock_ops, monkeypatch):
        fake = _fake_write_ops()
        mock_ops.return_value = fake
        result = json.loads(
            write_file_tool(str(get_hermes_home() / "auth.json"), "{}", task_id="w_abs")
        )
        assert "error" in result
        fake.write_file.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_relative_normal_file_write_allowed(self, mock_ops, monkeypatch, tmp_path):
        fake = _fake_write_ops()
        mock_ops.return_value = fake
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        result = json.loads(write_file_tool("main.py", "print('ok')\n", task_id="w_ok"))
        assert "error" not in result
        fake.write_file.assert_called_once()

    @patch("tools.file_tools._get_file_ops")
    def test_config_yaml_write_allowed(self, mock_ops, monkeypatch):
        # config.yaml is intentionally agent-writable in this fork.
        fake = _fake_write_ops()
        mock_ops.return_value = fake
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(write_file_tool("config.yaml", "model: gpt\n", task_id="w_cfg"))
        assert "error" not in result
        fake.write_file.assert_called_once()
