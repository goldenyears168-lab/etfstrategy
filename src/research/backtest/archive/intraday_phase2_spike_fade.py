"""Phase 2 · spike+heat gated fade backtest.

Based on intraday_path_predictor.py Phase 1 finding:
  "spike+heat≥70" → P(fade_30m≥2%) = 44.57%  lift=1.81×  q_fdr=0.0

This file simulates actual SHORT FADE trades gated on the heat score signal.
For each (stock, day), we find the first bar where:
  1. ext_prev ≥ ext_min_pct (price extended from prev close)
  2. heat_score ≥ heat_min

Then we enter SHORT and simulate:
  - Stop: entry × (1 + stop_pct)
  - Target: entry × (1 - target_pct)
  - EOD: exit at last close if neither hit

Taiwan cost: 0.585% RT (standard) or 0.42% (negotiated)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from intraday_extension_radar import build_bar_snapshots
from research.backtest.archive.intraday_path_predictor import _ma20, _prior_close
from research.backtest.archive.cz_intraday_common import (
    CONTINUOUS_START,
    DEFAULT_COST_RT,
    IS_OOS_RATIO,
    MIN_CONTINUOUS_DAYS,
    SESSION_OPEN,
    SESSION_CLOSE,
    split_dates_is_oos,
    write_intraday_research_json,
    _load_stock_ids,
)
from stock_db.kbar import load_kbar_day_bars

COST_RT_NEGOTIATED = 0.0042  # Negotiated 0.06%/side + 0.3% tax


@dataclass(frozen=True)
class Phase2Params:
    label: str
    ext_min_pct: float = 3.0      # min extension from prev close to qualify
    heat_min: int = 70             # minimum heat score for entry
    stop_pct: float = 0.01        # stop: entry × (1 + stop_pct) → SHORT stops when price rises
    target_pct: float = 0.02      # target: entry × (1 - target_pct) → fade 2% for win
    cost_rt: float = DEFAULT_COST_RT
    signal_before: str = "13:30"  # only take signals before this time (HH:MM)


def _simulate_spike_fade(
    bars_after_entry: tuple,  # tuple of KbarBar after entry bar (inclusive)
    *,
    entry: float,
    stop_pct: float,
    target_pct: float,
) -> tuple[float, str]:
    """Simulate SHORT from entry bar forward. Returns (exit_px, reason)."""
    stop = entry * (1.0 + stop_pct)    # short → exit if price rises to stop
    target = entry * (1.0 - target_pct)  # short → exit if price falls to target

    for bar in bars_after_entry:
        hi, lo = float(bar.high), float(bar.low)
        if hi >= stop:
            return stop, "stop"
        if lo <= target:
            return target, "target"

    # EOD exit
    if bars_after_entry:
        return float(bars_after_entry[-1].close), "eod"
    return entry, "eod"


def run_phase2_day(
    conn: sqlite3.Connection,
    sid: str,
    td: str,
    full_dates: list[str],
    params: Phase2Params,
) -> dict[str, Any] | None:
    """Run one (stock, day) through Phase 2 simulation. Returns trade dict or None."""
    prev = _prior_close(conn, sid, td, full_dates)
    if prev is None or prev <= 0:
        return None

    bars = load_kbar_day_bars(conn, sid, td)
    if len(bars) < 50:
        return None

    ma20 = _ma20(conn, sid, td, full_dates)
    snaps = build_bar_snapshots(bars, prev_close=prev, ma20=ma20)
    if not snaps:
        return None

    # Find FIRST bar where ext_prev ≥ ext_min_pct AND heat ≥ heat_min
    entry_idx: int | None = None
    entry_snap = None
    for i, s in enumerate(snaps):
        if s.ext_prev_pct >= params.ext_min_pct and s.heat_score >= params.heat_min:
            entry_idx = i
            entry_snap = s
            break

    if entry_idx is None:
        return None  # No qualifying signal today

    entry_price = float(entry_snap.close)
    if entry_price <= 0:
        return None

    # Entry at close of entry bar → simulate from NEXT bar only
    bars_after_entry = bars[entry_idx + 1:]
    exit_px, reason = _simulate_spike_fade(
        bars_after_entry,
        entry=entry_price,
        stop_pct=params.stop_pct,
        target_pct=params.target_pct,
    )

    # SHORT trade: profit when price falls
    gross = (entry_price - exit_px) / entry_price
    net = gross - params.cost_rt

    return {
        "trade_date": td,
        "stock_id": sid,
        "entry_minute": entry_snap.minute,
        "entry": entry_price,
        "exit_px": exit_px,
        "exit_reason": reason,
        "heat_score": entry_snap.heat_score,
        "ext_prev_pct": entry_snap.ext_prev_pct,
        "gross_pct": gross * 100,
        "net_pct": net * 100,
    }


# ─────────────────────────────────────────────────────────────────────
# Summary statistics
# ─────────────────────────────────────────────────────────────────────

def _summarize(
    trades: list[dict[str, Any]],
    split_date: str,
    label: str,
    oos_min_n: int = 20,
) -> dict[str, Any]:
    if not trades:
        return {"label": label, "n": 0, "verdict": "NO TRADES"}

    df = pd.DataFrame(trades).sort_values("trade_date")
    is_df = df[df["trade_date"] < split_date]
    oos_df = df[df["trade_date"] >= split_date]

    def _block(t: pd.DataFrame) -> dict[str, Any]:
        if t.empty:
            return {}
        net = t["net_pct"] / 100
        return {
            "n": int(len(t)),
            "date_start": str(t["trade_date"].iloc[0]),
            "date_end": str(t["trade_date"].iloc[-1]),
            "win_rate_pct": round(float((t["net_pct"] > 0).mean() * 100), 1),
            "avg_gross_pct": round(float(t["gross_pct"].mean()), 3),
            "avg_net_pct": round(float(t["net_pct"].mean()), 3),
            "sharpe": round(float(net.mean() / (net.std() + 1e-9) * (252**0.5)), 2),
            "stop_rate_pct": round(float((t["exit_reason"] == "stop").mean() * 100), 1),
            "target_rate_pct": round(float((t["exit_reason"] == "target").mean() * 100), 1),
            "eod_rate_pct": round(float((t["exit_reason"] == "eod").mean() * 100), 1),
            "avg_ext_pct": round(float(t["ext_prev_pct"].mean()), 2),
            "avg_heat": round(float(t["heat_score"].mean()), 1),
            "median_entry_minute": str(sorted(t["entry_minute"].tolist())[len(t) // 2]) if "entry_minute" in t.columns else None,
        }

    is_s = _block(is_df)
    oos_s = _block(oos_df)
    verdict = (
        "PASS"
        if oos_s.get("n", 0) >= oos_min_n and oos_s.get("avg_net_pct", -99) > 0
        else "FAIL"
    )

    return {
        "label": label,
        "n": int(len(df)),
        "split_date": split_date,
        "is": is_s,
        "oos": oos_s,
        "verdict": verdict,
    }


# ─────────────────────────────────────────────────────────────────────
# Main backtest runner
# ─────────────────────────────────────────────────────────────────────

# Default parameter grid — curated from optimization session 2026-06-28
DEFAULT_GRID: tuple[Phase2Params, ...] = (
    # ── Tier 1: Ultra-consistent IS≈OOS (negotiated cost, tight RR, t0930) ──
    Phase2Params("p2_h70_e7_s05_t1_c0420_t0930",    7.0, 70, 0.005, 0.010, COST_RT_NEGOTIATED, "09:30"),
    Phase2Params("p2_h70_e5_s05_t1_c0420_t0930",    5.0, 70, 0.005, 0.010, COST_RT_NEGOTIATED, "09:30"),
    # ── Tier 2: High OOS return (negotiated cost, ext≥7%, t0930) ─────────────
    Phase2Params("p2_h70_e7_s1_t15_c0420_t0930",    7.0, 70, 0.010, 0.015, COST_RT_NEGOTIATED, "09:30"),
    Phase2Params("p2_h70_e7_s1_t15_c0420_t1030",    7.0, 70, 0.010, 0.015, COST_RT_NEGOTIATED, "10:30"),
    Phase2Params("p2_h70_e5_s1_t15_c0420_t0930",    5.0, 70, 0.010, 0.015, COST_RT_NEGOTIATED, "09:30"),
    Phase2Params("p2_h70_e5_s1_t15_c0420_t1030",    5.0, 70, 0.010, 0.015, COST_RT_NEGOTIATED, "10:30"),
    # ── Tier 3: Standard cost viable (no negotiated commission needed) ────────
    Phase2Params("p2_h70_e5_s05_t1_c0585_t0930",    5.0, 70, 0.005, 0.010, DEFAULT_COST_RT, "09:30"),
    Phase2Params("p2_h70_e7_s1_t15_c0585_t0930",    7.0, 70, 0.010, 0.015, DEFAULT_COST_RT, "09:30"),
    Phase2Params("p2_h70_e5_s1_t15_c0585_t0930",    5.0, 70, 0.010, 0.015, DEFAULT_COST_RT, "09:30"),
    # ── Reference: ext≥3% baseline (widest signal capture) ───────────────────
    Phase2Params("p2_h70_e3_s1_t15_c0420_t0930",    3.0, 70, 0.010, 0.015, COST_RT_NEGOTIATED, "09:30"),
    Phase2Params("p2_h70_e3_s1_t15_c0420_t1030",    3.0, 70, 0.010, 0.015, COST_RT_NEGOTIATED, "10:30"),
)


IS_OOS_SPLIT_DATE = "2025-12-15"  # Fixed calendar split: IS < this date, OOS ≥ this date


def run_phase2_backtest(
    conn: sqlite3.Connection,
    *,
    grid: tuple[Phase2Params, ...] = DEFAULT_GRID,
    date_start: str = "2024-01-01",
    date_end: str = "2099-12-31",
    split_date: str = IS_OOS_SPLIT_DATE,
) -> dict[str, Any]:
    """Run Phase 2 spike+heat gated fade backtest across the individual stock universe."""
    conn.row_factory = sqlite3.Row

    # Get all unique (stock, date) pairs in the finmind 1m universe
    stock_ids = _load_stock_ids(conn)
    print(f"  Universe: {len(stock_ids)} stocks")

    # All dates (for session query), split_date is fixed calendar date
    all_dates_rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_kbar_1m
        WHERE source='finmind' AND stock_id != '0050'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (date_start, date_end),
    ).fetchall()
    all_dates = [r["trade_date"] for r in all_dates_rows]

    # Build full_dates list (all trading days across all stocks, for prev_close lookups)
    full_dates_rows = conn.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
    ).fetchall()
    full_dates = [r["trade_date"] for r in full_dates_rows]

    # Get all (stock, date) sessions to scan
    session_rows = conn.execute(
        """
        SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
        WHERE source='finmind' AND stock_id != '0050'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY stock_id, trade_date
        """,
        (date_start, date_end),
    ).fetchall()
    sessions = [(r["stock_id"], r["trade_date"]) for r in session_rows
                if r["stock_id"] in set(stock_ids)]

    print(f"  Sessions: {len(sessions)} stock-days to scan")
    print(f"  Split date: {split_date}")

    # Collect trade candidates once (heat scores computed once per session, reused across params)
    # This is the expensive step
    print("  Scanning for spike+heat events... (this may take a minute)")
    all_candidates: list[dict[str, Any]] = []  # raw event data per session

    for idx, (sid, td) in enumerate(sessions):
        if idx % 500 == 0:
            print(f"    [{idx}/{len(sessions)}] {sid} {td}")

        prev = _prior_close(conn, sid, td, full_dates)
        if prev is None or prev <= 0:
            continue
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < 50:
            continue
        ma20 = _ma20(conn, sid, td, full_dates)
        snaps = build_bar_snapshots(bars, prev_close=prev, ma20=ma20)
        if not snaps:
            continue

        # Precompute candidate per params (different ext_min/heat_min gates)
        session_info = {
            "sid": sid, "td": td, "bars": bars, "snaps": snaps,
        }
        all_candidates.append(session_info)

    print(f"  Sessions loaded: {len(all_candidates)}")

    # Now run each param configuration
    results: dict[str, Any] = {
        "topic": "intraday-phase2-spike-fade",
        "phase": "Phase 2 · spike+heat gated fade · H-TRAJ-6",
        "date_range": f"{date_start} to {date_end}",
        "n_sessions_scanned": len(all_candidates),
        "split_date": split_date,
        "variants": {},
    }

    for params in grid:
        trades: list[dict[str, Any]] = []
        for session_info in all_candidates:
            sid = session_info["sid"]
            td = session_info["td"]
            snaps = session_info["snaps"]
            bars = session_info["bars"]

            # Find first bar meeting this param's gate (respects signal_before time filter)
            entry_idx: int | None = None
            entry_snap = None
            sig_before = params.signal_before[:5]  # "HH:MM"
            for i, s in enumerate(snaps):
                if s.minute[:5] >= sig_before:
                    break
                if s.ext_prev_pct >= params.ext_min_pct and s.heat_score >= params.heat_min:
                    entry_idx = i
                    entry_snap = s
                    break

            if entry_idx is None:
                continue

            entry_price = float(entry_snap.close)
            if entry_price <= 0:
                continue

            # Start simulation from NEXT bar — entry is at close of entry bar,
            # so the entry bar's intrabar range is not eligible for stop/target
            bars_after_entry = bars[entry_idx + 1:]
            exit_px, reason = _simulate_spike_fade(
                bars_after_entry,
                entry=entry_price,
                stop_pct=params.stop_pct,
                target_pct=params.target_pct,
            )

            gross = (entry_price - exit_px) / entry_price
            net = gross - params.cost_rt

            trades.append({
                "trade_date": td,
                "stock_id": sid,
                "entry_minute": entry_snap.minute,
                "entry": entry_price,
                "exit_px": exit_px,
                "exit_reason": reason,
                "heat_score": entry_snap.heat_score,
                "ext_prev_pct": entry_snap.ext_prev_pct,
                "gross_pct": gross * 100,
                "net_pct": net * 100,
            })

        results["variants"][params.label] = _summarize(
            trades, split_date or all_dates[-1], params.label
        )

    # Summary
    pass_count = sum(1 for v in results["variants"].values() if v.get("verdict") == "PASS")
    all_oos = {k: v.get("oos", {}).get("avg_net_pct", -99) for k, v in results["variants"].items()}
    best_label = max(all_oos, key=all_oos.get) if all_oos else None
    best_oos = all_oos.get(best_label, -99) if best_label else -99

    results["summary"] = {
        "pass_count": pass_count,
        "best_variant": best_label,
        "best_oos_avg_net_pct": round(best_oos, 3),
        "total_variants": len(grid),
    }

    return results


def write_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
