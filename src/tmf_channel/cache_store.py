"""Day-lazy TX / tick cache store (SQLite SSOT + JSON fallback).

Ideal layout under ``$GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/``:
  - ``bars.sqlite`` — one row per 1m bar (tx_* materializations)
  - ``*.json`` / tick blobs — optional legacy; prefer data-dir over repo

Research harness and labs should call ``load_day`` / ``list_days`` — never
``json.load`` an entire seasonal file at import time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from stock_db import PROJECT_ROOT

_LOCK = threading.Lock()
_FILE_CACHE: dict[str, dict[str, Any]] = {}

_TX_SOURCES = (
    "tx_1m_fullnight_cache_full.json",
    "tx_1m_fullnight_cache.json",
    "tx_1m_daynight_cache.json",
    "tx_1m_janmar_holdout_cache.json",
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
    "tx_1m_cache.json",
)


def data_cache_dir() -> Path:
    data = os.environ.get("GOLDENSTOCKS_DATA_DIR", "").strip()
    root = Path(data) if data else PROJECT_ROOT
    p = root / "cache" / "tmf_channel"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bars_db_path() -> Path:
    return data_cache_dir() / "bars.sqlite"


def _candidates(name: str) -> list[Path]:
    out = [data_cache_dir() / name]
    out.append(PROJECT_ROOT / "reports" / "research" / "channel_lab" / name)
    return out


def resolve_cache_path(name: str) -> Path | None:
    for p in _candidates(name):
        if p.is_file() or p.is_symlink():
            # resolve symlink target existence
            try:
                if p.resolve().is_file():
                    return p
            except OSError:
                continue
    return None


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(bars_db_path()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
          source TEXT NOT NULL,
          day TEXT NOT NULL,
          t TEXT NOT NULL,
          o REAL, h REAL, l REAL, c REAL, v REAL,
          sess TEXT,
          PRIMARY KEY (source, day, t)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_bars_source_day ON bars(source, day)"
    )
    return con


def materialize_json_to_sqlite(
    names: Iterable[str] | None = None,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Load TX JSON caches into bars.sqlite (idempotent upsert)."""
    names = list(names or _TX_SOURCES)
    stats: dict[str, Any] = {"sources": {}, "db": str(bars_db_path())}
    with _LOCK:
        con = _connect()
        try:
            for name in names:
                path = resolve_cache_path(name)
                if path is None:
                    stats["sources"][name] = {"ok": False, "reason": "missing"}
                    continue
                blob = json.loads(path.read_text())
                if not isinstance(blob, dict):
                    stats["sources"][name] = {"ok": False, "reason": "bad_root"}
                    continue
                if replace:
                    con.execute("DELETE FROM bars WHERE source=?", (name,))
                n_days = 0
                n_rows = 0
                for day, rows in blob.items():
                    if not isinstance(rows, list):
                        continue
                    n_days += 1
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        t = str(r.get("t") or "")
                        if not t:
                            continue
                        con.execute(
                            """
                            INSERT INTO bars(source, day, t, o, h, l, c, v, sess)
                            VALUES (?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(source, day, t) DO UPDATE SET
                              o=excluded.o, h=excluded.h, l=excluded.l,
                              c=excluded.c, v=excluded.v, sess=excluded.sess
                            """,
                            (
                                name,
                                str(day),
                                t,
                                float(r.get("o") or 0),
                                float(r.get("h") or 0),
                                float(r.get("l") or 0),
                                float(r.get("c") or 0),
                                float(r.get("v") or 0),
                                str(r.get("sess") or ""),
                            ),
                        )
                        n_rows += 1
                stats["sources"][name] = {
                    "ok": True,
                    "path": str(path),
                    "n_days": n_days,
                    "n_rows": n_rows,
                }
            con.commit()
        finally:
            con.close()
    return stats


def list_days(source: str = "tx_1m_fullnight_cache_full.json") -> list[str]:
    if not bars_db_path().is_file():
        blob = load_json_cache(source, keep_in_memory=False)
        return sorted(blob.keys())
    con = _connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day",
            (source,),
        ).fetchall()
        if rows:
            return [r["day"] for r in rows]
    finally:
        con.close()
    blob = load_json_cache(source, keep_in_memory=False)
    return sorted(blob.keys())


def load_day(
    day: str,
    *,
    source: str = "tx_1m_fullnight_cache_full.json",
) -> list[dict[str, Any]]:
    """Return one session's bars — SQLite first, JSON fallback."""
    if bars_db_path().is_file():
        con = _connect()
        try:
            rows = con.execute(
                """
                SELECT t, o, h, l, c, v, sess FROM bars
                WHERE source=? AND day=?
                ORDER BY t
                """,
                (source, day),
            ).fetchall()
            if rows:
                return [
                    {
                        "t": r["t"],
                        "o": r["o"],
                        "h": r["h"],
                        "l": r["l"],
                        "c": r["c"],
                        "v": r["v"],
                        "sess": r["sess"],
                    }
                    for r in rows
                ]
        finally:
            con.close()
    blob = load_json_cache(source)
    rows = blob.get(day) or []
    return list(rows) if isinstance(rows, list) else []


def load_json_cache(name: str, *, keep_in_memory: bool = True) -> dict[str, Any]:
    with _LOCK:
        if keep_in_memory and name in _FILE_CACHE:
            return _FILE_CACHE[name]
    path = resolve_cache_path(name)
    if path is None:
        raise FileNotFoundError(f"cache not found: {name} (checked {_candidates(name)})")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise TypeError(f"{name} root must be object, got {type(data).__name__}")
    if keep_in_memory:
        with _LOCK:
            _FILE_CACHE[name] = data
    return data


def load_session_days(name: str, days: list[str]) -> dict[str, Any]:
    return {d: load_day(d, source=name) for d in days}


def clear_file_cache() -> None:
    with _LOCK:
        _FILE_CACHE.clear()


def cache_inventory() -> list[dict[str, Any]]:
    names = list(_TX_SOURCES) + [
        "tick_raw_cache.json",
        "tick_raw_cache_full83.json",
        "bars.sqlite",
    ]
    rows = []
    for n in names:
        if n == "bars.sqlite":
            p = bars_db_path()
        else:
            p = resolve_cache_path(n)
        rows.append(
            {
                "name": n,
                "path": str(p) if p and (p.is_file() or p.is_symlink()) else None,
                "bytes": p.stat().st_size if p and p.exists() else 0,
                "in_memory": n in _FILE_CACHE,
            }
        )
    return rows
