"""connect() / connect_ro() factory: PRAGMAs, schema-version gate, read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stock_db import connection as conn_mod
from stock_db.connection import SCHEMA_VERSION, connect, connect_ro


def test_connect_fresh_db_sets_schema_and_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        # Schema actually created.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "daily_bars" in tables
    finally:
        conn.close()


def test_connect_second_open_skips_ddl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "t.db"
    connect(db).close()

    calls: list[sqlite3.Connection] = []

    def _counting_migrate(conn: sqlite3.Connection) -> None:
        calls.append(conn)

    monkeypatch.setattr(conn_mod, "_migrate_schema", _counting_migrate)
    conn = connect(db)
    try:
        assert calls == []  # user_version gate skipped DDL/migrations
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()
    finally:
        conn.close()


def test_connect_ro_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    rw = connect(db)
    rw.execute(
        "INSERT INTO daily_bars (code, date, close, source, synced_at) "
        "VALUES ('2330', '2026-01-02', 100.0, 'test', '2026-01-02T00:00:00')"
    )
    rw.commit()
    rw.close()

    ro = connect_ro(db)
    try:
        row = ro.execute(
            "SELECT close FROM daily_bars WHERE code='2330'"
        ).fetchone()
        assert row["close"] == 100.0
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO daily_bars (code, date, close, source, synced_at) "
                "VALUES ('2317', '2026-01-02', 50.0, 'test', '2026-01-02T00:00:00')"
            )
    finally:
        ro.close()


def test_connect_ro_missing_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "no_such.db"
    with pytest.raises(FileNotFoundError, match="no_such.db"):
        connect_ro(missing)
