"""Tenant-ledger backends. SQLite is the default; Postgres is production.

Empty ``HESTIA_DATABASE_URL`` → one file under ``HESTIA_DATA_DIR``. A
``postgresql://`` URL → a pooled server. This is not a farm: tenants still
run on this host. See ``HESTIA_REPLICAS``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LedgerBackend(Protocol):
    backend_type: str

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...

    def execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> int: ...

    def execute_script(self, statements: tuple[str, ...]) -> None: ...

    def close(self) -> None: ...


def is_postgres_url(url: str) -> bool:
    return (url or "").strip().startswith(("postgresql://", "postgres://"))


def open_ledger(sqlite_path: Path, database_url: str = "") -> LedgerBackend:
    url = (database_url or "").strip()
    if not url:
        return SQLiteLedger(sqlite_path)
    if is_postgres_url(url):
        return PostgresLedger(url)
    raise RuntimeError(
        "HESTIA_DATABASE_URL must be postgresql://… or empty (SQLite under HESTIA_DATA_DIR)"
    )


def translate_placeholders(sql: str) -> str:
    """``?`` → ``%s`` for psycopg. Hestia SQL never puts ``?`` inside literals."""
    return sql.replace("?", "%s")


class SQLiteLedger:
    backend_type = "sqlite"

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, params)
        if cur.description is None:
            return []
        return [dict(row) for row in cur.fetchall()]

    def execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return int(cur.rowcount or 0)

    def execute_script(self, statements: tuple[str, ...]) -> None:
        try:
            for statement in statements:
                self._conn.execute(statement)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()


class PostgresLedger:
    backend_type = "postgresql"

    def __init__(self, database_url: str) -> None:
        if "options=" in database_url.lower() and "application_name=" not in database_url.lower():
            raise ValueError(
                "HESTIA_DATABASE_URL with custom options= is refused. "
                "Use a dedicated hestia database, not libpq options."
            )
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - exercised when extra missing
            raise RuntimeError(
                "PostgreSQL ledger requires the postgres extra: "
                'pip install "aimarket-hestia[postgres]"'
            ) from exc
        self.database_url = database_url
        self._pool = ConnectionPool(
            database_url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            cur = conn.execute(translate_placeholders(sql), params)
            if cur.description is None:
                conn.commit()
                return []
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
            return rows

    def execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._pool.connection() as conn:
            try:
                cur = conn.execute(translate_placeholders(sql), params)
                count = int(cur.rowcount or 0)
                conn.commit()
                return count
            except Exception:
                conn.rollback()
                raise

    def execute_script(self, statements: tuple[str, ...]) -> None:
        with self._pool.connection() as conn:
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        self._pool.close()
