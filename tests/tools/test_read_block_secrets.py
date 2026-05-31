"""Tests for get_read_block_error() — verifies sensitive secret files cannot
be read via the agent file tools (defense-in-depth; the agent process runs as
the owner of HERMES_HOME, so OS permissions do not protect these files)."""

from hermes_constants import get_hermes_home

from agent.file_safety import get_read_block_error


class TestReadBlockSecrets:
    def test_auth_json_blocked(self):
        path = str(get_hermes_home() / "auth.json")
        assert get_read_block_error(path) is not None

    def test_env_blocked(self):
        path = str(get_hermes_home() / ".env")
        assert get_read_block_error(path) is not None

    def test_mcp_tokens_blocked(self):
        path = str(get_hermes_home() / "mcp-tokens" / "some-server.json")
        assert get_read_block_error(path) is not None

    def test_proc_self_environ_blocked(self):
        assert get_read_block_error("/proc/self/environ") is not None

    def test_proc_pid_environ_blocked(self):
        assert get_read_block_error("/proc/1234/environ") is not None

    def test_proc_task_environ_blocked(self):
        assert get_read_block_error("/proc/1234/task/5678/environ") is not None


class TestReadBlockAllowed:
    def test_normal_project_file_allowed(self):
        assert get_read_block_error("/tmp/some_project/main.py") is None

    def test_hermes_config_yaml_allowed(self):
        # config.yaml is intentionally NOT a secret-tier file in this fork
        # (it is agent-writable per test_write_deny.test_hermes_config_not_env);
        # reading it must stay allowed.
        path = str(get_hermes_home() / "config.yaml")
        assert get_read_block_error(path) is None
