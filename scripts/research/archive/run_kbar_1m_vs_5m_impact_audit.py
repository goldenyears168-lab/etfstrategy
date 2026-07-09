#!/usr/bin/env python3
"""1m vs 5m kbar impact audit · 對照最可能受影響的決策點。

用法：
  PYTHONPATH=src python scripts/run_kbar_1m_vs_5m_impact_audit.py
  PYTHONPATH=src python scripts/run_kbar_1m_vs_5m_impact_audit.py --date-start 2025-01-01 --max-sessions 40
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from intraday_extension_radar import (  # noqa: E402
    ExtensionScanConfig,
    find_first_extension_exit,
    pick_exit_for_mode,
    scan_session,
)
from research.backtest.archive.cz_intraday_common import aggregate_5m  # noqa: E402
from research.backtest.expert_intraday_standalone import detect_orb_entry  # noqa: E402
from research.backtest.rrg_lens_score_swap import _rebalance_minutes  # noqa: E402
from research.backtest.rrg_mono_score_swap_c import intraday_hard_stop_trigger  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import KbarBar, load_kbar_day_bars, load_kbar_day_closes, price_at_or_before_minute  # noqa: E402

POLL_5M = _rebalance_minutes(interval_min=5, no_swap_before="09:30", last_minute="13:30")
SNAPSHOT_MINUTES = ("09:05", "13:00")
HARD_STOP_PCT = -0.08
I36_CONFIG = ExtensionScanConfig(
    exit_mode="combo_spike",
    poll_interval_min=1,
    min_ext_prev_pct=4.0,
    heat_threshold=70,
)


@dataclass
class AuditSummary:
    name: str
    n: int
    match_rate: float | None
    median_delta_bps: float | None
    p95_delta_bps: float | None
    notes: str


def _bars_to_df(bars: tuple[KbarBar, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "minute": b.minute,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume or 0,
            }
            for b in bars
        ]
    )


def _df_to_bars(df: pd.DataFrame) -> tuple[KbarBar, ...]:
    out: list[KbarBar] = []
    for row in df.itertuples(index=False):
        out.append(
            KbarBar(
                minute=str(row.minute),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=int(row.volume or 0),
            )
        )
    return tuple(out)


def aggregate_kbar_5m(bars_1m: tuple[KbarBar, ...]) -> tuple[KbarBar, ...]:
    df = aggregate_5m(_bars_to_df(bars_1m))
    if df.empty:
        return ()
    return _df_to_bars(df)


def _prior_close(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date < ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    prev_k = conn.execute(
        """
        SELECT trade_date FROM stock_kbar_1m
        WHERE stock_id = ? AND trade_date < ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (stock_id, trade_date),
    ).fetchone()
    if not prev_k:
        return None
    bars = load_kbar_day_bars(conn, stock_id, str(prev_k[0]))
    return float(bars[-1].close) if bars else None


def _price_5m_completed_at(bars_5m: tuple[KbarBar, ...], hhmm: str) -> float | None:
    """Last completed 5m bar close with bar open minute <= hhmm (PIT)."""
    target = hhmm if len(hhmm) > 5 else f"{hhmm}:00"
    last: float | None = None
    for bar in bars_5m:
        if bar.minute <= target:
            last = bar.close
        else:
            break
    return last


def _aggregate_5m_pit_at(bars_1m: tuple[KbarBar, ...], hhmm: str) -> float | None:
    """PIT 5m close at poll: aggregate 1m bars ≤ poll, return last bucket close."""
    target = hhmm if len(hhmm) > 5 else f"{hhmm}:00"
    sliced = tuple(b for b in bars_1m if b.minute <= target)
    if not sliced:
        return None
    bars_5m = aggregate_kbar_5m(sliced)
    return bars_5m[-1].close if bars_5m else None


def _price_5m_partial_at(bars_5m: tuple[KbarBar, ...], hhmm: str) -> float | None:
    """Legacy helper · full-day 5m bar open minute == poll (not PIT-safe)."""
    target = hhmm if len(hhmm) > 5 else f"{hhmm}:00"
    for bar in bars_5m:
        if bar.minute == target:
            return bar.close
    return _price_5m_completed_at(bars_5m, hhmm)


def _delta_bps(a: float, b: float) -> float:
    if a <= 0:
        return 0.0
    return abs(a - b) / a * 10_000.0


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def sample_sessions(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    max_sessions: int,
    min_bars: int,
) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, COUNT(*) AS n
        FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
          AND source IN ('finmind', 'yahoo')
        GROUP BY stock_id, trade_date
        HAVING n >= ?
        ORDER BY trade_date DESC, n DESC
        LIMIT ?
        """,
        (date_start, date_end, min_bars, max_sessions * 3),
    ).fetchall()
    seen_dates: set[str] = set()
    out: list[tuple[str, str]] = []
    for row in rows:
        d = str(row["trade_date"])
        if d in seen_dates and len(out) >= max_sessions:
            continue
        out.append((str(row["stock_id"]), d))
        seen_dates.add(d)
        if len(out) >= max_sessions:
            break
    return out


def audit_poll_price_parity(
    conn: sqlite3.Connection,
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    """C0 5m poll · 1m close@poll vs 5m conventions."""
    exact_completed = within_5_completed = 0
    exact_partial = within_5_partial = 0
    deltas_completed: list[float] = []
    deltas_partial: list[float] = []
    deltas_pit: list[float] = []
    exact_pit = within_5_pit = 0
    n = 0
    for sid, td in sessions:
        bars_1m = load_kbar_day_bars(conn, sid, td)
        if len(bars_1m) < 50:
            continue
        closes = load_kbar_day_closes(conn, sid, td)
        bars_5m = aggregate_kbar_5m(bars_1m)
        for minute in POLL_5M:
            px_1m = price_at_or_before_minute(closes, minute)
            px_completed = _price_5m_completed_at(bars_5m, minute)
            px_partial = _price_5m_partial_at(bars_5m, minute)
            px_pit = _aggregate_5m_pit_at(bars_1m, minute)
            if px_1m is None:
                continue
            n += 1
            if px_completed is not None:
                dc = _delta_bps(px_1m, px_completed)
                deltas_completed.append(dc)
                if dc < 0.01:
                    exact_completed += 1
                if dc <= 5.0:
                    within_5_completed += 1
            if px_partial is not None:
                dp = _delta_bps(px_1m, px_partial)
                deltas_partial.append(dp)
                if dp < 0.01:
                    exact_partial += 1
                if dp <= 5.0:
                    within_5_partial += 1
            if px_pit is not None:
                dpt = _delta_bps(px_1m, px_pit)
                deltas_pit.append(dpt)
                if dpt < 0.01:
                    exact_pit += 1
                if dpt <= 5.0:
                    within_5_pit += 1
    return {
        "name": "c0_poll_price_1m_vs_5m",
        "n": n,
        "completed_bar": {
            "exact_match_pct": _pct(exact_completed, n),
            "within_5bps_pct": _pct(within_5_completed, n),
            "median_delta_bps": round(median(deltas_completed), 3) if deltas_completed else None,
            "p95_delta_bps": round(_quantile(deltas_completed, 0.95) or 0.0, 3) if deltas_completed else None,
            "notes": "5m = last completed bar close ≤ poll",
        },
        "partial_bar_at_poll": {
            "exact_match_pct": _pct(exact_partial, n),
            "within_5bps_pct": _pct(within_5_partial, n),
            "median_delta_bps": round(median(deltas_partial), 3) if deltas_partial else None,
            "p95_delta_bps": round(_quantile(deltas_partial, 0.95) or 0.0, 3) if deltas_partial else None,
            "notes": "Full-day 5m bar with open==poll (includes future minutes in bucket)",
        },
        "pit_5m_aggregate_at_poll": {
            "exact_match_pct": _pct(exact_pit, n),
            "within_5bps_pct": _pct(within_5_pit, n),
            "median_delta_bps": round(median(deltas_pit), 3) if deltas_pit else None,
            "p95_delta_bps": round(_quantile(deltas_pit, 0.95) or 0.0, 3) if deltas_pit else None,
            "notes": "PIT-safe: aggregate 1m≤poll then take last 5m bucket close",
        },
    }


def audit_snapshot_minutes(
    conn: sqlite3.Connection,
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    out: dict[str, Any] = {"name": "snapshot_minutes", "minutes": {}}
    for snap in SNAPSHOT_MINUTES:
        exact = within_5 = 0
        deltas: list[float] = []
        n = 0
        for sid, td in sessions:
            bars_1m = load_kbar_day_bars(conn, sid, td)
            if len(bars_1m) < 30:
                continue
            closes = load_kbar_day_closes(conn, sid, td)
            bars_5m = aggregate_kbar_5m(bars_1m)
            px_1m = price_at_or_before_minute(closes, snap)
            px_5m = _price_5m_completed_at(bars_5m, snap)
            if px_1m is None or px_5m is None:
                continue
            n += 1
            d = _delta_bps(px_1m, px_5m)
            deltas.append(d)
            if d < 0.01:
                exact += 1
            if d <= 5.0:
                within_5 += 1
        out["minutes"][snap] = {
            "n": n,
            "exact_match_pct": _pct(exact, n),
            "within_5bps_pct": _pct(within_5, n),
            "median_delta_bps": round(median(deltas), 3) if deltas else None,
            "p95_delta_bps": round(_quantile(deltas, 0.95) or 0.0, 3) if deltas else None,
        }
    return out


def audit_extension_poll_interval(
    conn: sqlite3.Connection,
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    """Same 1m bars · I36 combo_spike · poll_interval 1 vs 5."""
    same_minute = same_day = diff_day = no_signal_both = only_1m = only_5m = 0
    minute_lag: list[int] = []
    price_delta_bps: list[float] = []
    n = 0
    for sid, td in sessions:
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < 100:
            continue
        prev_close = _prior_close(conn, sid, td)
        if prev_close is None:
            continue
        cfg1 = ExtensionScanConfig(
            exit_mode="combo_spike",
            poll_interval_min=1,
            min_ext_prev_pct=4.0,
        )
        cfg5 = ExtensionScanConfig(
            exit_mode="combo_spike",
            poll_interval_min=5,
            min_ext_prev_pct=4.0,
        )
        sig1 = find_first_extension_exit(bars, prev_close=prev_close, ma20=None, config=cfg1)
        sig5 = find_first_extension_exit(bars, prev_close=prev_close, ma20=None, config=cfg5)
        n += 1
        if sig1 is None and sig5 is None:
            no_signal_both += 1
            continue
        if sig1 is not None and sig5 is None:
            only_1m += 1
            continue
        if sig1 is None and sig5 is not None:
            only_5m += 1
            continue
        assert sig1 is not None and sig5 is not None
        if sig1.minute == sig5.minute:
            same_minute += 1
        else:
            diff_day += 1
            m1 = sig1.minute[:5]
            m5 = sig5.minute[:5]
            h1, mm1 = int(m1[:2]), int(m1[3:5])
            h5, mm5 = int(m5[:2]), int(m5[3:5])
            minute_lag.append((h1 * 60 + mm1) - (h5 * 60 + mm5))
        price_delta_bps.append(_delta_bps(sig1.price, sig5.price))
        if sig1.minute[:5] == sig5.minute[:5]:
            same_day += 1
    triggered = n - no_signal_both
    return {
        "name": "extension_i36_poll_1m_vs_5_on_1m_bars",
        "sessions_scanned": n,
        "both_no_signal": no_signal_both,
        "only_poll1_signal": only_1m,
        "only_poll5_signal": only_5m,
        "both_signal": triggered - only_1m - only_5m,
        "same_minute_pct": _pct(same_minute, max(1, triggered - only_1m - only_5m)),
        "same_hhmm_pct": _pct(same_day, max(1, triggered - only_1m - only_5m)),
        "median_minute_lag_when_diff": int(median(minute_lag)) if minute_lag else None,
        "median_exit_price_delta_bps": round(median(price_delta_bps), 3) if price_delta_bps else None,
        "notes": "Same 1m path · only poll grid differs (1 vs 5)",
    }


def audit_extension_bar_resolution(
    conn: sqlite3.Connection,
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    """I36 · 1m bars vs resampled 5m bars · poll_interval=5 both sides."""
    same_minute = only_1m = only_5m = no_both = 0
    price_delta_bps: list[float] = []
    n = 0
    cfg = ExtensionScanConfig(
        exit_mode="combo_spike",
        poll_interval_min=5,
        min_ext_prev_pct=4.0,
    )
    for sid, td in sessions:
        bars_1m = load_kbar_day_bars(conn, sid, td)
        bars_5m = aggregate_kbar_5m(bars_1m)
        if len(bars_1m) < 100 or len(bars_5m) < 20:
            continue
        prev_close = _prior_close(conn, sid, td)
        if prev_close is None:
            continue
        sig_1m = find_first_extension_exit(bars_1m, prev_close=prev_close, ma20=None, config=cfg)
        sig_5m = find_first_extension_exit(bars_5m, prev_close=prev_close, ma20=None, config=cfg)
        n += 1
        if sig_1m is None and sig_5m is None:
            no_both += 1
            continue
        if sig_1m is not None and sig_5m is None:
            only_1m += 1
            continue
        if sig_1m is None and sig_5m is not None:
            only_5m += 1
            continue
        assert sig_1m is not None and sig_5m is not None
        if sig_1m.minute[:5] == sig_5m.minute[:5]:
            same_minute += 1
        price_delta_bps.append(_delta_bps(sig_1m.price, sig_5m.price))
    triggered = n - no_both
    both = triggered - only_1m - only_5m
    return {
        "name": "extension_i36_1m_bars_vs_5m_bars",
        "sessions_scanned": n,
        "both_no_signal": no_both,
        "only_1m_path_signal": only_1m,
        "only_5m_path_signal": only_5m,
        "both_signal": both,
        "same_minute_pct": _pct(same_minute, max(1, both)),
        "median_exit_price_delta_bps": round(median(price_delta_bps), 3) if price_delta_bps else None,
        "notes": "VWAP/climax features recomputed on 5m OHLCV · poll=5",
    }


def audit_hard_stop(
    conn: sqlite3.Connection,
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    """−8% hard stop · 1m low scan vs 5m aggregated low."""
    both_hit = only_1m = only_5m = neither = 0
    minute_lag: list[int] = []
    cache: dict[tuple[str, str], tuple[KbarBar, ...]] = {}
    n = 0
    for sid, td in sessions:
        bars_1m = load_kbar_day_bars(conn, sid, td)
        if len(bars_1m) < 50:
            continue
        session_high = max(b.high for b in bars_1m[20:])
        entry_px = session_high * 0.97
        entry_minute = bars_1m[20].minute
        hit_1m = intraday_hard_stop_trigger(
            conn,
            stock_id=sid,
            trade_date=td,
            entry_px=entry_px,
            entry_date=td,
            entry_minute=entry_minute,
            stop_pct=HARD_STOP_PCT,
            bars_cache=cache,
        )
        stop_px = entry_px * (1.0 + HARD_STOP_PCT)
        hit_5m_minute: str | None = None
        for bar in aggregate_kbar_5m(bars_1m):
            if bar.minute < entry_minute:
                continue
            if bar.low <= stop_px:
                hit_5m_minute = bar.minute
                break
        if not hit_1m[1] and not hit_5m_minute:
            continue
        n += 1
        if hit_1m[1] and hit_5m_minute:
            both_hit += 1
            m1 = hit_1m[1][:5]
            m5 = hit_5m_minute[:5]
            h1, mm1 = int(m1[:2]), int(m1[3:5])
            h5, mm5 = int(m5[:2]), int(m5[3:5])
            minute_lag.append(abs((h1 * 60 + mm1) - (h5 * 60 + mm5)))
        elif hit_1m[1] and not hit_5m_minute:
            only_1m += 1
        elif not hit_1m[1] and hit_5m_minute:
            only_5m += 1
        else:
            neither += 1
    return {
        "name": "hard_stop_minus_8pct",
        "sessions_with_stop_hit": n,
        "both_trigger": both_hit,
        "only_1m_trigger": only_1m,
        "only_5m_trigger": only_5m,
        "both_trigger_same_timing_pct": _pct(
            sum(1 for x in minute_lag if x == 0),
            max(1, both_hit),
        ),
        "median_minute_lag_when_both": int(median(minute_lag)) if minute_lag else None,
        "notes": "Entry=97% of session high after 09:20 · only sessions that hit −8%",
    }


def audit_orb_detection(
    conn: sqlite3.Connection,
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    both = only_1m = only_5m = neither = 0
    minute_diff: list[int] = []
    for sid, td in sessions:
        bars_1m = load_kbar_day_bars(conn, sid, td)
        bars_5m = aggregate_kbar_5m(bars_1m)
        if len(bars_1m) < 30 or len(bars_5m) < 4:
            continue
        t1 = detect_orb_entry(bars_1m)
        t5 = detect_orb_entry(bars_5m)
        if t1 and t5:
            both += 1
            m1 = t1.entry_minute[:5]
            m5 = t5.entry_minute[:5]
            h1, mm1 = int(m1[:2]), int(m1[3:5])
            h5, mm5 = int(m5[:2]), int(m5[3:5])
            minute_diff.append(abs((h1 * 60 + mm1) - (h5 * 60 + mm5)))
        elif t1 and not t5:
            only_1m += 1
        elif not t1 and t5:
            only_5m += 1
        else:
            neither += 1
    triggered = both + only_1m + only_5m
    return {
        "name": "expert_orb_0905_0915",
        "sessions_scanned": len(sessions),
        "both_detect": both,
        "only_1m_detect": only_1m,
        "only_5m_detect": only_5m,
        "neither": neither,
        "same_minute_when_both_pct": _pct(
            sum(1 for x in minute_diff if x == 0),
            max(1, both),
        ),
        "median_minute_diff_when_both": int(median(minute_diff)) if minute_diff else None,
        "notes": "ORB window 09:05–09:15 · 5m has only one bar in OR window",
    }


def audit_3711_regression(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Known spike-fade case from tests/test_intraday_extension_radar.py."""
    bars = load_kbar_day_bars(conn, "3711", "2026-06-26")
    if len(bars) < 50:
        return {"name": "regression_3711", "skipped": True, "reason": "insufficient bars"}
    prev_close = 641.0
    ma20 = 601.0
    cfg1 = I36_CONFIG
    cfg5 = ExtensionScanConfig(
        exit_mode="combo_spike",
        poll_interval_min=5,
        min_ext_prev_pct=4.0,
    )
    bars_5m = aggregate_kbar_5m(bars)
    sig1 = find_first_extension_exit(bars, prev_close=prev_close, ma20=ma20, config=cfg1)
    sig5_poll = find_first_extension_exit(bars, prev_close=prev_close, ma20=ma20, config=cfg5)
    sig5_bars = find_first_extension_exit(bars_5m, prev_close=prev_close, ma20=ma20, config=cfg5)
    scan1 = scan_session(
        bars,
        trade_date="2026-06-26",
        stock_id="3711",
        prev_close=prev_close,
        ma20=ma20,
        config=cfg1,
    )
    peak = max(scan1.bars, key=lambda b: b.close)
    return {
        "name": "regression_3711_20260626",
        "peak_close": peak.close,
        "peak_ext_prev_pct": peak.ext_prev_pct,
        "poll1_exit": asdict(sig1) if sig1 else None,
        "poll5_on_1m_exit": asdict(sig5_poll) if sig5_poll else None,
        "poll5_on_5m_bars_exit": asdict(sig5_bars) if sig5_bars else None,
    }


def audit_c18acc_i36(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2025-12-31",
) -> dict[str, Any]:
    """Champion extension I36 · poll/grid/bar-resolution backtest parity."""
    from dataclasses import replace

    import stock_db.kbar as kmod
    from research.backtest.c18acc_intraday_1m_hold import INTRADAY_1M_ROUND3_FINE, run_intraday_1m_sweep

    i36 = next(c for c in INTRADAY_1M_ROUND3_FINE if c.variant_id == "I36")
    i36_p5 = replace(i36, poll_interval_min=5, variant_id="I36-poll5")
    payload_1m = run_intraday_1m_sweep(
        conn, date_start=date_start, date_end=date_end, configs=[i36, i36_p5]
    )
    rows = {r["variant_id"]: r for r in payload_1m.get("summaries", []) if r.get("variant_id") != "I0"}

    orig_loader = kmod.load_kbar_day_bars

    def _load_5m(c, sid, td, *, source=None):
        bars = orig_loader(c, sid, td, source=source)
        return aggregate_kbar_5m(bars)

    kmod.load_kbar_day_bars = _load_5m  # type: ignore[assignment]
    try:
        i36_bars5 = replace(i36, poll_interval_min=5, variant_id="I36-5m-bars")
        payload_5m = run_intraday_1m_sweep(
            conn, date_start=date_start, date_end=date_end, configs=[i36_bars5]
        )
    finally:
        kmod.load_kbar_day_bars = orig_loader  # type: ignore[assignment]

    r5 = next(r for r in payload_5m["summaries"] if r["variant_id"] == "I36-5m-bars")
    i36_row = rows["I36"]
    p5_row = rows["I36-poll5"]
    return {
        "name": "c18acc_i36_backtest",
        "window": f"{date_start} → {date_end}",
        "I36_poll1_on_1m": _summarize_backtest_row(i36_row),
        "I36_poll5_on_1m": _summarize_backtest_row(p5_row),
        "I36_poll5_on_5m_bars": _summarize_backtest_row(r5),
        "poll1_vs_poll5_identical": i36_row == p5_row,
        "notes": "Portfolio-level C18acc champion legs · I36 combo_spike overlay",
    }


def _summarize_backtest_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mean_excess_pct",
        "ext_exits",
        "median_hold_days",
        "delta_vs_i0_pp",
        "pct_ext_save_positive",
    )
    return {k: row.get(k) for k in keys}


def run_audit(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    max_sessions: int,
) -> dict[str, Any]:
    sessions = sample_sessions(
        conn,
        date_start=date_start,
        date_end=date_end,
        max_sessions=max_sessions,
        min_bars=200,
    )
    if not sessions:
        raise RuntimeError("No sessions with sufficient 1m coverage")
    return {
        "meta": {
            "date_start": date_start,
            "date_end": date_end,
            "max_sessions": max_sessions,
            "sessions_sampled": len(sessions),
            "poll_5m_grid": POLL_5M,
        },
        "audits": [
            audit_poll_price_parity(conn, sessions),
            audit_snapshot_minutes(conn, sessions),
            audit_extension_poll_interval(conn, sessions),
            audit_extension_bar_resolution(conn, sessions),
            audit_hard_stop(conn, sessions),
            audit_orb_detection(conn, sessions),
            audit_3711_regression(conn),
        ],
    }


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# 1m vs 5m K 影響審計",
        "",
        f"- 區間：{payload['meta']['date_start']} → {payload['meta']['date_end']}",
        f"- 樣本 session：{payload['meta']['sessions_sampled']}",
        "",
    ]
    for block in payload["audits"]:
        lines.append(f"## {block['name']}")
        lines.append("")
        for k, v in block.items():
            if k == "name":
                continue
            if isinstance(v, dict):
                lines.append(f"- **{k}**:")
                for sk, sv in v.items():
                    lines.append(f"  - {sk}: {sv}")
            else:
                lines.append(f"- **{k}**: {v}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1m vs 5m kbar impact audit")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-01-01")
    parser.add_argument("--date-end", default=date.today().isoformat())
    parser.add_argument("--max-sessions", type=int, default=60)
    parser.add_argument("--with-c18acc", action="store_true", help="Run I36 poll1 vs poll5 vs 5m-bars backtest")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_audit(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            max_sessions=args.max_sessions,
        )
        if args.with_c18acc:
            payload["audits"].append(
                audit_c18acc_i36(
                    conn,
                    date_start="2024-01-01",
                    date_end="2025-12-31",
                )
            )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or ROOT / "reports" / "research" / "rrg" / f"{stamp}_kbar_1m_vs_5m_impact_audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md_path.write_text(render_md(payload), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {md_path}")
    for block in payload["audits"]:
        print(f"\n=== {block['name']} ===")
        for k, v in block.items():
            if k != "name":
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
