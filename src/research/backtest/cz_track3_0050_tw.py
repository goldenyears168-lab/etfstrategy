"""Track 3 · Zarattini ORB adapted for Taiwan 0050 ETF.

Key changes vs Track 2 (QQQ spec):
- Long-only (no short) — 0050 in structural bull market 2024-2026
- Breakout (stop-buy) entry at OR_high, not 2nd-bar market entry
- 15m or 30m opening range (wider / more meaningful for TW)
- Stop calibrated to 0050's own ATR (not IR0002 benchmark index)
- Relative Volume filter and gap-up filter variants
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.cz_intraday_common import (
    BENCHMARK_DAILY_CODE,
    DEFAULT_COST_RT,
    DEFAULT_LEVERAGE,
    INITIAL_AUM_NTD,
    TRACK2_OOS_MIN_N,
    TradeRecord,
    atr14_from_1m,
    benchmark_buy_hold,
    filter_session,
    kbar_coverage,
    load_1m_frames,
    load_daily_bars,
    precompute_rvol,
    simulate_orb_tw_session,
    split_dates_is_oos,
    summarize_trades,
    write_intraday_research_json,
)

DEFAULT_STOCK_ID = "0050"

# (label, or_minutes, stop_mode, gap_filter_pct, rvol_min, min_or_range_pct)
# gap_filter_pct: min gap-up % from prev 1m close (0 = no filter)
# rvol_min: min OR relative volume vs 14-day avg (0 = no filter)
# min_or_range_pct: min OR high-low range as % of open (0 = no filter)
VARIANTS_TW: tuple[tuple[str, int, str, float, float, float], ...] = (
    # --- Baseline breakout variants (no extra filters) ---
    ("tw_lo_or5_rng",     5, "range",     0.000, 0.0, 0.000),
    ("tw_lo_or15_rng",   15, "range",     0.000, 0.0, 0.000),
    ("tw_lo_or30_rng",   30, "range",     0.000, 0.0, 0.000),
    # --- Rel Vol filters (Zarattini's core edge) ---
    ("tw_lo_or5_rv15",    5, "range",     0.000, 1.5, 0.000),
    ("tw_lo_or5_rv20",    5, "range",     0.000, 2.0, 0.000),
    ("tw_lo_or5_rv30",    5, "range",     0.000, 3.0, 0.000),
    ("tw_lo_or15_rv15",  15, "range",     0.000, 1.5, 0.000),
    ("tw_lo_or15_rv20",  15, "range",     0.000, 2.0, 0.000),
    ("tw_lo_or15_rv30",  15, "range",     0.000, 3.0, 0.000),
    # --- Gap-up filter (prev 1m close, corrected) ---
    ("tw_lo_or5_gap02",   5, "range",     0.002, 0.0, 0.000),
    ("tw_lo_or5_gap03",   5, "range",     0.003, 0.0, 0.000),
    ("tw_lo_or5_gap03_rv15", 5, "range",  0.003, 1.5, 0.000),
    # --- High-volatility day filter (wide OR range) ---
    ("tw_lo_or5_hv50",    5, "range",     0.000, 0.0, 0.005),
    ("tw_lo_or5_hv80",    5, "range",     0.000, 0.0, 0.008),
    # --- Wide stop variants for 5m OR ---
    ("tw_lo_or5_rv20_fix1", 5, "fixed1pct", 0.000, 2.0, 0.000),
    ("tw_lo_or5_rv30_fix1", 5, "fixed1pct", 0.000, 3.0, 0.000),
)


def _prev_close_from_1m(k1m: pd.DataFrame) -> dict[str, float]:
    """Derive prev_close for each trade_date from the 1m bars.

    Uses last close of the previous trading day (PIT-safe, no external daily data needed).
    Avoids the IR0002 (TAIEX index) scale mismatch bug.
    """
    last_close = (
        k1m.sort_values("minute")
        .groupby("trade_date")["close"]
        .last()
        .sort_index()
    )
    result: dict[str, float] = {}
    dates = list(last_close.index)
    for i in range(1, len(dates)):
        result[dates[i]] = float(last_close.iloc[i - 1])
    return result


def _prev_day_open_from_1m(k1m: pd.DataFrame) -> dict[str, float]:
    """Derive prev_day open for each trade_date (first bar's open of previous day)."""
    first_open = (
        k1m.sort_values("minute")
        .groupby("trade_date")["open"]
        .first()
        .sort_index()
    )
    result: dict[str, float] = {}
    dates = list(first_open.index)
    for i in range(1, len(dates)):
        result[dates[i]] = float(first_open.iloc[i - 1])
    return result


def run_track3_backtest(
    conn: sqlite3.Connection,
    *,
    stock_id: str = DEFAULT_STOCK_ID,
    cost_rt: float = DEFAULT_COST_RT,
    leverage: float = DEFAULT_LEVERAGE,
    initial_aum: float = INITIAL_AUM_NTD,
) -> dict[str, Any]:
    k1m = load_1m_frames(conn, stock_id)
    daily = load_daily_bars(conn, BENCHMARK_DAILY_CODE)
    coverage = kbar_coverage(conn, stock_id)

    if k1m.empty:
        return {"stock_id": stock_id, "error": "no_1m_data", "coverage": coverage}

    dates = sorted(k1m["trade_date"].unique())
    split_date = split_dates_is_oos(dates)

    # Derive prev_close from 1m data (avoids IR0002/TAIEX scale mismatch)
    prev_close_map = _prev_close_from_1m(k1m)

    # Precompute OR-window Rel Vol for each distinct OR minute width (avoid O(n²))
    rvol_cache: dict[int, pd.Series] = {}
    for _, or_min, _, _, rvol_min, _ in VARIANTS_TW:
        if rvol_min > 0 and or_min not in rvol_cache:
            rvol_cache[or_min] = precompute_rvol(k1m, or_min)

    # Precompute ATR14 from 1m data for all dates (used by atr_self stop modes)
    atr_map: dict[str, float | None] = {td: atr14_from_1m(k1m, td) for td in dates}

    # Group 1m bars by date once for fast lookup
    k1m_by_date: dict[str, pd.DataFrame] = {
        td: grp for td, grp in k1m.groupby("trade_date")
    }

    results: dict[str, Any] = {
        "topic": "cz-intraday-tw-track3",
        "track": "track3_0050_tw_adapted",
        "metrics_mode": "compounded_aum",
        "stock_id": stock_id,
        "benchmark_daily_code": BENCHMARK_DAILY_CODE,
        "spec": "Long-only OR breakout (stop-buy) · TW-adapted · 15/30m OR",
        "session": "09:00-13:30",
        "cost_rt_pct": round(cost_rt * 100, 3),
        "leverage_cap": leverage,
        "initial_aum_ntd": initial_aum,
        "split_date": split_date,
        "coverage": coverage,
        "variants": {},
    }

    trade_dates_any: list[str] = []

    for label, or_min, stop_mode, gap_pct, rvol_min, min_range in VARIANTS_TW:
        trades: list[TradeRecord] = []
        aum = initial_aum
        rvol_series = rvol_cache.get(or_min)
        for td in dates:
            day_bars = k1m_by_date.get(td)
            if day_bars is None or day_bars.empty:
                continue
            rvol_today = float(rvol_series.get(td, float("nan"))) if rvol_series is not None else None
            rec = simulate_orb_tw_session(
                day_bars,
                trade_date=td,
                prev_close=prev_close_map.get(td),
                rvol_today=rvol_today,
                atr14_val=atr_map.get(td),
                or_minutes=or_min,
                stop_mode=stop_mode,  # type: ignore[arg-type]
                gap_filter_pct=gap_pct,
                rvol_min=rvol_min,
                min_or_range_pct=min_range,
                aum=aum,
                leverage=leverage,
                cost_rt=cost_rt,
            )
            if rec is None:
                continue
            trades.append(rec)
            if np.isfinite(rec.pnl_ntd):
                aum = max(0.0, aum + rec.pnl_ntd)

        if label == "tw_lo_or15_rng":
            trade_dates_any = [t.trade_date for t in trades]

        results["variants"][label] = summarize_trades(
            trades,
            split_date=split_date,
            label=label,
            oos_min_n=TRACK2_OOS_MIN_N,
        )

    # Benchmark B&H: build daily return series from 1m last-close data
    last_close_1m = (
        k1m.sort_values("minute")
        .groupby("trade_date")["close"]
        .last()
        .sort_index()
    )
    bh_daily = pd.DataFrame({"close": last_close_1m})
    results["benchmark_bh_0050"] = benchmark_buy_hold(
        bh_daily,
        trade_dates_any or dates,
        split_date=split_date,
    )

    # Summarize hypotheses
    variant_results = results["variants"]
    best_oos_sharpe = max(
        (v.get("oos", {}).get("sharpe") or -99 for v in variant_results.values()),
        default=-99,
    )
    best_label = next(
        (k for k, v in variant_results.items()
         if (v.get("oos", {}).get("sharpe") or -99) == best_oos_sharpe),
        None,
    )
    results["summary"] = {
        "best_variant_oos_sharpe": round(best_oos_sharpe, 3),
        "best_variant": best_label,
        "pass_count": sum(
            1 for v in variant_results.values() if v.get("verdict") == "PASS"
        ),
        "total_variants": len(VARIANTS_TW),
    }

    return results


def write_track3_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
