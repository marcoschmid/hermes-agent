"""Load Jarvis prompt templates and persona resources from the workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path}: unterminated frontmatter")

    raw = text[4:end]
    body = text[end + 5 :]
    data: dict[str, Any] = {}
    current_key: str | None = None

    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(line[4:].strip())
            continue
        if ":" not in line:
            raise ValueError(f"{path}: unsupported frontmatter line: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        data[key] = value if value else []

    return data, body


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(line[4:].strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value in {"true", "false"}:
            data[key] = value == "true"
        elif value:
            data[key] = value
        else:
            data[key] = []
    return data


def _template_git_sha(workspace_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(workspace_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def load_prompt_resources(workspace_root: str, template_name: str, persona: str = "jarvis") -> dict[str, Any]:
    """Load a prompt template, persona, style, and workspace git SHA.

    The loader is read-only. It does not render prompts and does not write run
    snapshots; callers decide whether to opt into the resource layer.
    """
    root = Path(workspace_root).expanduser().resolve()
    template_path = root / "prompt-templates" / f"{template_name}.md"
    if not template_path.is_file():
        raise FileNotFoundError(f"Unknown prompt template: {template_name}")

    persona_dir = root / "personas" / persona
    persona_path = persona_dir / "PERSONA.md"
    style_path = persona_dir / "style.md"
    metadata_path = persona_dir / "metadata.yaml"
    if not persona_path.is_file():
        raise FileNotFoundError(f"Unknown persona: {persona}")
    if not style_path.is_file():
        raise FileNotFoundError(f"Unknown persona style: {persona}")

    template_text = template_path.read_text(encoding="utf-8")
    frontmatter, template_body = _parse_frontmatter(template_text, template_path)
    metadata = _parse_simple_yaml(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}

    return {
        "template": template_body,
        "template_frontmatter": frontmatter,
        "template_git_sha": _template_git_sha(root),
        "template_path": str(template_path),
        "persona": persona_path.read_text(encoding="utf-8"),
        "style": style_path.read_text(encoding="utf-8"),
        "persona_metadata": metadata,
        "persona_name": persona,
    }
