"""盤中 K SQLite cache · stock_kbar_1m（research）· stock_kbar_5m（主線 C0/radar）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from stock_db.util import utc_now_iso


@dataclass(frozen=True)
class KbarBar:
    minute: str
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None


def upsert_stock_kbar_1m(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_kbar_1m (
            stock_id, trade_date, minute, open, high, low, close, volume, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :minute, :open, :high, :low, :close, :volume, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, minute, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def kbar_day_coverage(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str = "finmind",
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM stock_kbar_1m
        WHERE stock_id = ? AND trade_date = ? AND source = ?
        """,
        (stock_id, trade_date, source),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


KBAR_SOURCE_PRIORITY = ("finmind", "yahoo")
KBAR_FUTURES_SOURCE = "finmind_fut"
MIN_BARS_PER_DAY = 4
MIN_BARS_5M_PER_DAY = 4
KBAR_5M_BAR_MINUTES = 5


def kbar_1m_day_sources(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> tuple[str, ...]:
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT source FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ?
            ORDER BY source
            """,
            (stock_id, trade_date),
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    return tuple(str(r[0]) for r in rows if r[0])


def _kbar_1m_count_for_source(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    source: str,
) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            """,
            (stock_id, trade_date, source),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["n"] or 0) if row else 0


def _kbar_5m_count_for_source(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    source: str,
) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM stock_kbar_5m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            """,
            (stock_id, trade_date, source),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["n"] or 0) if row else 0


def _kbar_5m_rows(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    source: str,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT minute, open, high, low, close, volume FROM stock_kbar_5m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            ORDER BY minute
            """,
            (stock_id, trade_date, source),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _kbar_5m_count(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM stock_kbar_5m
            WHERE stock_id = ? AND trade_date = ? AND source IN ('finmind', 'yahoo')
            """,
            (stock_id, trade_date),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0) if row else 0


def kbar_day_has_data(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> bool:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source IN ('finmind', 'yahoo')
            """,
            (stock_id, trade_date),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return int(row[0] or 0) >= MIN_BARS_PER_DAY if row else False


def upsert_stock_kbar_5m(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_kbar_5m (
            stock_id, trade_date, minute, open, high, low, close, volume, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :minute, :open, :high, :low, :close, :volume, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, minute, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def kbar_5m_day_coverage(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str = "finmind",
) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM stock_kbar_5m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            """,
            (stock_id, trade_date, source),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["n"] or 0) if row else 0


def kbar_5m_day_has_data(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    min_bars: int = MIN_BARS_5M_PER_DAY,
) -> bool:
    return _kbar_5m_count(conn, stock_id, trade_date) >= min_bars


def kbar_5m_backfill_complete(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    min_bars_5m: int = MIN_BARS_5M_PER_DAY,
) -> bool:
    """True when mainline + finmind_fut 1m sources are covered in 5m."""
    sources = kbar_1m_day_sources(conn, stock_id, trade_date)
    mainline_1m = sum(
        _kbar_1m_count_for_source(conn, stock_id, trade_date, src)
        for src in sources
        if src in KBAR_SOURCE_PRIORITY
    )
    if mainline_1m >= MIN_BARS_PER_DAY and not kbar_5m_day_has_data(
        conn, stock_id, trade_date, min_bars=min_bars_5m
    ):
        return False
    if KBAR_FUTURES_SOURCE in sources:
        if _kbar_1m_count_for_source(conn, stock_id, trade_date, KBAR_FUTURES_SOURCE) >= (
            MIN_BARS_PER_DAY
        ) and _kbar_5m_count_for_source(
            conn, stock_id, trade_date, KBAR_FUTURES_SOURCE
        ) < min_bars_5m:
            return False
    return True


def kbar_mainline_day_has_data(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> bool:
    """主線覆蓋 · 5m 表優先，過渡期 fallback 1m 聚合。"""
    return kbar_5m_day_has_data(conn, stock_id, trade_date) or kbar_day_has_data(
        conn, stock_id, trade_date
    )


def kbar_5m_latest_minute(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> str | None:
    """Latest stored 5m bar label for session (HH:MM:SS)."""
    try:
        row = conn.execute(
            """
            SELECT MAX(minute) AS latest FROM stock_kbar_5m
            WHERE stock_id = ? AND trade_date = ? AND source IN ('finmind', 'yahoo')
            """,
            (stock_id, trade_date),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row["latest"] is None:
        return None
    return str(row["latest"])


def kbar_5m_fresh_for_poll(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    poll_minute: str,
) -> bool:
    """True when stored 5m bars already cover poll_minute PIT lookup."""
    latest = kbar_5m_latest_minute(conn, stock_id, trade_date)
    if not latest:
        return False
    return latest >= kbar_sql_minute(poll_minute)


def _normalize_minute_label(minute: str) -> str:
    m = str(minute or "").strip()
    if len(m) == 5:
        return f"{m}:00"
    return m


def aggregate_bars_to_5m(bars: tuple[KbarBar, ...]) -> tuple[KbarBar, ...]:
    """Anchor 5m OHLCV at 09:00 · first bar = 09:00–09:04."""
    if not bars:
        return ()
    buckets: dict[int, list[KbarBar]] = {}
    for bar in bars:
        mfo = _minutes_from_open(bar.minute)
        if mfo is None or mfo < 0 or mfo > (13 - 9) * 60 + 30:
            continue
        idx = mfo // KBAR_5M_BAR_MINUTES
        buckets.setdefault(idx, []).append(bar)
    out: list[KbarBar] = []
    for idx in sorted(buckets):
        chunk = buckets[idx]
        h = 9 + (idx * KBAR_5M_BAR_MINUTES) // 60
        m = (idx * KBAR_5M_BAR_MINUTES) % 60
        label = f"{h:02d}:{m:02d}:00"
        out.append(
            KbarBar(
                minute=label,
                open=float(chunk[0].open),
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=float(chunk[-1].close),
                volume=sum(int(b.volume or 0) for b in chunk) or None,
            )
        )
    return tuple(out)


def aggregate_taiex_5s_rows_to_5m(
    finmind_rows: list[dict[str, Any]],
) -> tuple[str | None, tuple[KbarBar, ...]]:
    """FinMind ``TaiwanVariousIndicators5Seconds`` → session 5m OHLC.

    Point values (``TAIEX``) are bucketed by Asia/Taipei wall clock ·
    O=first / H=max / L=min / C=last · volume=None. Includes 13:30 close print
    as its own bar when present (Yahoo ^TWII 5m usually ends at 13:25).
    """
    buckets: dict[int, list[float]] = {}
    trade_date: str | None = None
    for r in finmind_rows:
        raw_dt = str(r.get("date") or "").strip()
        px_raw = r.get("TAIEX")
        if not raw_dt or px_raw is None:
            continue
        parts = raw_dt.split()
        if len(parts) < 2:
            continue
        td, hhmmss = parts[0][:10], parts[1]
        try:
            px = float(px_raw)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        mfo = _minutes_from_open(hhmmss)
        if mfo is None or mfo < 0 or mfo > (13 - 9) * 60 + 30:
            continue
        if trade_date is None:
            trade_date = td
        elif td != trade_date:
            # Multi-day payload should not happen for this dataset; skip other days.
            continue
        idx = mfo // KBAR_5M_BAR_MINUTES
        buckets.setdefault(idx, []).append(px)
    if not buckets:
        return trade_date, ()
    out: list[KbarBar] = []
    for idx in sorted(buckets):
        vals = buckets[idx]
        h = 9 + (idx * KBAR_5M_BAR_MINUTES) // 60
        m = (idx * KBAR_5M_BAR_MINUTES) % 60
        out.append(
            KbarBar(
                minute=f"{h:02d}:{m:02d}:00",
                open=float(vals[0]),
                high=max(vals),
                low=min(vals),
                close=float(vals[-1]),
                volume=None,
            )
        )
    return trade_date, tuple(out)


def kbar_bars_to_db_rows(
    stock_id: str,
    trade_date: str,
    bars: tuple[KbarBar, ...],
    *,
    source: str,
) -> list[dict[str, Any]]:
    return [
        {
            "stock_id": stock_id,
            "trade_date": trade_date,
            "minute": bar.minute,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "source": source,
        }
        for bar in bars
    ]


def upsert_yahoo_5m_rows(
    conn: sqlite3.Connection,
    stock_id: str,
    rows_5m: list[dict[str, Any]],
) -> int:
    """主線 sync：Yahoo 原生 5m → stock_kbar_5m（09:00 bar-open anchor）。"""
    if not rows_5m:
        return 0
    by_date: dict[str, dict[str, KbarBar]] = {}
    for r in rows_5m:
        bar = _row_to_kbar_bar(r)
        if bar is None:
            continue
        mfo = _minutes_from_open(bar.minute)
        if mfo is None or mfo < 0 or mfo > (13 - 9) * 60 + 30:
            continue
        td = str(r.get("trade_date") or "")[:10]
        if not td:
            continue
        label = _normalize_minute_label(bar.minute)
        by_date.setdefault(td, {})[label] = KbarBar(
            minute=label,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
    total = 0
    src = str(rows_5m[0].get("source") or "yahoo")
    for td, bars_map in by_date.items():
        bars = tuple(bars_map[k] for k in sorted(bars_map))
        total += upsert_stock_kbar_5m(
            conn,
            kbar_bars_to_db_rows(stock_id, td, bars, source=src),
        )
    return total


def upsert_1m_rows_as_5m(
    conn: sqlite3.Connection,
    stock_id: str,
    rows_1m: list[dict[str, Any]],
) -> int:
    """主線 sync：1m API rows → aggregate → stock_kbar_5m。"""
    if not rows_1m:
        return 0
    by_date: dict[str, list[KbarBar]] = {}
    for r in rows_1m:
        bar = _row_to_kbar_bar(r)
        if bar is None:
            continue
        td = str(r.get("trade_date") or "")[:10]
        if not td:
            continue
        by_date.setdefault(td, []).append(bar)
    total = 0
    for td, chunk in by_date.items():
        chunk.sort(key=lambda b: b.minute)
        bars_5m = aggregate_bars_to_5m(tuple(chunk))
        if not bars_5m:
            continue
        src = str(rows_1m[0].get("source") or "yahoo")
        total += upsert_stock_kbar_5m(
            conn,
            kbar_bars_to_db_rows(stock_id, td, bars_5m, source=src),
        )
    return total


def load_kbar_day_5m_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
) -> tuple[KbarBar, ...]:
    """主線 5m OHLCV · stock_kbar_5m 優先。"""
    if source:
        sources = (source,)
    else:
        sources = KBAR_SOURCE_PRIORITY
    merged: dict[str, KbarBar] = {}
    for src in sources:
        rows = _kbar_5m_rows(conn, stock_id, trade_date, src)
        for row in rows:
            bar = _row_to_kbar_bar(row)
            if bar and bar.minute not in merged:
                merged[bar.minute] = bar
    if merged:
        return tuple(merged[k] for k in sorted(merged))
    return aggregate_bars_to_5m(load_kbar_day_bars(conn, stock_id, trade_date, source=source))


def _backfill_source_1m_to_5m(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    source: str,
) -> int:
    bars_1m = load_kbar_day_bars(conn, stock_id, trade_date, source=source)
    bars_5m = aggregate_bars_to_5m(bars_1m)
    if not bars_5m:
        return 0
    return upsert_stock_kbar_5m(
        conn,
        kbar_bars_to_db_rows(stock_id, trade_date, bars_5m, source=source),
    )


def backfill_stock_kbar_5m_from_1m(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
) -> int:
    """單日 1m → 5m 寫入（migration / backfill）· 含 finmind_fut。"""
    if source:
        return _backfill_source_1m_to_5m(conn, stock_id, trade_date, source)
    discovered = kbar_1m_day_sources(conn, stock_id, trade_date)
    total = 0
    if any(s in KBAR_SOURCE_PRIORITY for s in discovered) or not discovered:
        bars_1m = load_kbar_day_bars(conn, stock_id, trade_date)
        bars_5m = aggregate_bars_to_5m(bars_1m)
        if bars_5m:
            src = next((s for s in KBAR_SOURCE_PRIORITY if s in discovered), "yahoo")
            total += upsert_stock_kbar_5m(
                conn,
                kbar_bars_to_db_rows(stock_id, trade_date, bars_5m, source=src),
            )
    if KBAR_FUTURES_SOURCE in discovered:
        total += _backfill_source_1m_to_5m(
            conn, stock_id, trade_date, KBAR_FUTURES_SOURCE
        )
    return total


def _row_to_kbar_bar(row: sqlite3.Row | dict[str, Any]) -> KbarBar | None:
    minute = str(row["minute"] if isinstance(row, sqlite3.Row) else row.get("minute") or "")
    close = row["close"] if isinstance(row, sqlite3.Row) else row.get("close")
    if not minute or close is None:
        return None
    try:
        px = float(close)
        if px <= 0:
            return None
    except (TypeError, ValueError):
        return None
    o = _opt_float(row["open"] if isinstance(row, sqlite3.Row) else row.get("open")) or px
    h = _opt_float(row["high"] if isinstance(row, sqlite3.Row) else row.get("high")) or px
    lo = _opt_float(row["low"] if isinstance(row, sqlite3.Row) else row.get("low")) or px
    vol = _opt_int(row["volume"] if isinstance(row, sqlite3.Row) else row.get("volume"))
    return KbarBar(minute=minute, open=o, high=h, low=lo, close=px, volume=vol)


def _kbar_1m_rows(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    source: str,
    *,
    columns: str,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            f"""
            SELECT {columns} FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            ORDER BY minute
            """,
            (stock_id, trade_date, source),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def load_kbar_day_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
) -> tuple[KbarBar, ...]:
    """回傳升序 OHLCV 1 分 K；source=None 時 FinMind 優先、Yahoo 補洞。"""
    if source:
        sources = (source,)
    else:
        sources = KBAR_SOURCE_PRIORITY
    merged: dict[str, KbarBar] = {}
    for src in sources:
        rows = _kbar_1m_rows(
            conn,
            stock_id,
            trade_date,
            src,
            columns="minute, open, high, low, close, volume",
        )
        for row in rows:
            bar = _row_to_kbar_bar(row)
            if bar and bar.minute not in merged:
                merged[bar.minute] = bar
    return tuple(merged[k] for k in sorted(merged))


def kbar_bars_from_finmind_rows(
    finmind_rows: list[dict[str, Any]],
) -> tuple[KbarBar, ...]:
    out: list[KbarBar] = []
    for r in finmind_rows:
        bar = _row_to_kbar_bar(
            {
                "minute": r.get("minute") or r.get("time"),
                "open": r.get("open"),
                "high": r.get("max") or r.get("high"),
                "low": r.get("min") or r.get("low"),
                "close": r.get("close"),
                "volume": r.get("volume"),
            }
        )
        if bar:
            out.append(bar)
    out.sort(key=lambda b: b.minute)
    return tuple(out)


def load_kbar_day_closes(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
) -> tuple[tuple[str, float], ...]:
    """回傳 (minute, close) 升序序列；source=None 時 FinMind 優先、Yahoo 補洞。"""
    if source:
        sources = (source,)
    else:
        sources = KBAR_SOURCE_PRIORITY
    merged: dict[str, float] = {}
    for src in sources:
        rows = _kbar_1m_rows(
            conn,
            stock_id,
            trade_date,
            src,
            columns="minute, close",
        )
        for row in rows:
            minute = str(row["minute"] or "")
            px = row["close"]
            if not minute or px is None or minute in merged:
                continue
            try:
                f = float(px)
                if f > 0:
                    merged[minute] = f
            except (TypeError, ValueError):
                continue
    return tuple(sorted(merged.items()))


def kbar_sql_minute(hhmm: str) -> str:
    m = hhmm.strip()
    if len(m) == 5:
        return f"{m}:00"
    return m


def price_at_or_before_minute(
    bars: tuple[tuple[str, float], ...],
    hhmm: str,
) -> float | None:
    if not bars:
        return None
    target = hhmm if len(hhmm) > 5 else f"{hhmm}:00"
    last: float | None = None
    for minute, px in bars:
        if minute <= target:
            last = px
        else:
            break
    return last


def _minutes_from_open(minute: str) -> int | None:
    parts = str(minute or "").strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (h - 9) * 60 + m


def aggregate_closes_to_bars(
    closes: tuple[tuple[str, float], ...],
    *,
    bar_minutes: int = 5,
) -> tuple[tuple[str, float], ...]:
    """Collapse 1m closes into bar closes · bar label = first minute of bucket (09:00 anchor)."""
    if bar_minutes <= 0:
        raise ValueError("bar_minutes must be positive")
    buckets: dict[int, tuple[str, float]] = {}
    for minute, px in closes:
        mfo = _minutes_from_open(minute)
        if mfo is None or mfo < 0 or mfo > (13 - 9) * 60 + 30:
            continue
        idx = mfo // bar_minutes
        # last close in bucket wins; keep first minute as label
        if idx not in buckets:
            h = 9 + (idx * bar_minutes) // 60
            m = (idx * bar_minutes) % 60
            buckets[idx] = (f"{h:02d}:{m:02d}", float(px))
        else:
            label, _ = buckets[idx]
            buckets[idx] = (label, float(px))
    return tuple(buckets[k] for k in sorted(buckets))


def load_kbar_day_5m_closes(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
    bar_minutes: int = 5,
) -> tuple[tuple[str, float], ...]:
    """主線 5m close 序列 · stock_kbar_5m 優先，無則由 1m 聚合。"""
    if bar_minutes != KBAR_5M_BAR_MINUTES:
        closes = load_kbar_day_closes(conn, stock_id, trade_date, source=source)
        return aggregate_closes_to_bars(closes, bar_minutes=bar_minutes)
    bars = load_kbar_day_5m_bars(conn, stock_id, trade_date, source=source)
    if not bars:
        return ()
    return tuple((b.minute, b.close) for b in bars)


def kbar_5m_close_at_minute(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    poll_minute: str,
    *,
    source: str | None = None,
    bar_minutes: int = 5,
    day_bars: tuple[tuple[str, float], ...] | None = None,
) -> float | None:
    """Latest aggregated 5m bar close at or before ``poll_minute``."""
    bars = day_bars or load_kbar_day_5m_closes(
        conn,
        stock_id,
        trade_date,
        source=source,
        bar_minutes=bar_minutes,
    )
    return price_at_or_before_minute(bars, poll_minute)


def finmind_kbar_rows_to_db(
    stock_id: str,
    finmind_rows: list[dict[str, Any]],
    *,
    source: str = "finmind",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in finmind_rows:
        minute = str(r.get("minute") or r.get("time") or "").strip()
        trade_date = str(r.get("date") or r.get("trade_date") or "")[:10]
        close = r.get("close")
        if not minute or not trade_date or close is None:
            continue
        try:
            px = float(close)
            if px <= 0:
                continue
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "minute": minute,
                "open": _opt_float(r.get("open")),
                "high": _opt_float(r.get("max") or r.get("high")),
                "low": _opt_float(r.get("min") or r.get("low")),
                "close": px,
                "volume": _opt_int(r.get("volume")),
                "source": source,
            }
        )
    return out


def _opt_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_int(val: object) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
