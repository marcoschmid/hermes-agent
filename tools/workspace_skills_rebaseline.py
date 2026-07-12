#!/usr/bin/env python3
"""Content-addressed, daemon-owned rebaseline of one workspace skill."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from agent.skill_utils import is_excluded_skill_path
from tools.skills_sync import (
    _dir_hash,
    _manifest_payload,
    _read_skill_name,
    _restore_manifest_if_owned,
    _write_manifest,
    is_sync_ignored_path,
)


REQUEST_FIELDS = {
    "schema_version",
    "skill_name",
    "expected_source_hash",
    "expected_target_hash",
    "expected_manifest_hash",
}

REQUEST_SCHEMA_VERSION = 2
_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MD5_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GLOB_CHARS = frozenset("*?[]")
_DEFAULT_FORBIDDEN_MARKERS = {
    "web-audit": ("mission-control", "mission_control"),
}


class RebaselineError(RuntimeError):
    """A fail-closed validation or compare-and-swap failure."""


@dataclass(frozen=True)
class _ManifestSnapshot:
    path: Path
    payload: bytes
    identity: tuple[int, int]
    records: dict[str, dict[str, Any]]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class _SkillState:
    name: str
    source_root: Path
    source_path: Path
    source_hash: str
    source_manifest_hash: str
    source_identity: tuple[int, int]
    target_root: Path
    target_path: Path
    target_hash: str
    target_manifest_hash: str
    target_identity: tuple[int, int]
    install_path: str
    recorded_manifest_hash: str
    manifest: _ManifestSnapshot


def _validate_skill_name(value: object) -> str:
    if not isinstance(value, str) or not _SKILL_NAME_RE.fullmatch(value):
        raise RebaselineError(f"invalid skill name: {value!r}")
    if any(char in value for char in _GLOB_CHARS) or value in {".", ".."}:
        raise RebaselineError(f"invalid skill name: {value!r}")
    return value


def _absolute_path(value: Path | str, label: str) -> Path:
    raw = os.fspath(value)
    if "\x00" in raw or any(char in raw for char in _GLOB_CHARS):
        raise RebaselineError(f"unsafe {label} path: {raw!r}")
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise RebaselineError(f"{label} path must be absolute and normalized: {raw!r}")
    return path


def _directory_root(value: Path | str, label: str) -> Path:
    path = _absolute_path(value, label)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RebaselineError(f"{label} directory is unavailable: {path}") from exc
    if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
        raise RebaselineError(
            f"{label} must be a real directory, not a symlink: {path}"
        )
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise RebaselineError(f"{label} directory cannot be resolved: {path}") from exc


def _is_ignored(relative: Path) -> bool:
    return is_sync_ignored_path(relative)


def _tree_file_open_barrier(_path: Path) -> None:
    """Test seam between tree-file lstat and no-follow open."""
    return None


def _tree_file_post_read_barrier(_path: Path) -> None:
    """Test seam between stable FD read and final path lstat."""
    return None


def _tree_directory_post_digest_barrier(_root: Path) -> None:
    """Test seam before directory identities/types are revalidated."""
    return None


def _stable_stat_tuple(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        stat.S_IMODE(value.st_mode),
    )


def _directory_stat_tuple(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
    )


def _update_tree_digest_entry(
    digest: Any,
    entry_type: bytes,
    relative_bytes: bytes,
    mode: int,
    content_length: int,
) -> None:
    digest.update(entry_type)
    digest.update(len(relative_bytes).to_bytes(8, "big"))
    digest.update(relative_bytes)
    digest.update(stat.S_IMODE(mode).to_bytes(4, "big"))
    digest.update(content_length.to_bytes(8, "big"))


def _tree_digest(root: Path) -> str:
    """Hash a managed tree with framed SHA-256 and identity-stable reads."""
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise RebaselineError(f"tree root is unavailable: {root}") from exc
    if root.is_symlink() or not stat.S_ISDIR(root_stat.st_mode):
        raise RebaselineError(f"tree root must be a real directory: {root}")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RebaselineError("tree no-follow reads are unavailable")

    directory_snapshots: list[tuple[Path, tuple[int, ...]]] = [
        (root, _directory_stat_tuple(root_stat))
    ]
    entries: list[tuple[bytes, Path, os.stat_result]] = []
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if _is_ignored(relative):
            continue
        try:
            relative_bytes = relative.as_posix().encode("utf-8")
            candidate_stat = candidate.lstat()
        except (UnicodeEncodeError, OSError) as exc:
            raise RebaselineError(f"tree path is unreadable: {candidate}") from exc
        if candidate.is_symlink():
            raise RebaselineError(f"tree contains a managed symlink: {candidate}")
        if not (
            stat.S_ISDIR(candidate_stat.st_mode) or stat.S_ISREG(candidate_stat.st_mode)
        ):
            raise RebaselineError(f"tree contains a non-file entry: {candidate}")
        if stat.S_ISDIR(candidate_stat.st_mode):
            directory_snapshots.append((
                candidate,
                _directory_stat_tuple(candidate_stat),
            ))
        entries.append((relative_bytes, candidate, candidate_stat))

    digest = hashlib.sha256(b"HERMES_WORKSPACE_SKILL_TREE_SHA256_V2\0")
    _update_tree_digest_entry(digest, b"D", b"", root_stat.st_mode, 0)
    for relative_bytes, candidate, path_before in sorted(
        entries, key=lambda item: item[0]
    ):
        if stat.S_ISDIR(path_before.st_mode):
            _update_tree_digest_entry(
                digest,
                b"D",
                relative_bytes,
                path_before.st_mode,
                0,
            )
            continue

        fd: int | None = None
        try:
            _tree_file_open_barrier(candidate)
            fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or _stable_stat_tuple(
                before
            ) != _stable_stat_tuple(path_before):
                raise RebaselineError(f"tree file changed before opening: {candidate}")
            _update_tree_digest_entry(
                digest,
                b"F",
                relative_bytes,
                before.st_mode,
                before.st_size,
            )
            read_size = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                read_size += len(chunk)
                digest.update(chunk)
            after = os.fstat(fd)
            _tree_file_post_read_barrier(candidate)
            path_after = candidate.lstat()
        except RebaselineError:
            raise
        except OSError as exc:
            raise RebaselineError(
                f"tree file is a symlink or changed: {candidate}"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
        if (
            read_size != before.st_size
            or _stable_stat_tuple(before) != _stable_stat_tuple(after)
            or _stable_stat_tuple(after) != _stable_stat_tuple(path_after)
        ):
            raise RebaselineError(f"tree file changed while reading: {candidate}")
    _tree_directory_post_digest_barrier(root)
    for directory, expected in directory_snapshots:
        try:
            directory_stat = directory.lstat()
            observed = _directory_stat_tuple(directory_stat)
        except OSError as exc:
            raise RebaselineError(f"tree directory changed: {directory}") from exc
        if observed != expected or directory.is_symlink():
            raise RebaselineError(f"tree directory changed: {directory}")
    return digest.hexdigest()


def _assert_managed_tree_safe(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
        path_stat = path.lstat()
    except (OSError, ValueError) as exc:
        raise RebaselineError(f"{label} path escapes its root: {path}") from exc
    if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
        raise RebaselineError(f"{label} must be a real directory: {path}")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RebaselineError(f"{label} path escapes its root: {path}") from exc

    for candidate in path.rglob("*"):
        relative = candidate.relative_to(path)
        if _is_ignored(relative):
            continue
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise RebaselineError(
                f"{label} changed while validating: {candidate}"
            ) from exc
        if candidate.is_symlink():
            raise RebaselineError(f"{label} contains a managed symlink: {candidate}")
        if not (
            stat.S_ISDIR(candidate_stat.st_mode) or stat.S_ISREG(candidate_stat.st_mode)
        ):
            raise RebaselineError(f"{label} contains a non-file entry: {candidate}")


def _json_object(raw: str, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RebaselineError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RebaselineError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RebaselineError(f"{label} must be a JSON object")
    return value


def _manifest_open_barrier(_manifest_file: Path) -> None:
    """Test seam between manifest path validation and its no-follow open."""
    return None


def _manifest_snapshot(
    manifest_file: Path | str, target_root: Path
) -> _ManifestSnapshot:
    path = _absolute_path(manifest_file, "manifest")
    try:
        manifest_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise RebaselineError(f"manifest parent is unavailable: {path.parent}") from exc
    if manifest_parent != target_root:
        raise RebaselineError("manifest must be a direct child of the target root")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RebaselineError("manifest no-follow reads are unavailable")
    fd: int | None = None
    try:
        path_before = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(path_before.st_mode):
            raise RebaselineError(
                f"manifest must be a regular file, not a symlink: {path}"
            )
        if stat.S_IMODE(path_before.st_mode) != 0o600:
            raise RebaselineError(f"manifest mode must be 0600: {path}")
        _manifest_open_barrier(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RebaselineError(f"manifest must be a regular file: {path}")
        if (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino):
            raise RebaselineError(f"manifest changed before opening: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(16 * 1024 * 1024 + 1)
        after = os.fstat(fd)
        path_after = path.lstat()
    except RebaselineError:
        raise
    except OSError as exc:
        raise RebaselineError(f"manifest is a symlink or changed: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if len(payload) > 16 * 1024 * 1024:
        raise RebaselineError("manifest is too large")
    identity = (before.st_dev, before.st_ino)
    before_snapshot = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
    )
    after_snapshot = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
    )
    if (
        before_snapshot != after_snapshot
        or identity != (path_after.st_dev, path_after.st_ino)
        or len(payload) != before.st_size
    ):
        raise RebaselineError(f"manifest changed while reading: {path}")

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RebaselineError("manifest is not UTF-8") from exc
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if ":" not in line:
            name = line.strip()
            record: dict[str, Any] = {"hash": ""}
        else:
            name, _, raw_record = line.partition(":")
            name = name.strip()
            raw_record = raw_record.strip()
            record = (
                _json_object(raw_record, f"manifest line {line_number}")
                if raw_record.startswith("{")
                else {"hash": raw_record}
            )
        _validate_skill_name(name)
        if name in records:
            raise RebaselineError(f"manifest contains duplicate skill {name!r}")
        records[name] = record
    return _ManifestSnapshot(path, payload, identity, records)


def _find_source_skill(name: str, source_root: Path) -> Path:
    matches: list[Path] = []
    for skill_md in source_root.rglob("SKILL.md"):
        relative = skill_md.relative_to(source_root)
        if is_excluded_skill_path(skill_md) or _is_ignored(relative):
            continue
        if skill_md.is_symlink():
            raise RebaselineError(f"source contains a managed symlink: {skill_md}")
        candidate_name = _read_skill_name(skill_md, skill_md.parent.name)
        if candidate_name == name:
            matches.append(skill_md.parent)
    if not matches:
        raise RebaselineError(f"skill {name!r} is absent from the source root")
    if len(matches) != 1:
        raise RebaselineError(f"skill {name!r} is ambiguous in the source root")
    return matches[0]


def _resolve_install_path(raw: object, target_root: Path) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RebaselineError("manifest install_path is missing or invalid")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(char in raw for char in _GLOB_CHARS)
    ):
        raise RebaselineError(f"manifest install_path is unsafe: {raw!r}")
    normalized = "/".join(pure.parts)
    target_path = target_root.joinpath(*pure.parts)
    try:
        target_path.relative_to(target_root)
        target_path.resolve(strict=True).relative_to(target_root)
    except (OSError, ValueError) as exc:
        raise RebaselineError(f"manifest install_path escapes target: {raw!r}") from exc
    return normalized, target_path


def _inspect_state(
    skill_name: str,
    *,
    source_root: Path | str,
    target_root: Path | str,
    manifest_file: Path | str,
) -> _SkillState:
    name = _validate_skill_name(skill_name)
    source = _directory_root(source_root, "source")
    target = _directory_root(target_root, "target")
    manifest = _manifest_snapshot(manifest_file, target)
    record = manifest.records.get(name)
    if not isinstance(record, dict):
        raise RebaselineError(f"skill {name!r} is not tracked by the manifest")
    install_path, target_path = _resolve_install_path(
        record.get("install_path"), target
    )
    provenance = record.get("provenance")
    if not (
        isinstance(provenance, dict)
        and provenance.get("kind") == "workspace"
        and provenance.get("source_root") == str(source)
    ):
        raise RebaselineError(f"manifest provenance does not own skill {name!r}")
    recorded_hash = record.get("hash")
    if not isinstance(recorded_hash, str) or not _MD5_RE.fullmatch(recorded_hash):
        raise RebaselineError(f"manifest hash is invalid for skill {name!r}")

    source_path = _find_source_skill(name, source)
    source_install_path = source_path.relative_to(source).as_posix()
    if source_install_path != install_path:
        raise RebaselineError(
            f"manifest install_path does not match source for skill {name!r}"
        )
    _assert_managed_tree_safe(source_path, source, "source skill")
    _assert_managed_tree_safe(target_path, target, "target skill")
    source_stat = source_path.lstat()
    target_stat = target_path.lstat()
    return _SkillState(
        name=name,
        source_root=source,
        source_path=source_path,
        source_hash=_tree_digest(source_path),
        source_manifest_hash=_dir_hash(source_path),
        source_identity=(source_stat.st_dev, source_stat.st_ino),
        target_root=target,
        target_path=target_path,
        target_hash=_tree_digest(target_path),
        target_manifest_hash=_dir_hash(target_path),
        target_identity=(target_stat.st_dev, target_stat.st_ino),
        install_path=install_path,
        recorded_manifest_hash=recorded_hash,
        manifest=manifest,
    )


def _state_is_aligned(state: _SkillState) -> bool:
    return (
        state.source_hash == state.target_hash
        and state.source_manifest_hash == state.target_manifest_hash
        and state.recorded_manifest_hash == state.source_manifest_hash
    )


def inspect_request(
    skill_name: str,
    *,
    source_root: Path | str,
    target_root: Path | str,
    manifest_file: Path | str,
) -> dict[str, object]:
    """Return the exact content-addressed request accepted by ``apply``."""
    state = _inspect_state(
        skill_name,
        source_root=source_root,
        target_root=target_root,
        manifest_file=manifest_file,
    )
    if _state_is_aligned(state):
        raise RebaselineError(f"skill {state.name!r} is already aligned; no-op refused")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "skill_name": state.name,
        "expected_source_hash": state.source_hash,
        "expected_target_hash": state.target_hash,
        "expected_manifest_hash": state.manifest.digest,
    }


def _request_open_barrier(_path: Path) -> None:
    """Test seam between request lstat and no-follow open."""
    return None


def _request_post_read_barrier(_path: Path) -> None:
    """Test seam between request FD read and final path lstat."""
    return None


def _request_stat_tuple(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        stat.S_IMODE(value.st_mode),
        value.st_uid,
    )


def _load_request(request_file: Path | str) -> dict[str, object]:
    path = _absolute_path(request_file, "request")
    try:
        path_before = path.lstat()
    except OSError as exc:
        raise RebaselineError(f"request file is unavailable: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(path_before.st_mode):
        raise RebaselineError(f"request file must not be a symlink: {path}")
    if stat.S_IMODE(path_before.st_mode) != 0o600:
        raise RebaselineError(f"request mode must be 0600: {path}")
    if path_before.st_uid != os.geteuid():
        raise RebaselineError(
            f"request owner must match effective uid {os.geteuid()}: {path}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise RebaselineError("request no-follow reads are unavailable")

    fd: int | None = None
    try:
        _request_open_barrier(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _request_stat_tuple(
            path_before
        ) != _request_stat_tuple(before):
            raise RebaselineError(f"request file changed before opening: {path}")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise RebaselineError(f"request mode must be 0600: {path}")
        if before.st_uid != os.geteuid():
            raise RebaselineError(
                f"request owner must match effective uid {os.geteuid()}: {path}"
            )
        if not stat.S_ISREG(before.st_mode):
            raise RebaselineError(f"request file must be regular: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(64 * 1024 + 1)
        after = os.fstat(fd)
        _request_post_read_barrier(path)
        path_after = path.lstat()
    except RebaselineError:
        raise
    except OSError as exc:
        raise RebaselineError(f"request file is a symlink or changed: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if len(payload) > 64 * 1024:
        raise RebaselineError("request file is too large")
    if (
        _request_stat_tuple(before) != _request_stat_tuple(after)
        or _request_stat_tuple(after) != _request_stat_tuple(path_after)
        or len(payload) != before.st_size
    ):
        raise RebaselineError("request file changed while reading")
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RebaselineError("request file is not UTF-8") from exc
    request = _json_object(raw, "request")
    if set(request) != REQUEST_FIELDS:
        raise RebaselineError(
            f"request fields must be exactly {sorted(REQUEST_FIELDS)!r}"
        )
    if (
        isinstance(request["schema_version"], bool)
        or request["schema_version"] != REQUEST_SCHEMA_VERSION
    ):
        raise RebaselineError(
            f"request schema_version must be {REQUEST_SCHEMA_VERSION}"
        )
    request["skill_name"] = _validate_skill_name(request["skill_name"])
    for field in (
        "expected_source_hash",
        "expected_target_hash",
        "expected_manifest_hash",
    ):
        value = request[field]
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise RebaselineError(f"request field {field!r} has an invalid hash")
    return request


def _assert_request_matches(request: dict[str, object], state: _SkillState) -> None:
    if request["expected_source_hash"] != state.source_hash:
        raise RebaselineError("stale request: source hash changed")
    if request["expected_target_hash"] != state.target_hash:
        raise RebaselineError("stale request: target hash changed")
    if request["expected_manifest_hash"] != state.manifest.digest:
        raise RebaselineError("stale request: manifest hash changed")


def _assert_transaction_identity(initial: _SkillState, current: _SkillState) -> None:
    _assert_request_matches(
        {
            "expected_source_hash": initial.source_hash,
            "expected_target_hash": initial.target_hash,
            "expected_manifest_hash": initial.manifest.digest,
        },
        current,
    )
    if current.source_identity != initial.source_identity:
        raise RebaselineError("stale request: source identity changed")
    if current.target_identity != initial.target_identity:
        raise RebaselineError("stale request: target identity changed")
    if current.manifest.identity != initial.manifest.identity:
        raise RebaselineError("stale request: manifest identity changed")


def _pre_replace_barrier(_target_path: Path) -> None:
    """Test seam immediately before the final expectation recheck."""
    return None


def _post_copy_barrier(_target_path: Path) -> None:
    """Test seam after live publication and before manifest mutation."""
    return None


def _manifest_update_barrier(_manifest_file: Path) -> None:
    """Test seam immediately before the manifest compare-and-swap."""
    return None


def _manifest_post_publish_barrier(_manifest_file: Path) -> None:
    """Test seam immediately after manifest publication."""
    return None


def _archive_fstat(fd: int) -> os.stat_result:
    return os.fstat(fd)


@dataclass
class _ArchiveRoot:
    path: Path
    fd: int
    identity: tuple[int, int]
    target_device: int

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _directory_open_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RebaselineError("directory no-follow operations are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _archive_root_post_open_barrier(_path: Path) -> None:
    """Test seam after archive-root FD validation and before path rebinding."""
    return None


def _assert_archive_root_binding(archive: _ArchiveRoot) -> os.stat_result:
    """Bind the lexical archive path to its protected directory descriptor."""
    try:
        held_stat = _archive_fstat(archive.fd)
        path_stat = archive.path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            raise RebaselineError(
                f"archive root changed to a symlink or non-directory: {archive.path}"
            )
        path_fd = os.open(archive.path, _directory_open_flags())
        try:
            opened_stat = _archive_fstat(path_fd)
        finally:
            os.close(path_fd)
    except RebaselineError:
        raise
    except OSError as exc:
        raise RebaselineError(
            f"archive root is a symlink or changed: {archive.path}"
        ) from exc

    expected = archive.identity
    if (
        (held_stat.st_dev, held_stat.st_ino) != expected
        or (path_stat.st_dev, path_stat.st_ino) != expected
        or (opened_stat.st_dev, opened_stat.st_ino) != expected
    ):
        raise RebaselineError(f"archive root changed after validation: {archive.path}")
    if held_stat.st_dev != archive.target_device:
        raise RebaselineError(
            f"archive root must be on the same filesystem as target: {archive.path}"
        )
    if stat.S_IMODE(held_stat.st_mode) != 0o700:
        raise RebaselineError(f"archive root mode must be 0700: {archive.path}")
    if held_stat.st_uid != os.geteuid():
        raise RebaselineError(
            f"archive root owner must match effective uid: {archive.path}"
        )
    return held_stat


def _ensure_archive_root(target_root: Path) -> _ArchiveRoot:
    """Open and pin an excluded, private, same-filesystem transaction root."""
    target_fd: int | None = None
    archive_fd: int | None = None
    root_fd: int | None = None
    archive_path = target_root / ".archive"
    root_path = archive_path / "rebaseline"
    try:
        flags = _directory_open_flags()
        target_fd = os.open(target_root, flags)
        target_stat = _archive_fstat(target_fd)
        target_path_stat = target_root.lstat()
        if (target_stat.st_dev, target_stat.st_ino) != (
            target_path_stat.st_dev,
            target_path_stat.st_ino,
        ):
            raise RebaselineError(f"target root changed: {target_root}")

        try:
            os.mkdir(".archive", 0o700, dir_fd=target_fd)
        except FileExistsError:
            pass
        archive_fd = os.open(".archive", flags, dir_fd=target_fd)
        archive_stat = _archive_fstat(archive_fd)
        if archive_stat.st_dev != target_stat.st_dev:
            raise RebaselineError(
                f"archive root must be on the same filesystem as target: {archive_path}"
            )

        try:
            os.mkdir("rebaseline", 0o700, dir_fd=archive_fd)
        except FileExistsError:
            pass
        root_fd = os.open("rebaseline", flags, dir_fd=archive_fd)
        root_stat = _archive_fstat(root_fd)
        if root_stat.st_dev != target_stat.st_dev:
            raise RebaselineError(
                f"archive root must be on the same filesystem as target: {root_path}"
            )
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise RebaselineError(
                f"archive rebaseline root mode must be 0700: {root_path}"
            )
        if root_stat.st_uid != os.geteuid():
            raise RebaselineError(
                f"archive rebaseline root owner must match effective uid: {root_path}"
            )
        if not is_excluded_skill_path(root_path / "SKILL.md"):
            raise RebaselineError(
                f"archive root is not excluded from skill discovery: {root_path}"
            )

        result = _ArchiveRoot(
            path=root_path,
            fd=root_fd,
            identity=(root_stat.st_dev, root_stat.st_ino),
            target_device=target_stat.st_dev,
        )
        root_fd = None
        _archive_root_post_open_barrier(root_path)
        _assert_archive_root_binding(result)
        return result
    except RebaselineError:
        if root_fd is None and "result" in locals():
            result.close()
        raise
    except OSError as exc:
        if root_fd is None and "result" in locals():
            result.close()
        raise RebaselineError(
            f"archive path is a symlink or changed: {root_path}"
        ) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if archive_fd is not None:
            os.close(archive_fd)
        if target_fd is not None:
            os.close(target_fd)


def _rename_noreplace_at(
    source: Path | str,
    destination: Path | str,
    *,
    source_dir_fd: int | None = None,
    destination_dir_fd: int | None = None,
) -> None:
    """Atomically rename without replacement using pinned directory FDs."""
    if platform.system() != "Darwin":
        raise OSError(errno.ENOTSUP, "atomic renameat no-replace unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    rc = renameatx_np(
        source_dir_fd if source_dir_fd is not None else -2,
        os.fsencode(source),
        destination_dir_fd if destination_dir_fd is not None else -2,
        os.fsencode(destination),
        0x00000004,
    )
    if rc != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination))


def _archive_display_path(archive: _ArchiveRoot, name: str) -> Path:
    return archive.path / name


def _new_archive_name(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(12)}"


def _archive_entry_stat(archive: _ArchiveRoot, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=archive.fd, follow_symlinks=False)
    except OSError as exc:
        raise RebaselineError(
            f"archive entry is unavailable: {_archive_display_path(archive, name)}"
        ) from exc


def _archive_entry_exists(archive: _ArchiveRoot, name: str) -> bool:
    try:
        os.stat(name, dir_fd=archive.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _archive_copy_barrier(_archive: _ArchiveRoot, _stage_name: str) -> None:
    """Test seam after private stage creation and before source copying."""
    return None


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _copy_directory_contents_fd(
    source_fd: int,
    destination_fd: int,
    relative_parts: tuple[str, ...],
) -> None:
    source_before = os.fstat(source_fd)
    if not stat.S_ISDIR(source_before.st_mode):
        raise RebaselineError("source changed to a non-directory during copy")
    try:
        names = sorted(os.listdir(source_fd), key=lambda value: value.encode("utf-8"))
    except (OSError, UnicodeEncodeError) as exc:
        raise RebaselineError("source directory is unreadable during copy") from exc

    for name in names:
        child_parts = (*relative_parts, name)
        relative = Path(*child_parts)
        if _is_ignored(relative):
            continue
        try:
            relative.as_posix().encode("utf-8")
            child_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except (OSError, UnicodeEncodeError) as exc:
            raise RebaselineError(
                f"source entry changed during copy: {relative}"
            ) from exc
        if stat.S_ISLNK(child_stat.st_mode):
            raise RebaselineError(f"source contains a managed symlink: {relative}")

        if stat.S_ISDIR(child_stat.st_mode):
            source_child_fd: int | None = None
            destination_child_fd: int | None = None
            try:
                source_child_fd = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=source_fd,
                )
                source_opened = os.fstat(source_child_fd)
                if _directory_stat_tuple(source_opened) != _directory_stat_tuple(
                    child_stat
                ):
                    raise RebaselineError(
                        f"source directory changed before copy: {relative}"
                    )
                os.mkdir(
                    name,
                    0o700,
                    dir_fd=destination_fd,
                )
                destination_child_fd = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=destination_fd,
                )
                _copy_directory_contents_fd(
                    source_child_fd,
                    destination_child_fd,
                    child_parts,
                )
                os.fchmod(
                    destination_child_fd,
                    stat.S_IMODE(source_opened.st_mode),
                )
                source_after = os.fstat(source_child_fd)
                path_after = os.stat(
                    name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                if _directory_stat_tuple(source_opened) != _directory_stat_tuple(
                    source_after
                ) or _directory_stat_tuple(source_after) != _directory_stat_tuple(
                    path_after
                ):
                    raise RebaselineError(
                        f"source directory changed during copy: {relative}"
                    )
            finally:
                if destination_child_fd is not None:
                    os.close(destination_child_fd)
                if source_child_fd is not None:
                    os.close(source_child_fd)
            continue

        if not stat.S_ISREG(child_stat.st_mode):
            raise RebaselineError(f"source contains a non-file entry: {relative}")
        source_file_fd: int | None = None
        destination_file_fd: int | None = None
        try:
            source_file_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=source_fd,
            )
            source_opened = os.fstat(source_file_fd)
            if not stat.S_ISREG(source_opened.st_mode) or _stable_stat_tuple(
                source_opened
            ) != _stable_stat_tuple(child_stat):
                raise RebaselineError(f"source file changed before copy: {relative}")
            destination_file_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                stat.S_IMODE(source_opened.st_mode),
                dir_fd=destination_fd,
            )
            read_size = 0
            while True:
                chunk = os.read(source_file_fd, 1024 * 1024)
                if not chunk:
                    break
                read_size += len(chunk)
                _write_all(destination_file_fd, chunk)
            os.fchmod(destination_file_fd, stat.S_IMODE(source_opened.st_mode))
            source_after = os.fstat(source_file_fd)
            path_after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if (
                read_size != source_opened.st_size
                or _stable_stat_tuple(source_opened) != _stable_stat_tuple(source_after)
                or _stable_stat_tuple(source_after) != _stable_stat_tuple(path_after)
            ):
                raise RebaselineError(f"source file changed during copy: {relative}")
        finally:
            if destination_file_fd is not None:
                os.close(destination_file_fd)
            if source_file_fd is not None:
                os.close(source_file_fd)

    source_after = os.fstat(source_fd)
    if _directory_stat_tuple(source_before) != _directory_stat_tuple(source_after):
        raise RebaselineError("source directory changed during copy")


def _copy_source_tree_to_archive(
    source: Path,
    archive: _ArchiveRoot,
    prefix: str,
) -> str:
    """Copy a source tree into a new archive child using only openat writes."""
    _assert_archive_root_binding(archive)
    stage_name = ""
    for _attempt in range(128):
        candidate = _new_archive_name(prefix)
        try:
            os.mkdir(candidate, 0o700, dir_fd=archive.fd)
            stage_name = candidate
            break
        except FileExistsError:
            continue
    if not stage_name:
        raise RebaselineError("could not allocate a unique archive stage")

    stage_fd: int | None = None
    source_fd: int | None = None
    try:
        stage_fd = os.open(stage_name, _directory_open_flags(), dir_fd=archive.fd)
        stage_stat = os.fstat(stage_fd)
        if (
            not stat.S_ISDIR(stage_stat.st_mode)
            or stage_stat.st_dev != archive.target_device
            or stage_stat.st_uid != os.geteuid()
        ):
            raise RebaselineError(
                f"archive stage is unsafe: {_archive_display_path(archive, stage_name)}"
            )
        _archive_copy_barrier(archive, stage_name)

        source_path_stat = source.lstat()
        source_fd = os.open(source, _directory_open_flags())
        source_opened = os.fstat(source_fd)
        if _directory_stat_tuple(source_path_stat) != _directory_stat_tuple(
            source_opened
        ):
            raise RebaselineError(f"source root changed before copy: {source}")
        _copy_directory_contents_fd(source_fd, stage_fd, ())
        os.fchmod(stage_fd, stat.S_IMODE(source_opened.st_mode))
        source_after = os.fstat(source_fd)
        source_path_after = source.lstat()
        if _directory_stat_tuple(source_opened) != _directory_stat_tuple(
            source_after
        ) or _directory_stat_tuple(source_after) != _directory_stat_tuple(
            source_path_after
        ):
            raise RebaselineError(f"source root changed during copy: {source}")
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if stage_fd is not None:
            os.close(stage_fd)
    return stage_name


@dataclass(frozen=True)
class _FdTreeEntry:
    parts: tuple[str, ...]
    relative_bytes: bytes
    path_stat: os.stat_result


def _collect_fd_tree_entries(
    directory_fd: int,
    relative_parts: tuple[str, ...],
    entries: list[_FdTreeEntry],
    directories: list[tuple[tuple[str, ...], tuple[int, ...]]],
) -> None:
    directory_before = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_before.st_mode):
        raise RebaselineError("archive tree contains a non-directory root")
    directories.append((relative_parts, _directory_stat_tuple(directory_before)))
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise RebaselineError("archive tree directory is unreadable") from exc
    for name in names:
        parts = (*relative_parts, name)
        relative = Path(*parts)
        if _is_ignored(relative):
            continue
        try:
            relative_bytes = "/".join(parts).encode("utf-8")
            path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except (OSError, UnicodeEncodeError) as exc:
            raise RebaselineError(f"archive tree entry changed: {relative}") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            raise RebaselineError(f"archive tree contains a symlink: {relative}")
        if not (stat.S_ISDIR(path_stat.st_mode) or stat.S_ISREG(path_stat.st_mode)):
            raise RebaselineError(f"archive tree contains a non-file: {relative}")
        entries.append(_FdTreeEntry(parts, relative_bytes, path_stat))
        if stat.S_ISDIR(path_stat.st_mode):
            child_fd = os.open(
                name,
                _directory_open_flags(),
                dir_fd=directory_fd,
            )
            try:
                if _directory_stat_tuple(os.fstat(child_fd)) != _directory_stat_tuple(
                    path_stat
                ):
                    raise RebaselineError(f"archive tree directory changed: {relative}")
                _collect_fd_tree_entries(child_fd, parts, entries, directories)
            finally:
                os.close(child_fd)
    directory_after = os.fstat(directory_fd)
    if _directory_stat_tuple(directory_before) != _directory_stat_tuple(
        directory_after
    ):
        raise RebaselineError("archive tree directory changed during traversal")


def _open_directory_parts(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            following = os.open(
                part,
                _directory_open_flags(),
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current
    except Exception:
        os.close(current)
        raise


def _tree_digest_dir_fd(root_fd: int) -> str:
    """Compute the canonical framed tree digest from a pinned directory FD."""
    root_stat = os.fstat(root_fd)
    entries: list[_FdTreeEntry] = []
    directories: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
    _collect_fd_tree_entries(root_fd, (), entries, directories)
    if not directories or _directory_stat_tuple(root_stat) != directories[0][1]:
        raise RebaselineError("archive tree root changed before traversal")

    digest = hashlib.sha256(b"HERMES_WORKSPACE_SKILL_TREE_SHA256_V2\0")
    _update_tree_digest_entry(digest, b"D", b"", root_stat.st_mode, 0)
    for entry in sorted(entries, key=lambda value: value.relative_bytes):
        if stat.S_ISDIR(entry.path_stat.st_mode):
            _update_tree_digest_entry(
                digest,
                b"D",
                entry.relative_bytes,
                entry.path_stat.st_mode,
                0,
            )
            continue
        parent_fd = _open_directory_parts(root_fd, entry.parts[:-1])
        file_fd: int | None = None
        try:
            file_fd = os.open(
                entry.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or _stable_stat_tuple(
                before
            ) != _stable_stat_tuple(entry.path_stat):
                raise RebaselineError(
                    f"archive tree file changed: {'/'.join(entry.parts)}"
                )
            _update_tree_digest_entry(
                digest,
                b"F",
                entry.relative_bytes,
                before.st_mode,
                before.st_size,
            )
            read_size = 0
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                read_size += len(chunk)
                digest.update(chunk)
            after = os.fstat(file_fd)
            path_after = os.stat(
                entry.parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                read_size != before.st_size
                or _stable_stat_tuple(before) != _stable_stat_tuple(after)
                or _stable_stat_tuple(after) != _stable_stat_tuple(path_after)
            ):
                raise RebaselineError(
                    f"archive tree file changed: {'/'.join(entry.parts)}"
                )
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(parent_fd)

    for parts, expected in directories:
        directory_fd = _open_directory_parts(root_fd, parts)
        try:
            if _directory_stat_tuple(os.fstat(directory_fd)) != expected:
                raise RebaselineError(
                    f"archive tree directory changed: {'/'.join(parts) or '.'}"
                )
        finally:
            os.close(directory_fd)
    return digest.hexdigest()


def _archive_tree_digest(archive: _ArchiveRoot, name: str) -> str:
    directory_fd: int | None = None
    try:
        before = _archive_entry_stat(archive, name)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise RebaselineError(
                f"archive entry is not a real directory: {_archive_display_path(archive, name)}"
            )
        directory_fd = os.open(
            name,
            _directory_open_flags(),
            dir_fd=archive.fd,
        )
        opened = os.fstat(directory_fd)
        if _directory_stat_tuple(before) != _directory_stat_tuple(opened):
            raise RebaselineError(
                f"archive entry changed: {_archive_display_path(archive, name)}"
            )
        digest = _tree_digest_dir_fd(directory_fd)
        after = os.fstat(directory_fd)
        path_after = _archive_entry_stat(archive, name)
        if _directory_stat_tuple(opened) != _directory_stat_tuple(
            after
        ) or _directory_stat_tuple(after) != _directory_stat_tuple(path_after):
            raise RebaselineError(
                f"archive entry changed: {_archive_display_path(archive, name)}"
            )
        return digest
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _timestamped_recovery_name(target_path: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f".{target_path.name}.rebaseline-recovery-{stamp}"


def _rollback_move_barrier(_recovery_path: Path, _target_path: Path) -> None:
    """Test seam immediately before recovery restoration."""
    return None


def _target_publish_validation_barrier(_target_path: Path) -> None:
    """Test seam after target publication and before post-copy validation."""
    return None


def _rename_target_into_archive_unique(
    target_path: Path,
    archive: _ArchiveRoot,
    prefix: str,
) -> str:
    _assert_archive_root_binding(archive)
    for _attempt in range(128):
        name = _new_archive_name(prefix)
        try:
            _rename_noreplace_at(
                target_path,
                name,
                destination_dir_fd=archive.fd,
            )
            return name
        except FileExistsError:
            continue
    raise RebaselineError("could not allocate a unique archive destination")


def _rename_archive_entry_to_target(
    archive: _ArchiveRoot,
    name: str,
    target_path: Path,
) -> None:
    _assert_archive_root_binding(archive)
    _rename_noreplace_at(
        name,
        target_path,
        source_dir_fd=archive.fd,
    )


def _publish_stage_to_target(
    stage_name: str,
    target_path: Path,
    archive: _ArchiveRoot,
    expected_tree_hash: str,
) -> tuple[int, int]:
    """Publish a staged tree and quarantine failures only under ``.archive``."""
    _assert_archive_root_binding(archive)
    stage_stat = _archive_entry_stat(archive, stage_name)
    stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
    if _archive_tree_digest(archive, stage_name) != expected_tree_hash:
        raise RebaselineError("staged tree changed before target publication")
    _rename_archive_entry_to_target(archive, stage_name, target_path)
    _target_publish_validation_barrier(target_path)
    try:
        target_stat = target_path.lstat()
        target_identity = (target_stat.st_dev, target_stat.st_ino)
        valid = (
            target_identity == stage_identity
            and _tree_digest(target_path) == expected_tree_hash
        )
    except (OSError, RebaselineError):
        target_identity = None
        valid = False
    if valid:
        return stage_identity

    quarantine_path: Path | None = None
    if target_identity == stage_identity:
        try:
            quarantine_name = _rename_target_into_archive_unique(
                target_path,
                archive,
                f".{target_path.name}.rebaseline-publish-quarantine-",
            )
            quarantine_path = _archive_display_path(archive, quarantine_name)
            quarantine_stat = _archive_entry_stat(archive, quarantine_name)
            if (quarantine_stat.st_dev, quarantine_stat.st_ino) != stage_identity:
                raise RebaselineError(
                    f"published target quarantine identity changed: {quarantine_path}"
                )
        except Exception as exc:
            raise RebaselineError(
                f"published target changed and quarantine failed at "
                f"{quarantine_path}: {exc}"
            ) from exc
    raise RebaselineError(
        f"published target changed before verification; quarantine={quarantine_path}"
    )


def _rollback_target(
    target_path: Path,
    recovery_name: str,
    installed_identity: tuple[int, int] | None,
    installed_hash: str,
    recovery_identity: tuple[int, int],
    recovery_hash: str,
    archive: _ArchiveRoot,
) -> dict[str, object]:
    """Restore the original only when every observed object is still owned."""
    errors: list[str] = []
    quarantines: list[str] = []
    recovery_path = _archive_display_path(archive, recovery_name)

    current_identity: tuple[int, int] | None = None
    current_hash: str | None = None
    if os.path.lexists(target_path):
        try:
            current_stat = target_path.lstat()
            current_identity = (current_stat.st_dev, current_stat.st_ino)
            current_hash = _tree_digest(target_path)
        except Exception as exc:
            errors.append(f"could not inspect live target during rollback: {exc}")

    already_restored = (
        current_identity == recovery_identity and current_hash == recovery_hash
    )
    if (
        not already_restored
        and installed_identity is not None
        and current_identity == installed_identity
        and current_hash == installed_hash
    ):
        quarantine: Path | None = None
        try:
            quarantine_name = _rename_target_into_archive_unique(
                target_path,
                archive,
                f".{target_path.name}.rebaseline-rollback-quarantine-",
            )
            quarantine = _archive_display_path(archive, quarantine_name)
            quarantine_stat = _archive_entry_stat(archive, quarantine_name)
            if (
                quarantine_stat.st_dev,
                quarantine_stat.st_ino,
            ) != installed_identity or _archive_tree_digest(
                archive, quarantine_name
            ) != installed_hash:
                errors.append(f"rollback quarantine verification failed: {quarantine}")
            quarantines.append(str(quarantine))
        except Exception as exc:
            if (
                quarantine is not None
                and _archive_entry_exists(archive, quarantine.name)
                and str(quarantine) not in quarantines
            ):
                quarantines.append(str(quarantine))
            errors.append(
                f"could not quarantine installed target at {quarantine}: {exc}"
            )
    elif not already_restored and os.path.lexists(target_path):
        errors.append(
            f"live target changed concurrently and was preserved: {target_path}"
        )

    recovery_owned = False
    if _archive_entry_exists(archive, recovery_name):
        try:
            recovery_stat = _archive_entry_stat(archive, recovery_name)
            recovery_owned = (
                not stat.S_ISLNK(recovery_stat.st_mode)
                and (recovery_stat.st_dev, recovery_stat.st_ino) == recovery_identity
                and _archive_tree_digest(archive, recovery_name) == recovery_hash
            )
        except Exception as exc:
            errors.append(f"could not verify recovery path {recovery_path}: {exc}")
            recovery_owned = False
    if not os.path.lexists(target_path) and recovery_owned:
        try:
            _rollback_move_barrier(recovery_path, target_path)
            _rename_archive_entry_to_target(archive, recovery_name, target_path)
        except Exception as exc:
            errors.append(f"could not restore recovery {recovery_path}: {exc}")
    elif not os.path.lexists(target_path) and not recovery_owned:
        errors.append(f"owned recovery is unavailable: {recovery_path}")

    target_restored = False
    try:
        final_stat = target_path.lstat()
        target_restored = (
            final_stat.st_dev,
            final_stat.st_ino,
        ) == recovery_identity and _tree_digest(target_path) == recovery_hash
    except Exception as exc:
        errors.append(f"could not verify final target state {target_path}: {exc}")
    return {
        "target_restored": target_restored,
        "recovery_path": str(recovery_path),
        "quarantine_paths": quarantines,
        "errors": errors,
    }


def apply_request(
    request_file: Path | str,
    *,
    source_root: Path | str,
    target_root: Path | str,
    manifest_file: Path | str,
) -> dict[str, object]:
    """Apply one exact rebaseline request as a fail-closed transaction."""
    request = _load_request(request_file)
    name = str(request["skill_name"])
    initial = _inspect_state(
        name,
        source_root=source_root,
        target_root=target_root,
        manifest_file=manifest_file,
    )
    if _state_is_aligned(initial):
        raise RebaselineError(
            f"skill {initial.name!r} is already aligned; replay/no-op refused"
        )
    _assert_request_matches(request, initial)

    archive = _ensure_archive_root(initial.target_root)
    try:
        return _apply_request_with_archive(initial, archive)
    finally:
        archive.close()


def _apply_request_with_archive(
    initial: _SkillState,
    archive: _ArchiveRoot,
) -> dict[str, object]:
    try:
        stage_name = _copy_source_tree_to_archive(
            initial.source_path,
            archive,
            f".{initial.target_path.name}.rebaseline-stage-",
        )
    except Exception as exc:
        raise RebaselineError(f"source partial copy failed: {exc}") from exc
    if _archive_tree_digest(archive, stage_name) != initial.source_hash:
        raise RebaselineError("source changed during staged copy")

    current = _inspect_state(
        initial.name,
        source_root=initial.source_root,
        target_root=initial.target_root,
        manifest_file=initial.manifest.path,
    )
    _assert_transaction_identity(initial, current)
    _pre_replace_barrier(initial.target_path)
    current = _inspect_state(
        initial.name,
        source_root=initial.source_root,
        target_root=initial.target_root,
        manifest_file=initial.manifest.path,
    )
    _assert_transaction_identity(initial, current)

    recovery_name = _timestamped_recovery_name(initial.target_path)
    recovery_path = _archive_display_path(archive, recovery_name)
    if _archive_entry_exists(archive, recovery_name):
        raise RebaselineError(
            f"timestamped recovery path already exists: {recovery_path}"
        )
    installed_identity: tuple[int, int] | None = None
    published_manifest_identity: tuple[int, int] | None = None
    published_manifest_bytes: bytes | None = None
    recovery_identity = initial.target_identity
    try:
        _assert_archive_root_binding(archive)
        _rename_noreplace_at(
            initial.target_path,
            recovery_name,
            destination_dir_fd=archive.fd,
        )
        recovery_stat = _archive_entry_stat(archive, recovery_name)
        recovery_identity = (recovery_stat.st_dev, recovery_stat.st_ino)
        if (
            recovery_identity != initial.target_identity
            or _archive_tree_digest(archive, recovery_name) != initial.target_hash
        ):
            raise RebaselineError("target changed while creating timestamped recovery")

        installed_identity = _publish_stage_to_target(
            stage_name,
            initial.target_path,
            archive,
            initial.source_hash,
        )
        _assert_managed_tree_safe(
            initial.target_path,
            initial.target_root,
            "published target skill",
        )
        if _tree_digest(initial.target_path) != initial.source_hash:
            raise RebaselineError("post-copy target hash verification failed")

        _post_copy_barrier(initial.target_path)
        post_copy = _inspect_state(
            initial.name,
            source_root=initial.source_root,
            target_root=initial.target_root,
            manifest_file=initial.manifest.path,
        )
        if post_copy.source_hash != initial.source_hash:
            raise RebaselineError("source changed after target publication")
        if post_copy.source_identity != initial.source_identity:
            raise RebaselineError("source identity changed after target publication")
        if post_copy.target_hash != initial.source_hash:
            raise RebaselineError("post-copy target hash verification failed")
        if post_copy.target_manifest_hash != initial.source_manifest_hash:
            raise RebaselineError("post-copy target manifest hash verification failed")
        if post_copy.target_identity != installed_identity:
            raise RebaselineError(
                "published target identity changed before manifest update"
            )
        if (
            post_copy.manifest.digest != initial.manifest.digest
            or post_copy.manifest.identity != initial.manifest.identity
        ):
            raise RebaselineError("manifest changed before update")

        records = dict(initial.manifest.records)
        updated_record = dict(records[initial.name])
        updated_record["hash"] = initial.source_manifest_hash
        records[initial.name] = updated_record
        published_manifest_bytes = _manifest_payload(records)
        _manifest_update_barrier(initial.manifest.path)
        published_manifest_identity = _write_manifest(
            records,
            initial.manifest.path,
            expected_bytes=initial.manifest.payload,
            expected_identity=initial.manifest.identity,
        )
        _manifest_post_publish_barrier(initial.manifest.path)
        final_manifest = _manifest_snapshot(initial.manifest.path, initial.target_root)
        if (
            final_manifest.identity != published_manifest_identity
            or final_manifest.payload != published_manifest_bytes
        ):
            raise RebaselineError("manifest changed after publication")
        final_record = final_manifest.records.get(initial.name, {})
        if final_record.get("hash") != initial.source_manifest_hash:
            raise RebaselineError("manifest post-publication hash verification failed")
        _assert_archive_root_binding(archive)
        final_target_stat = initial.target_path.lstat()
        if (
            final_target_stat.st_dev,
            final_target_stat.st_ino,
        ) != installed_identity or _tree_digest(
            initial.target_path
        ) != initial.source_hash:
            raise RebaselineError("target changed after manifest publication")
    except Exception as exc:
        try:
            rollback = _rollback_target(
                initial.target_path,
                recovery_name,
                installed_identity,
                initial.source_hash,
                initial.target_identity,
                initial.target_hash,
                archive,
            )
        except Exception as rollback_exc:
            rollback = {
                "target_restored": False,
                "recovery_path": str(recovery_path),
                "quarantine_paths": [],
                "errors": [f"rollback execution failed: {rollback_exc}"],
            }
        rollback_errors = list(rollback["errors"])
        if (
            published_manifest_bytes is not None
            and published_manifest_identity is not None
        ):
            try:
                _restore_manifest_if_owned(
                    initial.manifest.path,
                    published_manifest_identity,
                    published_manifest_bytes,
                    initial.manifest.payload,
                )
            except Exception as restore_exc:
                rollback_errors.append(f"manifest restore failed: {restore_exc}")

        manifest_restored = False
        try:
            rollback_manifest = _manifest_snapshot(
                initial.manifest.path,
                initial.target_root,
            )
            manifest_restored = rollback_manifest.payload == initial.manifest.payload
        except RebaselineError as verify_exc:
            rollback_errors.append(
                f"could not verify final manifest state {initial.manifest.path}: {verify_exc}"
            )

        if "changed concurrently" in str(exc):
            cause = f"manifest changed concurrently: {exc}"
        elif isinstance(exc, RebaselineError):
            cause = str(exc)
        else:
            cause = f"rebaseline failed: {exc}"

        report = {
            "recovery_path": rollback["recovery_path"],
            "quarantine_paths": rollback["quarantine_paths"],
            "rollback_errors": rollback_errors,
            "target_restored": rollback["target_restored"],
            "manifest_restored": manifest_restored,
            "manifest_path": str(initial.manifest.path),
        }
        incomplete = (
            bool(rollback_errors)
            or not bool(rollback["target_restored"])
            or not manifest_restored
        )
        status = "rollback_incomplete" if incomplete else "rollback_complete"
        raise RebaselineError(
            f"{cause}; {status}={json.dumps(report, sort_keys=True)}"
        ) from exc

    return {
        "ok": True,
        "action": "rebaselined",
        "skill_name": initial.name,
        "source_hash": initial.source_hash,
        "previous_target_hash": initial.target_hash,
        "manifest_hash": hashlib.sha256(published_manifest_bytes).hexdigest(),
        "recovery_path": str(recovery_path),
    }


def _discover_source_skills(source_root: Path) -> dict[str, Path]:
    skills: dict[str, Path] = {}
    for skill_md in sorted(source_root.rglob("SKILL.md")):
        relative = skill_md.relative_to(source_root)
        if is_excluded_skill_path(skill_md) or _is_ignored(relative):
            continue
        if skill_md.is_symlink():
            raise RebaselineError(f"source contains a managed symlink: {skill_md}")
        name = _validate_skill_name(_read_skill_name(skill_md, skill_md.parent.name))
        if name in skills:
            raise RebaselineError(f"skill {name!r} is ambiguous in the source root")
        _assert_managed_tree_safe(
            skill_md.parent, source_root, f"source skill {name!r}"
        )
        skills[name] = skill_md.parent
    return skills


def _forbidden_marker_findings(
    skill_name: str,
    target_path: Path,
    target_root: Path,
    canonical_path: Path | None,
) -> list[dict[str, str]]:
    markers = _DEFAULT_FORBIDDEN_MARKERS.get(skill_name, ())
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in sorted(target_path.rglob("*")):
        relative = candidate.relative_to(target_path)
        if _is_ignored(relative) or not candidate.is_file() or candidate.is_symlink():
            continue
        relative_text = relative.as_posix().lower()
        try:
            payload = candidate.read_bytes().lower()
        except OSError as exc:
            raise RebaselineError(
                f"target changed while checking retired markers: {candidate}"
            ) from exc
        for marker in markers:
            marker_bytes = marker.encode("utf-8")
            if marker in relative_text or marker_bytes in payload:
                key = (marker, relative.as_posix())
                if key not in seen:
                    seen.add(key)
                    prefix = ""
                    if canonical_path is None or target_path != canonical_path:
                        prefix = target_path.relative_to(target_root).as_posix() + "/"
                    findings.append({
                        "skill_name": skill_name,
                        "marker": marker,
                        "path": prefix + relative.as_posix(),
                    })
                break
    return findings


def _discover_active_target_skills(target_root: Path) -> dict[str, list[Path]]:
    """Mirror Hermes runtime discovery while excluding canonical archive dirs."""
    discovered: dict[str, list[Path]] = {}
    for skill_md in sorted(target_root.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        if skill_md.is_symlink():
            raise RebaselineError(f"active runtime SKILL.md is a symlink: {skill_md}")
        name = _validate_skill_name(_read_skill_name(skill_md, skill_md.parent.name))
        discovered.setdefault(name, []).append(skill_md.parent)
    return discovered


def verify_roots(
    *,
    source_root: Path | str,
    target_root: Path | str,
    manifest_file: Path | str,
) -> dict[str, object]:
    """Verify all workspace-manifest skills against source and target hashes."""
    source = _directory_root(source_root, "source")
    target = _directory_root(target_root, "target")
    manifest = _manifest_snapshot(manifest_file, target)
    source_skills = _discover_source_skills(source)
    active_skills = _discover_active_target_skills(target)
    mismatches: list[dict[str, object]] = []
    forbidden_findings: list[dict[str, str]] = []
    active_duplicates = [
        {"skill_name": name, "paths": [str(path) for path in paths]}
        for name, paths in sorted(active_skills.items())
        if len(paths) > 1
    ]
    for duplicate in active_duplicates:
        mismatches.append({
            "skill_name": duplicate["skill_name"],
            "reason": "active_duplicate",
            "paths": duplicate["paths"],
        })

    for name, source_path in sorted(source_skills.items()):
        record = manifest.records.get(name)
        if record is None:
            mismatches.append({"skill_name": name, "reason": "manifest_missing"})
            continue
        try:
            state = _inspect_state(
                name,
                source_root=source,
                target_root=target,
                manifest_file=manifest.path,
            )
        except RebaselineError as exc:
            mismatches.append({
                "skill_name": name,
                "reason": "invalid_state",
                "error": str(exc),
            })
            continue
        record_hash = record.get("hash") if isinstance(record, dict) else None
        if (
            state.source_hash != state.target_hash
            or state.source_manifest_hash != record_hash
            or state.source_path != source_path
        ):
            mismatches.append({
                "skill_name": name,
                "reason": "hash_mismatch",
                "source_hash": state.source_hash,
                "target_hash": state.target_hash,
                "manifest_skill_hash": record_hash,
                "expected_manifest_skill_hash": state.source_manifest_hash,
            })
        if state.target_path not in active_skills.get(name, []):
            mismatches.append({
                "skill_name": name,
                "reason": "runtime_not_discoverable",
                "path": str(state.target_path),
            })

    for name in sorted(set(manifest.records) - set(source_skills)):
        mismatches.append({"skill_name": name, "reason": "source_missing"})

    for name, paths in sorted(active_skills.items()):
        canonical = None
        if name in manifest.records and name in source_skills:
            try:
                _install_path, canonical = _resolve_install_path(
                    manifest.records[name].get("install_path"), target
                )
            except RebaselineError:
                canonical = None
        for path in paths:
            findings = _forbidden_marker_findings(name, path, target, canonical)
            forbidden_findings.extend(findings)
            for finding in findings:
                mismatches.append({
                    "skill_name": name,
                    "reason": "forbidden_marker",
                    "marker": finding["marker"],
                    "path": finding["path"],
                })

    return {
        "ok": not mismatches,
        "checked": len(source_skills),
        "source": str(source),
        "target": str(target),
        "manifest": str(manifest.path),
        "manifest_hash": manifest.digest,
        "skill_hash_algorithm": "sha256-framed-tree-v2",
        "manifest_skill_hash_algorithm": "skills_sync_md5_v1",
        "manifest_hash_algorithm": "sha256",
        "mismatches": mismatches,
        "forbidden_findings": forbidden_findings,
        "active_duplicates": active_duplicates,
    }


def _write_request_output(path_value: Path | str, request: dict[str, object]) -> Path:
    path = _absolute_path(path_value, "output")
    try:
        parent = path.parent.resolve(strict=True)
        parent_stat = parent.lstat()
    except OSError as exc:
        raise RebaselineError(f"output parent is unavailable: {path.parent}") from exc
    if parent.is_symlink() or not stat.S_ISDIR(parent_stat.st_mode):
        raise RebaselineError(f"output parent must be a real directory: {path.parent}")
    payload = (
        json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise RebaselineError(f"output already exists: {path}") from exc
    except OSError as exc:
        raise RebaselineError(f"could not write request output: {path}") from exc
    return path


def _add_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, help="Workspace skills root")
    parser.add_argument("--target", required=True, help="Hermes skills root")
    parser.add_argument("--manifest", required=True, help="Workspace manifest file")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed one-skill Hermes workspace rebaseline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Create a content-addressed request"
    )
    inspect_parser.add_argument("--skill", required=True)
    inspect_parser.add_argument("--output", help="Write the request as a new 0600 file")
    _add_roots(inspect_parser)

    apply_parser = subparsers.add_parser(
        "apply", help="Apply exactly one content-addressed request"
    )
    apply_parser.add_argument("--request", required=True)
    _add_roots(apply_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="Verify complete workspace/runtime/manifest parity"
    )
    _add_roots(verify_parser)
    return parser


def _print_json(value: dict[str, object], *, stream: Any = None) -> None:
    print(json.dumps(value, sort_keys=True, indent=2), file=stream or sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            request = inspect_request(
                args.skill,
                source_root=args.source,
                target_root=args.target,
                manifest_file=args.manifest,
            )
            if args.output:
                output = _write_request_output(args.output, request)
                _print_json({
                    "ok": True,
                    "request": request,
                    "request_path": str(output),
                })
            else:
                _print_json(request)
            return 0
        if args.command == "apply":
            result = apply_request(
                args.request,
                source_root=args.source,
                target_root=args.target,
                manifest_file=args.manifest,
            )
            _print_json(result)
            return 0
        result = verify_roots(
            source_root=args.source,
            target_root=args.target,
            manifest_file=args.manifest,
        )
        _print_json(result)
        return 0 if result["ok"] else 1
    except RebaselineError as exc:
        _print_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised via CLI smoke tests
    raise SystemExit(main())
