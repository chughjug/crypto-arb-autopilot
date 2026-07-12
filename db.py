"""Database layer — Supabase/PostgreSQL when DATABASE_URL is set, else local SQLite."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_pg_pool = None
_pg_conns: dict[str, Connection] = {}
_sqlite_conns: dict[str, sqlite3.Connection] = {}


def use_postgres() -> bool:
    return bool(_database_url())


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _adapt_sql(sql: str) -> str:
    if not use_postgres():
        return sql
    return sql.replace("?", "%s")


class _Row:
    """sqlite3.Row-like dict for both backends."""

    def __init__(self, data: dict[str, Any] | None):
        self._data = data or {}

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def keys(self):
        return self._data.keys()

    def get(self, key: str, default=None):
        return self._data.get(key, default)


class _Cursor:
    def __init__(self, rows: list[_Row]):
        self._rows = rows
        self._i = 0

    def fetchone(self) -> _Row | None:
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def fetchall(self) -> list[_Row]:
        rest = self._rows[self._i :]
        self._i = len(self._rows)
        return rest


class Connection:
    """Thin wrapper matching sqlite3.Connection.execute() used by auth/autopilot."""

    def __init__(self, backend, raw):
        self._backend = backend
        self._raw = raw

    def execute(self, sql: str, params: tuple | list = ()) -> _Cursor:
        sql = _adapt_sql(sql)
        if self._backend == "pg":
            from psycopg.rows import dict_row

            cur = self._raw.cursor(row_factory=dict_row)
            try:
                cur.execute(sql, tuple(params))
                if cur.description:
                    rows = [_Row(dict(r)) for r in cur.fetchall()]
                else:
                    rows = []
            except Exception:
                self._raw.rollback()
                raise
            finally:
                cur.close()
            return _Cursor(rows)
        cur = self._raw.execute(sql, tuple(params))
        rows = [_Row({k: row[k] for k in row.keys()}) for row in cur.fetchall()]
        return _Cursor(rows)

    def executescript(self, sql: str) -> None:
        if self._backend == "pg":
            with self._raw.cursor() as cur:
                cur.execute(sql)
            return
        self._raw.executescript(sql)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        if self._backend == "sqlite":
            self._raw.close()


def connect(db_name: str = "app", sqlite_path: str | None = None) -> Connection:
    """Open a cached connection. db_name keys SQLite files; Postgres uses one shared pool."""
    url = _database_url()
    if url:
        global _pg_pool, _pg_conns
        with _lock:
            if "pg" in _pg_conns:
                return _pg_conns["pg"]
            if _pg_pool is None:
                from psycopg_pool import ConnectionPool

                _pg_pool = ConnectionPool(
                    url,
                    min_size=1,
                    max_size=int(os.environ.get("DB_POOL_SIZE", "8")),
                    kwargs={"autocommit": False},
                )
                _init_postgres_schema(_pg_pool)
            raw = _pg_pool.getconn()
            conn = Connection("pg", raw)
            _pg_conns["pg"] = conn
            return conn

    path = sqlite_path or str(Path(__file__).parent / "data" / f"{db_name}.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if path not in _sqlite_conns:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _sqlite_conns[path] = conn
        return Connection("sqlite", _sqlite_conns[path])


def release(conn: Connection) -> None:
    """Return a Postgres connection to the pool."""
    if conn._backend == "pg":
        global _pg_pool
        if _pg_pool is not None:
            _pg_pool.putconn(conn._raw)


def _init_postgres_schema(pool) -> None:
    schema_path = Path(__file__).parent / "supabase_schema.sql"
    if not schema_path.is_file():
        return
    lines = []
    for line in schema_path.read_text().splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    sql = "\n".join(lines)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
        conn.commit()
    _ensure_postgres_migrations(pool)


def _ensure_postgres_migrations(pool) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE auth_challenges ADD COLUMN IF NOT EXISTS payload TEXT")
            cur.execute("ALTER TABLE venue_credentials ADD COLUMN IF NOT EXISTS key_fingerprint TEXT")
            cur.execute("ALTER TABLE autopilot_config ADD COLUMN IF NOT EXISTS overrides TEXT")
        conn.commit()


def backend_label() -> str:
    return "postgresql" if use_postgres() else "sqlite"
