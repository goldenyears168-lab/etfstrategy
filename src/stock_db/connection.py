"""Database connection factory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from stock_db.util import DEFAULT_DB_PATH
from stock_db._schema import _SCHEMA, _migrate_schema

def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_schema(conn)
    from stock_db.copytrade import ensure_copytrade_schema

    ensure_copytrade_schema(conn)
    return conn
