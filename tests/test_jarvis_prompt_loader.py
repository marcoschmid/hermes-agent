import subprocess
from pathlib import Path

import pytest


def _write_template(root: Path, name: str = "task-execution") -> None:
    templates = root / "prompt-templates"
    templates.mkdir(parents=True)
    (templates / f"{name}.md").write_text(
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

Template body for {{task}}.
""",
        encoding="utf-8",
    )


def _write_persona(root: Path, persona: str = "jarvis") -> None:
    persona_dir = root / "personas" / persona
    persona_dir.mkdir(parents=True)
    (persona_dir / "PERSONA.md").write_text("# Jarvis\n\nDeutsch, du-Form.\n", encoding="utf-8")
    (persona_dir / "style.md").write_text("# Style\n\nKurz und klar.\n", encoding="utf-8")
    (persona_dir / "metadata.yaml").write_text("persona_id: jarvis\nversion: 0.1.0\n", encoding="utf-8")


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    _write_template(root)
    _write_persona(root)
    return root


def test_loads_template_and_default_persona(workspace_root: Path) -> None:
    from agent.jarvis_prompt_loader import load_prompt_resources

    resources = load_prompt_resources(str(workspace_root), "task-execution")

    assert resources["template_frontmatter"]["name"] == "task-execution"
    assert resources["template_frontmatter"]["version"] == "0.1.0"
    assert "Template body" in resources["template"]
    assert "Deutsch, du-Form" in resources["persona"]
    assert "Kurz und klar" in resources["style"]


def test_rejects_unknown_template(workspace_root: Path) -> None:
    from agent.jarvis_prompt_loader import load_prompt_resources

    with pytest.raises(FileNotFoundError, match="Unknown prompt template"):
        load_prompt_resources(str(workspace_root), "missing")


def test_rejects_unknown_persona(workspace_root: Path) -> None:
    from agent.jarvis_prompt_loader import load_prompt_resources

    with pytest.raises(FileNotFoundError, match="Unknown persona"):
        load_prompt_resources(str(workspace_root), "task-execution", persona="missing")


def test_resolves_template_git_sha_from_workspace_git(workspace_root: Path) -> None:
    subprocess.run(["git", "init"], cwd=workspace_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=workspace_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace_root, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace_root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=workspace_root, check=True, capture_output=True)
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace_root, text=True).strip()

    from agent.jarvis_prompt_loader import load_prompt_resources

    resources = load_prompt_resources(str(workspace_root), "task-execution")

    assert resources["template_git_sha"] == expected
