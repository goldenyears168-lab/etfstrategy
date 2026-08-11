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
from datetime import date as _date
from datetime import timedelta as _timedelta
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

# --- post-midnight (00:00-04:59) calendar convention, per source -------------
# A session key D does NOT mean the same thing in every cache, and the two live
# conventions are mutually incompatible:
#
#   "session"  D's rows span D 08:45 -> D+1 04:59. The 00:00-04:59 tail is the
#              tail of D's OWN night session and its calendar date is D+1, so
#              chronologically it belongs at the END of the array. These files
#              carry an explicit per-bar ``cal`` field.
#   "calendar" every row in D is calendar date D. The 00:00-04:59 block is the
#              tail of the PREVIOUS session's night, so it belongs at the FRONT
#              of the array. No ``cal`` field.
#
# Evidence (bar OHLC vs FinMind TaiwanFuturesTick, see
# scripts/research/audit_tx_1m_fullnight_cache_quality.py):
#   tx_1m_fullnight_cache_full.json — 83/83 sessions cal offset = +1 day,
#     24,532 post-midnight bars match D+1 ticks with max deviation 0.0pt.
#   tx_1m_fullnight_cache.json      — 43/43 sessions cal offset = +1 day.
#   tx_1m_tick_built_fullnight_aug  — post-midnight bars match SAME-day ticks
#     with max deviation 0.0pt (checked 2026-08-04/05/06/07); matching against
#     D+1 gives deviations up to 1901pt. Opposite convention, confirmed.
# Sources listed as "calendar" that simply contain zero post-midnight bars are
# unaffected either way (tx_1m_daynight_cache, tx_1m_cache, the three holdout
# caches, tx_1m_tick_built_582d).
_POST_MIDNIGHT_CONVENTION: dict[str, str] = {
    "tx_1m_fullnight_cache_full.json": "session",
    "tx_1m_fullnight_cache.json": "session",
    "tx_1m_tick_built_fullnight_aug": "calendar",
    "tx_1m_tick_built_582d": "calendar",
    "tx_1m_daynight_cache.json": "calendar",
    "tx_1m_cache.json": "calendar",
    "tx_1m_janmar_holdout_cache.json": "calendar",
    "tx_1m_julsep_holdout_cache.json": "calendar",
    "tx_1m_octdec_holdout_cache.json": "calendar",
}


def post_midnight_convention(source: str) -> str:
    """Return "session" or "calendar" for a cache source, or raise.

    Deliberately fails loud for an unregistered source rather than guessing a
    calendar date for its 00:00-04:59 bars — guessing is exactly what produced
    the ~1000pt phantom bar/tick mismatches investigated on 2026-08-11.
    """
    try:
        return _POST_MIDNIGHT_CONVENTION[source]
    except KeyError:
        raise KeyError(
            f"unknown post-midnight calendar convention for source {source!r}; "
            "register it in cache_store._POST_MIDNIGHT_CONVENTION after checking "
            "its 00:00-04:59 bars against raw ticks "
            "(scripts/research/audit_tx_1m_fullnight_cache_quality.py)"
        ) from None


def bar_calendar_date(day: str, t: str, *, source: str, row: dict | None = None) -> str:
    """Real calendar date of one bar. ``row['cal']`` wins when present."""
    if row is not None:
        cal = row.get("cal")
        if cal:
            return str(cal)
    if t >= "05:00":
        return day
    if post_midnight_convention(source) == "session":
        y, m, d = (int(x) for x in day.split("-"))
        return (_date(y, m, d) + _timedelta(days=1)).isoformat()
    return day


def bar_timestamp(day: str, t: str, *, source: str, row: dict | None = None) -> str:
    """ISO-8601 +08:00 timestamp of the bar's real instant.

    Use this instead of ``f"{day}T{t}:00.000+08:00"`` anywhere the timestamp is
    parsed back into a real instant (NQ/ES gate lookups, tick alignment, VIXTWN
    joins). The naive form is 24h early for every 00:00-04:59 bar of a
    "session"-convention source.
    """
    return f"{bar_calendar_date(day, t, source=source, row=row)}T{t}:00.000+08:00"


def bar_timestamps(day: str, rows: list[dict[str, Any]], *, source: str) -> list[str]:
    return [
        bar_timestamp(day, str(r.get("t") or ""), source=source, row=r) for r in rows
    ]


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


def _chronological(
    day: str, rows: list[dict[str, Any]], *, source: str
) -> list[dict[str, Any]]:
    """Stamp each bar with its real calendar date and sort into true time order.

    The SQLite table has no ``cal`` column and its ``ORDER BY t`` is a plain
    lexicographic sort, which for a "session"-convention source hoists the
    00:00-04:59 night tail from the END of the session to the FRONT — i.e. the
    engine would see the last 5 hours of the night session *before* the day
    session of the same key. Sorting by (calendar date, clock time) restores the
    order the JSON files already have, and is a no-op for every
    "calendar"-convention source.
    """
    out = []
    for r in rows:
        rr = dict(r)
        t = str(rr.get("t") or "")
        rr["cal"] = bar_calendar_date(day, t, source=source, row=rr)
        out.append(rr)
    out.sort(key=lambda r: (r["cal"], str(r.get("t") or "")))
    return out


def load_day(
    day: str,
    *,
    source: str = "tx_1m_fullnight_cache_full.json",
) -> list[dict[str, Any]]:
    """Return one session's bars in true chronological order — SQLite first,
    JSON fallback. Every row carries a ``cal`` field (real calendar date); use
    ``bar_timestamp()`` rather than interpolating ``day`` into a timestamp.
    """
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
                return _chronological(
                    day,
                    [
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
                    ],
                    source=source,
                )
        finally:
            con.close()
    blob = load_json_cache(source)
    rows = blob.get(day) or []
    if not isinstance(rows, list):
        return []
    return _chronological(day, rows, source=source)


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
