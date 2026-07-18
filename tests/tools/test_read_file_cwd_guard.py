"""F3 regression: read_file_tool must apply the secret-store read guard to the
TASK-RESOLVED path, not the raw caller string.

read_file_tool resolves the target with _resolve_path_for_task (which follows
the live terminal cwd), but the guard was called on the unresolved caller
string, which get_read_block_error resolves against the Hermes PROCESS cwd.
When the terminal cwd is HERMES_HOME (the gateway/daemon deployment), a
relative `read_file("auth.json")` slipped past the guard and was then read
from the terminal cwd."""

import json
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home

from tools.file_tools import read_file_tool, _read_tracker


class _FakeReadResult:
    def __init__(self, content):
        self.content = content

    def to_dict(self):
        return {"content": self.content, "total_lines": 1,
                "file_size": len(self.content)}


def _fake_ops(content):
    fake = MagicMock()
    fake.read_file = lambda path, offset=1, limit=500: _FakeReadResult(content)
    return fake


class TestReadFileCwdGuard:
    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_relative_auth_json_blocked_via_terminal_cwd(self, mock_ops, monkeypatch):
        mock_ops.return_value = _fake_ops("FAKE_TOKEN=cwdleak0001")
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(read_file_tool("auth.json", task_id="f3_auth"))
        assert "error" in result
        assert "Defense-in-depth" in result["error"]
        assert "cwdleak0001" not in json.dumps(result)

    @patch("tools.file_tools._get_file_ops")
    def test_relative_dotenv_blocked_via_terminal_cwd(self, mock_ops, monkeypatch):
        mock_ops.return_value = _fake_ops("OPENAI_API_KEY=sk-cwdleak0002")
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(read_file_tool(".env", task_id="f3_env"))
        assert "error" in result
        assert "Defense-in-depth" in result["error"]
        assert "cwdleak0002" not in json.dumps(result)

    @patch("tools.file_tools._get_file_ops")
    def test_relative_mcp_token_blocked_via_terminal_cwd(self, mock_ops, monkeypatch):
        mock_ops.return_value = _fake_ops("cwdleak0003")
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        result = json.loads(read_file_tool("mcp-tokens/srv.json", task_id="f3_tok"))
        assert "error" in result
        assert "Defense-in-depth" in result["error"]

    @patch("tools.file_tools._get_file_ops")
    def test_absolute_secret_still_blocked(self, mock_ops, monkeypatch):
        # Parity check: absolute path must remain blocked (existing behavior).
        mock_ops.return_value = _fake_ops("FAKE_TOKEN=abs0004")
        result = json.loads(
            read_file_tool(str(get_hermes_home() / "auth.json"), task_id="f3_abs")
        )
        assert "error" in result
        assert "Defense-in-depth" in result["error"]

    @patch("tools.file_tools._get_file_ops")
    def test_relative_normal_file_still_allowed(self, mock_ops, monkeypatch, tmp_path):
        # A legitimate relative read from a normal terminal cwd must still work.
        mock_ops.return_value = _fake_ops("print('ok')\n")
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        result = json.loads(read_file_tool("main.py", task_id="f3_ok"))
        assert "error" not in result
        assert "content" in result
