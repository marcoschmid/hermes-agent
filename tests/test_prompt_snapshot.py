from agent.prompt_snapshot import build_prompt_snapshot


def test_builds_sanitized_prompt_snapshot() -> None:
    resources = {
        "template_frontmatter": {"name": "task-execution", "version": "0.1.0"},
        "template_git_sha": "abc123",
        "persona_name": "jarvis",
    }
    rendered_prompt = "Use access_token=tok_live_123 and refresh_token: secret-refresh"
    inputs = {"task": "deploy", "api_key": "sk-live-secret"}

    snapshot = build_prompt_snapshot(resources, inputs, rendered_prompt)

    assert snapshot["template_name"] == "task-execution"
    assert snapshot["template_version"] == "0.1.0"
    assert snapshot["template_git_sha"] == "abc123"
    assert snapshot["persona"] == "jarvis"
    assert snapshot["inputs_resolved"]["task"] == "deploy"
    assert snapshot["inputs_resolved"]["api_key"] == "[REDACTED]"
    assert "tok_live_123" not in snapshot["rendered_prompt"]
    assert "secret-refresh" not in snapshot["rendered_prompt"]
    assert "[REDACTED]" in snapshot["rendered_prompt"]


def test_snapshot_preserves_non_sensitive_inputs() -> None:
    resources = {
        "template_frontmatter": {"name": "daily-briefing", "version": "0.1.0"},
        "template_git_sha": None,
        "persona_name": "jarvis",
    }

    snapshot = build_prompt_snapshot(resources, {"date": "2026-05-02"}, "Briefing ohne Secrets")

    assert snapshot["inputs_resolved"] == {"date": "2026-05-02"}
    assert snapshot["rendered_prompt"] == "Briefing ohne Secrets"
