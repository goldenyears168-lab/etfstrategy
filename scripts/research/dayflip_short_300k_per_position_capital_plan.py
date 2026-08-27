#!/usr/bin/env python3
"""dayflip-short post-dump 做多——30萬當「單一部位保證金上限」，搭配多少總資金？

使用者釐清：30萬不是總資金，是比照做空側 margin_cap_twd:300000 的精神，當「每一個
部位」的保證金上限（不是固定1口，是用到300k保證金為止，同一訊號可以買多口）。這裡
規劃：每個部位固定用滿(但不超過)30萬保證金（median margin/口~56,700，換算約5-6口/
部位），然後掃總資金規模，找出能撐幾個部位同時在場、且回檔可接受的搭配。

沿用 dayflip_short_proactive_capital_sizing.py 的逐日資金排程模擬骨架（TX-real訊號、
移動停利5%/最長10日、13.5%保證金率），只改一個地方：進場時的保證金從
「固定1口」改成「用滿300k保證金上限、最少1口」。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_300k_per_position_capital_plan.py
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TX_BARS_DB = DATA_DIR / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
MARGIN_RATE = 0.135
LOT_SHARES = 2000
ROLLING_WINDOW_MIN = 15
CONFIRM_MINUTES = 10
LAG_THRESHOLD_PCT = 0.3
PER_POSITION_MARGIN_CAP_NTD = 300_000  # 比照 dayflip-futures-short 的 margin_cap_twd
MARGIN_CALL_DANGER_ZONE_PCT = 15.0

# 用「總資金 / 30萬」約等於同時可撐幾個滿倉部位來選規模：3/5/8/10/15/20 個部位
CAPITAL_SCENARIOS_NTD = tuple(PER_POSITION_MARGIN_CAP_NTD * n for n in (3, 5, 8, 10, 15, 20))


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def load_tx_minute_closes(tx_con: sqlite3.Connection, t01: str) -> dict[str, float]:
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND day=? AND sess='day'", (TX_SOURCE, t01)
    ).fetchall()
    return {str(t)[:5]: float(c) for t, c in rows if c and float(c) > 0}


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

    open_positions: list[dict] = []
    realized_pnl = 0.0
    skipped_for_capital = 0
    taken = 0
    nav_series = []
    utilization_series = []
    max_concurrent_seen = 0
    lots_per_position = []

    for day_idx, day in enumerate(calendar):
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = total_capital - margin_used
        utilization_series.append(margin_used / total_capital * 100 if total_capital > 0 else 0.0)

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
            margin_per_lot = estimate_margin_ntd(entry_price)
            if margin_per_lot <= 0:
                continue
            # 每個部位用滿(不超過)30萬保證金上限，最少1口
            n_lots = max(1, int(PER_POSITION_MARGIN_CAP_NTD // margin_per_lot))
            margin = margin_per_lot * n_lots
            if margin <= available_margin:
                open_positions.append({
                    "stock": s["stock"], "entry_price": entry_price, "entry_day_idx": day_idx,
                    "margin": margin, "peak": entry_price, "n_lots": n_lots,
                })
                available_margin -= margin
                taken += 1
                lots_per_position.append(n_lots)
                max_concurrent_seen = max(max_concurrent_seen, len(open_positions))
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
        "taken": taken,
        "skipped_for_capital": skipped_for_capital,
        "total_ret_pct": total_ret_pct,
        "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe_annualized,
        "avg_capital_utilization_pct": float(np.mean(utilization_series)) if utilization_series else 0.0,
        "max_concurrent_seen": max_concurrent_seen,
        "avg_lots_per_position": float(np.mean(lots_per_position)) if lots_per_position else 0.0,
        "near_margin_call_zone": max_dd <= -MARGIN_CALL_DANGER_ZONE_PCT,
    }


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    signals = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_stock_minute_closes(con, sid, t01)
        tx_closes = load_tx_minute_closes(tx_con, t01)
        if len(tx_closes) < 50 or len(stock_closes) < 50:
            continue
        sig = find_rolling_dip_signal(stock_closes, tx_closes)
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

    print("=== 每部位保證金上限30萬（比照做空側margin_cap_twd）→ 搭配多少總資金 ===")
    print(f"訊號: {len(signals)}/{len(trades)} 筆有效\n")
    if not signals:
        raise SystemExit("no signals produced")

    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-09")
    print(f"交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）\n")

    header = (
        f"{'總資金(約幾個部位)':>22} {'成交':>6} {'因資金跳過':>8} "
        f"{'平均口數/部位':>10} {'最大同時在場':>8} {'總報酬%':>10} {'年化Sharpe':>10} "
        f"{'最大回檔%':>10} {'逼近追繳區':>8}"
    )
    print(header)
    results = {}
    for cap in CAPITAL_SCENARIOS_NTD:
        n_positions = round(cap / PER_POSITION_MARGIN_CAP_NTD)
        r = run_simulation(signals, fut_cache, calendar, cap)
        results[cap] = r
        print(
            f"{cap:>14,}({n_positions:>3}部位) {r['taken']:>6} {r['skipped_for_capital']:>8} "
            f"{r['avg_lots_per_position']:>10.1f} {r['max_concurrent_seen']:>8} "
            f"{r['total_ret_pct']:>10.1f} {r['sharpe_annualized']:>10.3f} {r['max_drawdown_pct']:>10.1f} "
            f"{('是' if r['near_margin_call_zone'] else '否'):>8}"
        )

    print(
        "\n⚠️ 限制：TX-real訊號(15分/0.3%/10分，未在此重掃)、移動停利5%/最長10日、"
        "13.5%保證金概估、5bps成本未經滑價實測、優先序用n_seats。逐日mark-to-market"
        "算的max drawdown，不是簡化cumsum。"
    )

    best_cap = min(
        (c for c in CAPITAL_SCENARIOS_NTD if not results[c]["near_margin_call_zone"]),
        key=lambda c: c,
        default=None,
    )
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="300k-per-position-margin-cap-capital-plan",
        ts="2026-08-09",
        params={
            "per_position_margin_cap_ntd": PER_POSITION_MARGIN_CAP_NTD,
            "capital_scenarios_ntd": list(CAPITAL_SCENARIOS_NTD),
        },
        n_observations=len(signals),
        metric_name="min_capital_avoiding_margin_call_zone_ntd",
        metric_value=float(best_cap) if best_cap else float("nan"),
        status="kept" if best_cap else "rejected",
        source=__file__,
        notes=(
            "使用者釐清：30萬是單一部位保證金上限（比照做空側margin_cap_twd），不是"
            "總資金——每個部位用滿(不超過)30萬保證金、最少1口，掃3/5/8/10/15/20個"
            "部位對應的總資金規模。" + (
                f"最小能避開15%回檔警戒區的總資金約{best_cap:,.0f}NTD。"
                if best_cap else "掃過的規模全部都逼近或跌破15%回檔警戒區，未找到安全組合。"
            )
        ),
        tags=["dayflip-short", "post-dump", "long-side", "capital-sizing", "300k-per-position"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
