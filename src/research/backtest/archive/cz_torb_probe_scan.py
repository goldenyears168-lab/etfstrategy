"""TORB probe-time scan for 0050 · based on Tsai et al. (2019) §5.

Tsai TORB finding: for TAIEX index futures, optimal probe time t_p = 37 min.
This file scans t_p ∈ {5, 10, 15, 20, 25, 30, 37, 45, 60} on 0050 1m data.

Adds two filters that weren't in Track 3:
  - Vol State: only trade if prev-day |return| is in top-N% (Lundström §4)
  - No extra filter: baseline probe-time scan

TORB signal = standard long-only breakout after probe window closes.
Same infrastructure as Track 3; only the OR window width varies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.archive.cz_intraday_common import (
    DEFAULT_COST_RT,
    INITIAL_AUM_NTD,
    DEFAULT_LEVERAGE,
    IS_OOS_RATIO,
    TRACK2_OOS_MIN_N,
    TradeRecord,
    atr14_from_1m,
    precompute_rvol,
    simulate_orb_tw_session,
    split_dates_is_oos,
    summarize_trades,
    load_1m_frames,
    write_intraday_research_json,
)
from research.backtest.archive.cz_track3_0050_tw import _prev_close_from_1m

# Probe times to scan
PROBE_TIMES = [5, 10, 15, 20, 25, 30, 37, 45, 60]

# Vol State thresholds to test (top-N% of vol days)
# 0 = no filter; 0.7 = top 30% vol days only; 0.8 = top 20%
VOL_STATE_THRESHOLDS = [0.0, 0.7, 0.8]


def _prev_day_abs_return(k1m: pd.DataFrame) -> pd.Series:
    """Prev-day absolute close-to-close return for each date. PIT-safe."""
    last_close = (
        k1m.sort_values("minute")
        .groupby("trade_date")["close"]
        .last()
        .sort_index()
    )
    returns = last_close.pct_change().abs()  # abs |day return|
    # shift(1): today's vol state is known from yesterday
    return returns.shift(1)


def _rolling_percentile_rank(series: pd.Series, window: int = 100) -> pd.Series:
    """Rolling percentile rank within a window. Values in [0, 1]."""
    def _prank(x: np.ndarray) -> float:
        if len(x) < 2:
            return np.nan
        return float(np.sum(x[:-1] < x[-1]) / (len(x) - 1))

    return series.rolling(window, min_periods=20).apply(_prank, raw=True)


def run_torb_probe_scan(
    conn: sqlite3.Connection,
    *,
    stock_id: str = "0050",
    cost_rt: float = DEFAULT_COST_RT,
    leverage: float = DEFAULT_LEVERAGE,
    initial_aum: float = INITIAL_AUM_NTD,
) -> dict[str, Any]:
    k1m = load_1m_frames(conn, stock_id)
    if k1m.empty:
        return {"error": "no_1m_data"}

    dates = sorted(k1m["trade_date"].unique())
    split_date = split_dates_is_oos(dates)

    prev_close_map = _prev_close_from_1m(k1m)

    # Precompute vol-state percentile for each day
    abs_ret = _prev_day_abs_return(k1m)
    vol_pct = _rolling_percentile_rank(abs_ret)  # NaN until 20 days
    vol_pct_map: dict[str, float] = vol_pct.to_dict()

    # Precompute ATR for all dates (needed for atr_self stop modes – not used here,
    # but keep consistent with Track 3 pattern; we use range stop for TORB)
    atr_map: dict[str, float | None] = {}
    for td in dates:
        atr_map[td] = atr14_from_1m(k1m, td)

    k1m_by_date = {td: grp for td, grp in k1m.groupby("trade_date")}

    results: dict[str, Any] = {
        "topic": "cz-torb-probe-scan-0050",
        "strategy": "TORB probe-time scan · Tsai et al. (2019) §5 adapted for 0050",
        "stock_id": stock_id,
        "date_range": f"{dates[0]} to {dates[-1]}",
        "n_days": len(dates),
        "split_date": split_date,
        "cost_rt_pct": round(cost_rt * 100, 3),
        "probe_times": PROBE_TIMES,
        "vol_state_thresholds": VOL_STATE_THRESHOLDS,
        "variants": {},
    }

    for tp in PROBE_TIMES:
        for vol_thresh in VOL_STATE_THRESHOLDS:
            tag = f"torb_tp{tp:02d}" + (f"_vs{int(vol_thresh*100)}" if vol_thresh > 0 else "_base")
            trades: list[TradeRecord] = []
            aum = initial_aum

            for td in dates:
                day_bars = k1m_by_date.get(td)
                if day_bars is None or day_bars.empty:
                    continue

                # Vol State filter
                if vol_thresh > 0:
                    vpct = vol_pct_map.get(td, float("nan"))
                    if np.isnan(vpct) or vpct < vol_thresh:
                        continue

                rec = simulate_orb_tw_session(
                    day_bars,
                    trade_date=td,
                    prev_close=prev_close_map.get(td),
                    rvol_today=None,
                    atr14_val=atr_map.get(td),
                    or_minutes=tp,
                    stop_mode="range",
                    gap_filter_pct=0.0,
                    rvol_min=0.0,
                    min_or_range_pct=0.0,
                    aum=aum,
                    leverage=leverage,
                    cost_rt=cost_rt,
                )
                if rec is None:
                    continue
                trades.append(rec)
                if np.isfinite(rec.pnl_ntd):
                    aum = max(0.0, aum + rec.pnl_ntd)

            results["variants"][tag] = summarize_trades(
                trades,
                split_date=split_date,
                label=tag,
                oos_min_n=TRACK2_OOS_MIN_N,
            )

    # Best OOS summary
    pass_count = sum(1 for v in results["variants"].values() if v.get("verdict") == "PASS")
    all_oos = {k: v.get("oos", {}).get("avg_net_pct", -99) for k, v in results["variants"].items()}
    best_label = max(all_oos, key=all_oos.get) if all_oos else None
    best_oos = all_oos.get(best_label, -99) if best_label else -99

    results["summary"] = {
        "pass_count": pass_count,
        "best_variant": best_label,
        "best_oos_avg_net_pct": round(best_oos, 3),
        "total_variants": len(PROBE_TIMES) * len(VOL_STATE_THRESHOLDS),
        "taiex_optimal_tp": 37,
    }

    return results


def write_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
