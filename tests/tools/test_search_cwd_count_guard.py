"""R2-F3 regression: search_files secret filtering must be cwd-correct and must
also cover counts / total_count.

Two gaps from the first search fix:
1. The guard and the match-path filter resolved relative paths against the
   PROCESS cwd, not the task terminal cwd — so a search with path='.' from a
   terminal cwd of HERMES_HOME still surfaced auth.json.
2. Only matches/files were filtered; `counts` and `total_count` survived, so
   output_mode='count' was a blind regex oracle over secret files."""

import json
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home

from tools.file_tools import search_tool, _read_tracker
from tools.file_operations import SearchResult, SearchMatch


def _fake_ops(result):
    fake = MagicMock()
    fake.search = MagicMock(return_value=result)
    return fake


class TestSearchCwdMatchFilter:
    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_relative_match_under_hermes_home_cwd_filtered(self, mock_ops, monkeypatch):
        # Match path is RELATIVE to the search root; with terminal cwd =
        # HERMES_HOME, './auth.json' resolves to the secret store.
        result = SearchResult(
            matches=[
                SearchMatch(path="auth.json", line_number=1,
                            content='"token":"CWDLEAK0001"'),
                SearchMatch(path="notes.txt", line_number=1, content="benign"),
            ],
            total_count=2,
        )
        mock_ops.return_value = _fake_ops(result)
        monkeypatch.setenv("TERMINAL_CWD", str(get_hermes_home()))
        out = json.loads(search_tool(pattern=".", path=".", target="content",
                                     task_id="s_cwd"))
        assert "CWDLEAK0001" not in json.dumps(out)
        assert "benign" in json.dumps(out)


class TestSearchCountModeOracle:
    def setup_method(self):
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops")
    def test_count_mode_drops_secret_paths_and_recomputes_total(self, mock_ops):
        home = get_hermes_home()
        result = SearchResult(
            matches=[],
            files=[],
            counts={str(home / "auth.json"): 5, "/tmp/project/main.py": 2},
            total_count=7,
        )
        mock_ops.return_value = _fake_ops(result)
        out = json.loads(search_tool(pattern="api_key", path=str(home),
                                     target="content", output_mode="count",
                                     task_id="s_count"))
        blob = json.dumps(out)
        assert "auth.json" not in blob
        # The non-secret count survives; the secret count (5) must not leak.
        assert out.get("total_count") == 2, out

    @patch("tools.file_tools._get_file_ops")
    def test_count_mode_normal_paths_unaffected(self, mock_ops):
        result = SearchResult(
            matches=[],
            files=[],
            counts={"/tmp/a.py": 3, "/tmp/b.py": 4},
            total_count=7,
        )
        mock_ops.return_value = _fake_ops(result)
        out = json.loads(search_tool(pattern="x", path="/tmp", target="content",
                                     output_mode="count", task_id="s_count_ok"))
        assert out.get("total_count") == 7
