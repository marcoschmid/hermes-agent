"""Tests for the daemon-owned single-skill workspace rebaseline CLI."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from agent.skill_utils import is_excluded_skill_path
from tools import skills_sync
from tools import workspace_skills_rebaseline as rebaseline


@pytest.fixture
def rebaseline_state(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "workspace" / "skills"
    skill = source / "audits" / "web-audit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: web-audit\n---\n# Web Audit v1\n",
        encoding="utf-8",
    )
    target = tmp_path / "hermes" / "skills"
    manifest = target / ".workspace_manifest"
    skills_sync.sync_skills(
        quiet=True,
        source_dir=source,
        target_dir=target,
        manifest_file=manifest,
    )
    (skill / "SKILL.md").write_text(
        "---\nname: web-audit\n---\n# Web Audit v2\n",
        encoding="utf-8",
    )
    return {
        "source": source,
        "source_skill": skill,
        "target": target,
        "target_skill": target / "audits" / "web-audit",
        "manifest": manifest,
    }


@pytest.fixture
def legacy_rebaseline_state(
    rebaseline_state: dict[str, Path],
) -> dict[str, Path]:
    """Convert the synced v3 record to the exact historical v2 shape."""
    state = rebaseline_state
    record = skills_sync._read_manifest_records(state["manifest"])["web-audit"]
    state["manifest"].write_text(
        f"web-audit:{record['hash']}\n",
        encoding="utf-8",
    )
    state["manifest"].chmod(0o600)
    return state


def test_request_schema_is_explicit_and_content_addressed() -> None:
    try:
        rebaseline = importlib.import_module("tools.workspace_skills_rebaseline")
    except ModuleNotFoundError:
        rebaseline = None

    assert rebaseline is not None, "workspace rebaseline CLI is not implemented"
    assert rebaseline.REQUEST_FIELDS == {
        "schema_version",
        "skill_name",
        "expected_install_path",
        "expected_source_hash",
        "expected_target_hash",
        "expected_manifest_hash",
    }


def test_inspect_builds_exact_request_from_current_state(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state

    request = rebaseline.inspect_request(
        "web-audit",
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert set(request) == rebaseline.REQUEST_FIELDS
    assert request == {
        "schema_version": 3,
        "skill_name": "web-audit",
        "expected_install_path": "audits/web-audit",
        "expected_source_hash": rebaseline._tree_digest(state["source_skill"]),
        "expected_target_hash": rebaseline._tree_digest(state["target_skill"]),
        "expected_manifest_hash": hashlib.sha256(
            state["manifest"].read_bytes()
        ).hexdigest(),
    }


def test_legacy_v2_record_inspects_applies_and_migrates_only_requested_record(
    legacy_rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = legacy_rebaseline_state
    legacy_hash = skills_sync._read_manifest_records(state["manifest"])[
        "web-audit"
    ]["hash"]
    unrelated = {
        "hash": "1" * 32,
        "install_path": "foreign/unrelated",
        "provenance": {
            "kind": "workspace",
            "source_root": "/different/workspace",
        },
    }
    state["manifest"].write_text(
        "web-audit:"
        + legacy_hash
        + "\n"
        + "unrelated:"
        + json.dumps(unrelated, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    state["manifest"].chmod(0o600)
    manifest_before = state["manifest"].read_bytes()

    request = rebaseline.inspect_request(
        "web-audit",
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )
    assert request["expected_manifest_hash"] == hashlib.sha256(
        manifest_before
    ).hexdigest()
    assert request["expected_install_path"] == "audits/web-audit"
    request_file = tmp_path / "legacy-web-audit-rebaseline.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    request_file.chmod(0o600)

    result = rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert result["action"] == "rebaselined"
    records = skills_sync._read_manifest_records(state["manifest"])
    assert records["web-audit"] == {
        "hash": skills_sync._dir_hash(state["source_skill"]),
        "install_path": "audits/web-audit",
        "provenance": {
            "kind": "workspace",
            "source_root": str(state["source"].resolve()),
        },
    }
    assert records["unrelated"] == unrelated
    assert rebaseline._tree_digest(state["target_skill"]) == rebaseline._tree_digest(
        state["source_skill"]
    )


@pytest.mark.parametrize("changed", ["source", "target", "legacy_record"])
def test_legacy_request_cas_rejects_changed_bound_state(
    legacy_rebaseline_state: dict[str, Path],
    tmp_path: Path,
    changed: str,
) -> None:
    state = legacy_rebaseline_state
    request = rebaseline.inspect_request(
        "web-audit",
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )
    request_file = tmp_path / f"legacy-stale-{changed}.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    request_file.chmod(0o600)

    if changed == "source":
        (state["source_skill"] / "SKILL.md").write_text(
            "---\nname: web-audit\n---\n# changed source\n",
            encoding="utf-8",
        )
    elif changed == "target":
        (state["target_skill"] / "SKILL.md").write_text(
            "---\nname: web-audit\n---\n# changed target\n",
            encoding="utf-8",
        )
    else:
        state["manifest"].write_text(
            f"web-audit:{'0' * 32}\n",
            encoding="utf-8",
        )
        state["manifest"].chmod(0o600)

    target_before = rebaseline._tree_digest(state["target_skill"])
    target_identity = state["target_skill"].lstat().st_ino
    manifest_before = state["manifest"].read_bytes()
    with pytest.raises(rebaseline.RebaselineError, match="stale request"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert state["target_skill"].lstat().st_ino == target_identity
    assert rebaseline._tree_digest(state["target_skill"]) == target_before
    assert state["manifest"].read_bytes() == manifest_before


def test_legacy_request_cas_rejects_joint_source_and_target_relocation(
    legacy_rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = legacy_rebaseline_state
    request = rebaseline.inspect_request(
        "web-audit",
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )
    request_file = tmp_path / "legacy-relocation.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    request_file.chmod(0o600)

    relocated_source = state["source"] / "relocated" / "web-audit"
    relocated_target = state["target"] / "relocated" / "web-audit"
    relocated_source.parent.mkdir()
    relocated_target.parent.mkdir()
    source_identity = state["source_skill"].lstat().st_ino
    target_identity = state["target_skill"].lstat().st_ino
    state["source_skill"].rename(relocated_source)
    state["target_skill"].rename(relocated_target)
    assert relocated_source.lstat().st_ino == source_identity
    assert relocated_target.lstat().st_ino == target_identity

    target_before = rebaseline._tree_digest(relocated_target)
    manifest_before = state["manifest"].read_bytes()
    with pytest.raises(
        rebaseline.RebaselineError, match="stale request: install path changed"
    ):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert relocated_target.lstat().st_ino == target_identity
    assert rebaseline._tree_digest(relocated_target) == target_before
    assert state["manifest"].read_bytes() == manifest_before
    assert not (state["target"] / ".archive" / "rebaseline").exists()


@pytest.mark.parametrize(
    ("record", "expected_error"),
    [
        ({"hash": "not-an-md5"}, "manifest hash is invalid"),
        ({"hash": "0" * 32, "install_path": None}, "install_path"),
        (
            {"hash": "0" * 32, "install_path": "audits/web-audit"},
            "provenance",
        ),
        ({"hash": "0" * 32, "provenance": None}, "install_path"),
        (
            {
                "hash": "0" * 32,
                "provenance": {"kind": "workspace", "source_root": "/wrong"},
            },
            "install_path",
        ),
        (
            {"hash": "0" * 32, "unexpected": "not-v2"},
            "legacy manifest record",
        ),
    ],
)
def test_legacy_resolution_rejects_invalid_hash_and_partial_or_extended_records(
    legacy_rebaseline_state: dict[str, Path],
    record: dict[str, object],
    expected_error: str,
) -> None:
    state = legacy_rebaseline_state
    state["manifest"].write_text(
        "web-audit:"
        + json.dumps(record, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    state["manifest"].chmod(0o600)

    with pytest.raises(rebaseline.RebaselineError, match=expected_error):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


@pytest.mark.parametrize("source_case", ["missing", "ambiguous"])
def test_legacy_resolution_requires_one_unique_source_skill(
    legacy_rebaseline_state: dict[str, Path],
    source_case: str,
) -> None:
    state = legacy_rebaseline_state
    if source_case == "missing":
        (state["source_skill"] / "SKILL.md").unlink()
    else:
        duplicate = state["source"] / "duplicates" / "other-folder"
        duplicate.mkdir(parents=True)
        (duplicate / "SKILL.md").write_text(
            "---\nname: web-audit\n---\n# Duplicate\n",
            encoding="utf-8",
        )

    expected_error = (
        "skill 'web-audit' is absent"
        if source_case == "missing"
        else "skill 'web-audit' is ambiguous"
    )
    with pytest.raises(rebaseline.RebaselineError, match=expected_error):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_legacy_resolution_requires_exact_canonical_target(
    legacy_rebaseline_state: dict[str, Path],
) -> None:
    state = legacy_rebaseline_state
    relocated = state["target"] / "elsewhere" / "web-audit"
    relocated.parent.mkdir()
    state["target_skill"].rename(relocated)

    with pytest.raises(
        rebaseline.RebaselineError, match="legacy canonical target is unavailable"
    ):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_legacy_resolution_rejects_unsafe_canonical_target_tree(
    legacy_rebaseline_state: dict[str, Path],
) -> None:
    state = legacy_rebaseline_state
    (state["target_skill"] / "unsafe-link").symlink_to("SKILL.md")

    with pytest.raises(rebaseline.RebaselineError, match="symlink"):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


@pytest.mark.parametrize(
    "skill_name",
    ["../web-audit", "web-*", "web?audit", "[web-audit]", "/web-audit"],
)
def test_inspect_rejects_path_and_wildcard_skill_names(
    rebaseline_state: dict[str, Path],
    skill_name: str,
) -> None:
    state = rebaseline_state

    with pytest.raises(rebaseline.RebaselineError, match="skill name"):
        rebaseline.inspect_request(
            skill_name,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_inspect_rejects_any_managed_symlink(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state
    (state["source_skill"] / "alias.md").symlink_to("SKILL.md")

    with pytest.raises(rebaseline.RebaselineError, match="symlink"):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_inspect_rejects_manifest_install_path_escape(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state
    record = skills_sync._read_manifest_records(state["manifest"])["web-audit"]
    record["install_path"] = "../outside"
    state["manifest"].write_text(
        "web-audit:" + json.dumps(record, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(rebaseline.RebaselineError, match="install_path"):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_manifest_swap_to_symlink_before_open_is_rejected_without_following(
    rebaseline_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    secret = state["manifest"].parent.parent / "daemon-secret"
    secret.write_text("must never become manifest input", encoding="utf-8")
    retained = state["manifest"].with_name(".workspace_manifest.retained")

    def swap_to_symlink(path: Path) -> None:
        path.rename(retained)
        path.symlink_to(secret)

    monkeypatch.setattr(rebaseline, "_manifest_open_barrier", swap_to_symlink)

    with pytest.raises(
        rebaseline.RebaselineError, match="manifest.*symlink|manifest changed"
    ):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def _request_file(
    state: dict[str, Path], tmp_path: Path
) -> tuple[Path, dict[str, object]]:
    request = rebaseline.inspect_request(
        "web-audit",
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )
    request_file = tmp_path / "web-audit-rebaseline.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    request_file.chmod(0o600)
    return request_file, request


def _active_skill_paths(target: Path, name: str) -> list[Path]:
    result = []
    for skill_md in target.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        if skills_sync._read_skill_name(skill_md, skill_md.parent.name) == name:
            result.append(skill_md.parent)
    return sorted(result)


def _write_partial_archive_file(
    archive,
    stage_name: str,
    filename: str,
    payload: bytes,
) -> None:
    stage_fd = os.open(
        stage_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=archive.fd,
    )
    file_fd: int | None = None
    try:
        file_fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=stage_fd,
        )
        os.write(file_fd, payload)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(stage_fd)


def test_tree_digest_is_framed_sha256_and_avoids_path_content_ambiguity(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a").write_bytes(b"bc")
    (right / "ab").write_bytes(b"c")

    left_digest = rebaseline._tree_digest(left)
    right_digest = rebaseline._tree_digest(right)

    assert len(left_digest) == len(right_digest) == 64
    assert left_digest != right_digest


@pytest.mark.parametrize(
    ("relative", "left_mode", "right_mode"),
    [
        (Path("run.sh"), 0o755, 0o644),
        (Path("scripts"), 0o751, 0o700),
        (Path("."), 0o750, 0o700),
    ],
)
def test_tree_digest_binds_every_file_and_directory_mode(
    tmp_path: Path,
    relative: Path,
    left_mode: int,
    right_mode: int,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "scripts").mkdir(parents=True)
        (root / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (root / "scripts" / "task.sh").write_text("same", encoding="utf-8")
    (left / relative).chmod(left_mode)
    (right / relative).chmod(right_mode)

    assert rebaseline._tree_digest(left) != rebaseline._tree_digest(right)


def test_mode_only_drift_inspects_and_apply_restores_exact_mode_parity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace" / "skills"
    source_skill = source / "audits" / "web-audit"
    scripts = source_skill / "scripts"
    scripts.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text(
        "---\nname: web-audit\n---\n# Web Audit\n",
        encoding="utf-8",
    )
    run_script = source_skill / "run.sh"
    run_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (scripts / "task.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source_skill.chmod(0o750)
    scripts.chmod(0o751)
    run_script.chmod(0o755)

    target = tmp_path / "hermes" / "skills"
    manifest = target / ".workspace_manifest"
    skills_sync.sync_skills(
        quiet=True,
        source_dir=source,
        target_dir=target,
        manifest_file=manifest,
    )
    target_skill = target / "audits" / "web-audit"
    target_skill.chmod(0o700)
    (target_skill / "scripts").chmod(0o700)
    (target_skill / "run.sh").chmod(0o644)
    state = {
        "source": source,
        "source_skill": source_skill,
        "target": target,
        "target_skill": target_skill,
        "manifest": manifest,
    }

    request_file, request = _request_file(state, tmp_path)
    assert request["expected_source_hash"] != request["expected_target_hash"]

    rebaseline.apply_request(
        request_file,
        source_root=source,
        target_root=target,
        manifest_file=manifest,
    )

    for relative in (Path("."), Path("scripts"), Path("run.sh")):
        assert stat.S_IMODE((target_skill / relative).stat().st_mode) == stat.S_IMODE(
            (source_skill / relative).stat().st_mode
        )
    assert rebaseline._tree_digest(target_skill) == rebaseline._tree_digest(
        source_skill
    )


def test_tree_digest_uses_exact_shared_ignore_semantics(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    noisy = tmp_path / "noisy"
    clean.mkdir()
    (clean / "SKILL.md").write_text("same", encoding="utf-8")
    shutil.copytree(clean, noisy)
    (noisy / ".tmp").write_text("ignored", encoding="utf-8")
    (noisy / "file.tmp").write_text("ignored", encoding="utf-8")
    (noisy / "node_modules").mkdir()
    (noisy / "node_modules" / "generated.js").write_text("ignored")

    assert skills_sync.is_sync_ignored_path(Path(".tmp")) is True
    assert rebaseline._tree_digest(clean) == rebaseline._tree_digest(noisy)


def test_tree_digest_rejects_file_swapped_to_symlink_before_nofollow_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    managed = tree / "SKILL.md"
    managed.write_text("managed", encoding="utf-8")
    outside = tmp_path / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    retained = tree / "retained"

    def swap(path: Path) -> None:
        if path != managed:
            return
        path.rename(retained)
        path.symlink_to(outside)

    monkeypatch.setattr(rebaseline, "_tree_file_open_barrier", swap)

    with pytest.raises(rebaseline.RebaselineError, match="tree file.*symlink|changed"):
        rebaseline._tree_digest(tree)


def test_tree_digest_rejects_directory_type_change_during_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    managed_dir = tree / "managed"
    managed_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    def replace_directory(_root: Path) -> None:
        managed_dir.rmdir()
        managed_dir.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        rebaseline,
        "_tree_directory_post_digest_barrier",
        replace_directory,
    )

    with pytest.raises(rebaseline.RebaselineError, match="tree directory.*changed"):
        rebaseline._tree_digest(tree)


def test_tree_digest_rejects_directory_contents_added_during_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "SKILL.md").write_text("managed", encoding="utf-8")

    def add_late_file(_root: Path) -> None:
        (tree / "late.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(
        rebaseline,
        "_tree_directory_post_digest_barrier",
        add_late_file,
    )

    with pytest.raises(rebaseline.RebaselineError, match="tree directory.*changed"):
        rebaseline._tree_digest(tree)


def test_dirfd_tree_digest_rejects_root_mode_change_before_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir(mode=0o750)
    (tree / "SKILL.md").write_text("managed", encoding="utf-8")
    root_fd = os.open(
        tree,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    real_collect = rebaseline._collect_fd_tree_entries

    def change_root_before_collect(directory_fd, relative_parts, entries, directories):
        if not relative_parts:
            os.fchmod(directory_fd, 0o700)
        return real_collect(directory_fd, relative_parts, entries, directories)

    monkeypatch.setattr(
        rebaseline,
        "_collect_fd_tree_entries",
        change_root_before_collect,
    )
    try:
        with pytest.raises(rebaseline.RebaselineError, match="root.*changed"):
            rebaseline._tree_digest_dir_fd(root_fd)
    finally:
        os.close(root_fd)


def test_inspect_uses_schema_v3_path_and_sha256_tree_expectations(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state

    request = rebaseline.inspect_request(
        "web-audit",
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert request["schema_version"] == 3
    assert request["expected_install_path"] == "audits/web-audit"
    assert request["expected_source_hash"] == rebaseline._tree_digest(
        state["source_skill"]
    )
    assert request["expected_target_hash"] == rebaseline._tree_digest(
        state["target_skill"]
    )
    assert len(str(request["expected_source_hash"])) == 64
    assert len(str(request["expected_target_hash"])) == 64


def test_inspect_rejects_fully_aligned_noop(rebaseline_state: dict[str, Path]) -> None:
    state = rebaseline_state
    shutil.copyfile(
        state["target_skill"] / "SKILL.md",
        state["source_skill"] / "SKILL.md",
    )

    with pytest.raises(rebaseline.RebaselineError, match="already aligned|no-op"):
        rebaseline.inspect_request(
            "web-audit",
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_apply_replaces_one_skill_keeps_timestamped_recovery_then_updates_manifest(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    stale_manifest = state["target_skill"] / "manifests" / "mission-control.ts"
    stale_manifest.parent.mkdir()
    stale_manifest.write_text(
        "export const slug = 'mission-control';\n", encoding="utf-8"
    )
    old_target_hash = rebaseline._tree_digest(state["target_skill"])
    old_target_inode = state["target_skill"].lstat().st_ino
    request_file, _request = _request_file(state, tmp_path)

    result = rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    source_hash = rebaseline._tree_digest(state["source_skill"])
    assert result["ok"] is True
    assert result["skill_name"] == "web-audit"
    assert result["previous_target_hash"] == old_target_hash
    assert result["source_hash"] == source_hash
    assert rebaseline._tree_digest(state["target_skill"]) == source_hash
    assert state["target_skill"].lstat().st_ino != old_target_inode
    assert not stale_manifest.exists()
    record = skills_sync._read_manifest_records(state["manifest"])["web-audit"]
    assert record["hash"] == skills_sync._dir_hash(state["source_skill"])
    assert stat.S_IMODE(state["manifest"].stat().st_mode) == 0o600

    recovery = Path(str(result["recovery_path"]))
    assert recovery.parent == state["target"] / ".archive" / "rebaseline"
    assert recovery.name.startswith(".web-audit.rebaseline-recovery-")
    assert recovery.lstat().st_ino == old_target_inode
    assert rebaseline._tree_digest(recovery) == old_target_hash
    assert (recovery / "manifests" / "mission-control.ts").exists()


def test_success_stage_and_recovery_are_runtime_excluded_under_protected_archive(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)

    result = rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    archive_root = state["target"] / ".archive" / "rebaseline"
    recovery = Path(str(result["recovery_path"]))
    assert recovery.is_relative_to(archive_root)
    assert stat.S_IMODE(archive_root.stat().st_mode) == 0o700
    assert is_excluded_skill_path(recovery / "SKILL.md")
    assert _active_skill_paths(state["target"], "web-audit") == [state["target_skill"]]


def test_partial_copy_stage_is_retained_only_inside_excluded_archive(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)

    def partial_copy(archive, stage_name):
        _write_partial_archive_file(
            archive,
            stage_name,
            "SKILL.md",
            b"---\nname: web-audit\n---\n# partial\n",
        )
        raise OSError("simulated partial copy")

    monkeypatch.setattr(rebaseline, "_archive_copy_barrier", partial_copy)

    with pytest.raises(rebaseline.RebaselineError, match="partial copy"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    stages = list(
        (state["target"] / ".archive" / "rebaseline").glob(
            ".web-audit.rebaseline-stage-*"
        )
    )
    assert len(stages) == 1
    assert is_excluded_skill_path(stages[0] / "SKILL.md")
    assert _active_skill_paths(state["target"], "web-audit") == [state["target_skill"]]


def test_archive_symlink_is_rejected_before_any_target_mutation(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    (state["target"] / ".archive").symlink_to(outside, target_is_directory=True)
    request_file, _request = _request_file(state, tmp_path)
    original_inode = state["target_skill"].lstat().st_ino

    with pytest.raises(rebaseline.RebaselineError, match="archive.*symlink"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert state["target_skill"].lstat().st_ino == original_inode
    assert list(outside.iterdir()) == []


def test_archive_root_must_share_target_filesystem(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    real_fstat = os.fstat
    calls = 0

    def different_device(fd: int):
        nonlocal calls
        calls += 1
        value = real_fstat(fd)
        if calls != 3:
            return value
        fields = list(value)
        fields[2] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(
        rebaseline,
        "_archive_fstat",
        different_device,
        raising=False,
    )

    with pytest.raises(rebaseline.RebaselineError, match="same filesystem"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_archive_root_swap_after_fd_validation_fails_before_external_write(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_target = (state["target_skill"] / "SKILL.md").read_bytes()
    original_manifest = state["manifest"].read_bytes()
    archive_root = state["target"] / ".archive" / "rebaseline"
    archive_root.mkdir(parents=True)
    archive_root.chmod(0o700)
    retained = archive_root.with_name("rebaseline-retained")
    outside = tmp_path / "outside-archive-root"
    outside.mkdir()

    def swap_root_after_validation(path: Path) -> None:
        assert path == archive_root
        path.rename(retained)
        path.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        rebaseline,
        "_archive_root_post_open_barrier",
        swap_root_after_validation,
        raising=False,
    )

    with pytest.raises(rebaseline.RebaselineError, match="archive.*changed|symlink"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert list(outside.iterdir()) == []
    assert list(retained.iterdir()) == []
    assert (state["target_skill"] / "SKILL.md").read_bytes() == original_target
    assert state["manifest"].read_bytes() == original_manifest


def test_apply_changes_only_requested_skill_and_manifest_record(tmp_path: Path) -> None:
    source = tmp_path / "workspace" / "skills"
    for name in ("web-audit", "other-skill"):
        skill = source / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n# {name} v1\n",
            encoding="utf-8",
        )
    target = tmp_path / "hermes" / "skills"
    manifest = target / ".workspace_manifest"
    skills_sync.sync_skills(
        quiet=True,
        source_dir=source,
        target_dir=target,
        manifest_file=manifest,
    )
    original_records = skills_sync._read_manifest_records(manifest)
    other_inode = (target / "other-skill").lstat().st_ino
    for name in ("web-audit", "other-skill"):
        (source / name / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n# {name} v2\n",
            encoding="utf-8",
        )
    state = {
        "source": source,
        "target": target,
        "manifest": manifest,
    }
    request_file, _request = _request_file(state, tmp_path)

    rebaseline.apply_request(
        request_file,
        source_root=source,
        target_root=target,
        manifest_file=manifest,
    )

    assert b"web-audit v2" in (target / "web-audit" / "SKILL.md").read_bytes()
    assert b"other-skill v1" in (target / "other-skill" / "SKILL.md").read_bytes()
    assert (target / "other-skill").lstat().st_ino == other_inode
    records = skills_sync._read_manifest_records(manifest)
    assert records["other-skill"] == original_records["other-skill"]
    assert records["web-audit"]["hash"] == skills_sync._dir_hash(source / "web-audit")


@pytest.mark.parametrize("changed", ["source", "target", "manifest"])
def test_apply_rejects_stale_request_without_mutating_target(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    changed: str,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_target = (state["target_skill"] / "SKILL.md").read_bytes()
    original_manifest = state["manifest"].read_bytes()
    if changed == "source":
        (state["source_skill"] / "changed-after-request.txt").write_text("race")
    elif changed == "target":
        (state["target_skill"] / "changed-after-request.txt").write_text("race")
    else:
        state["manifest"].write_bytes(original_manifest + b"\n")

    with pytest.raises(rebaseline.RebaselineError, match=f"stale request: {changed}"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert (state["target_skill"] / "SKILL.md").read_bytes() == original_target
    if changed != "target":
        assert not (state["target_skill"] / "changed-after-request.txt").exists()
    assert state["manifest"].read_bytes() == (
        original_manifest + b"\n" if changed == "manifest" else original_manifest
    )
    assert not list(
        state["target_skill"].parent.glob(".web-audit.rebaseline-recovery-*")
    )


def test_apply_rejects_unknown_or_duplicate_request_fields(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    request_file, request = _request_file(state, tmp_path)
    request["command"] = "anything"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    with pytest.raises(rebaseline.RebaselineError, match="request fields"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    duplicate = json.dumps({
        key: value for key, value in request.items() if key != "command"
    })
    duplicate = duplicate[:-1] + ',"skill_name":"other"}'
    request_file.write_text(duplicate, encoding="utf-8")
    with pytest.raises(rebaseline.RebaselineError, match="duplicate key"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


@pytest.mark.parametrize("old_shape", ["actual-v2", "wrong-version-v2"])
def test_apply_rejects_old_request_schema_fail_closed(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    old_shape: str,
) -> None:
    state = rebaseline_state
    request_file, request = _request_file(state, tmp_path)
    request["schema_version"] = 2
    if old_shape == "actual-v2":
        request.pop("expected_install_path")
        expected_error = "request fields"
    else:
        expected_error = "request schema_version must be 3"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    with pytest.raises(rebaseline.RebaselineError, match=expected_error):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


@pytest.mark.parametrize(
    "install_path",
    [
        None,
        True,
        "",
        ".",
        "..",
        "../web-audit",
        "/audits/web-audit",
        "audits//web-audit",
        "audits/./web-audit",
        "audits/web-*",
        "audits/web-audit\x00suffix",
    ],
)
def test_apply_rejects_malformed_expected_install_path(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    install_path: object,
) -> None:
    state = rebaseline_state
    request_file, request = _request_file(state, tmp_path)
    request["expected_install_path"] = install_path
    request_file.write_text(json.dumps(request), encoding="utf-8")

    with pytest.raises(
        rebaseline.RebaselineError,
        match="request field 'expected_install_path'.*invalid",
    ):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_apply_rejects_symlink_request_file(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    request_link = tmp_path / "request-link.json"
    request_link.symlink_to(request_file)

    with pytest.raises(rebaseline.RebaselineError, match="request.*symlink"):
        rebaseline.apply_request(
            request_link,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_apply_requires_request_mode_0600(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    request_file.chmod(0o644)

    with pytest.raises(rebaseline.RebaselineError, match="request mode must be 0600"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_apply_requires_request_owned_by_effective_uid(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    monkeypatch.setattr(
        rebaseline.os, "geteuid", lambda: request_file.stat().st_uid + 1
    )

    with pytest.raises(rebaseline.RebaselineError, match="request owner"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_request_path_replacement_between_lstat_and_open_is_rejected(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    retained = tmp_path / "retained-request.json"

    def replace_request(path: Path) -> None:
        path.rename(retained)
        shutil.copyfile(retained, path)
        path.chmod(0o600)

    monkeypatch.setattr(rebaseline, "_request_open_barrier", replace_request)

    with pytest.raises(rebaseline.RebaselineError, match="request.*changed"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_request_path_replacement_after_fd_read_is_rejected(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)

    def replace_request(path: Path) -> None:
        replacement = tmp_path / "replacement-request.json"
        shutil.copyfile(path, replacement)
        replacement.chmod(0o600)
        os.replace(replacement, path)

    monkeypatch.setattr(rebaseline, "_request_post_read_barrier", replace_request)

    with pytest.raises(rebaseline.RebaselineError, match="request.*changed"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_successful_request_replay_is_rejected_fail_closed(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    with pytest.raises(
        rebaseline.RebaselineError, match="replay|already aligned|stale"
    ):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )


def test_partial_source_copy_failure_leaves_target_and_manifest_unchanged(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_target = (state["target_skill"] / "SKILL.md").read_bytes()
    original_inode = state["target_skill"].lstat().st_ino
    original_manifest = state["manifest"].read_bytes()

    def partial_copy(archive, stage_name):
        _write_partial_archive_file(
            archive,
            stage_name,
            "PARTIAL",
            b"incomplete",
        )
        raise OSError("simulated partial copy")

    monkeypatch.setattr(rebaseline, "_archive_copy_barrier", partial_copy)

    with pytest.raises(rebaseline.RebaselineError, match="partial copy"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert state["target_skill"].lstat().st_ino == original_inode
    assert (state["target_skill"] / "SKILL.md").read_bytes() == original_target
    assert state["manifest"].read_bytes() == original_manifest


def test_post_copy_hash_change_blocks_manifest_and_preserves_both_versions(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_manifest = state["manifest"].read_bytes()
    foreign = b"---\nname: web-audit\n---\n# post-copy concurrent edit\n"

    def change_published_target(_target: Path) -> None:
        (state["target_skill"] / "SKILL.md").write_bytes(foreign)

    monkeypatch.setattr(rebaseline, "_post_copy_barrier", change_published_target)

    with pytest.raises(rebaseline.RebaselineError, match="post-copy target hash"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert (state["target_skill"] / "SKILL.md").read_bytes() == foreign
    assert state["manifest"].read_bytes() == original_manifest
    recoveries = list(
        (state["target"] / ".archive" / "rebaseline").glob(
            ".web-audit.rebaseline-recovery-*"
        )
    )
    assert len(recoveries) == 1
    assert b"Web Audit v1" in (recoveries[0] / "SKILL.md").read_bytes()


def test_post_copy_relocation_is_rejected_before_manifest_mutation(
    legacy_rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = legacy_rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_manifest = state["manifest"].read_bytes()
    original_target = (state["target_skill"] / "SKILL.md").read_bytes()
    original_target_identity = state["target_skill"].lstat().st_ino
    relocated_source = state["source"] / "relocated" / "web-audit"
    relocated_target = state["target"] / "relocated" / "web-audit"
    relocated_source.parent.mkdir()
    relocated_target.parent.mkdir()
    barrier_state = {"manifest_update_reached": False}

    def relocate_published_pair(_target: Path) -> None:
        source_identity = state["source_skill"].lstat().st_ino
        published_identity = state["target_skill"].lstat().st_ino
        state["source_skill"].rename(relocated_source)
        state["target_skill"].rename(relocated_target)
        assert relocated_source.lstat().st_ino == source_identity
        assert relocated_target.lstat().st_ino == published_identity

    def record_manifest_update_reached(_manifest: Path) -> None:
        barrier_state["manifest_update_reached"] = True

    monkeypatch.setattr(
        rebaseline,
        "_post_copy_barrier",
        relocate_published_pair,
    )
    monkeypatch.setattr(
        rebaseline,
        "_manifest_update_barrier",
        record_manifest_update_reached,
    )

    with pytest.raises(rebaseline.RebaselineError) as error:
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert "post-copy install path changed" in str(error.value)
    assert barrier_state["manifest_update_reached"] is False
    assert state["manifest"].read_bytes() == original_manifest
    record = skills_sync._read_manifest_records(state["manifest"])["web-audit"]
    assert set(record) == {"hash"}
    assert state["target_skill"].lstat().st_ino == original_target_identity
    assert (state["target_skill"] / "SKILL.md").read_bytes() == original_target


def test_manifest_update_barrier_relocation_is_rejected_before_manifest_write(
    legacy_rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = legacy_rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_manifest = state["manifest"].read_bytes()
    relocated_source = state["source"] / "relocated" / "web-audit"
    relocated_target = state["target"] / "relocated" / "web-audit"
    relocated_source.parent.mkdir()
    relocated_target.parent.mkdir()
    barrier_state = {"manifest_write_reached": False}
    real_write_manifest = rebaseline._write_manifest

    def relocate_published_pair(_manifest: Path) -> None:
        state["source_skill"].rename(relocated_source)
        state["target_skill"].rename(relocated_target)

    def record_manifest_write(*args, **kwargs):
        barrier_state["manifest_write_reached"] = True
        return real_write_manifest(*args, **kwargs)

    monkeypatch.setattr(
        rebaseline,
        "_manifest_update_barrier",
        relocate_published_pair,
    )
    monkeypatch.setattr(rebaseline, "_write_manifest", record_manifest_write)

    with pytest.raises(rebaseline.RebaselineError) as error:
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert "post-copy install path changed" in str(error.value)
    assert barrier_state["manifest_write_reached"] is False
    assert state["manifest"].read_bytes() == original_manifest
    record = skills_sync._read_manifest_records(state["manifest"])["web-audit"]
    assert set(record) == {"hash"}


def test_publish_validation_quarantine_is_excluded_from_runtime_discovery(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)

    def corrupt_published_target(target: Path) -> None:
        (target / "corrupt-after-publish.txt").write_text("corrupt")

    monkeypatch.setattr(
        rebaseline,
        "_target_publish_validation_barrier",
        corrupt_published_target,
    )

    with pytest.raises(rebaseline.RebaselineError, match="published target.*changed"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    quarantines = list(
        (state["target"] / ".archive" / "rebaseline").glob(
            ".web-audit.rebaseline-publish-quarantine-*"
        )
    )
    assert len(quarantines) == 1
    assert is_excluded_skill_path(quarantines[0] / "SKILL.md")
    assert _active_skill_paths(state["target"], "web-audit") == [state["target_skill"]]


def test_manifest_race_rolls_back_skill_and_preserves_raced_manifest(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_target = (state["target_skill"] / "SKILL.md").read_bytes()
    original_inode = state["target_skill"].lstat().st_ino
    original_manifest = state["manifest"].read_bytes()
    raced_manifest = original_manifest + b"\n"

    def race_manifest(_manifest: Path) -> None:
        state["manifest"].write_bytes(raced_manifest)

    monkeypatch.setattr(rebaseline, "_manifest_update_barrier", race_manifest)

    with pytest.raises(rebaseline.RebaselineError, match="manifest changed"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert state["target_skill"].lstat().st_ino == original_inode
    assert (state["target_skill"] / "SKILL.md").read_bytes() == original_target
    assert state["manifest"].read_bytes() == raced_manifest
    assert skills_sync._dir_hash(state["target_skill"]) != skills_sync._dir_hash(
        state["source_skill"]
    )


def test_manifest_race_preserves_concurrently_edited_live_target(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_manifest = state["manifest"].read_bytes()
    raced_manifest = original_manifest + b"\n"
    foreign = b"---\nname: web-audit\n---\n# concurrent daemon edit\n"

    def race_both(_manifest: Path) -> None:
        (state["target_skill"] / "SKILL.md").write_bytes(foreign)
        state["manifest"].write_bytes(raced_manifest)

    monkeypatch.setattr(rebaseline, "_manifest_update_barrier", race_both)

    with pytest.raises(rebaseline.RebaselineError, match="manifest changed"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert (state["target_skill"] / "SKILL.md").read_bytes() == foreign
    assert state["manifest"].read_bytes() == raced_manifest
    recoveries = list(
        (state["target"] / ".archive" / "rebaseline").glob(
            ".web-audit.rebaseline-recovery-*"
        )
    )
    assert len(recoveries) == 1
    assert b"Web Audit v1" in (recoveries[0] / "SKILL.md").read_bytes()


def test_manifest_replacement_after_publish_is_preserved_and_target_rolls_back(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_target = (state["target_skill"] / "SKILL.md").read_bytes()
    original_inode = state["target_skill"].lstat().st_ino
    raced = {}

    def replace_manifest_after_publish(path: Path) -> None:
        replacement = path.parent / ".foreign-manifest"
        raced["payload"] = path.read_bytes() + b"\n"
        replacement.write_bytes(raced["payload"])
        replacement.chmod(0o600)
        os.replace(replacement, path)
        raced["inode"] = path.lstat().st_ino

    monkeypatch.setattr(
        rebaseline,
        "_manifest_post_publish_barrier",
        replace_manifest_after_publish,
    )

    with pytest.raises(rebaseline.RebaselineError, match="manifest changed"):
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    assert state["target_skill"].lstat().st_ino == original_inode
    assert (state["target_skill"] / "SKILL.md").read_bytes() == original_target
    assert state["manifest"].lstat().st_ino == raced["inode"]
    assert state["manifest"].read_bytes() == raced["payload"]


def test_target_restore_failure_is_reported_as_rollback_incomplete_with_paths(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    original_manifest = state["manifest"].read_bytes()
    real_rename = rebaseline._rename_noreplace_at

    def race_manifest(_manifest: Path) -> None:
        state["manifest"].write_bytes(original_manifest + b"\n")

    def deny_recovery_restore(source, destination, **kwargs) -> None:
        if (
            "rebaseline-recovery" in str(source)
            and Path(destination) == state["target_skill"]
            and kwargs.get("source_dir_fd") is not None
        ):
            raise RuntimeError("restore denied")
        real_rename(source, destination, **kwargs)

    monkeypatch.setattr(rebaseline, "_manifest_update_barrier", race_manifest)
    monkeypatch.setattr(rebaseline, "_rename_noreplace_at", deny_recovery_restore)

    with pytest.raises(rebaseline.RebaselineError) as error:
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    message = str(error.value)
    assert "rollback_incomplete" in message
    assert "restore denied" in message
    assert "recovery" in message
    assert "quarantine" in message


def test_manifest_restore_failure_is_never_swallowed_and_final_state_is_reported(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)

    def fail_after_publish(_manifest: Path) -> None:
        raise OSError("post-publish verification failed")

    def fail_manifest_restore(*_args, **_kwargs) -> None:
        raise RuntimeError("manifest restore denied")

    monkeypatch.setattr(
        rebaseline,
        "_manifest_post_publish_barrier",
        fail_after_publish,
    )
    monkeypatch.setattr(
        rebaseline,
        "_restore_manifest_if_owned",
        fail_manifest_restore,
    )

    with pytest.raises(rebaseline.RebaselineError) as error:
        rebaseline.apply_request(
            request_file,
            source_root=state["source"],
            target_root=state["target"],
            manifest_file=state["manifest"],
        )

    message = str(error.value)
    assert "rollback_incomplete" in message
    assert "manifest restore denied" in message
    assert str(state["manifest"]) in message


def test_verify_reports_hash_drift_and_retired_web_audit_artifact(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state
    stale = state["target_skill"] / "manifests" / "mission-control.ts"
    stale.parent.mkdir()
    stale.write_text("export const slug = 'mission-control';\n", encoding="utf-8")

    result = rebaseline.verify_roots(
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert result["ok"] is False
    assert result["checked"] == 1
    assert any(
        item["skill_name"] == "web-audit" and item["reason"] == "hash_mismatch"
        for item in result["mismatches"]
    )
    assert result["forbidden_findings"] == [
        {
            "skill_name": "web-audit",
            "marker": "mission-control",
            "path": "manifests/mission-control.ts",
        }
    ]


def test_verify_passes_after_apply_and_ignores_runtime_artifacts(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    ignored = state["source_skill"] / "node_modules" / ".bin"
    ignored.mkdir(parents=True)
    (ignored / "tool").symlink_to("../../tool.js")
    (state["source_skill"] / "node_modules" / "tool.js").write_text("generated")
    request_file, _request = _request_file(state, tmp_path)
    rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    result = rebaseline.verify_roots(
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert result["ok"] is True
    assert result["checked"] == 1
    assert result["mismatches"] == []
    assert result["forbidden_findings"] == []
    assert not (state["target_skill"] / "node_modules").exists()


def test_verify_fails_when_workspace_source_is_missing_from_manifest(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state
    extra = state["source"] / "tools" / "extra-skill"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text(
        "---\nname: extra-skill\n---\n# Extra\n",
        encoding="utf-8",
    )

    result = rebaseline.verify_roots(
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert result["ok"] is False
    assert {
        "skill_name": "extra-skill",
        "reason": "manifest_missing",
    } in result["mismatches"]


def test_source_discovery_excludes_archived_skill_snapshots(
    rebaseline_state: dict[str, Path],
) -> None:
    state = rebaseline_state
    archived = state["source"] / ".archive" / "rebaseline" / "web-audit"
    archived.mkdir(parents=True)
    (archived / "SKILL.md").write_text(
        "---\nname: web-audit\n---\n# Archived snapshot\n",
        encoding="utf-8",
    )

    assert rebaseline._discover_source_skills(state["source"]) == {
        "web-audit": state["source_skill"]
    }
    assert (
        rebaseline._find_source_skill("web-audit", state["source"])
        == state["source_skill"]
    )


def test_verify_detects_active_runtime_duplicate_and_forbidden_marker(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    request_file, _request = _request_file(state, tmp_path)
    rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )
    duplicate = state["target"] / "foreign" / "duplicate-web-audit"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text(
        "---\nname: web-audit\n---\n# duplicate mission-control\n",
        encoding="utf-8",
    )

    result = rebaseline.verify_roots(
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert result["ok"] is False
    assert any(
        mismatch["reason"] == "active_duplicate"
        and mismatch["skill_name"] == "web-audit"
        for mismatch in result["mismatches"]
    )
    assert any(
        finding["skill_name"] == "web-audit"
        and finding["marker"] == "mission-control"
        and "foreign/duplicate-web-audit" in finding["path"]
        for finding in result["forbidden_findings"]
    )


def test_verify_does_not_treat_archive_recovery_marker_as_active(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    state = rebaseline_state
    stale = state["target_skill"] / "manifests" / "mission-control.ts"
    stale.parent.mkdir()
    stale.write_text("mission-control", encoding="utf-8")
    request_file, _request = _request_file(state, tmp_path)
    result = rebaseline.apply_request(
        request_file,
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )
    recovery = Path(str(result["recovery_path"]))
    assert (recovery / "manifests" / "mission-control.ts").exists()

    verified = rebaseline.verify_roots(
        source_root=state["source"],
        target_root=state["target"],
        manifest_file=state["manifest"],
    )

    assert verified["ok"] is True
    assert verified["forbidden_findings"] == []
    assert verified["active_duplicates"] == []


def test_cli_inspect_apply_and_final_verify_command(
    rebaseline_state: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = rebaseline_state
    request_file = tmp_path / "request.json"
    common = [
        "--source",
        str(state["source"]),
        "--target",
        str(state["target"]),
        "--manifest",
        str(state["manifest"]),
    ]

    assert (
        rebaseline.main([
            "inspect",
            "--skill",
            "web-audit",
            "--output",
            str(request_file),
            *common,
        ])
        == 0
    )
    inspect_output = json.loads(capsys.readouterr().out)
    assert inspect_output["ok"] is True
    assert request_file.exists()
    assert stat.S_IMODE(request_file.stat().st_mode) == 0o600

    assert rebaseline.main(["apply", "--request", str(request_file), *common]) == 0
    apply_output = json.loads(capsys.readouterr().out)
    assert apply_output["action"] == "rebaselined"

    # This is the exact option shape required by the Task-9 final command.
    assert rebaseline.main(["verify", *common]) == 0
    verify_output = json.loads(capsys.readouterr().out)
    assert verify_output["ok"] is True
