"""F2 regression: the V4A patch path must not read or write HERMES_HOME secret
stores.

Two layers are exercised (with mocks — no real subprocess/patch machinery):

1. ShellFileOperations.read_file_raw — used by _validate_operations BEFORE any
   write-deny check. Without a guard it reads the secret (and a near-miss hunk
   echoes verbatim source lines back to the model via the fuzzy match hint).
   The guard must short-circuit before any shell exec.

2. patch_tool entry guard — must reject Update/Add/Delete/MOVE operations whose
   path is a secret store, before parsing/validation (patch_v4a). The original
   entry regex did not even recognize ``*** Move File:``.
"""

import json
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home

from tools.file_operations import ShellFileOperations
from tools.file_tools import patch_tool


def _exec_returning(content):
    """Fake ShellFileOperations._exec: wc -c -> length, everything else -> content."""
    def _e(command, cwd=None, timeout=None, stdin_data=None):
        r = MagicMock()
        r.exit_code = 0
        r.stdout = str(len(content)) if command.strip().startswith("wc -c") else content
        return r
    return _e


def _ops_with_exec(content):
    ops = ShellFileOperations(MagicMock())
    ops._exec = MagicMock(side_effect=_exec_returning(content))
    return ops


class TestReadFileRawBlocksSecrets:
    def test_read_file_raw_auth_json_blocked(self):
        ops = _ops_with_exec('{"access_token":"RAWLEAK0001"}')
        res = ops.read_file_raw(str(get_hermes_home() / "auth.json"))
        assert res.error is not None
        assert "RAWLEAK0001" not in (res.content or "")

    def test_read_file_raw_env_blocked(self):
        ops = _ops_with_exec("OPENAI_API_KEY=sk-RAWLEAK0002\n")
        res = ops.read_file_raw(str(get_hermes_home() / ".env"))
        assert res.error is not None
        assert "RAWLEAK0002" not in (res.content or "")

    def test_read_file_raw_mcp_token_blocked(self):
        ops = _ops_with_exec('{"token":"RAWLEAK0003"}')
        res = ops.read_file_raw(str(get_hermes_home() / "mcp-tokens" / "srv.json"))
        assert res.error is not None
        assert "RAWLEAK0003" not in (res.content or "")

    def test_read_file_raw_normal_file_allowed(self):
        ops = _ops_with_exec("print('hello raw')\n")
        res = ops.read_file_raw("/tmp/proj/main.py")
        assert res.error is None
        assert "hello raw" in res.content

    def test_read_file_raw_config_yaml_allowed(self):
        # config.yaml is intentionally agent-readable in this fork.
        ops = _ops_with_exec("model: gpt\n")
        res = ops.read_file_raw(str(get_hermes_home() / "config.yaml"))
        assert res.error is None
        assert "model: gpt" in res.content


def _fake_patch_ops():
    fake = MagicMock()
    fake.patch_v4a.return_value = MagicMock(to_dict=lambda: {"success": True})
    return fake


class TestPatchEntryBlocksSecrets:
    @patch("tools.file_tools._get_file_ops")
    def test_v4a_update_env_blocked(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        env = get_hermes_home() / ".env"
        patch_str = (
            "*** Begin Patch\n"
            f"*** Update File: {env}\n"
            "@@\n-OPENAI_API_KEY=sk-WRONG\n+OPENAI_API_KEY=sk-x\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_env"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_update_auth_json_blocked(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        auth = get_hermes_home() / "auth.json"
        patch_str = (
            "*** Begin Patch\n"
            f"*** Update File: {auth}\n"
            "@@\n-a\n+b\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_auth"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_delete_secret_blocked(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        auth = get_hermes_home() / "auth.json"
        patch_str = f"*** Begin Patch\n*** Delete File: {auth}\n*** End Patch"
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_del"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_move_secret_src_blocked(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        auth = get_hermes_home() / "auth.json"
        patch_str = (
            "*** Begin Patch\n"
            f"*** Move File: {auth} -> /tmp/exfil_auth.json\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_move"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_move_secret_dest_blocked(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        dest = get_hermes_home() / "auth.json"
        patch_str = (
            "*** Begin Patch\n"
            f"*** Move File: /tmp/whatever.json -> {dest}\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_movedst"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_update_no_space_marker_blocked(self, mock_ops):
        # The parser accepts `***Update File:` (zero whitespace after ***);
        # the entry guard must too, or the secret slips past.
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        env = get_hermes_home() / ".env"
        patch_str = (
            "*** Begin Patch\n"
            f"***Update File: {env}\n"
            "@@\n-a\n+b\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_nospace"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_move_no_space_marker_blocked(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        auth = get_hermes_home() / "auth.json"
        patch_str = (
            "*** Begin Patch\n"
            f"***Move File: {auth} -> /tmp/exfil.json\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_movenospace"))
        assert "error" in result
        fake.patch_v4a.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_v4a_normal_update_still_works(self, mock_ops):
        fake = _fake_patch_ops()
        mock_ops.return_value = fake
        patch_str = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/proj/main.py\n"
            "@@\n-a\n+b\n"
            "*** End Patch"
        )
        result = json.loads(patch_tool(mode="patch", patch=patch_str, task_id="f2_ok"))
        assert "error" not in result
        fake.patch_v4a.assert_called_once()
