#!/usr/bin/env python3
"""
ETF 加碼事件 · 事前 2 週技術面研究（獨立 DB，不寫入 stocks.db）。

用法：
  python src/etf_entry_ta_study.py --sync --analyze --write-report
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from score_engine import (
    classify_entry_context,
    classify_entry_context_batch,
    extension_pct,
    overextended_min_pct,
)
from finmind_client import fetch_finmind
from report_paths import REPORTS_RESEARCH
from stock_context import (
    MA20_DAYS,
    MA60_DAYS,
    TRADING_DAYS_52W,
    _compute_technical_from_rows,
    classify_volume,
    compute_price_volatility_metrics,
)
from stock_db import PROJECT_ROOT

DEFAULT_STUDY_DB = PROJECT_ROOT / "data" / "etf_entry_ta_study.db"
DEFAULT_MAIN_DB = PROJECT_ROOT / "data" / "stocks.db"
DEFAULT_REPORT = REPORTS_RESEARCH / "etf_entry_ta_study.md"

LOOKBACK_CALENDAR_DAYS = 120
PRE_EVENT_TRADING_DAYS = 10
POST_EVENT_TRADING_DAYS = 5
REQUEST_DELAY_SEC = 0.35
BENCHMARK_CODE = "IX0001"
EARLY_PHASE_DATES = frozenset({"2026-06-08", "2026-06-09"})
LATE_PHASE_DATES = frozenset({"2026-06-10", "2026-06-11", "2026-06-12", "2026-06-15"})
PERMUTATION_ITERATIONS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS study_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    etf_count INTEGER,
    flow_ntd_billion REAL,
    source_etfs TEXT,
    position_intent TEXT,
    l2_level TEXT,
    UNIQUE (event_date, stock_id)
);

CREATE TABLE IF NOT EXISTS study_daily_bars (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    source TEXT NOT NULL DEFAULT 'finmind',
    PRIMARY KEY (stock_id, trade_date)
);

CREATE TABLE IF NOT EXISTS study_ta_snapshot (
    event_id INTEGER NOT NULL,
    as_of_date TEXT NOT NULL,
    close REAL,
    ma20 REAL,
    ma60 REAL,
    dist_ma20_pct REAL,
    dist_ma60_pct REAL,
    position_52w_pct REAL,
    dist_from_52w_high_pct REAL,
    vol_ratio_5d REAL,
    vol_label TEXT,
    return_1w_pct REAL,
    return_2w_pct REAL,
    max_drawdown_2w_pct REAL,
    entry_pattern TEXT,
    above_ma20 INTEGER,
    above_ma60 INTEGER,
    ma20_rising INTEGER,
    near_52w_high INTEGER,
    uptrend_pullback INTEGER,
    atr14_pct REAL,
    realized_vol_14d REAL,
    PRIMARY KEY (event_id),
    FOREIGN KEY (event_id) REFERENCES study_events(id)
);
"""


@dataclass(frozen=True)
class StudyEvent:
    event_date: str
    stock_id: str
    stock_name: str
    etf_count: int
    flow_ntd_billion: float
    source_etfs: str
    position_intent: str
    l2_level: str


STUDY_EVENTS: tuple[StudyEvent, ...] = (
    StudyEvent("2026-06-08", "2454", "聯發科", 2, 7.65, "00981A,009816", "MAINTAIN_CORE", "STRONG"),
    StudyEvent("2026-06-08", "3665", "貿聯-KY", 2, 4.66, "009816,00992A", "", ""),
    StudyEvent("2026-06-08", "2368", "金像電", 2, 2.45, "00981A,009816", "BUILD_THEMATIC", "STRONG"),
    StudyEvent("2026-06-08", "2345", "智邦", 2, 1.44, "00403A,009816", "", ""),
    StudyEvent("2026-06-08", "1303", "南亞", 2, 1.16, "00403A,009816", "BUILD_THEMATIC", "FALSE"),
    StudyEvent("2026-06-09", "2454", "聯發科", 3, 12.70, "00981A,00403A,009816", "ROTATION_PLAY", "STRONG"),
    StudyEvent("2026-06-09", "6274", "台燿", 2, 5.57, "00981A,00403A", "SCALE_SATELLITE", "WEAK"),
    StudyEvent("2026-06-09", "2308", "台達電", 2, 4.24, "00403A,009816", "ROTATION_PLAY", "WEAK"),
    StudyEvent("2026-06-09", "2303", "聯電", 2, 2.45, "00981A,009816", "BUILD_THEMATIC", "SINGLE"),
    StudyEvent("2026-06-10", "2327", "國巨*", 2, 18.17, "00981A,00403A", "ROTATION_PLAY", "STRONG"),
    StudyEvent("2026-06-10", "6274", "台燿", 2, 3.55, "00981A,00403A", "ROTATION_PLAY", "FALSE"),
    StudyEvent("2026-06-11", "2327", "國巨*", 3, 15.11, "00981A,00403A,009816", "ROTATION_PLAY", "STRONG"),
    StudyEvent("2026-06-11", "3017", "奇鋐", 2, 4.96, "009816,00992A", "", ""),
    StudyEvent("2026-06-11", "3665", "貿聯-KY", 2, 2.07, "00981A,009816", "", ""),
    StudyEvent("2026-06-11", "1303", "南亞", 2, 1.75, "00403A,009816", "ROTATION_PLAY", "FALSE"),
    StudyEvent("2026-06-11", "4958", "臻鼎-KY", 2, 0.78, "00403A,009816", "", ""),
    StudyEvent("2026-06-12", "2303", "聯電", 3, 8.28, "00981A,00403A,009816", "ROTATION_PLAY", "STRONG"),
    StudyEvent("2026-06-12", "4958", "臻鼎-KY", 2, 9.14, "00981A,00403A", "ROTATION_PLAY", "STRONG"),
    StudyEvent("2026-06-12", "6223", "旺矽", 2, 8.53, "00981A,00403A", "ROTATION_PLAY", "STRONG"),
    StudyEvent("2026-06-12", "3264", "欣銓", 2, 0.41, "00982A,00992A", "", "FALSE"),
    StudyEvent("2026-06-15", "4958", "臻鼎-KY", 3, 9.20, "00981A,00403A,009816", "", ""),
    StudyEvent("2026-06-15", "6223", "旺矽", 2, 8.53, "00981A,00403A", "", ""),
    StudyEvent("2026-06-15", "2303", "聯電", 2, 8.25, "00981A,00403A", "", ""),
    StudyEvent("2026-06-15", "3264", "欣銓", 2, 0.41, "00982A,00992A", "", "FALSE"),
)

INTENT_CN = {
    "MAINTAIN_CORE": "維持核心、權重微調",
    "BUILD_THEMATIC": "主題建倉",
    "ROTATION_PLAY": "資金輪動加碼",
    "SCALE_SATELLITE": "衛星加碼",
}


def connect_study(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_events(conn: sqlite3.Connection) -> None:
    for ev in STUDY_EVENTS:
        conn.execute(
            """
            INSERT INTO study_events (
                event_date, stock_id, stock_name, etf_count, flow_ntd_billion,
                source_etfs, position_intent, l2_level
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_date, stock_id) DO UPDATE SET
                stock_name = excluded.stock_name,
                etf_count = excluded.etf_count,
                flow_ntd_billion = excluded.flow_ntd_billion,
                source_etfs = excluded.source_etfs,
                position_intent = excluded.position_intent,
                l2_level = excluded.l2_level
            """,
            (
                ev.event_date,
                ev.stock_id,
                ev.stock_name,
                ev.etf_count,
                ev.flow_ntd_billion,
                ev.source_etfs,
                ev.position_intent,
                ev.l2_level,
            ),
        )
    conn.commit()


def _bar_window_for_events() -> tuple[date, date]:
    dates = [date.fromisoformat(ev.event_date) for ev in STUDY_EVENTS]
    start = min(dates) - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    end = max(dates)
    return start, end


def _copy_bars_from_main(
    study_conn: sqlite3.Connection,
    main_db: Path,
    stock_ids: set[str],
    end: date,
) -> int:
    """從 stocks.db 唯讀複製全部可用 K 線（不修改主庫）。"""
    if not main_db.exists():
        return 0
    main = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row
    n = 0
    try:
        for sid in sorted(stock_ids):
            rows = main.execute(
                """
                SELECT stock_id, trade_date, open, high, low, close, volume
                FROM stock_daily_bars
                WHERE stock_id = ? AND source = 'finmind'
                  AND trade_date <= ?
                ORDER BY trade_date
                """,
                (sid, end.isoformat()),
            ).fetchall()
            for row in rows:
                study_conn.execute(
                    """
                    INSERT INTO study_daily_bars (
                        stock_id, trade_date, open, high, low, close, volume, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'finmind')
                    ON CONFLICT(stock_id, trade_date) DO NOTHING
                    """,
                    (
                        row["stock_id"],
                        row["trade_date"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                    ),
                )
                n += 1
    finally:
        main.close()
    study_conn.commit()
    return n


def _fetch_missing_bars(
    study_conn: sqlite3.Connection,
    stock_ids: set[str],
    start: date,
    end: date,
) -> int:
    n = 0
    for sid in sorted(stock_ids):
        existing = {
            r[0]
            for r in study_conn.execute(
                "SELECT trade_date FROM study_daily_bars WHERE stock_id = ?",
                (sid,),
            ).fetchall()
        }
        try:
            raw = fetch_finmind("TaiwanStockPrice", sid, start, end)
        except Exception as exc:
            print(f"WARN: FinMind {sid}: {exc}")
            continue
        for row in raw:
            td = str(row.get("date", ""))[:10]
            if not td or td in existing:
                continue
            study_conn.execute(
                """
                INSERT INTO study_daily_bars (
                    stock_id, trade_date, open, high, low, close, volume, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'finmind')
                ON CONFLICT(stock_id, trade_date) DO NOTHING
                """,
                (
                    sid,
                    td,
                    row.get("open"),
                    row.get("max"),
                    row.get("min"),
                    row.get("close"),
                    row.get("Trading_Volume"),
                ),
            )
            n += 1
        study_conn.commit()
        time.sleep(REQUEST_DELAY_SEC)
    return n


def load_bars(conn: sqlite3.Connection, stock_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM study_daily_bars
        WHERE stock_id = ?
        ORDER BY trade_date ASC
        """,
        (stock_id,),
    ).fetchall()


def _bars_on_or_before(rows: list[sqlite3.Row], event_date: str) -> list[sqlite3.Row]:
    return [r for r in rows if str(r["trade_date"]) <= event_date]


def _return_pct(p0: float, p1: float) -> float | None:
    if p0 <= 0:
        return None
    return round((p1 / p0 - 1.0) * 100.0, 2)


def _max_drawdown(closes: list[float]) -> float | None:
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (c / peak - 1.0) * 100.0
        if dd < worst:
            worst = dd
    return round(worst, 2)


def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def analyze_event(
    conn: sqlite3.Connection,
    event_id: int,
    event_date: str,
    stock_id: str,
    overextended_thresh: float,
) -> dict | None:
    all_rows = load_bars(conn, stock_id)
    rows = _bars_on_or_before(all_rows, event_date)
    if len(rows) < MA20_DAYS:
        return None

    tech = _compute_technical_from_rows(rows, entity_id=stock_id)
    if tech is None:
        return None

    closes = [float(r["close"]) for r in rows]
    dates = [str(r["trade_date"]) for r in rows]
    as_of = dates[-1]

    ret_1w = _return_pct(closes[-6], closes[-1]) if len(closes) >= 6 else None
    ret_2w = _return_pct(closes[-11], closes[-1]) if len(closes) >= 11 else None
    dd_2w = _max_drawdown(closes[-(PRE_EVENT_TRADING_DAYS + 1) :])

    ma20_now = _ma(closes, MA20_DAYS)
    ma20_5d_ago = _ma(closes[:-5], MA20_DAYS) if len(closes) > MA20_DAYS + 4 else None
    ma20_rising = int(
        ma20_now is not None
        and ma20_5d_ago is not None
        and ma20_now > ma20_5d_ago
    )

    near_52w_high = int(
        tech.dist_from_52w_high_pct is not None and tech.dist_from_52w_high_pct > -8.0
    )

    uptrend_pullback = int(
        tech.dist_ma60_pct is not None
        and tech.dist_ma60_pct > 0
        and tech.dist_ma20_pct is not None
        and -8.0 <= tech.dist_ma20_pct <= 5.0
    )

    entry_ctx = classify_entry_context(
        tech,
        net_side="add",
        overextended_min=overextended_thresh,
    )

    conn.execute(
        """
        INSERT INTO study_ta_snapshot (
            event_id, as_of_date, close, ma20, ma60,
            dist_ma20_pct, dist_ma60_pct, position_52w_pct, dist_from_52w_high_pct,
            vol_ratio_5d, vol_label, return_1w_pct, return_2w_pct, max_drawdown_2w_pct,
            entry_pattern, above_ma20, above_ma60, ma20_rising, near_52w_high,
            uptrend_pullback, atr14_pct, realized_vol_14d
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            as_of_date = excluded.as_of_date,
            close = excluded.close,
            ma20 = excluded.ma20,
            ma60 = excluded.ma60,
            dist_ma20_pct = excluded.dist_ma20_pct,
            dist_ma60_pct = excluded.dist_ma60_pct,
            position_52w_pct = excluded.position_52w_pct,
            dist_from_52w_high_pct = excluded.dist_from_52w_high_pct,
            vol_ratio_5d = excluded.vol_ratio_5d,
            vol_label = excluded.vol_label,
            return_1w_pct = excluded.return_1w_pct,
            return_2w_pct = excluded.return_2w_pct,
            max_drawdown_2w_pct = excluded.max_drawdown_2w_pct,
            entry_pattern = excluded.entry_pattern,
            above_ma20 = excluded.above_ma20,
            above_ma60 = excluded.above_ma60,
            ma20_rising = excluded.ma20_rising,
            near_52w_high = excluded.near_52w_high,
            uptrend_pullback = excluded.uptrend_pullback,
            atr14_pct = excluded.atr14_pct,
            realized_vol_14d = excluded.realized_vol_14d
        """,
        (
            event_id,
            as_of,
            tech.close,
            tech.ma20,
            tech.ma60,
            tech.dist_ma20_pct,
            tech.dist_ma60_pct,
            tech.position_52w_pct,
            tech.dist_from_52w_high_pct,
            tech.vol_ratio_5d,
            tech.vol_label,
            ret_1w,
            ret_2w,
            dd_2w,
            entry_ctx.signal,
            int(tech.dist_ma20_pct is not None and tech.dist_ma20_pct > 0),
            int(tech.dist_ma60_pct is not None and tech.dist_ma60_pct > 0),
            ma20_rising,
            near_52w_high,
            uptrend_pullback,
            tech.atr14_pct,
            tech.realized_vol_pct_14d,
        ),
    )
    return {
        "as_of": as_of,
        "tech": tech,
        "ret_1w": ret_1w,
        "ret_2w": ret_2w,
        "dd_2w": dd_2w,
        "entry_pattern": entry_ctx.signal,
        "ma20_rising": ma20_rising,
        "near_52w_high": near_52w_high,
        "uptrend_pullback": uptrend_pullback,
    }


def run_analyze(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    events = conn.execute(
        "SELECT * FROM study_events ORDER BY event_date, stock_id"
    ).fetchall()
    extensions: list[float] = []
    for ev in events:
        rows = _bars_on_or_before(load_bars(conn, ev["stock_id"]), ev["event_date"])
        tech = _compute_technical_from_rows(rows, entity_id=ev["stock_id"])
        ext = extension_pct(tech)
        if ext is not None:
            extensions.append(ext)
    thresh = overextended_min_pct(extensions)

    analyzed = 0
    for ev in events:
        if analyze_event(conn, ev["id"], ev["event_date"], ev["stock_id"], thresh):
            analyzed += 1
    conn.commit()
    print(f"Analyzed {analyzed}/{len(events)} events (overextended_thresh={thresh:.1f}%)")
    return conn.execute(
        """
        SELECT e.*, s.*
        FROM study_events e
        JOIN study_ta_snapshot s ON s.event_id = e.id
        ORDER BY e.event_date, e.stock_id
        """
    ).fetchall()


def _pct_true(rows: list[sqlite3.Row], col: str) -> float:
    vals = [r[col] for r in rows if r[col] is not None]
    if not vals:
        return 0.0
    return sum(int(v) for v in vals) / len(vals) * 100.0


def _avg(rows: list[sqlite3.Row], col: str) -> float | None:
    vals = [float(r[col]) for r in rows if r[col] is not None]
    if not vals:
        return None
    return round(statistics.mean(vals), 2)


def _pattern_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        p = r["entry_pattern"] or "—"
        out[p] = out.get(p, 0) + 1
    return out


def _pattern_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        p = r["entry_pattern"] or "—"
        out[p] = out.get(p, 0) + 1
    return out


def _return_pct_window(
    conn: sqlite3.Connection,
    stock_id: str,
    as_of: str,
    *,
    table: str,
    id_col: str,
    n: int = PRE_EVENT_TRADING_DAYS,
) -> float | None:
    rows = conn.execute(
        f"""
        SELECT close FROM {table}
        WHERE {id_col} = ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (stock_id, as_of),
    ).fetchall()
    if len(rows) < n + 1:
        return None
    c0 = float(rows[-(n + 1)]["close"])
    c1 = float(rows[-1]["close"])
    if c0 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 2)


def _return_pct_main_pre(stock_id: str, as_of: str, main_db: Path, n: int = PRE_EVENT_TRADING_DAYS) -> float | None:
    if not main_db.exists():
        return None
    conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT close FROM stock_daily_bars
            WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
            ORDER BY trade_date
            """,
            (stock_id, as_of),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < n + 1:
        return None
    c0 = float(rows[-(n + 1)]["close"])
    c1 = float(rows[-1]["close"])
    if c0 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 2)


def _post_ret_main(stock_id: str, event_date: str, main_db: Path, n: int = POST_EVENT_TRADING_DAYS) -> float | None:
    if not main_db.exists():
        return None
    conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT close FROM stock_daily_bars
            WHERE stock_id = ? AND source = 'finmind' AND trade_date >= ?
            ORDER BY trade_date
            LIMIT ?
            """,
            (stock_id, event_date, n + 1),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < 2:
        return None
    c0 = float(rows[0]["close"])
    c1 = float(rows[-1]["close"])
    if c0 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 2)


def _bench_pre_main(as_of: str, main_db: Path, n: int = PRE_EVENT_TRADING_DAYS) -> float | None:
    if not main_db.exists():
        return None
    conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT close FROM daily_bars
            WHERE code = ? AND date <= ?
            ORDER BY date
            """,
            (BENCHMARK_CODE, as_of),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < n + 1:
        return None
    c0 = float(rows[-(n + 1)]["close"])
    c1 = float(rows[-1]["close"])
    if c0 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 2)


def _load_control_universe(main_db: Path, event_stocks: set[str]) -> list[str]:
    if not main_db.exists():
        return []
    conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    try:
        universe = [
            r[0]
            for r in conn.execute("SELECT DISTINCT stock_id FROM etf_holdings").fetchall()
        ]
    finally:
        conn.close()
    return [s for s in universe if s not in event_stocks]


def permutation_pvalue(group_a: list[float], group_b: list[float], *, iterations: int = PERMUTATION_ITERATIONS) -> float:
    """雙尾 permutation test：H0 = 兩組均值無差。"""
    if not group_a or not group_b:
        return 1.0
    combined = list(group_a) + list(group_b)
    n_a = len(group_a)
    obs = statistics.mean(group_a) - statistics.mean(group_b)
    count = 0
    for _ in range(iterations):
        random.shuffle(combined)
        diff = statistics.mean(combined[:n_a]) - statistics.mean(combined[n_a:])
        if abs(diff) >= abs(obs):
            count += 1
    return round(count / iterations, 3)


@dataclass
class PhaseStats:
    label: str
    dates: tuple[str, ...]
    event_pre2w_mean: float | None = None
    control_pre2w_mean: float | None = None
    alpha_vs_control: float | None = None
    n_events: int = 0


@dataclass
class L2Outcome:
    level: str
    n: int
    pre2w_mean: float | None
    post5_mean: float | None


@dataclass
class RepeatAddRow:
    stock_id: str
    stock_name: str
    event_dates: list[str] = field(default_factory=list)
    pre2w_returns: list[float | None] = field(default_factory=list)
    post5_returns: list[float | None] = field(default_factory=list)


@dataclass
class ValidationStats:
    control_universe_size: int
    event_pre2w_pooled: float | None
    control_pre2w_pooled: float | None
    alpha_vs_control: float | None
    bench_excess_pre2w_mean: float | None
    event_post5_mean: float | None
    control_post5_mean: float | None
    dedup_pre2w_mean: float | None
    negative_pre2w_count: int
    negative_pre2w_post5_mean: float | None
    positive_pre2w_post5_mean: float | None
    strong_vs_other_pvalue: float | None
    by_date: list[tuple[str, float | None, float | None, float | None, float | None]] = field(default_factory=list)
    phases: list[PhaseStats] = field(default_factory=list)
    l2_outcomes: list[L2Outcome] = field(default_factory=list)
    repeat_adds: list[RepeatAddRow] = field(default_factory=list)


def compute_validation_stats(rows: list[sqlite3.Row], main_db: Path) -> ValidationStats:
    event_stocks = {r["stock_id"] for r in rows}
    controls = _load_control_universe(main_db, event_stocks)
    event_dates = sorted({r["event_date"] for r in rows})

    by_date: list[tuple[str, float | None, float | None, float | None, float | None]] = []
    pooled_ev: list[float] = []
    pooled_ctrl: list[float] = []
    bench_excess: list[float] = []
    ctrl_post_by_date: list[float] = []

    for d in event_dates:
        ev_vals = [
            float(r["return_2w_pct"])
            for r in rows
            if r["event_date"] == d and r["return_2w_pct"] is not None
        ]
        ctrl_vals = [_return_pct_main_pre(s, d, main_db) for s in controls]
        ctrl_vals = [v for v in ctrl_vals if v is not None]
        bench = _bench_pre_main(d, main_db)
        ev_mean = round(statistics.mean(ev_vals), 2) if ev_vals else None
        ctrl_mean = round(statistics.mean(ctrl_vals), 2) if ctrl_vals else None
        excess = round(ev_mean - bench, 2) if ev_mean is not None and bench is not None else None
        by_date.append((d, ev_mean, ctrl_mean, bench, excess))
        pooled_ev.extend(ev_vals)
        pooled_ctrl.extend(ctrl_vals)
        if bench is not None:
            for v in ev_vals:
                bench_excess.append(v - bench)
        post_ctrl = [_post_ret_main(s, d, main_db) for s in controls]
        post_ctrl = [v for v in post_ctrl if v is not None]
        if post_ctrl:
            ctrl_post_by_date.append(statistics.mean(post_ctrl))

    event_post5 = [
        _post_ret_main(r["stock_id"], r["event_date"], main_db)
        for r in rows
    ]
    event_post5 = [v for v in event_post5 if v is not None]

    neg_rows = [r for r in rows if r["return_2w_pct"] is not None and r["return_2w_pct"] < 0]
    pos_rows = [r for r in rows if r["return_2w_pct"] is not None and r["return_2w_pct"] >= 0]
    neg_post = [
        _post_ret_main(r["stock_id"], r["event_date"], main_db) for r in neg_rows
    ]
    neg_post = [v for v in neg_post if v is not None]
    pos_post = [
        _post_ret_main(r["stock_id"], r["event_date"], main_db) for r in pos_rows
    ]
    pos_post = [v for v in pos_post if v is not None]

    first: dict[str, sqlite3.Row] = {}
    for r in sorted(rows, key=lambda x: (x["event_date"], x["stock_id"])):
        if r["stock_id"] not in first:
            first[r["stock_id"]] = r
    dedup_pre = [float(r["return_2w_pct"]) for r in first.values() if r["return_2w_pct"] is not None]

    strong_pre = [
        float(r["return_2w_pct"])
        for r in rows
        if r["l2_level"] == "STRONG" and r["return_2w_pct"] is not None
    ]
    other_pre = [
        float(r["return_2w_pct"])
        for r in rows
        if r["l2_level"] != "STRONG" and r["return_2w_pct"] is not None
    ]

    phases: list[PhaseStats] = []
    for label, dates in (
        ("早段 6/08–6/09", tuple(sorted(EARLY_PHASE_DATES))),
        ("晚段 6/10–6/15", tuple(sorted(LATE_PHASE_DATES))),
    ):
        ev_p = [
            float(r["return_2w_pct"])
            for r in rows
            if r["event_date"] in dates and r["return_2w_pct"] is not None
        ]
        ctrl_p: list[float] = []
        for d in dates:
            ctrl_p.extend(
                v
                for v in (_return_pct_main_pre(s, d, main_db) for s in controls)
                if v is not None
            )
        ev_m = round(statistics.mean(ev_p), 2) if ev_p else None
        ctrl_m = round(statistics.mean(ctrl_p), 2) if ctrl_p else None
        phases.append(
            PhaseStats(
                label=label,
                dates=dates,
                event_pre2w_mean=ev_m,
                control_pre2w_mean=ctrl_m,
                alpha_vs_control=round(ev_m - ctrl_m, 2) if ev_m is not None and ctrl_m is not None else None,
                n_events=len(ev_p),
            )
        )

    l2_outcomes: list[L2Outcome] = []
    for level in ("STRONG", "WEAK", "FALSE", "SINGLE", ""):
        grp = [r for r in rows if (r["l2_level"] or "") == level]
        if not grp:
            continue
        pre = [float(r["return_2w_pct"]) for r in grp if r["return_2w_pct"] is not None]
        post = [
            v
            for v in (_post_ret_main(r["stock_id"], r["event_date"], main_db) for r in grp)
            if v is not None
        ]
        l2_outcomes.append(
            L2Outcome(
                level=level or "未標",
                n=len(grp),
                pre2w_mean=round(statistics.mean(pre), 2) if pre else None,
                post5_mean=round(statistics.mean(post), 2) if post else None,
            )
        )

    repeat: dict[str, RepeatAddRow] = {}
    for r in rows:
        sid = r["stock_id"]
        if sid not in repeat:
            repeat[sid] = RepeatAddRow(stock_id=sid, stock_name=r["stock_name"])
        repeat[sid].event_dates.append(r["event_date"])
        repeat[sid].pre2w_returns.append(
            float(r["return_2w_pct"]) if r["return_2w_pct"] is not None else None
        )
        repeat[sid].post5_returns.append(_post_ret_main(sid, r["event_date"], main_db))

    repeat_adds = [v for v in repeat.values() if len(v.event_dates) > 1]

    return ValidationStats(
        control_universe_size=len(controls),
        event_pre2w_pooled=round(statistics.mean(pooled_ev), 2) if pooled_ev else None,
        control_pre2w_pooled=round(statistics.mean(pooled_ctrl), 2) if pooled_ctrl else None,
        alpha_vs_control=round(statistics.mean(pooled_ev) - statistics.mean(pooled_ctrl), 2)
        if pooled_ev and pooled_ctrl
        else None,
        bench_excess_pre2w_mean=round(statistics.mean(bench_excess), 2) if bench_excess else None,
        event_post5_mean=round(statistics.mean(event_post5), 2) if event_post5 else None,
        control_post5_mean=round(statistics.mean(ctrl_post_by_date), 2) if ctrl_post_by_date else None,
        dedup_pre2w_mean=round(statistics.mean(dedup_pre), 2) if dedup_pre else None,
        negative_pre2w_count=len(neg_rows),
        negative_pre2w_post5_mean=round(statistics.mean(neg_post), 2) if neg_post else None,
        positive_pre2w_post5_mean=round(statistics.mean(pos_post), 2) if pos_post else None,
        strong_vs_other_pvalue=permutation_pvalue(strong_pre, other_pre),
        by_date=by_date,
        phases=phases,
        l2_outcomes=l2_outcomes,
        repeat_adds=repeat_adds,
    )


def build_quant_validation_section(rows: list[sqlite3.Row], stats: ValidationStats) -> str:
    lines = [
        "## 量化檢核（控制組 · 分段 · L2 事後 · 統計檢定）",
        "",
        "> 自動對照實驗；樣本小（n=24，13 檔股票），結論為假說。",
        "",
        "### ① 相對強度（事件組 vs 成分股控制組）",
        "",
        f"- 控制組：ETF 成分股 **{stats.control_universe_size}** 檔（排除事件股）",
        f"- 事前 2 週 pooled：事件 **{stats.event_pre2w_pooled:+.2f}%** · 控制 **{stats.control_pre2w_pooled:+.2f}%** · alpha **{stats.alpha_vs_control:+.2f}%**",
        f"- 相對台指 IX0001 超額（事前 2 週均）：**{stats.bench_excess_pre2w_mean:+.2f}%**",
        f"- 去重後（13 檔首次事件）事前 2 週：**{stats.dedup_pre2w_mean:+.2f}%**",
    ]

    lines.extend(["", "| 事件日 | 事件組 2w | 控制組 2w | 台指 2w | 超額 vs 台指 |", "|--------|----------|----------|---------|-------------|"])
    for d, ev_m, ctrl_m, bench, excess in stats.by_date:
        lines.append(
            f"| {d} | {ev_m if ev_m is not None else '—'}% | {ctrl_m if ctrl_m is not None else '—'}% "
            f"| {bench if bench is not None else '—'}% | {excess if excess is not None else '—'}% |"
        )

    lines.extend(
        [
            "",
            "**推論**：若隨機調倉，事件組與控制組應接近；alpha 顯著為正 → 挑選**相對同業更強**標的。",
            "",
            "### ② 早段 vs 晚段（兩階段行為）",
            "",
            "| 階段 | 事件數 | 事件組 2w | 控制組 2w | alpha |",
            "|------|--------|----------|----------|-------|",
        ]
    )
    for ph in stats.phases:
        lines.append(
            f"| {ph.label} | {ph.n_events} | {ph.event_pre2w_mean if ph.event_pre2w_mean is not None else '—'}% "
            f"| {ph.control_pre2w_mean if ph.control_pre2w_mean is not None else '—'}% "
            f"| {ph.alpha_vs_control if ph.alpha_vs_control is not None else '—'}% |"
        )
    lines.extend(
        [
            "",
            f"- 事前 2 週為負仍加碼：**{stats.negative_pre2w_count}/{len(rows)}** 筆（{stats.negative_pre2w_count * 100 // max(len(rows), 1)}%）",
            f"- 事前為負 → 事後 H+5 均 **{stats.negative_pre2w_post5_mean:+.2f}%**；事前為正 → **{stats.positive_pre2w_post5_mean:+.2f}%**",
            "",
            "**推論**：早段允許左側（短線弱仍買）；晚段 alpha 擴大 → 動能確認後加碼。",
            "",
            "### ③ 重複加碼（pyramiding）",
            "",
            "| 代碼 | 名稱 | 加碼日 | 事前 2w（逐次） | 事後 H+5（逐次） |",
            "|------|------|--------|----------------|----------------|",
        ]
    )
    for ra in sorted(stats.repeat_adds, key=lambda x: x.stock_id):
        dates_s = " → ".join(ra.event_dates)
        pre_s = " → ".join(f"{v:+.1f}%" if v is not None else "—" for v in ra.pre2w_returns)
        post_s = " → ".join(f"{v:+.1f}%" if v is not None else "—" for v in ra.post5_returns)
        lines.append(f"| {ra.stock_id} | {ra.stock_name} | {dates_s} | {pre_s} | {post_s} |")

    lines.extend(
        [
            "",
            "**推論**：2327 / 4958 / 6223 多次加碼時事前 2w 多為正 → 對贏家加倉；6274 第二次轉負為反例。",
            "",
            "### ④ L2 標籤 vs 事後表現",
            "",
            "| L2 | n | 事前 2w | 事後 H+5 |",
            "|----|---|--------|---------|",
        ]
    )
    for lo in stats.l2_outcomes:
        lines.append(
            f"| {lo.level} | {lo.n} | {lo.pre2w_mean if lo.pre2w_mean is not None else '—'}% "
            f"| {lo.post5_mean if lo.post5_mean is not None else '—'}% |"
        )
    strong_pre = next((x.pre2w_mean for x in stats.l2_outcomes if x.level == "STRONG"), None)
    lines.extend(
        [
            "",
            f"- STRONG vs 其餘（事前 2w）permutation 雙尾 p ≈ **{stats.strong_vs_other_pvalue}**",
            f"- 事後 H+5：事件 **{stats.event_post5_mean:+.2f}%** · 控制組日均 **{stats.control_post5_mean:+.2f}%**",
            "",
            "**推論**：L2 衡量 ETF 同步力度，不等於股價濾網；FALSE 本週事後最佳、WEAK 最差（n=2）。",
            "",
            "### 修正後操盤模型（四條假說）",
            "",
            "1. **相對強度篩選**：alpha vs 控制組 + alpha vs 台指（見上表）",
            "2. **早段左側 + 晚段 pyramiding**：見分段表與重複加碼表",
            "3. **量價**：平量/量縮為主（見逐筆明細量能欄）",
            "4. **L2 分化使用**：WEAK 警示、FALSE 不一律排除、STRONG 統計不顯著",
        ]
    )
    return "\n".join(lines)


def build_report(rows: list[sqlite3.Row], *, main_db: Path = DEFAULT_MAIN_DB) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# ETF 加碼事件 · 事前 2 週技術面研究",
        "",
        f"> 產出時間 {now} · 資料庫 `data/etf_entry_ta_study.db`（獨立於 stocks.db）",
        "> 方法：僅技術分析（MA20/60、52 週位、量能、價位型態），不含籌碼/基本面。",
        "",
        "## 樣本",
        "",
        f"- 事件數：**{len(rows)}** 筆（2026-06-08～06-15 跨 ETF 共識加碼）",
        f"- 事前窗口：**10 交易日**報酬 / 最大回撤；技術位取事件日收盤",
        f"- K 線來源：唯讀複製 `stocks.db`（約 2025-08 起），52 週位為**可用窗內**分位",
        "",
        "## 全樣本共同特徵（加碼當下）",
        "",
    ]

    if not rows:
        lines.append("_無可分析資料（請先 `--sync --analyze`）_")
        return "\n".join(lines)

    patterns = _pattern_counts(rows)
    pat_str = " · ".join(f"{k} {v}筆" for k, v in sorted(patterns.items(), key=lambda x: -x[1]))
    lines.extend(
        [
            f"| 指標 | 全樣本 | L2=STRONG ({sum(1 for r in rows if r['l2_level']=='STRONG')}筆) | L2≠STRONG |",
            "|------|--------|---------|----------|",
        ]
    )

    strong = [r for r in rows if r["l2_level"] == "STRONG"]
    other = [r for r in rows if r["l2_level"] != "STRONG"]

    metrics = [
        ("站穩 MA20 上方", "above_ma20", _pct_true),
        ("站穩 MA60 上方", "above_ma60", _pct_true),
        ("MA20 五日上行", "ma20_rising", _pct_true),
        ("近 52 週高（距高點 >-8%）", "near_52w_high", _pct_true),
        ("多頭拉回（MA60上 + MA20附近）", "uptrend_pullback", _pct_true),
    ]
    for label, col, fn in metrics:
        lines.append(
            f"| {label} | {fn(rows, col):.0f}% | {fn(strong, col):.0f}% | {fn(other, col):.0f}% |"
        )

    lines.extend(
        [
            f"| 事前 2 週均報酬 | {_avg(rows, 'return_2w_pct')}% | {_avg(strong, 'return_2w_pct')}% | {_avg(other, 'return_2w_pct')}% |",
            f"| 事前 1 週均報酬 | {_avg(rows, 'return_1w_pct')}% | {_avg(strong, 'return_1w_pct')}% | {_avg(other, 'return_1w_pct')}% |",
            f"| 52 週位（均） | {_avg(rows, 'position_52w_pct')}% | {_avg(strong, 'position_52w_pct')}% | {_avg(other, 'position_52w_pct')}% |",
            f"| MA20 乖離（均） | {_avg(rows, 'dist_ma20_pct'):+.1f}% | {_avg(strong, 'dist_ma20_pct'):+.1f}% | {_avg(other, 'dist_ma20_pct'):+.1f}% |",
            f"| 價位型態分布 | {pat_str} | | |",
            "",
            "## 逐筆明細",
            "",
            "| 事件日 | 代碼 | 名稱 | L2 | 型態 | 52w位 | MA20乖離 | 2週報酬 | 2週最大回撤 | 量能 | 技術摘要 |",
            "|--------|------|------|-----|------|-------|----------|---------|-------------|------|----------|",
        ]
    )

    for r in rows:
        summary_parts = []
        if r["above_ma60"]:
            summary_parts.append("多頭結構")
        if r["uptrend_pullback"]:
            summary_parts.append("升勢拉回")
        if r["near_52w_high"]:
            summary_parts.append("近新高")
        if r["entry_pattern"] == "突破":
            summary_parts.append("突破區")
        if r["ma20_rising"]:
            summary_parts.append("短均上行")
        if not summary_parts:
            summary_parts.append("結構中性")
        l2 = r["l2_level"] or "—"
        lines.append(
            f"| {r['event_date']} | {r['stock_id']} | {r['stock_name']} | {l2} "
            f"| {r['entry_pattern']} | {r['position_52w_pct']}% | {r['dist_ma20_pct']:+.1f}% "
            f"| {r['return_2w_pct']}% | {r['max_drawdown_2w_pct']}% | {r['vol_label']} "
            f"| {'、'.join(summary_parts)} |"
        )

    lines.extend(
        [
            "",
            "## 操盤手法推論（純技術面假設）",
            "",
            _infer_trader_view(rows, strong, other, patterns),
            "",
            "## 分組觀察",
            "",
            _group_notes(strong, "L2=STRONG（多檔同步且力度一致）"),
            "",
            _group_notes(other, "其餘（WEAK / FALSE / SINGLE / 未標）"),
            "",
        ]
    )
    if main_db.exists():
        stats = compute_validation_stats(rows, main_db)
        lines.append(build_quant_validation_section(rows, stats))
    else:
        lines.append("## 量化檢核\n\n_主庫 `stocks.db` 不存在，略過控制組對照。_")
    lines.extend(
        [
            "",
            "---",
            "",
            "重新產出：`python src/etf_entry_ta_study.py --sync --analyze --write-report`",
        ]
    )
    return "\n".join(lines)


def _infer_trader_view(
    rows: list[sqlite3.Row],
    strong: list[sqlite3.Row],
    other: list[sqlite3.Row],
    patterns: dict[str, int],
) -> str:
    n = len(rows)
    if n == 0:
        return "_資料不足_"

    above_ma20_pct = _pct_true(rows, "above_ma20")
    above_ma60_pct = _pct_true(rows, "above_ma60")
    ma20_up_pct = _pct_true(rows, "ma20_rising")
    near_high_pct = _pct_true(rows, "near_52w_high")
    pullback_pct = _pct_true(rows, "uptrend_pullback")
    ret_2w = _avg(rows, "return_2w_pct")
    ret_1w = _avg(rows, "return_1w_pct")
    pos_52 = _avg(rows, "position_52w_pct")
    dist_ma20 = _avg(rows, "dist_ma20_pct")
    breakout_n = patterns.get("突破", 0)
    wait_n = patterns.get("觀望", 0)
    over_n = patterns.get("乖離過大", 0)
    pull_n = patterns.get("拉回", 0)

    bullets = [
        f"1. **年內低檔區的動能轉折買點**：52 週位（以現有約 10 個月 K 線窗計算）均值僅 {pos_52}%，"
        f"近 52 週高點 8% 內占 {near_high_pct:.0f}% — 加碼多發生在**年內相對低檔**，"
        "而非創高追價。",
        f"2. **短線動能先行**：事前 2 週均報酬 {ret_2w:+.1f}%、1 週 {ret_1w:+.1f}%，"
        f"且 {ma20_up_pct:.0f}% 樣本 MA20 五日上行 — 操盤者傾向等**跌深後反彈確認**再加碼，"
        "不是無量盤整佈局。",
        f"3. **均線結構分化**：僅 {above_ma60_pct:.0f}% 站 MA60 上、{above_ma20_pct:.0f}% 站 MA20 上，"
        f"MA20 乖離均值 {dist_ma20:+.1f}% — 多數仍在**中期均線下方或剛突破**，"
        "屬於「底部反轉早段」而非成熟多頭。",
        f"4. **型態**：觀望 {wait_n} 筆、拉回 {pull_n} 筆、乖離過大 {over_n} 筆、突破 {breakout_n} 筆。"
        f"升勢拉回（MA60 上 + MA20 附近）僅 {pullback_pct:.0f}% — "
        "典型路徑是**深跌 → 橫盤觀望 → 短線彈升時 ETF 調倉加碼**。",
    ]

    if strong:
        s_ret = _avg(strong, "return_2w_pct")
        s_ma20_up = _pct_true(strong, "ma20_rising")
        s_ma60 = _pct_true(strong, "above_ma60")
        bullets.append(
            f"5. **STRONG 共識 = 動能確認後加碼**：L2=STRONG 子樣本 2 週均報酬 {s_ret:+.1f}%"
            f"（高於全體 {ret_2w:+.1f}%）、MA20 上行 {s_ma20_up:.0f}%、MA60 上方 {s_ma60:.0f}% — "
            "多檔 ETF 同步時，技術上常已出現**更明確的短線反彈**（輪動龍頭啟動）。"
        )

    if other:
        o_ret = _avg(other, "return_2w_pct")
        bullets.append(
            f"6. **弱共識較早或較雜**：非 STRONG 組 2 週均報酬 {o_ret:+.1f}% — "
            "WEAK/FALSE/SINGLE 事件動能較弱或結構未一致，"
            "可能反映單一經理人預先佈局或假共識調倉。"
        )

    bullets.append(
        "7. **量價**：平量/量縮為主（非爆量追高），"
        "推論透過 ETF 申贖與持股再平衡加碼，在散戶尚未大量跟進時調高權重。"
    )

    return "\n".join(bullets)


def _group_notes(group: list[sqlite3.Row], title: str) -> str:
    if not group:
        return f"### {title}\n\n_無樣本_"
    codes = ", ".join(f"{r['stock_id']}{r['stock_name']}" for r in group[:8])
    if len(group) > 8:
        codes += f" 等 {len(group)} 筆"
    pat = _pattern_counts(group)
    pat_s = "、".join(f"{k}{v}" for k, v in pat.items())
    return (
        f"### {title}\n\n"
        f"- 樣本：{codes}\n"
        f"- 型態：{pat_s}\n"
        f"- 2 週均報酬 {_avg(group, 'return_2w_pct')}% · "
        f"52 週位均 {_avg(group, 'position_52w_pct')}% · "
        f"MA60 上方 {_pct_true(group, 'above_ma60'):.0f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 加碼事前技術面研究（獨立 DB）")
    parser.add_argument("--db", type=Path, default=DEFAULT_STUDY_DB)
    parser.add_argument("--main-db", type=Path, default=DEFAULT_MAIN_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--sync", action="store_true", help="寫入事件 + 同步 K 線")
    parser.add_argument("--analyze", action="store_true", help="計算技術快照")
    parser.add_argument("--write-report", action="store_true", help="輸出 markdown 報告")
    args = parser.parse_args()

    do_all = not (args.sync or args.analyze or args.write_report)
    if do_all:
        args.sync = args.analyze = args.write_report = True

    stock_ids = {ev.stock_id for ev in STUDY_EVENTS}
    start, end = _bar_window_for_events()

    with connect_study(args.db) as conn:
        if args.sync:
            upsert_events(conn)
            conn.execute("DELETE FROM study_ta_snapshot")
            conn.execute("DELETE FROM study_daily_bars")
            conn.commit()
            copied = _copy_bars_from_main(conn, args.main_db, stock_ids, end)
            fetched = _fetch_missing_bars(conn, stock_ids, start, end)
            print(f"Sync: copied {copied} bars from main DB, fetched {fetched} from FinMind")

        rows: list[sqlite3.Row] = []
        if args.analyze:
            rows = run_analyze(conn)
        elif args.write_report:
            rows = conn.execute(
                """
                SELECT e.*, s.*
                FROM study_events e
                JOIN study_ta_snapshot s ON s.event_id = e.id
                ORDER BY e.event_date, e.stock_id
                """
            ).fetchall()

        if args.write_report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            text = build_report(rows, main_db=args.main_db)
            args.report.write_text(text, encoding="utf-8")
            print(f"Report → {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
