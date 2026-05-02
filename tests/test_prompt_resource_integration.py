from pathlib import Path


def _write_workspace(root: Path) -> None:
    template_dir = root / "prompt-templates"
    template_dir.mkdir(parents=True)
    (template_dir / "task-execution.md").write_text(
        """---
name: task-execution
version: 0.1.0
inputs:
  - task
schema_ref: _schema.json
golden_set: golden/task-execution.jsonl
risk: medium
owner: jarvis-os-redesign
---

Task template for {{task}}.
""",
        encoding="utf-8",
    )
    persona_dir = root / "personas" / "jarvis"
    persona_dir.mkdir(parents=True)
    (persona_dir / "PERSONA.md").write_text("# Jarvis\n\nDeutsch.\n", encoding="utf-8")
    (persona_dir / "style.md").write_text("# Style\n\nKurz.\n", encoding="utf-8")
    (persona_dir / "metadata.yaml").write_text("persona_id: jarvis\n", encoding="utf-8")


def test_prompt_resources_flag_absent_is_noop(tmp_path: Path, monkeypatch) -> None:
    _write_workspace(tmp_path)
    monkeypatch.delenv("JARVIS_PROMPT_RESOURCES", raising=False)

    from agent.prompt_builder import build_prompt_resource_context

    context, snapshot = build_prompt_resource_context(
        workspace_root=str(tmp_path),
        template_name="task-execution",
        inputs={"task": "deploy"},
    )

    assert context == ""
    assert snapshot is None


def test_prompt_resources_flag_loads_metadata_and_snapshot(tmp_path: Path, monkeypatch) -> None:
    _write_workspace(tmp_path)
    monkeypatch.setenv("JARVIS_PROMPT_RESOURCES", "1")

    from agent.prompt_builder import build_prompt_resource_context

    context, snapshot = build_prompt_resource_context(
        workspace_root=str(tmp_path),
        template_name="task-execution",
        inputs={"task": "deploy", "api_key": "sk-live-secret"},
    )

    assert "Jarvis Prompt Resources" in context
    assert "task-execution@0.1.0" in context
    assert "persona=jarvis" in context
    assert snapshot is not None
    assert snapshot["template_name"] == "task-execution"
    assert snapshot["persona"] == "jarvis"
    assert snapshot["inputs_resolved"]["api_key"] == "[REDACTED]"
