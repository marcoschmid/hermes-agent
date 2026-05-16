"""SQLite state store for the notification hub."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

try:  # pragma: no cover - exercised when the optional dependency is installed.
    import aiosqlite
except ImportError:  # pragma: no cover - local minimal fallback is covered.
    aiosqlite = None  # type: ignore[assignment]


DEFAULT_DB_PATH = Path("/Users/marco/Code/hermes-agent/data/hub_state.db")
ENV_DB_PATH = "HUB_STATE_DB_PATH"
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

Params = Sequence[Any] | Iterable[Any]


def _resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> Path:
    configured = db_path if db_path is not None else os.getenv(ENV_DB_PATH)
    return Path(configured or DEFAULT_DB_PATH).expanduser().resolve()


class HubState:
    """Minimal async DAO for the hub SQLite database."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path = _resolve_db_path(db_path)
        self._conn: Any | None = None
        self._lock = asyncio.Lock()
        self._transaction_owner: asyncio.Task[Any] | None = None

    async def connect(self) -> "HubState":
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if aiosqlite is not None:
            self._conn = await aiosqlite.connect(str(self.path))
        else:
            self._conn = await asyncio.to_thread(
                sqlite3.connect,
                str(self.path),
                check_same_thread=False,
            )
        self._conn.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        await self.execute("PRAGMA foreign_keys=ON")
        return self

    async def close(self) -> None:
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        if aiosqlite is not None and isinstance(conn, aiosqlite.Connection):
            await conn.close()
        else:
            await asyncio.to_thread(conn.close)

    async def migrate(self) -> None:
        for migration_path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(migration_path.name.split("_", 1)[0])
            sql = migration_path.read_text(encoding="utf-8")
            await self._run(lambda: self._executescript_unlocked(sql))
            await self.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )

    async def execute(self, sql: str, params: Params = ()) -> None:
        await self._run(lambda: self._execute_unlocked(sql, params, commit=True))

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        return await self._run(lambda: self._fetchone_unlocked(sql, params))

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return await self._run(lambda: self._fetchall_unlocked(sql, params))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["HubState"]:
        if self._transaction_owner is not None:
            raise RuntimeError("Nested hub_state transactions are not supported")

        async with self._lock:
            self._transaction_owner = asyncio.current_task()
            try:
                await self._execute_unlocked("BEGIN", (), commit=False)
                yield self
                await self._execute_unlocked("COMMIT", (), commit=False)
            except Exception:
                await self._execute_unlocked("ROLLBACK", (), commit=False)
                raise
            finally:
                self._transaction_owner = None

    async def __aenter__(self) -> "HubState":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("HubState is not connected")
        return self._conn

    async def _run(self, operation: Any) -> Any:
        if self._transaction_owner is asyncio.current_task():
            return await operation()
        async with self._lock:
            return await operation()

    async def _execute_unlocked(self, sql: str, params: Params, *, commit: bool) -> None:
        conn = self._require_conn()
        if aiosqlite is not None and isinstance(conn, aiosqlite.Connection):
            cursor = await conn.execute(sql, tuple(params))
            await cursor.close()
            if commit and self._transaction_owner is None:
                await conn.commit()
            return

        def run() -> None:
            conn.execute(sql, tuple(params))
            if commit and self._transaction_owner is None:
                conn.commit()

        await asyncio.to_thread(run)

    async def _fetchone_unlocked(self, sql: str, params: Params) -> sqlite3.Row | None:
        conn = self._require_conn()
        if aiosqlite is not None and isinstance(conn, aiosqlite.Connection):
            cursor = await conn.execute(sql, tuple(params))
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

        def run() -> sqlite3.Row | None:
            cursor = conn.execute(sql, tuple(params))
            return cursor.fetchone()

        return await asyncio.to_thread(run)

    async def _fetchall_unlocked(self, sql: str, params: Params) -> list[sqlite3.Row]:
        conn = self._require_conn()
        if aiosqlite is not None and isinstance(conn, aiosqlite.Connection):
            cursor = await conn.execute(sql, tuple(params))
            try:
                return await cursor.fetchall()
            finally:
                await cursor.close()

        def run() -> list[sqlite3.Row]:
            cursor = conn.execute(sql, tuple(params))
            return cursor.fetchall()

        return await asyncio.to_thread(run)

    async def _executescript_unlocked(self, sql: str) -> None:
        conn = self._require_conn()
        if aiosqlite is not None and isinstance(conn, aiosqlite.Connection):
            await conn.executescript(sql)
            await conn.commit()
            return

        def run() -> None:
            conn.executescript(sql)
            conn.commit()

        await asyncio.to_thread(run)


async def connect(db_path: str | os.PathLike[str] | None = None) -> HubState:
    return await HubState(db_path).connect()
