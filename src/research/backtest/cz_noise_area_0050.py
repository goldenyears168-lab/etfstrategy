"""Noise Area strategy for 0050 ETF · Zarattini §2 adapted for Taiwan.

Original paper: SPY 09:30-16:00 ET, evaluate every 30 min.
Taiwan adaptation: 0050 09:00-13:30, evaluate at 09:30, 10:00, ..., 13:00.

Core idea: define "noise area" from historical 14-day avg drift at each time point.
Enter long when 0050 breaks above the upper boundary (more bullish than normal).
Trailing stop = max(upper_bound, VWAP).

Variants test Volatility Multiplier (VM): 0.75, 1.0, 1.5, 2.0, 2.5
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.cz_intraday_common import (
    DEFAULT_COST_RT,
    IS_OOS_RATIO,
    SESSION_CLOSE,
    SESSION_OPEN,
    filter_session,
    load_1m_frames,
    session_vwap,
    write_intraday_research_json,
    split_dates_is_oos,
)

# Taiwan session evaluation times (every 30 min after open)
EVAL_MINUTES = [
    "09:30:00", "10:00:00", "10:30:00", "11:00:00",
    "11:30:00", "12:00:00", "12:30:00", "13:00:00",
]
EVAL_SET = set(EVAL_MINUTES)

# (label, vm, gap_up_only, early_only)
# vm: Volatility Multiplier (scales noise area width)
# gap_up_only: only enter on gap-up days (open > prev_close)
# early_only: only use first eval point (09:30) to avoid late entries
VARIANTS: tuple[tuple[str, float, bool, bool], ...] = (
    ("na_vm075",         0.75, False, False),
    ("na_vm100",         1.00, False, False),
    ("na_vm150",         1.50, False, False),
    ("na_vm200",         2.00, False, False),
    ("na_vm250",         2.50, False, False),
    ("na_vm150_early",   1.50, False, True),
    ("na_vm200_early",   2.00, False, True),
    ("na_vm150_gapup",   1.50, True,  False),
    ("na_vm200_gapup",   2.00, True,  False),
)


# ─────────────────────────────────────────────────────────────────────
# Precomputation: rolling 14-day average drift at each eval minute
# ─────────────────────────────────────────────────────────────────────

def precompute_noise_sigma(k1m: pd.DataFrame) -> pd.DataFrame:
    """For each (date, eval_minute), compute rolling 14-day mean of
    (close_at_eval / day_open - 1). PIT-safe: shift(1) before rolling.

    Returns DataFrame with index=trade_date, columns=EVAL_MINUTES.
    Values are σ (signed average drift); NaN where < 14 prior days.
    """
    daily_open = (
        k1m.sort_values("minute")
        .groupby("trade_date")["open"]
        .first()
        .sort_index()
    )

    series: dict[str, pd.Series] = {}
    for em in EVAL_MINUTES:
        # Last close at or before eval_minute each day
        closes = (
            k1m[k1m["minute"] <= em]
            .groupby("trade_date")["close"]
            .last()
            .sort_index()
        )
        returns = closes / daily_open - 1.0
        returns = returns.dropna().sort_index()
        # PIT-safe: yesterday's 14-day mean informs today's signal
        sigma_series = returns.shift(1).rolling(14, min_periods=14).mean()
        series[em] = sigma_series

    return pd.DataFrame(series)


# ─────────────────────────────────────────────────────────────────────
# Per-day simulation
# ─────────────────────────────────────────────────────────────────────

def simulate_noise_area_day(
    day_1m: pd.DataFrame,
    *,
    sigma_row: pd.Series,
    vm: float,
    prev_close: float | None,
    gap_up_only: bool,
    early_only: bool,
    cost_rt: float = DEFAULT_COST_RT,
) -> dict[str, Any] | None:
    """Long-only Noise Area simulation for one trading day.

    Returns trade dict or None if no signal / no entry.
    """
    day = day_1m.sort_values("minute").reset_index(drop=True)
    if day.empty:
        return None

    today_open = float(day.iloc[0]["open"])
    if today_open <= 0:
        return None

    # Gap filter
    if gap_up_only:
        if prev_close is None or prev_close <= 0:
            return None
        if today_open <= prev_close:
            return None

    # Reference anchors for boundary calculation
    ref_up = max(today_open, prev_close) if prev_close and prev_close > 0 else today_open
    ref_dn = min(today_open, prev_close) if prev_close and prev_close > 0 else today_open

    # Precompute intraday VWAP for all bars
    typical = (day["high"] + day["low"] + day["close"]) / 3.0
    vol = day["volume"].fillna(0).astype(float).clip(lower=0)
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum().replace(0, np.nan)
    day = day.copy()
    day["vwap"] = cum_pv / cum_v

    in_position = False
    entry_px: float = 0.0
    entry_ub: float = 0.0
    entry_minute: str = ""
    checked_early = False

    for _, bar in day.iterrows():
        cur_min = str(bar["minute"])
        cur_close = float(bar["close"])
        cur_low = float(bar["low"])
        cur_vwap = float(bar["vwap"]) if not np.isnan(bar["vwap"]) else today_open

        if not in_position and cur_min in EVAL_SET:
            if early_only and checked_early:
                pass  # skip further eval points
            else:
                checked_early = True
                sigma = sigma_row.get(cur_min)
                if sigma is not None and not np.isnan(float(sigma)):
                    sig = float(sigma)
                    ub = ref_up * (1.0 + vm * sig)
                    lb = ref_dn * (1.0 - vm * sig)  # noqa: F841 (unused for long-only)

                    # Long signal: today's price exceeds upper noise boundary
                    if cur_close > ub:
                        entry_px = cur_close
                        entry_ub = ub
                        entry_minute = cur_min
                        in_position = True

        elif in_position:
            # Trailing stop = max(entry_ub, current VWAP)
            trail_stop = max(entry_ub, cur_vwap)
            if cur_low <= trail_stop:
                exit_px = trail_stop
                gross = (exit_px - entry_px) / entry_px
                net = gross - cost_rt
                return {
                    "gross_pct": gross * 100,
                    "net_pct": net * 100,
                    "exit": "stop",
                    "entry_minute": entry_minute,
                }

    if in_position:
        exit_px = float(day.iloc[-1]["close"])
        gross = (exit_px - entry_px) / entry_px
        net = gross - cost_rt
        return {
            "gross_pct": gross * 100,
            "net_pct": net * 100,
            "exit": "eod",
            "entry_minute": entry_minute,
        }

    return None


# ─────────────────────────────────────────────────────────────────────
# Results summarization
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
            "stop_rate_pct": round(float((t["exit"] == "stop").mean() * 100), 1),
        }

    is_s = _block(is_df)
    oos_s = _block(oos_df)
    oos_n = oos_s.get("n", 0)
    oos_avg = oos_s.get("avg_net_pct", -99)
    verdict = "PASS" if oos_n >= oos_min_n and oos_avg > 0 else "FAIL"

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

def run_noise_area_backtest(
    conn: sqlite3.Connection,
    *,
    stock_id: str = "0050",
    cost_rt: float = DEFAULT_COST_RT,
    date_start: str = "2024-01-01",
    date_end: str = "2099-12-31",
) -> dict[str, Any]:
    k1m = load_1m_frames(conn, stock_id)
    if k1m.empty:
        return {"error": "no_1m_data", "stock_id": stock_id}

    # Date filter for smoke test
    k1m = k1m[(k1m["trade_date"] >= date_start) & (k1m["trade_date"] <= date_end)]
    if k1m.empty:
        return {"error": "no_data_in_date_range"}

    dates = sorted(k1m["trade_date"].unique())
    split_date = split_dates_is_oos(dates)

    # Precompute sigma table for all eval minutes
    sigma_df = precompute_noise_sigma(k1m)

    # Prev close map from 1m last close
    last_close = (
        k1m.sort_values("minute")
        .groupby("trade_date")["close"]
        .last()
        .sort_index()
    )
    prev_close_map: dict[str, float] = {}
    date_list = list(last_close.index)
    for i in range(1, len(date_list)):
        prev_close_map[date_list[i]] = float(last_close.iloc[i - 1])

    # Group by date
    k1m_by_date = {td: grp for td, grp in k1m.groupby("trade_date")}

    results: dict[str, Any] = {
        "topic": "cz-noise-area-0050",
        "strategy": "Zarattini §2 Noise Area adapted for Taiwan 0050",
        "stock_id": stock_id,
        "date_range": f"{dates[0]} to {dates[-1]}",
        "n_days": len(dates),
        "split_date": split_date,
        "cost_rt_pct": round(cost_rt * 100, 3),
        "variants": {},
    }

    for label, vm, gap_up_only, early_only in VARIANTS:
        trades: list[dict[str, Any]] = []
        for td in dates:
            day_bars = k1m_by_date.get(td)
            if day_bars is None or day_bars.empty:
                continue
            sigma_row = sigma_df.loc[td] if td in sigma_df.index else pd.Series(dtype=float)
            if sigma_row.isna().all():
                continue
            rec = simulate_noise_area_day(
                day_bars,
                sigma_row=sigma_row,
                vm=vm,
                prev_close=prev_close_map.get(td),
                gap_up_only=gap_up_only,
                early_only=early_only,
                cost_rt=cost_rt,
            )
            if rec is not None:
                rec["trade_date"] = td
                trades.append(rec)

        results["variants"][label] = _summarize(
            trades, split_date or dates[-1], label
        )

    # Best variant summary
    pass_count = sum(1 for v in results["variants"].values() if v.get("verdict") == "PASS")
    best_oos = max(
        (v.get("oos", {}).get("avg_net_pct", -99) for v in results["variants"].values()),
        default=-99,
    )
    best_label = next(
        (k for k, v in results["variants"].items()
         if v.get("oos", {}).get("avg_net_pct", -99) == best_oos),
        None,
    )
    results["summary"] = {
        "pass_count": pass_count,
        "best_oos_avg_net_pct": round(best_oos, 3),
        "best_variant": best_label,
        "total_variants": len(VARIANTS),
    }

    return results


def write_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
