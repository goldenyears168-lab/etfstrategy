#!/usr/bin/env python3
"""dayflip-short post-dump 做多——最終組合驗證.

整合這條研究線驗證過的三件事：
  1) 進場：滾動窗口相對弱勢訊號（個股 vs 0050，15分鐘滾動窗口，落後門檻0.3%，
     10分鐘收斂確認）——比原始個股價格訊號在樣本外全面勝出
     （dayflip_short_rolling_relative_dip_signal.py）
  2) 出場：移動停利5%（比固定停利、比多腳位分段都好，樣本外驗證過）
     （dayflip_short_post_dump_long_trailing_stop_sweep.py）
  3) 資金：逐日保證金排程模擬，抓「同時能開幾倉」的真實限制
     （dayflip_short_post_dump_long_capital_simulation.py）

進場訊號的門檻(0.3%)已經在前一輪walk-forward驗證過，這裡直接用在全部206筆
交易上做資金排程模擬（不用再切train/test——訊號規則本身已經是樣本外驗證過的
固定規則，這裡驗證的是「這個固定規則+不同資金規模」的整體表現）。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_final_combined.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
MARGIN_RATE = 0.135
LOT_SHARES = 2000
BENCH = "0050"
ROLLING_WINDOW_MIN = 15
LAG_THRESHOLD_PCT = 0.3
CONFIRM_MINUTES = 10
CAPITAL_SCENARIOS_NTD = (1_000_000, 2_000_000, 3_000_000, 5_000_000)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def find_rolling_dip_signal(stock_closes: dict, bench_closes: dict) -> tuple[str, float] | None:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None
    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret
    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None
    worst_idx, worst_val = None, 0.0
    for i, m in enumerate(lag_minutes):
        if rolling_lag[m] < -LAG_THRESHOLD_PCT and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_idx = i
    if worst_idx is None:
        return None
    worst_minute = lag_minutes[worst_idx]
    for i in range(worst_idx + 1, len(lag_minutes)):
        m = lag_minutes[i]
        if (i - worst_idx) >= CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
            return m, stock_closes[m]
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def build_calendar(con: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE stock_id='0050' AND source='finmind' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def run_simulation(signals: list[dict], fut_cache: dict, calendar: list[str], total_capital: float) -> dict:
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda s: -s["n_seats"])

    open_positions = []
    realized_pnl = 0.0
    skipped_for_capital = 0
    taken = 0
    nav_series = []
    utilization_series = []

    for day_idx, day in enumerate(calendar):
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = total_capital - margin_used
        utilization_series.append(margin_used / total_capital * 100)

        still_open = []
        for p in open_positions:
            m = fut_cache.get(p["stock"]) or {}
            px = m.get(day)
            if px is None:
                still_open.append(p)
                continue
            close = float(px[1])
            if close <= 0:
                still_open.append(p)
                continue
            p["peak"] = max(p["peak"], close)
            pullback = (p["peak"] - close) / p["peak"] * 100
            hold_days = day_idx - p["entry_day_idx"]
            if pullback >= TRAIL_PCT or hold_days >= MAX_HOLD_DAYS:
                raw_ret = (close / p["entry_price"] - 1) * 100
                net_ret = raw_ret - ROUND_TRIP_COST_PCT
                realized_pnl += p["margin"] / MARGIN_RATE * (net_ret / 100)
                available_margin += p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open

        for s in signals_by_date.get(day, []):
            m = fut_cache.get(s["stock"]) or {}
            if day not in m:
                continue
            fut_close = float(m[day][1])
            if fut_close <= 0:
                continue
            entry_price = fut_close * s["entry_frac"]
            margin = estimate_margin_ntd(entry_price)
            if margin <= available_margin:
                open_positions.append({
                    "stock": s["stock"], "entry_price": entry_price, "entry_day_idx": day_idx,
                    "margin": margin, "peak": entry_price,
                })
                available_margin -= margin
                taken += 1
            else:
                skipped_for_capital += 1

        unrealized = 0.0
        for p in open_positions:
            m = fut_cache.get(p["stock"]) or {}
            px = m.get(day)
            if px is None:
                continue
            close = float(px[1])
            if close <= 0:
                continue
            notional = p["margin"] / MARGIN_RATE
            unrealized += notional * (close / p["entry_price"] - 1)
        nav_series.append(total_capital + realized_pnl + unrealized)

    nav_arr = np.array(nav_series)
    daily_ret = np.diff(nav_arr) / nav_arr[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]
    peak = np.maximum.accumulate(nav_arr)
    dd = (nav_arr - peak) / peak * 100
    max_dd = float(dd.min()) if len(dd) else 0.0
    total_ret_pct = (nav_arr[-1] / total_capital - 1) * 100 if len(nav_arr) else 0.0
    sharpe_annualized = (
        float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if len(daily_ret) > 1 and daily_ret.std() > 0
        else float("nan")
    )
    return {
        "taken": taken, "skipped_for_capital": skipped_for_capital,
        "total_ret_pct": total_ret_pct, "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe_annualized,
        "avg_capital_utilization_pct": float(np.mean(utilization_series)) if utilization_series else 0.0,
    }


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    signals = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_minute_closes(con, sid, t01)
        bench_closes = load_minute_closes(con, BENCH, t01)
        sig = find_rolling_dip_signal(stock_closes, bench_closes)
        if sig is None:
            continue
        _, sig_price = sig
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        signals.append({
            "stock": sid, "trade_date": t01, "entry_frac": sig_price / day_close,
            "n_seats": int(t["n_seats"]),
        })

    print("=== 最終組合：滾動相對弱勢進場 + 移動停利5% + 資金/保證金排程 ===")
    print(f"訊號數（滾動相對弱勢訊號有觸發）: {len(signals)}/{len(trades)}\n")

    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-07")
    med_margin = np.median([
        estimate_margin_ntd(float((fut_cache.get(s['stock']) or {}).get(s['trade_date'], [0, 0])[1]) * s['entry_frac'])
        for s in signals if s["trade_date"] in (fut_cache.get(s["stock"]) or {})
    ])
    print(f"單口保證金中位數: ~{med_margin:,.0f} NTD\n")

    print(f"{'總資金(NTD)':>14} {'約可撐幾口':>10} {'成交':>6} {'因資金跳過':>10} "
          f"{'總報酬%':>10} {'年化Sharpe':>10} {'最大回檔%':>10} {'平均使用率%':>10}")
    for cap in CAPITAL_SCENARIOS_NTD:
        result = run_simulation(signals, fut_cache, calendar, cap)
        n_lots = cap / med_margin
        print(
            f"{cap:>14,} {n_lots:>10.1f} {result['taken']:>6} {result['skipped_for_capital']:>10} "
            f"{result['total_ret_pct']:>10.1f} {result['sharpe_annualized']:>10.3f} "
            f"{result['max_drawdown_pct']:>10.1f} {result['avg_capital_utilization_pct']:>10.1f}"
        )

    print(
        "\n⚠️ 這是整條研究線的組合，不是全新驗證——三個組件各自的walk-forward驗證\n"
        "   見各自的trial registry紀錄；這裡只是把三個已驗證的規則接在一起跑一次\n"
        "   完整的資金排程，本身沒有再做一次train/test切分。\n"
        "   老限制仍在：0050代理台指期、13.5%保證金概估、5bps成本未經滑價實測、\n"
        "   n_seats優先序未驗證、沒有margin call機制。"
    )

    best_cap = 2_000_000
    best_result = run_simulation(signals, fut_cache, calendar, best_cap)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-final-combined-strategy",
        ts="2026-08-09",
        params={
            "entry": "rolling_relative_dip_0.3pct", "exit": "trailing_stop_5pct",
            "capital_scenarios_ntd": list(CAPITAL_SCENARIOS_NTD),
        },
        n_observations=len(signals),
        metric_name=f"total_ret_pct_at_{best_cap}_ntd",
        metric_value=best_result["total_ret_pct"],
        status="kept" if best_result["total_ret_pct"] > 0 else "rejected",
        source=__file__,
        notes=(
            "最終組合：滾動相對弱勢進場(0.3%)+移動停利5%+資金排程模擬。"
            f"{best_cap:,}NTD規模：總報酬{best_result['total_ret_pct']:+.1f}%，"
            f"年化Sharpe{best_result['sharpe_annualized']:.3f}，"
            f"最大回檔{best_result['max_drawdown_pct']:.1f}%。完整規模對照見腳本輸出。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "final-combined"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
