"""Track 4 · Zarattini ORB cross-sectional replication on TW individual stocks.

Universe: Finmind 1m individual stocks with continuous daily data from 2026-01-02.
Approach: Pool trades across all stocks; IS/OOS split by calendar date.

Key insight: individual TW stocks have OR5m range averaging 2%+, far exceeding
the 0.585% round-trip cost—unlike 0050 ETF (0.4% average OR range).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtest.cz_intraday_common import (
    CONTINUOUS_START,
    DEFAULT_COST_RT,
    MIN_CONTINUOUS_DAYS,
    UniverseSpec,
    _load_stock_1m,
    _load_stock_ids,
    simulate_orb_long_range_pooled,
    split_dates_is_oos,
    write_intraday_research_json,
)

# Re-export for backward compatibility (Track 5, scripts)
__all__ = [
    "CONTINUOUS_START",
    "MIN_CONTINUOUS_DAYS",
    "_load_stock_ids",
    "_load_stock_1m",
    "run_track4_backtest",
    "write_track4_report",
]

# (label, or_minutes, min_or_range_pct, prev_day_bull_filter, use_vwap_confirm)
# Pooled by-trade gross/net returns (not compounded AUM)
VARIANTS_XSEC: tuple[tuple[str, int, float, bool, bool], ...] = (
    # Baseline: 5m OR breakout, long-only, range stop, no extra filter
    ("xs_or5_rng",          5, 0.000, False, False),
    # High-OR-range days only (≥ 1%)
    ("xs_or5_hv100",        5, 0.010, False, False),
    # Very wide range (≥ 1.5%)
    ("xs_or5_hv150",        5, 0.015, False, False),
    # Extra wide (≥ 2%)
    ("xs_or5_hv200",        5, 0.020, False, False),
    # 15m OR, baseline
    ("xs_or15_rng",        15, 0.000, False, False),
    # 15m OR, high range
    ("xs_or15_hv100",      15, 0.010, False, False),
    # 15m OR, very wide
    ("xs_or15_hv150",      15, 0.015, False, False),
    # Prev-day direction filter: only trade if previous day close > open (bullish)
    ("xs_or5_prevbull",     5, 0.000, True,  False),
    # Combined: wide range + prev bull
    ("xs_or5_hv100_prevbull", 5, 0.010, True, False),
)

IS_OOS_RATIO = 0.65  # More OOS data for better validation


def _simulate_one_day(
    day_1m: pd.DataFrame,
    *,
    or_minutes: int,
    min_or_range_pct: float,
    prev_day_close: float | None,
    prev_day_open: float | None,
    prev_day_bull_filter: bool,
    cost_rt: float,
) -> dict[str, Any] | None:
    """Return per-trade dict or None if no signal."""
    return simulate_orb_long_range_pooled(
        day_1m,
        or_minutes=or_minutes,
        min_or_range_pct=min_or_range_pct,
        prev_day_close=prev_day_close,
        prev_day_open=prev_day_open,
        prev_day_bull_filter=prev_day_bull_filter,
        cost_rt=cost_rt,
    )


def _run_stock(
    stock_id: str,
    k1m: pd.DataFrame,
    *,
    or_minutes: int,
    min_or_range_pct: float,
    prev_day_bull_filter: bool,
    cost_rt: float,
) -> list[dict[str, Any]]:
    """Run all days for one stock with given variant params."""
    if k1m.empty:
        return []

    dates = sorted(k1m["trade_date"].unique())
    k1m_by_date = {td: grp for td, grp in k1m.groupby("trade_date")}

    # Prev-day maps (derived from 1m)
    last_close = {
        td: float(g.sort_values("minute")["close"].iloc[-1])
        for td, g in k1m_by_date.items()
    }
    first_open = {
        td: float(g.sort_values("minute")["open"].iloc[0])
        for td, g in k1m_by_date.items()
    }

    records: list[dict[str, Any]] = []
    for i, td in enumerate(dates):
        day_bars = k1m_by_date.get(td)
        if day_bars is None or day_bars.empty:
            continue
        prev_td = dates[i - 1] if i > 0 else None
        prev_close = last_close.get(prev_td) if prev_td else None
        prev_open = first_open.get(prev_td) if prev_td else None

        result = _simulate_one_day(
            day_bars,
            or_minutes=or_minutes,
            min_or_range_pct=min_or_range_pct,
            prev_day_close=prev_close,
            prev_day_open=prev_open,
            prev_day_bull_filter=prev_day_bull_filter,
            cost_rt=cost_rt,
        )
        if result is not None:
            result["trade_date"] = td
            result["stock_id"] = stock_id
            records.append(result)
    return records


def _summarize_pooled(
    trades: list[dict[str, Any]],
    split_date: str,
    label: str,
) -> dict[str, Any]:
    if not trades:
        return {"label": label, "n": 0, "verdict": "NO TRADES"}

    df = pd.DataFrame(trades).sort_values("trade_date").reset_index(drop=True)
    is_df = df[df["trade_date"] < split_date]
    oos_df = df[df["trade_date"] >= split_date]

    def _block(t: pd.DataFrame, tag: str) -> dict[str, Any]:
        if t.empty:
            return {}
        net = t["net_pct"] / 100  # back to fraction for Sharpe
        return {
            "n": int(len(t)),
            "date_start": str(t["trade_date"].iloc[0]),
            "date_end": str(t["trade_date"].iloc[-1]),
            "win_rate_pct": round(float((t["net_pct"] > 0).mean() * 100), 1),
            "avg_gross_pct": round(float(t["gross_pct"].mean()), 3),
            "avg_net_pct": round(float(t["net_pct"].mean()), 3),
            "sharpe": round(float(net.mean() / (net.std() + 1e-9) * (252**0.5)), 2),
            "stop_rate_pct": round(float((t["exit"] == "stop").mean() * 100), 1),
            "avg_or_range_pct": round(float(t["or_range_pct"].mean()), 3),
        }

    is_s = _block(is_df, "is")
    oos_s = _block(oos_df, "oos")
    oos_avg = oos_s.get("avg_net_pct", -99)
    verdict = "PASS" if oos_s.get("n", 0) >= 30 and oos_avg > 0 else "FAIL"

    return {
        "label": label,
        "n": int(len(df)),
        "split_date": split_date,
        "is": is_s,
        "oos": oos_s,
        "verdict": verdict,
    }


def run_track4_backtest(
    conn: sqlite3.Connection,
    *,
    cost_rt: float = DEFAULT_COST_RT,
) -> dict[str, Any]:
    spec = UniverseSpec.finmind_cross_section()
    stock_ids = _load_stock_ids(conn, spec)
    n_stocks = len(stock_ids)
    print(f"Universe: {n_stocks} individual stocks with ≥{MIN_CONTINUOUS_DAYS} days from {CONTINUOUS_START}")

    # Load all 1m data for each stock (cached per stock)
    stock_data: dict[str, pd.DataFrame] = {}
    for sid in stock_ids:
        df = _load_stock_1m(conn, sid, spec=spec)
        if not df.empty:
            stock_data[sid] = df

    # Determine global split date from all available dates across all stocks
    all_dates: list[str] = []
    for df in stock_data.values():
        all_dates.extend(df["trade_date"].unique().tolist())
    all_dates_sorted = sorted(set(all_dates))
    split_date = split_dates_is_oos(all_dates_sorted, IS_OOS_RATIO)
    if split_date is None:
        split_date = all_dates_sorted[int(len(all_dates_sorted) * IS_OOS_RATIO)]

    results: dict[str, Any] = {
        "topic": "cz-intraday-tw-track4-cross-section",
        "track": "track4_tw_individual_stocks",
        "metrics_mode": "pooled_per_trade",
        "universe_n_stocks": n_stocks,
        "continuous_start": CONTINUOUS_START,
        "split_date": split_date,
        "cost_rt_pct": round(cost_rt * 100, 3),
        "variants": {},
    }

    for label, or_min, min_range, prev_bull, _vwap in VARIANTS_XSEC:
        print(f"  Running {label}...")
        all_trades: list[dict[str, Any]] = []
        for sid, k1m in stock_data.items():
            trades = _run_stock(
                sid,
                k1m,
                or_minutes=or_min,
                min_or_range_pct=min_range,
                prev_day_bull_filter=prev_bull,
                cost_rt=cost_rt,
            )
            all_trades.extend(trades)
        results["variants"][label] = _summarize_pooled(all_trades, split_date, label)

    # Overall summary
    variant_results = results["variants"]
    pass_variants = [k for k, v in variant_results.items() if v.get("verdict") == "PASS"]
    best_oos = max(
        (v.get("oos", {}).get("avg_net_pct", -99) for v in variant_results.values()),
        default=-99,
    )
    results["summary"] = {
        "pass_count": len(pass_variants),
        "pass_variants": pass_variants,
        "best_oos_avg_net_pct": round(best_oos, 3),
        "total_variants": len(VARIANTS_XSEC),
        "split_date": split_date,
    }
    return results


def write_track4_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
