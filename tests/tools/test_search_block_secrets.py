"""F1 regression: search_files must not leak HERMES_HOME secret stores.

The agent process runs as the owner of HERMES_HOME, so OS permissions do not
protect its own secret files from the file tools. read_file already enforces
get_read_block_error; search_tool must enforce the same guard, both on the
requested path (explicit single-file search) and on returned match/file paths
(directory recursion can surface non-hidden secrets like auth.json or
mcp-tokens/*.json, which rg/grep do NOT exclude)."""

import json
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home

from tools.file_tools import search_tool, _read_tracker
from tools.file_operations import SearchResult, SearchMatch


def _fake_ops_returning(matches=None, files=None):
    """Fake file_ops whose .search returns attacker-visible secret content."""
    fake = MagicMock()

    def _search(pattern, path, target, file_glob, limit, offset, output_mode, context):
        return SearchResult(
            matches=list(matches or []),
            files=list(files or []),
            total_count=len(matches or []) + len(files or []),
        )

    fake.search = MagicMock(side_effect=_search)
    return fake


class TestSearchExplicitSecretFileBlocked:
    """Pointing `path` directly at a secret file must be refused."""

    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_search_env_content_blocked(self, mock_ops):
        secret = "OPENAI_API_KEY=sk-LEAKVALUE0001"
        fake = _fake_ops_returning(
            matches=[SearchMatch(path=str(get_hermes_home() / ".env"),
                                 line_number=1, content=secret)]
        )
        mock_ops.return_value = fake
        path = str(get_hermes_home() / ".env")
        result = json.loads(search_tool(pattern=".", path=path,
                                        target="content", task_id="f1_env"))
        assert "error" in result
        assert "secret store" in result["error"]
        assert "LEAKVALUE0001" not in json.dumps(result)
        fake.search.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_search_auth_json_blocked(self, mock_ops):
        fake = _fake_ops_returning(
            matches=[SearchMatch(path=str(get_hermes_home() / "auth.json"),
                                 line_number=1, content='"token":"LEAKAUTH0002"')]
        )
        mock_ops.return_value = fake
        path = str(get_hermes_home() / "auth.json")
        result = json.loads(search_tool(pattern=":", path=path,
                                        target="content", task_id="f1_auth"))
        assert "error" in result
        assert "LEAKAUTH0002" not in json.dumps(result)
        fake.search.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_search_mcp_token_file_blocked(self, mock_ops):
        fake = _fake_ops_returning(
            matches=[SearchMatch(path=str(get_hermes_home() / "mcp-tokens" / "srv.json"),
                                 line_number=1, content="LEAKTOK0003")]
        )
        mock_ops.return_value = fake
        path = str(get_hermes_home() / "mcp-tokens" / "srv.json")
        result = json.loads(search_tool(pattern=".", path=path,
                                        target="content", task_id="f1_tok"))
        assert "error" in result
        assert "LEAKTOK0003" not in json.dumps(result)
        fake.search.assert_not_called()

    @patch("tools.file_tools._get_file_ops")
    def test_search_proc_environ_blocked(self, mock_ops):
        fake = _fake_ops_returning(
            matches=[SearchMatch(path="/proc/self/environ", line_number=1,
                                 content="OPENAI_API_KEY=sk-LEAKENV0004")]
        )
        mock_ops.return_value = fake
        result = json.loads(search_tool(pattern=".", path="/proc/self/environ",
                                        target="content", task_id="f1_proc"))
        assert "error" in result
        assert "LEAKENV0004" not in json.dumps(result)
        fake.search.assert_not_called()


class TestSearchDirectoryRecursionFiltersSecrets:
    """A directory search that surfaces a (non-hidden) secret path must have
    that match/file dropped from the result before it reaches the model."""

    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_directory_match_on_auth_json_filtered(self, mock_ops):
        home = get_hermes_home()
        fake = _fake_ops_returning(
            matches=[
                SearchMatch(path=str(home / "auth.json"), line_number=1,
                            content='"token":"LEAKDIR0005"'),
                SearchMatch(path=str(home / "notes.txt"), line_number=1,
                            content="benign match"),
            ]
        )
        mock_ops.return_value = fake
        result = json.loads(search_tool(pattern=".", path=str(home),
                                        target="content", task_id="f1_dir"))
        assert "LEAKDIR0005" not in json.dumps(result)
        # The benign, non-secret match must survive.
        assert "benign match" in json.dumps(result)

    @patch("tools.file_tools._get_file_ops")
    def test_files_mode_excludes_secret_paths(self, mock_ops):
        home = get_hermes_home()
        fake = _fake_ops_returning(
            files=[str(home / "auth.json"),
                   str(home / "mcp-tokens" / "srv.json"),
                   str(home / "README.md")]
        )
        mock_ops.return_value = fake
        result = json.loads(search_tool(pattern="*.json", path=str(home),
                                        target="files", output_mode="files_only",
                                        task_id="f1_files"))
        blob = json.dumps(result)
        assert "auth.json" not in blob
        assert "mcp-tokens" not in blob
        assert "README.md" in blob


class TestSearchAllowed:
    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_normal_project_file_allowed(self, mock_ops):
        fake = _fake_ops_returning(
            matches=[SearchMatch(path="/tmp/proj/main.py", line_number=3,
                                 content="def hello(): ...")]
        )
        mock_ops.return_value = fake
        result = json.loads(search_tool(pattern="hello", path="/tmp/proj/main.py",
                                        target="content", task_id="f1_ok"))
        assert "error" not in result
        fake.search.assert_called_once()

    @patch("tools.file_tools._get_file_ops")
    def test_config_yaml_allowed(self, mock_ops):
        # config.yaml is intentionally agent-readable in this fork.
        fake = _fake_ops_returning(
            matches=[SearchMatch(path=str(get_hermes_home() / "config.yaml"),
                                 line_number=1, content="model: gpt")]
        )
        mock_ops.return_value = fake
        path = str(get_hermes_home() / "config.yaml")
        result = json.loads(search_tool(pattern="model", path=path,
                                        target="content", task_id="f1_cfg"))
        assert "error" not in result
        fake.search.assert_called_once()
