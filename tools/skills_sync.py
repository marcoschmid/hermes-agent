#!/usr/bin/env python3
"""
Skills Sync -- Manifest-based seeding and updating of bundled skills.

Copies bundled skills from the repo's skills/ directory into ~/.hermes/skills/
and uses a manifest to track which skills have been synced and their origin hash.

Manifest format (v3): each line is ``skill_name:{json}`` with origin hash,
install path and source-root provenance. Old v1 names and v2 ``name:hash``
entries remain readable and are migrated only when ownership is provable.

Update logic:
  - NEW skills (not in manifest): copied to user dir, origin hash recorded.
  - EXISTING skills (in manifest, present in user dir):
      * If user copy matches origin hash: user hasn't modified it → safe to
        update from bundled if bundled changed. New origin hash recorded.
      * If user copy differs from origin hash: user customized it → SKIP.
  - DELETED by user (in manifest, absent from user dir): respected, not re-added.
  - REMOVED from bundled (in manifest, gone from repo): cleaned from manifest.

The manifest lives at ~/.hermes/skills/.bundled_manifest.
"""

import hashlib
import json
import logging
import os
import ctypes
import errno
import platform
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from hermes_constants import get_bundled_skills_dir, get_hermes_home, get_optional_skills_dir
from agent.skill_utils import is_excluded_skill_path
from typing import Dict, List, Tuple

try:
    from tools.path_security import validate_within_dir
except ImportError:
    from path_security import validate_within_dir

logger = logging.getLogger(__name__)

SYNC_IGNORED_DIRS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "test-results",
}
SYNC_IGNORED_FILES = {".DS_Store"}
SYNC_IGNORED_SUFFIXES = {".pyc", ".pyo", ".swp", ".swo", ".tmp"}


HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
MANIFEST_FILE = SKILLS_DIR / ".bundled_manifest"


def is_sync_ignored_path(path: Path | str) -> bool:
    """Return whether *path* is excluded by sync hash/copy/watch semantics."""
    candidate = Path(path)
    return any(
        part in SYNC_IGNORED_DIRS
        or part in SYNC_IGNORED_FILES
        or any(part.endswith(suffix) for suffix in SYNC_IGNORED_SUFFIXES)
        for part in candidate.parts
    )


def _get_bundled_dir() -> Path:
    """Locate the bundled skills/ directory.

    Checks HERMES_BUNDLED_SKILLS env var first (set by Nix wrapper),
    then a wheel-installed data dir, then falls back to the relative
    path from this source file.
    """
    return get_bundled_skills_dir(Path(__file__).parent.parent / "skills")


def _get_optional_dir() -> Path:
    """Locate the official optional-skills/ directory."""
    return get_optional_skills_dir(Path(__file__).parent.parent / "optional-skills")


def _skill_source_safe(skill_src: Path, source_root: Path) -> bool:
    """Return True iff every path inside *skill_src* resolves under *source_root*.

    Walks the skill directory and follows each symlink target to ensure it
    does not escape the workspace boundary. Required because ``shutil.copytree``
    defaults to ``symlinks=False`` (deref + copy target) and even with
    ``symlinks=True`` (preserve), the preserved symlinks would let Hermes
    runtime read out-of-tree files later.
    """
    try:
        root_resolved = source_root.resolve()
    except OSError:
        return False
    for path in [skill_src, *skill_src.rglob("*")]:
        try:
            resolved = path.resolve()
            resolved.relative_to(root_resolved)
        except (ValueError, OSError):
            return False
    return True


def _staged_tree_safe(stage: Path) -> bool:
    """Reject every symlink that resolves outside its immutable skill tree."""
    if stage.is_symlink() or not stage.is_dir():
        return False
    try:
        stage_root = stage.resolve()
    except OSError:
        return False
    for path in stage.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(stage_root)
        except (ValueError, OSError):
            return False
    return True


def _tree_fingerprint_nofollow(root: Path) -> str | None:
    """Fingerprint a tree without ever dereferencing a symlink."""
    try:
        root_stat = root.lstat()
    except OSError:
        return None
    digest = hashlib.sha256()
    digest.update(f"{root_stat.st_dev}:{root_stat.st_ino}".encode("ascii"))
    if root.is_symlink():
        try:
            digest.update(os.readlink(root).encode("utf-8", errors="surrogateescape"))
            return digest.hexdigest()
        except OSError:
            return None
    try:
        paths = sorted(root.rglob("*"))
        for path in paths:
            rel = path.relative_to(root).as_posix()
            item_stat = path.lstat()
            digest.update(rel.encode("utf-8") + b"\0")
            digest.update(str(stat.S_IFMT(item_stat.st_mode)).encode("ascii") + b"\0")
            if path.is_symlink():
                digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(item_stat.st_mode):
                digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError:
        return None


def _unsafe_live_quarantine_barrier(_path: Path) -> None:
    """Test seam between unsafe-live snapshot and atomic quarantine."""
    return None


def _quarantine_unsafe_owned_tree(path: Path) -> Path | None:
    expected = _tree_fingerprint_nofollow(path)
    if expected is None:
        return None
    quarantine = _unique_empty_path(
        path.parent,
        f".{path.name}.unsafe-live-retained-",
    )
    _unsafe_live_quarantine_barrier(path)
    try:
        _rename_noreplace(path, quarantine)
    except (FileNotFoundError, FileExistsError):
        return None
    if _tree_fingerprint_nofollow(quarantine) == expected:
        return quarantine
    if not os.path.lexists(path):
        try:
            _rename_noreplace(quarantine, path)
        except OSError:
            logger.error("Retained raced unsafe-live tree at %s", quarantine, exc_info=True)
    return None


def _read_manifest_records(manifest_file: Path | None = None) -> Dict[str, dict]:
    """Read v1/v2 hashes and v3 provenance records into one internal shape."""
    effective_manifest = Path(manifest_file) if manifest_file is not None else MANIFEST_FILE
    if not effective_manifest.exists():
        return {}
    result: Dict[str, dict] = {}
    try:
        for line in effective_manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" not in line:
                result[line] = {"hash": ""}
                continue
            name, _, raw = line.partition(":")
            name = name.strip()
            raw = raw.strip()
            if raw.startswith("{"):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    record = {"hash": raw}
                if isinstance(record, dict):
                    result[name] = record
                    continue
            result[name] = {"hash": raw}
        return result
    except (OSError, IOError):
        return {}


def _read_manifest(manifest_file: Path | None = None) -> Dict[str, str]:
    """
    Read the manifest as a dict of {skill_name: origin_hash}.

    Handles v1 (plain names), v2 (name:hash) and v3 JSON-record formats.
    v1 entries get an empty hash string which triggers migration on next sync.
    """
    return {
        name: str(record.get("hash", ""))
        for name, record in _read_manifest_records(manifest_file).items()
    }


_MANIFEST_EXPECTED_UNSET = object()


def _manifest_payload(entries: Dict[str, object]) -> bytes:
    def serialize(value: object) -> str:
        if isinstance(value, dict):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        return str(value)

    data = "\n".join(
        f"{name}:{serialize(value)}" for name, value in sorted(entries.items())
    ) + "\n"
    return data.encode("utf-8")


def _manifest_identity(path: Path) -> Tuple[int, int] | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)


def _manifest_recovery_path(parent: Path, prefix: str) -> Path:
    import tempfile

    fd, name = tempfile.mkstemp(dir=str(parent), prefix=prefix)
    os.close(fd)
    os.unlink(name)
    return Path(name)


def _file_publish_barrier(_path: Path) -> None:
    """Test seam immediately between file CAS check and publication."""
    return None


def _published_file_snapshot(path: Path) -> Tuple[Tuple[int, int], bytes] | None:
    try:
        before = path.lstat()
        payload = path.read_bytes()
        after = path.lstat()
    except OSError:
        return None
    identity = (before.st_dev, before.st_ino)
    if identity != (after.st_dev, after.st_ino):
        return None
    return identity, payload


def _verify_or_quarantine_published_file(
    path: Path,
    expected_identity: Tuple[int, int],
    expected_bytes: bytes,
) -> bool:
    snapshot = _published_file_snapshot(path)
    if snapshot == (expected_identity, expected_bytes):
        return True
    if snapshot is not None:
        _quarantine_observed_path(
            path,
            snapshot[0],
            f".{path.name}.publish-rejected-retained-",
        )
    return False


def _write_bytes_cas(
    path: Path,
    data: bytes,
    expected_bytes: bytes | None,
    expected_identity: Tuple[int, int] | None,
) -> Tuple[int, int]:
    """Publish bytes with byte/inode CAS and retained failure recovery objects."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.publish-",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    published_identity = _manifest_identity(tmp_path)
    if published_identity is None:
        raise OSError(f"publish temp disappeared: {tmp_path}")

    current_bytes = path.read_bytes() if path.exists() else None
    current_identity = _manifest_identity(path)
    if current_bytes != expected_bytes or current_identity != expected_identity:
        raise OSError(f"file changed concurrently: {path}")

    _file_publish_barrier(path)
    if expected_bytes is None:
        _rename_noreplace(tmp_path, path)
        if not _verify_or_quarantine_published_file(path, published_identity, data):
            raise OSError(f"published file changed before verification: {path}")
        return published_identity

    backup = _manifest_recovery_path(path.parent, f".{path.name}.prepublish-")
    _rename_noreplace(path, backup)
    if backup.read_bytes() != expected_bytes or _manifest_identity(backup) != expected_identity:
        if not os.path.lexists(path):
            try:
                _rename_noreplace(backup, path)
            except OSError:
                logger.error("Retained raced file at %s", backup, exc_info=True)
        raise OSError(f"file changed concurrently: {path}")
    try:
        _rename_noreplace(tmp_path, path)
        if not _verify_or_quarantine_published_file(path, published_identity, data):
            if not os.path.lexists(path):
                _rename_noreplace(backup, path)
            raise OSError(f"published file changed before verification: {path}")
    except BaseException:
        if not os.path.lexists(path):
            try:
                _rename_noreplace(backup, path)
            except OSError:
                logger.error("Retained original file at %s", backup, exc_info=True)
        raise
    return published_identity


def _write_manifest(
    entries: Dict[str, object],
    manifest_file: Path | None = None,
    *,
    expected_bytes: bytes | None | object = _MANIFEST_EXPECTED_UNSET,
    expected_identity: Tuple[int, int] | None | object = _MANIFEST_EXPECTED_UNSET,
) -> Tuple[int, int]:
    """Write the manifest file atomically (v2 hashes or v3 JSON records).

    Uses exclusive consuming rename publication plus byte/inode CAS. Unique
    failure recovery objects are retained so cleanup cannot delete an ABA replacement.
    """
    effective_manifest = Path(manifest_file) if manifest_file is not None else MANIFEST_FILE
    data = _manifest_payload(entries)

    if expected_bytes is _MANIFEST_EXPECTED_UNSET:
        expected_bytes = effective_manifest.read_bytes() if effective_manifest.exists() else None
    if expected_identity is _MANIFEST_EXPECTED_UNSET:
        expected_identity = _manifest_identity(effective_manifest)

    try:
        return _write_bytes_cas(
            effective_manifest,
            data,
            expected_bytes,
            expected_identity,
        )
    except Exception as e:
        logger.error("Failed to write skills manifest %s: %s", effective_manifest, e, exc_info=True)
        raise


def _restore_manifest_if_owned(
    manifest_file: Path,
    published_identity: Tuple[int, int] | None,
    published_bytes: bytes,
    original_bytes: bytes | None,
) -> None:
    """Rollback a published manifest only while its exact inode is still ours."""
    if published_identity is None or not manifest_file.exists():
        return
    if (
        _manifest_identity(manifest_file) != published_identity
        or manifest_file.read_bytes() != published_bytes
    ):
        return
    observed = _manifest_recovery_path(
        manifest_file.parent,
        f".{manifest_file.name}.rollback-observed-",
    )
    _rename_noreplace(manifest_file, observed)
    if (
        _manifest_identity(observed) != published_identity
        or observed.read_bytes() != published_bytes
    ):
        if not os.path.lexists(manifest_file):
            _rename_noreplace(observed, manifest_file)
        return
    if original_bytes is not None and not os.path.lexists(manifest_file):
        import tempfile

        fd, restore_name = tempfile.mkstemp(
            dir=str(manifest_file.parent),
            prefix=f".{manifest_file.name}.rollback-original-",
        )
        with os.fdopen(fd, "wb") as restore:
            restore.write(original_bytes)
            restore.flush()
            os.fsync(restore.fileno())
        try:
            restore_path = Path(restore_name)
            restore_identity = _manifest_identity(restore_path)
            _rename_noreplace(restore_path, manifest_file)
            if (
                restore_identity is None
                or not _verify_or_quarantine_published_file(
                    manifest_file,
                    restore_identity,
                    original_bytes,
                )
            ):
                raise OSError(
                    f"restored manifest changed before verification: {manifest_file}"
                )
        except FileExistsError:
            pass
    # Recovery objects stay retained; no compare-then-unlink ABA path.


def _manifest_commit_barrier() -> None:
    """Test seam for a failure immediately after manifest publication."""
    return None


def _reset_remove_barrier(_destination: Path) -> None:
    """Test seam between reset ownership snapshot and atomic quarantine."""
    return None


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """Read the name field from SKILL.md YAML frontmatter, falling back to *fallback*."""
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return fallback
    in_frontmatter = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


def _discover_bundled_skills(bundled_dir: Path) -> List[Tuple[str, Path]]:
    """
    Find all SKILL.md files in the bundled directory.
    Returns list of (skill_name, skill_directory_path) tuples.
    """
    skills = []
    if not bundled_dir.exists():
        return skills

    for skill_md in bundled_dir.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        skill_dir = skill_md.parent
        skill_name = _read_skill_name(skill_md, skill_dir.name)
        skills.append((skill_name, skill_dir))

    return skills


def _compute_relative_dest(
    skill_dir: Path,
    bundled_dir: Path,
    target_dir: Path | None = None,
) -> Path:
    """
    Compute the destination path in SKILLS_DIR preserving the category structure.
    e.g., bundled/skills/mlops/axolotl -> ~/.hermes/skills/mlops/axolotl
    """
    rel = skill_dir.relative_to(bundled_dir)
    return (Path(target_dir) if target_dir is not None else SKILLS_DIR) / rel


def _find_skill_dest_by_name(skill_name: str, target_dir: Path) -> Path | None:
    """Find an installed skill directory by its SKILL.md name field."""
    if not target_dir.exists():
        return None

    ignored_parts = {".git", ".github", ".hub"}
    for skill_md in target_dir.rglob("SKILL.md"):
        if ignored_parts.intersection(skill_md.parts):
            continue
        candidate_name = _read_skill_name(skill_md, skill_md.parent.name)
        if candidate_name == skill_name:
            return skill_md.parent
    return None


def _dir_hash(directory: Path) -> str:
    """Compute a hash of all file contents in a directory for change detection."""
    hasher = hashlib.md5()
    for fpath in sorted(directory.rglob("*")):
        rel = fpath.relative_to(directory)
        if is_sync_ignored_path(rel):
            continue
        if fpath.is_file():
            hasher.update(str(rel).encode("utf-8"))
            hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


def _copytree_ignore(_directory: str, names: List[str]) -> set[str]:
    """Exclude generated/runtime artifacts from hashes and copied skill trees."""
    return {
        name
        for name in names
        if is_sync_ignored_path(Path(name))
    }


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename *source* only when *destination* does not exist."""
    if platform.system() == "Darwin":
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
            -2,
            os.fsencode(source),
            -2,
            os.fsencode(destination),
            0x00000004,  # RENAME_EXCL
        )
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(destination))
        return
    # Hermes production is macOS. Other platforms fail closed if no native
    # no-replace primitive is available rather than silently clobbering.
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(destination))
    raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable", str(destination))


def _path_fingerprint(path: Path) -> str | None:
    try:
        identity = path.lstat()
    except OSError:
        return None
    prefix = f"{identity.st_dev}:{identity.st_ino}:"
    if path.is_symlink():
        return f"{prefix}link:{os.readlink(path)}"
    if path.is_dir():
        return f"{prefix}dir:{_dir_hash(path)}"
    if path.is_file():
        return f"{prefix}file:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return None


def _unique_empty_path(parent: Path, prefix: str) -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp(dir=str(parent), prefix=prefix))
    path.rmdir()
    return path


def _copy_skill_tree_atomic(
    skill_src: Path,
    dest: Path,
    *,
    retain_backup: bool = False,
    expected_hash: str | None = None,
    expected_absent: bool = False,
    expected_source_hash: str | None = None,
) -> Path | None:
    """Copy then compare-and-swap a skill without clobbering concurrent changes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = _unique_empty_path(dest.parent, f".{dest.name}.sync-stage-")
    backup = _unique_empty_path(dest.parent, f".{dest.name}.sync-backup-")
    backup_created = False
    try:
        shutil.copytree(skill_src, stage, symlinks=True, ignore=_copytree_ignore)
        _stage_validation_barrier(stage, dest)
        prepared_hash = expected_source_hash or _dir_hash(skill_src)
        if (
            not _staged_tree_safe(stage)
            or _dir_hash(stage) != prepared_hash
        ):
            raise OSError(
                f"staged source changed or contains symlink escape: {skill_src}"
            )
        if expected_absent:
            if os.path.lexists(dest):
                raise OSError(f"destination changed after preflight: {dest}")
            try:
                _publish_stage_checked(
                    stage,
                    dest,
                    prepared_hash,
                )
            except FileExistsError as exc:
                raise OSError(f"destination changed after preflight: {dest}") from exc
            return None
        if expected_hash is not None:
            if not dest.exists() or _dir_hash(dest) != expected_hash:
                raise OSError(f"destination changed after preflight: {dest}")
        if os.path.lexists(dest):
            _rename_noreplace(dest, backup)
            backup_created = True
            # A write racing the first comparison is now captured in the backup.
            # Verify again before exposing the staged replacement.
            if expected_hash is not None and _dir_hash(backup) != expected_hash:
                _rename_noreplace(backup, dest)
                backup_created = False
                raise OSError(f"destination changed after preflight: {dest}")
        try:
            _publish_stage_checked(
                stage,
                dest,
                prepared_hash,
            )
        except FileExistsError as exc:
            raise OSError(f"destination changed after preflight: {dest}") from exc
        # Backups are always retained. Immediate recursive deletion has no
        # inode-conditional primitive and could remove an ABA replacement.
    except BaseException:
        # Retain a unique failed stage as recovery evidence. Never recursively
        # delete a path from an exception handler where ownership may have raced.
        if backup_created and not os.path.lexists(dest):
            try:
                _rename_noreplace(backup, dest)
                backup_created = False
            except OSError:
                logger.error(
                    "skills-sync rollback left original at %s; destination was not touched",
                    backup,
                    exc_info=True,
                )
        raise
    return backup if backup_created else None


def _stage_validation_barrier(_stage: Path, _dest: Path) -> None:
    """Test seam after copying source and before validating the private stage."""
    return None


def _stage_publish_barrier(_stage: Path, _dest: Path) -> None:
    """Test seam after stage validation and immediately before final rename."""
    return None


def _conditional_quarantine_barrier(_path: Path) -> None:
    """Test seam between observing a published inode and quarantining it."""
    return None


def _quarantine_observed_path(
    path: Path,
    observed_identity: Tuple[int, int],
    prefix: str,
) -> Path | None:
    """Quarantine exactly the observed inode; restore a later raced replacement."""
    quarantine = _unique_empty_path(path.parent, prefix)
    _conditional_quarantine_barrier(path)
    try:
        _rename_noreplace(path, quarantine)
    except (FileNotFoundError, FileExistsError):
        return None
    quarantined_stat = quarantine.lstat()
    quarantined_identity = (quarantined_stat.st_dev, quarantined_stat.st_ino)
    if quarantined_identity == observed_identity:
        return quarantine
    if not os.path.lexists(path):
        try:
            _rename_noreplace(quarantine, path)
        except OSError:
            logger.error("Retained raced replacement at %s", quarantine, exc_info=True)
    return None


def _publish_stage_checked(
    stage: Path,
    destination: Path,
    expected_hash: str,
) -> None:
    """Publish a private stage and retract it if its exact state changed."""
    stage_stat = stage.lstat()
    expected_identity = (stage_stat.st_dev, stage_stat.st_ino)
    _stage_publish_barrier(stage, destination)
    try:
        immediate_stat = stage.lstat()
        immediate_identity = (immediate_stat.st_dev, immediate_stat.st_ino)
    except OSError:
        immediate_identity = None
    if immediate_identity != expected_identity:
        if os.path.lexists(stage):
            rejected = _unique_empty_path(
                stage.parent,
                f".{destination.name}.prepublish-rejected-retained-",
            )
            try:
                _rename_noreplace(stage, rejected)
            except OSError:
                logger.error("Could not quarantine replaced private stage %s", stage, exc_info=True)
        raise OSError(f"published stage changed before verification: {destination}")
    _rename_noreplace(stage, destination)

    try:
        published_stat = destination.lstat()
        published_identity = (published_stat.st_dev, published_stat.st_ino)
        valid = (
            published_identity == expected_identity
            and _staged_tree_safe(destination)
            and _dir_hash(destination) == expected_hash
        )
    except OSError:
        valid = False
        published_identity = None
    if valid:
        return

    if published_identity is not None:
        _quarantine_observed_path(
            destination,
            published_identity,
            f".{destination.name}.publish-rejected-retained-",
        )
    raise OSError(f"published stage changed before verification: {destination}")


def _delete_move_barrier(_dest: Path) -> None:
    """Test seam between stale-target ownership check and atomic quarantine."""
    return None


def _description_publish_barrier(_source: Path, _destination: Path) -> None:
    """Test seam between DESCRIPTION staging and no-clobber publication."""
    return None


def _description_source_open_barrier(_source: Path) -> None:
    """Test seam immediately before the no-follow source open."""
    return None


def _copy_description_noclobber(
    source: Path,
    destination: Path,
    source_root: Path,
) -> str | None:
    """Stage one DESCRIPTION and publish only if the destination remains absent."""
    import tempfile

    if not _skill_source_safe(source, source_root):
        return None
    expected_source_stat = source.lstat()
    expected_source_identity = (
        expected_source_stat.st_dev,
        expected_source_stat.st_ino,
    )
    if not stat.S_ISREG(expected_source_stat.st_mode):
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, stage_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.publish-retained-",
    )
    stage = Path(stage_name)
    source_fd: int | None = None
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("O_NOFOLLOW unavailable for DESCRIPTION source")
        _description_source_open_barrier(source)
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(f"DESCRIPTION source is not a regular file: {source}")
        if (source_stat.st_dev, source_stat.st_ino) != expected_source_identity:
            raise OSError(f"DESCRIPTION source changed before no-follow open: {source}")
        before_read = (
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        )
        with os.fdopen(source_fd, "rb", closefd=False) as source_handle, os.fdopen(
            fd, "wb"
        ) as stage_handle:
            shutil.copyfileobj(source_handle, stage_handle)
            stage_handle.flush()
            os.fsync(stage_handle.fileno())
        after_read_stat = os.fstat(source_fd)
        after_read = (
            after_read_stat.st_size,
            after_read_stat.st_mtime_ns,
            after_read_stat.st_ctime_ns,
        )
        if after_read != before_read:
            raise OSError(f"DESCRIPTION source changed while reading: {source}")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
    expected_stage_fingerprint = _path_fingerprint(stage)
    if expected_stage_fingerprint is None:
        raise OSError(f"DESCRIPTION stage disappeared: {stage}")
    stage_stat = stage.lstat()
    expected_stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
    _description_publish_barrier(source, destination)
    try:
        immediate_stage_stat = stage.lstat()
        immediate_stage_identity = (
            immediate_stage_stat.st_dev,
            immediate_stage_stat.st_ino,
        )
    except OSError:
        immediate_stage_identity = None
    if immediate_stage_identity != expected_stage_identity:
        if os.path.lexists(stage):
            rejected = _unique_empty_path(
                stage.parent,
                f".{destination.name}.prepublish-rejected-retained-",
            )
            try:
                _rename_noreplace(stage, rejected)
            except OSError:
                logger.error(
                    "Could not quarantine replaced DESCRIPTION stage %s",
                    stage,
                    exc_info=True,
                )
        raise OSError(f"DESCRIPTION stage changed before publication: {destination}")
    try:
        _rename_noreplace(stage, destination)
    except FileExistsError:
        return None
    published_fingerprint = _path_fingerprint(destination)
    if published_fingerprint == expected_stage_fingerprint:
        return published_fingerprint
    try:
        destination_stat = destination.lstat()
        destination_identity = (destination_stat.st_dev, destination_stat.st_ino)
    except OSError:
        destination_identity = None
    if destination_identity is not None:
        _quarantine_observed_path(
            destination,
            destination_identity,
            f".{destination.name}.publish-rejected-retained-",
        )
    raise OSError(f"DESCRIPTION stage changed before publication: {destination}")


def _rollback_live_changes(
    changes: List[Tuple[Path, Path | None, str | None]],
    target_root: Path,
) -> None:
    for dest, backup, installed_fingerprint in reversed(changes):
        current_fingerprint = _path_fingerprint(dest)
        if installed_fingerprint is not None and current_fingerprint == installed_fingerprint:
            quarantine = _unique_empty_path(
                dest.parent,
                f".{dest.name}.sync-rollback-retained-",
            )
            _rename_noreplace(dest, quarantine)
            if _path_fingerprint(quarantine) != installed_fingerprint:
                if not os.path.lexists(dest):
                    _rename_noreplace(quarantine, dest)
                logger.error(
                    "skills-sync rollback preserved raced destination at %s",
                    quarantine,
                )
                continue
        elif current_fingerprint is not None:
            logger.error(
                "skills-sync rollback preserved concurrently changed destination %s",
                dest,
            )
            continue
        if backup is not None and not os.path.lexists(dest):
            try:
                _rename_noreplace(backup, dest)
            except OSError:
                logger.error(
                    "skills-sync rollback retained backup at %s",
                    backup,
                    exc_info=True,
                )
        # Do not rmdir parent paths during rollback; an empty-directory ABA
        # replacement has no conditional-delete primitive either.


def _discard_live_backups(changes: List[Tuple[Path, Path | None, str | None]]) -> None:
    # Deliberately retain unique backups. A separate age-gated maintenance pass
    # may inventory them, but this transaction never performs path deletion.
    return None


def _safe_rel_install_path(path: Path, base: Path) -> str:
    """Return a normalized relative POSIX path, rejecting traversal/absolute paths."""
    rel = path.relative_to(base)
    posix = rel.as_posix()
    pure = PurePosixPath(posix)
    parts = [part for part in pure.parts if part not in {"", "."}]
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe optional skill path: {posix}")
    return "/".join(parts)


def _resolve_manifest_install_path(raw: object, target_root: Path) -> Path | None:
    """Resolve a manifest install_path only after strict traversal validation."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part == ".." for part in pure.parts):
        return None
    candidate = target_root.joinpath(*pure.parts)
    if validate_within_dir(candidate, target_root):
        return None
    return candidate


def _skill_file_list(skill_dir: Path) -> List[str]:
    """List files inside a skill directory in lock-file format."""
    files: List[str] = []
    for fpath in sorted(skill_dir.rglob("*")):
        if fpath.is_file():
            files.append(fpath.relative_to(skill_dir).as_posix())
    return files


def _content_hash(directory: Path) -> str:
    """Return the same hash style the skills hub lock uses, falling back locally."""
    try:
        from tools.skills_guard import content_hash

        return content_hash(directory)
    except Exception:
        # Hashing is provenance metadata only; keep sync resilient if guard
        # dependencies are unavailable in a packaged/update context.
        return _dir_hash(directory)


def _optional_skill_index() -> Dict[str, Tuple[str, str, Path]]:
    """Return official optional skills keyed by folder name and frontmatter name.

    Values are ``(folder_name, install_path, source_dir)``. Multiple keys may
    point to the same skill so callers can accept either the folder slug used
    by the hub lock or the user-facing frontmatter name.
    """
    optional_dir = _get_optional_dir()
    index: Dict[str, Tuple[str, str, Path]] = {}
    if not optional_dir.exists():
        return index
    for skill_md in sorted(optional_dir.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        src = skill_md.parent
        try:
            install_path = _safe_rel_install_path(src, optional_dir)
        except ValueError:
            continue
        folder_name = src.name
        frontmatter_name = _read_skill_name(skill_md, folder_name)
        value = (folder_name, install_path, src)
        index[folder_name] = value
        index[frontmatter_name] = value
    return index


def _move_to_restore_backup(path: Path, backup_root: Path) -> str:
    """Move an existing skill directory into a restore backup, preserving rel path."""
    rel = path.relative_to(SKILLS_DIR)
    target = backup_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        suffix = 1
        while target.with_name(f"{target.name}-{suffix}").exists():
            suffix += 1
        target = target.with_name(f"{target.name}-{suffix}")
    _rename_noreplace(path, target)
    return rel.as_posix()


def restore_official_optional_skill(name: str, *, restore: bool = False) -> dict:
    """Restore one or all official optional skills from repo source.

    ``restore=False`` only performs exact-match provenance backfill. ``restore=True``
    repairs already-mutated/reorganized skills by backing up matching active
    copies and copying the official optional source into its canonical path.
    """
    index = _optional_skill_index()
    if not index:
        return {"ok": False, "message": "No official optional skills directory found.", "restored": [], "backfilled": [], "backed_up": []}

    targets = sorted(set(index.values()), key=lambda item: item[1]) if name in {"all", "*"} else []
    if not targets:
        target = index.get(name)
        if target is None:
            return {"ok": False, "message": f"Official optional skill not found: {name}", "restored": [], "backfilled": [], "backed_up": []}
        targets = [target]

    restored: List[str] = []
    backed_up: List[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = SKILLS_DIR / ".restore-backups" / f"official-optional-{timestamp}"

    for folder_name, install_path, src in targets:
        dest = SKILLS_DIR / Path(*install_path.split("/"))
        src_hash = _dir_hash(src)
        canonical_ok = dest.exists() and _dir_hash(dest) == src_hash

        # Find already-active copies of this official skill by frontmatter name
        # or folder slug, even if curator moved it into another category.
        src_frontmatter = _read_skill_name(src / "SKILL.md", folder_name)
        matches: List[Path] = []
        if SKILLS_DIR.exists():
            for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
                if is_excluded_skill_path(skill_md):
                    continue
                candidate = skill_md.parent
                try:
                    candidate.relative_to(SKILLS_DIR)
                except ValueError:
                    continue
                candidate_name = _read_skill_name(skill_md, candidate.name)
                if candidate == dest:
                    continue
                if candidate.name == folder_name or candidate_name in {folder_name, src_frontmatter}:
                    matches.append(candidate)

        if restore:
            for match in matches:
                if match.exists():
                    backed_up.append(_move_to_restore_backup(match, backup_root))
            if dest.exists() and not canonical_ok:
                backed_up.append(_move_to_restore_backup(dest, backup_root))
            if not dest.exists():
                _copy_skill_tree_atomic(
                    src,
                    dest,
                    expected_absent=True,
                    expected_source_hash=src_hash,
                )
                restored.append(folder_name)
        elif not canonical_ok:
            continue

    backfilled = _backfill_optional_provenance(quiet=True)
    return {
        "ok": True,
        "message": "Official optional skill repair complete.",
        "restored": restored,
        "backfilled": backfilled,
        "backed_up": backed_up,
        "backup_dir": str(backup_root) if backed_up else "",
    }


def _backfill_optional_provenance(quiet: bool = False) -> List[str]:
    """Mark already-present official optional skills as hub-installed.

    This covers the migration case where a skill used to be bundled (or was
    manually copied into the active skills tree) and later lives under
    optional-skills/. If the active copy is byte-identical to the official
    optional source, record official hub provenance without copying or
    reinstalling anything. Modified/local skills are left alone.
    """
    optional_dir = _get_optional_dir()
    if not optional_dir.exists():
        return []

    lock_path = SKILLS_DIR / ".hub" / "lock.json"
    original_lock_bytes = lock_path.read_bytes() if lock_path.exists() else None
    original_lock_identity = _manifest_identity(lock_path)
    try:
        data = json.loads(original_lock_bytes) if original_lock_bytes is not None else {"version": 1, "installed": {}}
    except (json.JSONDecodeError, OSError):
        data = {"version": 1, "installed": {}}
    installed = data.setdefault("installed", {})
    existing_paths = {
        entry.get("install_path")
        for entry in installed.values()
        if isinstance(entry, dict)
    }

    backfilled: List[str] = []
    changed = False
    for skill_md in sorted(optional_dir.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        src = skill_md.parent
        try:
            install_path = _safe_rel_install_path(src, optional_dir)
        except ValueError as e:
            logger.debug("Skipping optional skill with unsafe path %s: %s", src, e)
            continue
        dest = SKILLS_DIR / Path(*install_path.split("/"))
        if not dest.exists() or not dest.is_dir():
            continue
        if _dir_hash(dest) != _dir_hash(src):
            continue

        lock_name = src.name
        if lock_name in installed or install_path in existing_paths:
            continue

        timestamp = datetime.now(timezone.utc).isoformat()
        installed[lock_name] = {
            "source": "official",
            "identifier": f"official/{install_path}",
            "trust_level": "builtin",
            "scan_verdict": "backfilled",
            "content_hash": _content_hash(dest),
            "install_path": install_path,
            "files": _skill_file_list(dest),
            "metadata": {"backfilled_from": "optional-skills"},
            "installed_at": timestamp,
            "updated_at": timestamp,
        }
        existing_paths.add(install_path)
        backfilled.append(lock_name)
        changed = True
        if not quiet:
            print(f"  = {lock_name} (official optional provenance backfilled)")

    if changed:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        _write_bytes_cas(
            lock_path,
            payload,
            original_lock_bytes,
            original_lock_identity,
        )
    return backfilled


def sync_skills(
    quiet: bool = False,
    source_dir: Path | str | None = None,
    target_dir: Path | str | None = None,
    manifest_file: Path | str | None = None,
    remove_deleted: bool = False,
) -> dict:
    """
    Sync skills into ~/.hermes/skills/ using the manifest.

    Returns:
        dict with keys: copied (list), updated (list), skipped (int),
                        user_modified (list), cleaned (list), removed (int),
                        total_bundled (int)
    """
    bundled_dir = Path(source_dir) if source_dir is not None else _get_bundled_dir()
    skills_dir = Path(target_dir) if target_dir is not None else SKILLS_DIR
    active_manifest = Path(manifest_file) if manifest_file is not None else MANIFEST_FILE

    if not bundled_dir.exists():
        return {
            "copied": [], "updated": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "removed": 0,
            "rejected": [], "validation_errors": [],
            "copy_errors": [],
            "total_bundled": 0, "optional_provenance_backfilled": [],
        }

    original_manifest_bytes = (
        active_manifest.read_bytes() if active_manifest.exists() else None
    )
    original_manifest_identity = _manifest_identity(active_manifest)

    skills_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest_records(active_manifest)
    bundled_skills = _discover_bundled_skills(bundled_dir)
    bundled_names = {name for name, _skill_src in bundled_skills}

    copied = []
    updated = []
    user_modified = []
    rejected = []
    copy_errors = []
    skipped = 0
    removed = 0
    cleaned = []
    rejected_sources: List[Tuple[str, Path]] = []

    prepared_skills: List[Tuple[str, Path, Path, str, str]] = []
    for skill_name, skill_src in bundled_skills:
        dest = _compute_relative_dest(skill_src, bundled_dir, skills_dir)
        # GAP-1: validate the source BEFORE computing _dir_hash. Hashing reads
        # file bytes through symlinks (fpath.read_bytes() follows them), which
        # would leak external content into RAM and into the manifest hash even
        # if the copy is later rejected. Reject up front.
        if not _skill_source_safe(skill_src, bundled_dir):
            rejected.append(skill_name)
            rejected_sources.append((skill_name, skill_src))
            if not quiet:
                print(f"  ⚠ {skill_name}: source contains symlink escaping workspace — skipped")
            logger.warning(
                "skills-sync rejected %s before hash: symlink escape from %s outside %s",
                skill_name, skill_src, bundled_dir,
            )
            continue
        install_path = dest.relative_to(skills_dir).as_posix()
        prepared_skills.append(
            (skill_name, skill_src, dest, _dir_hash(skill_src), install_path)
        )

    live_changes: List[Tuple[Path, Path | None, str | None]] = []

    for skill_name, skill_src in rejected_sources:
        record = manifest.get(skill_name)
        if not isinstance(record, dict):
            continue
        install_path = record.get("install_path")
        provenance = record.get("provenance")
        is_v3_owned = (
            isinstance(install_path, str)
            and install_path
            and isinstance(provenance, dict)
            and provenance.get("kind") == "workspace"
            and provenance.get("source_root") == str(bundled_dir.resolve())
        )
        is_legacy_owned = (
            not is_v3_owned
            and bool(str(record.get("hash", "")))
            and "install_path" not in record
            and "provenance" not in record
        )
        if is_v3_owned:
            live_dest = _resolve_manifest_install_path(install_path, skills_dir)
        elif is_legacy_owned:
            legacy_dest = _compute_relative_dest(skill_src, bundled_dir, skills_dir)
            live_dest = (
                legacy_dest
                if validate_within_dir(legacy_dest, skills_dir) is None
                else None
            )
        else:
            continue
        if live_dest is None:
            logger.warning(
                "skills-sync refused unsafe manifest install_path for %s: %r",
                skill_name,
                install_path,
            )
            continue
        if not live_dest.exists() or _staged_tree_safe(live_dest):
            continue
        quarantine = _quarantine_unsafe_owned_tree(live_dest)
        if quarantine is None:
            logger.warning(
                "skills-sync preserved raced unsafe live target %s at %s",
                skill_name,
                live_dest,
            )
            continue
        # Never roll an unsafe tree back to live if the later manifest write
        # fails. The old manifest entry may remain temporarily, but execution
        # stays fail-safe and the retained quarantine is recoverable evidence.
        del manifest[skill_name]
        cleaned.append(skill_name)
        removed += 1

    for skill_name, _skill_src, dest, _bundled_hash, install_path in prepared_skills:
        record = manifest.get(skill_name)
        if not isinstance(record, dict) or not dest.exists() or _staged_tree_safe(dest):
            continue
        provenance = record.get("provenance")
        is_v3_owned = (
            record.get("install_path") == install_path
            and isinstance(provenance, dict)
            and provenance.get("kind") == "workspace"
            and provenance.get("source_root") == str(bundled_dir.resolve())
        )
        is_legacy_owned = (
            not is_v3_owned
            and bool(str(record.get("hash", "")))
            and "install_path" not in record
            and "provenance" not in record
        )
        if not (is_v3_owned or is_legacy_owned):
            continue
        quarantine = _quarantine_unsafe_owned_tree(dest)
        if quarantine is None:
            continue
        del manifest[skill_name]
        cleaned.append(skill_name)
        removed += 1

    existing_hashes = {
        dest: _dir_hash(dest)
        for _skill_name, _skill_src, dest, _bundled_hash, _install_path in prepared_skills
        if dest.exists() and _staged_tree_safe(dest)
    }

    for skill_name, skill_src, dest, bundled_hash, install_path in prepared_skills:

        record = {
            "hash": bundled_hash,
            "install_path": install_path,
            "provenance": {
                "kind": "workspace",
                "source_root": str(bundled_dir.resolve()),
            },
        }

        origin_record = manifest.get(skill_name)
        if (
            isinstance(origin_record, dict)
            and "install_path" not in origin_record
            and "provenance" not in origin_record
            and dest in existing_hashes
            and str(origin_record.get("hash", "")) == bundled_hash
            and existing_hashes[dest] == bundled_hash
        ):
            manifest[skill_name] = record

        if skill_name not in manifest:
            # ── New skill — never offered before ──
            try:
                if dest.exists():
                    # User already has a skill with the same name — don't overwrite.
                    # Only baseline in the manifest when the on-disk copy is
                    # byte-identical to bundled (e.g. a reset that re-syncs, or
                    # a coincidentally identical install); that case is harmless
                    # to track. If the copy differs (custom skill, hub-installed,
                    # or user-edited) skip the manifest write: recording
                    # bundled_hash there would poison update detection by making
                    # user_hash != origin_hash read as "user-modified" on every
                    # subsequent sync, permanently blocking bundled updates.
                    skipped += 1
                    if existing_hashes[dest] == bundled_hash:
                        manifest[skill_name] = record
                    elif not quiet:
                        print(
                            f"  ⚠ {skill_name}: bundled version shipped but you "
                            f"already have a local skill by this name — yours "
                            f"was kept. Run `hermes skills reset {skill_name}` "
                            f"to replace it with the bundled version."
                        )
                else:
                    _copy_skill_tree_atomic(
                        skill_src,
                        dest,
                        expected_absent=True,
                        expected_source_hash=bundled_hash,
                    )
                    live_changes.append((dest, None, _path_fingerprint(dest)))
                    copied.append(skill_name)
                    manifest[skill_name] = record
                    if not quiet:
                        print(f"  + {skill_name}")
            except (OSError, IOError) as e:
                if (
                    "destination changed after preflight" in str(e)
                    or "staged source changed" in str(e)
                    or "published stage changed" in str(e)
                ):
                    _rollback_live_changes(live_changes, skills_dir)
                    raise
                copy_errors.append(
                    {"skill": skill_name, "operation": "copy", "error": str(e)}
                )
                if not quiet:
                    print(f"  ! Failed to copy {skill_name}: {e}")
                # Do NOT add to manifest — next sync should retry

        elif dest.exists():
            # ── Existing skill — in manifest AND on disk ──
            origin_record = manifest.get(skill_name, {})
            origin_hash = str(origin_record.get("hash", ""))
            user_hash = existing_hashes[dest]

            if not origin_hash:
                # v1 migration: no origin hash recorded. Set baseline from
                # user's current copy so future syncs can detect modifications.
                manifest[skill_name] = {**record, "hash": user_hash}
                if user_hash == bundled_hash:
                    skipped += 1  # already in sync
                else:
                    # Can't tell if user modified or bundled changed — be safe
                    skipped += 1
                continue

            if user_hash != origin_hash:
                # User modified this skill — don't overwrite their changes
                user_modified.append(skill_name)
                if not quiet:
                    print(f"  ~ {skill_name} (user-modified, skipping)")
                continue

            # User copy matches origin — check if bundled has a newer version
            if bundled_hash != origin_hash:
                try:
                    backup = _copy_skill_tree_atomic(
                        skill_src,
                        dest,
                        retain_backup=True,
                        expected_hash=user_hash,
                        expected_source_hash=bundled_hash,
                    )
                    live_changes.append((dest, backup, _path_fingerprint(dest)))
                    manifest[skill_name] = record
                    updated.append(skill_name)
                    if not quiet:
                        print(f"  ↑ {skill_name} (updated)")
                except (OSError, IOError) as e:
                    if (
                        "destination changed after preflight" in str(e)
                        or "staged source changed" in str(e)
                        or "published stage changed" in str(e)
                    ):
                        _rollback_live_changes(live_changes, skills_dir)
                        raise
                    copy_errors.append(
                        {"skill": skill_name, "operation": "update", "error": str(e)}
                    )
                    if not quiet:
                        print(f"  ! Failed to update {skill_name}: {e}")
            else:
                skipped += 1  # bundled unchanged, user unchanged

        else:
            # ── In manifest but not on disk — user deleted it ──
            skipped += 1

    # Clean stale manifest entries (skills removed from source dir).
    # With remove_deleted=True, also delete only the manifest-tracked target skill.
    for name in sorted(set(manifest.keys()) - bundled_names):
        stale_record = manifest[name]
        if remove_deleted:
            install_path = stale_record.get("install_path")
            provenance = stale_record.get("provenance")
            if not (
                isinstance(install_path, str)
                and install_path
                and isinstance(provenance, dict)
                and provenance.get("kind") == "workspace"
                and provenance.get("source_root") == str(bundled_dir.resolve())
            ):
                # Legacy manifests cannot prove which same-named directory is ours.
                # Keep both target and manifest until a live source can migrate it.
                continue
            dest = _resolve_manifest_install_path(install_path, skills_dir)
            if dest is None:
                logger.warning(
                    "skills-sync refused unsafe stale install_path for %s: %r",
                    name,
                    install_path,
                )
                continue
            if dest.exists():
                escape_err = validate_within_dir(dest, skills_dir)
                if escape_err:
                    logger.warning(
                        "skills-sync refused remove %s at %s: %s",
                        name, dest, escape_err,
                    )
                    continue
                expected = str(stale_record.get("hash", ""))
                expected_fingerprint = _path_fingerprint(dest)
                if (
                    not expected
                    or expected_fingerprint is None
                    or _dir_hash(dest) != expected
                ):
                    logger.warning(
                        "skills-sync preserved modified stale target %s at %s",
                        name,
                        dest,
                    )
                    continue
                try:
                    backup = _unique_empty_path(
                        dest.parent,
                        f".{dest.name}.sync-delete-backup-",
                    )
                    _delete_move_barrier(dest)
                    _rename_noreplace(dest, backup)
                    if (
                        _path_fingerprint(backup) != expected_fingerprint
                        or _dir_hash(backup) != expected
                    ):
                        if not os.path.lexists(dest):
                            try:
                                _rename_noreplace(backup, dest)
                            except OSError:
                                logger.error(
                                    "skills-sync retained raced stale target at %s",
                                    backup,
                                    exc_info=True,
                                )
                        _rollback_live_changes(live_changes, skills_dir)
                        raise OSError(
                            f"stale target changed after ownership check: {dest}"
                        )
                    live_changes.append((dest, backup, None))
                    removed += 1
                except (OSError, IOError) as e:
                    if "stale target changed after ownership check" in str(e):
                        raise
                    logger.debug("Could not remove deleted source skill %s at %s: %s", name, dest, e)
                    continue
        del manifest[name]
        cleaned.append(name)

    # Also copy DESCRIPTION.md files for categories (if not already present)
    for desc_md in bundled_dir.rglob("DESCRIPTION.md"):
        rel = desc_md.relative_to(bundled_dir)
        dest_desc = skills_dir / rel
        if not dest_desc.exists():
            try:
                installed_fingerprint = _copy_description_noclobber(
                    desc_md,
                    dest_desc,
                    bundled_dir,
                )
                if installed_fingerprint is not None:
                    live_changes.append((dest_desc, None, installed_fingerprint))
            except (OSError, IOError) as e:
                logger.debug("Could not copy %s: %s", desc_md, e)

    published_manifest_bytes = _manifest_payload(manifest)
    published_manifest_identity = None
    try:
        published_manifest_identity = _write_manifest(
            manifest,
            active_manifest,
            expected_bytes=original_manifest_bytes,
            expected_identity=original_manifest_identity,
        )
        _manifest_commit_barrier()
    except BaseException:
        _rollback_live_changes(live_changes, skills_dir)
        _restore_manifest_if_owned(
            active_manifest,
            published_manifest_identity,
            published_manifest_bytes,
            original_manifest_bytes,
        )
        raise
    _discard_live_backups(live_changes)
    optional_provenance_backfilled = (
        _backfill_optional_provenance(quiet=quiet)
        if source_dir is None and target_dir is None and manifest_file is None
        else []
    )

    return {
        "copied": copied,
        "updated": updated,
        "skipped": skipped,
        "user_modified": user_modified,
        "cleaned": cleaned,
        "removed": removed,
        "rejected": rejected,
        "validation_errors": rejected,  # alias for watcher log forward-compat
        "copy_errors": copy_errors,
        "total_bundled": len(bundled_skills),
        "optional_provenance_backfilled": optional_provenance_backfilled,
    }


def reset_bundled_skill(name: str, restore: bool = False) -> dict:
    """
    Reset a bundled skill's manifest tracking so future syncs work normally.

    When a user edits a bundled skill, subsequent syncs mark it as
    ``user_modified`` and skip it forever — even if the user later copies
    the bundled version back into place, because the manifest still holds
    the *old* origin hash. This function breaks that loop.

    Args:
        name: The skill name (matches the manifest key / skill frontmatter name).
        restore: If True, also delete the user's copy in SKILLS_DIR and let
                 the next sync re-copy the current bundled version. If False
                 (default), only clear the manifest entry — the user's
                 current copy is preserved but future updates work again.

    Returns:
        dict with keys:
          - ok: bool, whether the reset succeeded
          - action: one of "manifest_cleared", "restored", "not_in_manifest",
                    "bundled_missing"
          - message: human-readable description
          - synced: dict from sync_skills() if a sync was triggered, else None
    """
    manifest = _read_manifest()
    bundled_dir = _get_bundled_dir()
    bundled_skills = _discover_bundled_skills(bundled_dir)
    bundled_by_name = dict(bundled_skills)

    in_manifest = name in manifest
    is_bundled = name in bundled_by_name

    if not in_manifest and not is_bundled:
        return {
            "ok": False,
            "action": "not_in_manifest",
            "message": (
                f"'{name}' is not a tracked bundled skill. Nothing to reset. "
                f"(Hub-installed skills use `hermes skills uninstall`.)"
            ),
            "synced": None,
        }

    # Step 1: drop the manifest entry so next sync treats it as new
    if in_manifest:
        del manifest[name]
        _write_manifest(manifest)

    # Step 2 (optional): delete the user's copy so next sync re-copies bundled
    deleted_user_copy = False
    if restore:
        if not is_bundled:
            return {
                "ok": False,
                "action": "bundled_missing",
                "message": (
                    f"'{name}' has no bundled source — manifest entry cleared "
                    f"but cannot restore from bundled (skill was removed upstream)."
                ),
                "synced": None,
            }
        # The destination mirrors the bundled path relative to bundled_dir.
        dest = _compute_relative_dest(bundled_by_name[name], bundled_dir)
        if dest.exists():
            try:
                expected_fingerprint = _path_fingerprint(dest)
                if expected_fingerprint is None:
                    raise OSError(f"could not fingerprint reset target: {dest}")
                quarantine = _unique_empty_path(
                    dest.parent,
                    f".{dest.name}.reset-retained-",
                )
                _reset_remove_barrier(dest)
                _rename_noreplace(dest, quarantine)
                if _path_fingerprint(quarantine) != expected_fingerprint:
                    if not os.path.lexists(dest):
                        _rename_noreplace(quarantine, dest)
                    raise OSError(f"reset target changed after ownership check: {dest}")
                deleted_user_copy = True
            except (OSError, IOError) as e:
                return {
                    "ok": False,
                    "action": "manifest_cleared",
                    "message": (
                        f"Cleared manifest entry for '{name}' but could not "
                        f"delete user copy at {dest}: {e}"
                    ),
                    "synced": None,
                }

    # Step 3: run sync to re-baseline (or re-copy if we deleted)
    synced = sync_skills(quiet=True)

    if restore and deleted_user_copy:
        action = "restored"
        message = f"Restored '{name}' from bundled source."
    elif restore:
        # Nothing on disk to delete, but we re-synced — acts like a fresh install
        action = "restored"
        message = f"Restored '{name}' (no prior user copy, re-copied from bundled)."
    else:
        action = "manifest_cleared"
        message = (
            f"Cleared manifest entry for '{name}'. Future `hermes update` runs "
            f"will re-baseline against your current copy and accept upstream changes."
        )

    return {"ok": True, "action": action, "message": message, "synced": synced}


if __name__ == "__main__":
    print("Syncing bundled skills into ~/.hermes/skills/ ...")
    result = sync_skills(quiet=False)
    parts = [
        f"{len(result['copied'])} new",
        f"{len(result['updated'])} updated",
        f"{result['skipped']} unchanged",
    ]
    if result["user_modified"]:
        names = result["user_modified"]
        MAX_SHOW = 5
        shown = ", ".join(names[:MAX_SHOW])
        if len(names) > MAX_SHOW:
            shown += f", +{len(names) - MAX_SHOW} more"
        parts.append(f"{len(names)} user-modified (kept): {shown}")
    if result["cleaned"]:
        parts.append(f"{len(result['cleaned'])} cleaned from manifest")
    if result["removed"]:
        parts.append(f"{result['removed']} removed")
    if result.get("optional_provenance_backfilled"):
        parts.append(f"{len(result['optional_provenance_backfilled'])} official optional backfilled")
    print(f"\nDone: {', '.join(parts)}. {result['total_bundled']} total bundled.")
