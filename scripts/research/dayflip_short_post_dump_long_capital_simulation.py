#!/usr/bin/env python3
"""dayflip-short post-dump 做多——真的資金/保證金排程模擬.

前幾輪回測都刻意不報 max drawdown/累積報酬，因為假設「資金依序不重疊投入」在
這個策略上嚴重不成立（測試期22天就有81筆訊號）。這裡補上真的逐日模擬：
  - 固定總資金 TOTAL_CAPITAL_NTD，每筆用 estimate_margin_ntd()（跟
    src/order/dayflip_short_signal.py 同一套 13.5% 概估）算保證金
  - 逐日：先處理當天到期出場（移動停利5%，同前一輪驗證過的規則）釋放保證金，
    再處理當天新訊號進場——保證金不夠就跳過，記錄「因資金不足被跳過」的訊號數
  - 多訊號搶同一筆資金時，用 n_seats（觸發分點數，訊號強度代理）排優先序
  - 逐日 mark-to-market 算真正的 portfolio NAV，才能算出有意義的 max drawdown
    / Sharpe / 總報酬——不是前幾輪那種數學上站不住腳的簡化曲線

掃過幾種資金規模（等於同時能撐幾口的量級），看資金夠不夠用、放大資金報酬會不會
線性放大（訊號夠不夠多）。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_capital_simulation.py
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
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15
CAPITAL_SCENARIOS_NTD = (300_000, 600_000, 1_000_000, 2_000_000, 5_000_000)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_entry_price(con: sqlite3.Connection, stock_id: str, t01: str) -> tuple[float, str] | None:
    raw = load_kbar_day_bars(con, stock_id, t01)
    bars = [
        (b.minute[:5], b.low, b.close)
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.low and b.low > 0 and b.close
    ]
    if len(bars) < 50:
        return None
    running_low = bars[0][1]
    running_low_idx = 0
    for i, (minute, low, close) in enumerate(bars):
        if low < running_low:
            running_low = low
            running_low_idx = i
        if (i - running_low_idx) >= MIN_MINUTES_OFF_LOW and (close / running_low - 1) * 100 >= REBOUND_THRESHOLD_PCT:
            return close, "intraday_signal"
    return bars[-1][2], "close_fallback"


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
    cal_idx = {d: i for i, d in enumerate(calendar)}
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda s: -s["n_seats"])  # 強訊號優先搶資金

    open_positions = []  # each: stock, entry_price, entry_day_idx, margin, peak
    realized_pnl = 0.0
    skipped_for_capital = 0
    taken = 0
    nav_series = []
    utilization_series = []

    for day_idx, day in enumerate(calendar):
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = total_capital - margin_used
        utilization_series.append(margin_used / total_capital * 100)

        # (1) 出場
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
                realized_pnl += p["margin"] / MARGIN_RATE * (net_ret / 100)  # 用名目本金(margin/rate)算P&L
                available_margin += p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open

        # (2) 進場
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

        # (3) mark-to-market NAV
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
        nav = total_capital + realized_pnl + unrealized
        nav_series.append(nav)

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
        "sharpe_annualized": sharpe_annualized, "final_nav": float(nav_arr[-1]) if len(nav_arr) else total_capital,
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
        entry = find_entry_price(con, sid, t01)
        if entry is None:
            continue
        entry_price, _ = entry
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        signals.append({
            "stock": sid, "trade_date": t01, "entry_frac": entry_price / day_close,
            "n_seats": int(t["n_seats"]),
        })

    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-07")
    print(f"=== 資金/保證金排程模擬（移動停利{TRAIL_PCT:.0f}%，最長{MAX_HOLD_DAYS}日）===")
    print(f"訊號數: {len(signals)} · 交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）\n")

    med_margin = np.median([
        estimate_margin_ntd(float((fut_cache.get(s['stock']) or {}).get(s['trade_date'], [0,0])[1]) * s['entry_frac'])
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
            f"{result['total_ret_pct']:>10.1f} {result['sharpe_annualized']:>10.3f} {result['max_drawdown_pct']:>10.1f} "
            f"{result['avg_capital_utilization_pct']:>10.1f}"
        )

    best_cap = CAPITAL_SCENARIOS_NTD[-1]
    best_result = run_simulation(signals, fut_cache, calendar, best_cap)

    print(
        "\n⚠️ 限制：\n"
        "  1) 保證金用13.5%概估（跟src/order/dayflip_short_signal.py同一套，非官方\n"
        "     逐檔試算表），實際保證金隨個股期貨規格不同會有差異。\n"
        "  2) 多訊號搶資金用n_seats(觸發分點數)排優先序，這個排序規則沒有另外驗證\n"
        "     是不是比FIFO或隨機更好。\n"
        "  3) 全部訊號共用同一個進出場規則(因果訊號+移動停利5%)，沒有針對不同\n"
        "     資金規模重新調整參數。\n"
        "  4) 沒算融資利息/機會成本；NAV是逐日mark-to-market但沒有真的模擬\n"
        "     保證金追繳(margin call)機制。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-capital-margin-simulation",
        ts="2026-08-09",
        params={
            "capital_scenarios_ntd": list(CAPITAL_SCENARIOS_NTD), "trail_pct": TRAIL_PCT,
            "margin_rate": MARGIN_RATE, "priority": "n_seats_desc",
        },
        n_observations=len(signals),
        metric_name=f"total_ret_pct_at_{best_cap}_ntd",
        metric_value=best_result["total_ret_pct"],
        status="kept" if best_result["total_ret_pct"] > 0 else "rejected",
        source=__file__,
        notes=(
            f"真的逐日資金/保證金排程模擬（非前幾輪的簡化equity curve）。掃過"
            f"{CAPITAL_SCENARIOS_NTD}NTD幾種資金規模，報成交數/因資金跳過數/總報酬/"
            "Sharpe/max drawdown，詳見腳本輸出表格。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "capital-simulation", "margin"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
