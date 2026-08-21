"""Database connection factory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from stock_db.util import DEFAULT_DB_PATH
from stock_db._schema import _SCHEMA, _migrate_schema

# Bump SCHEMA_VERSION whenever _SCHEMA, _migrate_schema, or the copytrade
# schema (ensure_copytrade_schema) changes, so connect() re-runs the DDL.
SCHEMA_VERSION = 8

_TIMEOUT_SEC = 60.0
_BUSY_TIMEOUT_MS = 60_000


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a read-write connection, ensuring the schema is up to date."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # e.g. read-only filesystem — keep the existing journal mode.
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_version != SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        _migrate_schema(conn)
        from stock_db.copytrade import ensure_copytrade_schema

        ensure_copytrade_schema(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


def connect_ro(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a read-only connection; never runs any DDL or migrations."""
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only=ON")
    return conn
