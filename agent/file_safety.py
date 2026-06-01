"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def hermes_secret_store_files() -> set[Path]:
    """Exact HERMES_HOME credential-store files the agent must never read or write.

    The agent process runs as the owner of HERMES_HOME, so OS permissions do
    not protect these from the agent's own file tools. This is the single
    registry shared by both the read guard (get_read_block_error) and the write
    deny list — add new credential stores here, not in two places.
    """
    h = _hermes_home_path()
    return {
        h / "auth.json",                       # hermes_cli auth / token store
        h / ".env",                            # provider keys
        h / ".anthropic_oauth.json",           # anthropic PKCE OAuth tokens
        h / "slack_tokens.json",               # Slack OAuth tokens
        h / "whatsapp" / "session" / "creds.json",  # WhatsApp session creds
    }


def hermes_secret_store_dirs() -> list[Path]:
    """HERMES_HOME credential-store directories — any file within is protected."""
    h = _hermes_home_path()
    return [
        h / "mcp-tokens",          # per-server MCP OAuth tokens
        h / "auth",                # google_oauth.json (+ .lock)
        h / "secrets",             # notify-token, etc.
        h / "weixin" / "accounts", # per-account Weixin bearer tokens
    ]


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    system_paths = [
        os.path.join(home, ".ssh", "authorized_keys"),
        os.path.join(home, ".ssh", "id_rsa"),
        os.path.join(home, ".ssh", "id_ed25519"),
        os.path.join(home, ".ssh", "config"),
        os.path.join(home, ".bashrc"),
        os.path.join(home, ".zshrc"),
        os.path.join(home, ".profile"),
        os.path.join(home, ".bash_profile"),
        os.path.join(home, ".zprofile"),
        os.path.join(home, ".netrc"),
        os.path.join(home, ".pgpass"),
        os.path.join(home, ".npmrc"),
        os.path.join(home, ".pypirc"),
        "/etc/sudoers",
        "/etc/passwd",
        "/etc/shadow",
    ]
    hermes_paths = [str(p) for p in hermes_secret_store_files()]
    # Deny the secret directory ROOTS themselves too (not just their children
    # via the prefix list) so a write at the bare dir path on a fresh profile
    # can't shadow the dir and break future credential persistence.
    hermes_dir_roots = [str(d) for d in hermes_secret_store_dirs()]
    return {os.path.realpath(p) for p in (system_paths + hermes_paths + hermes_dir_roots)}


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    system_dirs = [
        os.path.join(home, ".ssh"),
        os.path.join(home, ".aws"),
        os.path.join(home, ".gnupg"),
        os.path.join(home, ".kube"),
        "/etc/sudoers.d",
        "/etc/systemd",
        os.path.join(home, ".docker"),
        os.path.join(home, ".azure"),
        os.path.join(home, ".config", "gh"),
    ]
    hermes_dirs = [str(d) for d in hermes_secret_store_dirs()]
    return [os.path.realpath(p) + os.sep for p in (system_dirs + hermes_dirs)]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved HERMES_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    safe_root = get_safe_write_root()
    if safe_root and not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):
        return True

    return False


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets internal Hermes cache files
    or sensitive secret stores."""
    resolved = Path(path).expanduser().resolve()
    hermes_home = _hermes_home_path().resolve()

    # ── Secret-store guard ────────────────────────────────────────────
    # The agent process runs as the owner of HERMES_HOME, so OS permissions
    # do not protect these files from the agent's own file-read tool. Block
    # them at the application layer to prevent token/secret exfiltration.
    # Single registry shared with the write deny list (file safety invariant:
    # add credential stores in hermes_secret_store_files/dirs, not here).
    secret_files = {p.resolve() for p in hermes_secret_store_files()}
    secret_dirs = [d.resolve() for d in hermes_secret_store_dirs()]
    is_secret = resolved in secret_files
    if not is_secret:
        for sd in secret_dirs:
            try:
                resolved.relative_to(sd)
                is_secret = True
                break
            except ValueError:
                continue
    # /proc/.../environ leaks the process environment (provider keys). Covers
    # /proc/<pid>/environ, /proc/self/environ, /proc/<pid>/task/<tid>/environ.
    if not is_secret and str(resolved).startswith("/proc/") and resolved.name == "environ":
        is_secret = True
    if is_secret:
        return (
            f"Access denied: {path} is a sensitive Hermes secret store "
            "and cannot be read directly. Use the appropriate Hermes tool "
            "if you need credential-backed functionality."
        )

    blocked_dirs = [
        hermes_home / "skills" / ".hub" / "index-cache",
        hermes_home / "skills" / ".hub",
    ]
    for blocked in blocked_dirs:
        try:
            resolved.relative_to(blocked)
        except ValueError:
            continue
        return (
            f"Access denied: {path} is an internal Hermes cache file "
            "and cannot be read directly to prevent prompt injection. "
            "Use the skills_list or skill_view tools instead."
        )
    return None
