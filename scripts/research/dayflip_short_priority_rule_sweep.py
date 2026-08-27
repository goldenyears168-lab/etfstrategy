#!/usr/bin/env python3
"""dayflip-short post-dump 做多——同日搶資金優先序規則有沒有差.

前一輪資金/保證金排程模擬(dayflip_short_post_dump_long_capital_simulation.py)在
「同一天多筆訊號搶同一筆資金」時，用 n_seats(觸發分點數，訊號強度代理)降冪排序，
但這個排序規則本身從未跟其他排法比較過——只是「感覺合理」就採用了。這裡補測：

  (a) n_seats_desc  — 現行基準規則
  (b) fifo          — 不重排，維持 all_trades.csv 原始（訊號日→交易日）自然順序
  (c) random         — 每天內部訊號隨機打亂，seed=0..9 各跑一次，報 10 次的分佈
                        （不是只看一次抽樣結果）

其餘模擬邏輯（保證金 13.5%、移動停利 5%、最長 10 交易日、成本 5bps）與參考腳本
逐日資金排程完全一致，只抽換「同日訊號排序」這一段，其他都是同一套函式。只在
2,000,000 NTD 這個先前選定的資金甜蜜點跑，比較四個規則（含 10 次 random 的分佈）
的總報酬、Sharpe、max drawdown、因資金不足被跳過的訊號數。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_priority_rule_sweep.py
"""

from __future__ import annotations

import csv
import json
import random
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
TOTAL_CAPITAL_NTD = 2_000_000  # 前一輪找到的甜蜜點
RANDOM_SEEDS = tuple(range(10))  # 0..9，明確整數 seed（不用 argless random）


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


def _order_same_day_signals(day_signals: list[dict], rule: str, rng: random.Random | None) -> list[dict]:
    """依 rule 排序同一天內搶資金的訊號。fifo 保持原始輸入順序（不重排）。"""
    if rule == "n_seats_desc":
        return sorted(day_signals, key=lambda s: -s["n_seats"])
    if rule == "fifo":
        return list(day_signals)
    if rule == "random":
        assert rng is not None
        shuffled = list(day_signals)
        rng.shuffle(shuffled)
        return shuffled
    raise ValueError(f"unknown rule: {rule}")


def run_simulation(
    signals: list[dict], fut_cache: dict, calendar: list[str], total_capital: float,
    rule: str, seed: int | None = None,
) -> dict:
    cal_idx = {d: i for i, d in enumerate(calendar)}
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)

    rng = random.Random(seed) if rule == "random" else None
    for d in signals_by_date:
        signals_by_date[d] = _order_same_day_signals(signals_by_date[d], rule, rng)

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

    # 保留 all_trades.csv 原始列順序作為 fifo 的基準（load_trades() 已是檔案順序，
    # 且下面依 trade_date 分組後同一天內仍保留這個相對順序）
    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-07")

    print(f"=== 同日搶資金優先序規則比較（資金{TOTAL_CAPITAL_NTD:,}NTD，移動停利{TRAIL_PCT:.0f}%，最長{MAX_HOLD_DAYS}日）===")
    print(f"訊號數: {len(signals)} · 交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）\n")

    n_seats_result = run_simulation(signals, fut_cache, calendar, TOTAL_CAPITAL_NTD, "n_seats_desc")
    fifo_result = run_simulation(signals, fut_cache, calendar, TOTAL_CAPITAL_NTD, "fifo")
    random_results = [
        run_simulation(signals, fut_cache, calendar, TOTAL_CAPITAL_NTD, "random", seed=seed)
        for seed in RANDOM_SEEDS
    ]

    def fmt_row(label: str, r: dict) -> str:
        return (
            f"{label:>16} {r['taken']:>6} {r['skipped_for_capital']:>10} "
            f"{r['total_ret_pct']:>10.1f} {r['sharpe_annualized']:>10.3f} {r['max_drawdown_pct']:>10.1f} "
            f"{r['avg_capital_utilization_pct']:>10.1f}"
        )

    header = (
        f"{'規則':>16} {'成交':>6} {'因資金跳過':>10} "
        f"{'總報酬%':>10} {'年化Sharpe':>10} {'最大回檔%':>10} {'平均使用率%':>10}"
    )
    print(header)
    print(fmt_row("n_seats_desc", n_seats_result))
    print(fmt_row("fifo", fifo_result))
    for seed, r in zip(RANDOM_SEEDS, random_results):
        print(fmt_row(f"random(seed={seed})", r))

    random_total_ret = np.array([r["total_ret_pct"] for r in random_results])
    random_sharpe = np.array([r["sharpe_annualized"] for r in random_results])
    random_dd = np.array([r["max_drawdown_pct"] for r in random_results])
    random_skipped = np.array([r["skipped_for_capital"] for r in random_results])

    print(f"\n{'random 分佈 (n=10 seeds)':>16}")
    print(
        f"  total_ret_pct: mean={random_total_ret.mean():.1f} std={random_total_ret.std():.1f} "
        f"min={random_total_ret.min():.1f} max={random_total_ret.max():.1f}"
    )
    print(
        f"  sharpe:        mean={random_sharpe.mean():.3f} std={random_sharpe.std():.3f} "
        f"min={random_sharpe.min():.3f} max={random_sharpe.max():.3f}"
    )
    print(
        f"  max_dd_pct:    mean={random_dd.mean():.1f} std={random_dd.std():.1f} "
        f"min={random_dd.min():.1f} max={random_dd.max():.1f}"
    )
    print(
        f"  skipped:       mean={random_skipped.mean():.1f} std={random_skipped.std():.1f} "
        f"min={random_skipped.min()} max={random_skipped.max()}"
    )

    tied_with_fifo = abs(n_seats_result["total_ret_pct"] - fifo_result["total_ret_pct"]) < 1e-6
    n_seats_within_random_range = (
        random_total_ret.min() <= n_seats_result["total_ret_pct"] <= random_total_ret.max()
    )
    n_seats_beats_random_mean = n_seats_result["total_ret_pct"] > random_total_ret.mean()
    n_seats_beats_random_max = n_seats_result["total_ret_pct"] > random_total_ret.max()
    n_seats_beats_fifo = n_seats_result["total_ret_pct"] > fifo_result["total_ret_pct"]

    if tied_with_fifo:
        # 同一組訊號被 take/skip（只是進場順序不同，不影響誰被納入），故兩個
        # 固定規則的結果在這份資料上完全相同。真正的比較是「固定排序 vs 隨機排序」。
        if n_seats_beats_random_max:
            verdict = (
                "n_seats_desc 與 fifo 結果完全打平（同一組訊號被納入/跳過，只是進場順序不同，"
                "不影響 NAV），故 n_seats 這個排序準則本身沒有比「不排序」更好；但兩個固定規則都"
                "優於全部 10 次 random 抽樣，顯示『用固定、可重現的規則排序』本身有價值——"
                "只是價值不是來自 n_seats 這個特定準則"
            )
        elif n_seats_beats_random_mean:
            verdict = (
                "n_seats_desc 與 fifo 結果完全打平，且落在 random 抽樣分佈的上緣附近但未明顯超出，"
                "n_seats 這個排序準則相對 fifo 沒有增量價值，相對 random 的優勢也弱，證據不足"
            )
        else:
            verdict = "n_seats_desc 與 fifo 打平，且未優於 random 分佈，優先序規則整體看不出價值"
    elif n_seats_beats_fifo and n_seats_beats_random_max:
        verdict = "n_seats_desc 優於 fifo 也優於全部 random 抽樣，明確較佳"
    elif n_seats_within_random_range:
        verdict = "n_seats_desc 落在 random 10 次抽樣的分佈範圍內，與隨機排序無法區分"
    else:
        verdict = "n_seats_desc 劣於 fifo 或劣於 random 分佈（優先序規則本身可能沒有正向貢獻）"

    print(f"\n=== 結論 ===\n{verdict}")
    print(
        f"n_seats_desc 總報酬 {n_seats_result['total_ret_pct']:.1f}% vs fifo {fifo_result['total_ret_pct']:.1f}% "
        f"vs random mean {random_total_ret.mean():.1f}% (std {random_total_ret.std():.1f}, "
        f"range [{random_total_ret.min():.1f}, {random_total_ret.max():.1f}])"
    )

    status = "kept" if (n_seats_beats_fifo and n_seats_beats_random_max) else "rejected"

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="capital-priority-rule-sweep",
        ts="2026-08-09",
        params={
            "total_capital_ntd": TOTAL_CAPITAL_NTD,
            "trail_pct": TRAIL_PCT,
            "margin_rate": MARGIN_RATE,
            "rules_compared": ["n_seats_desc", "fifo", "random"],
            "random_seeds": list(RANDOM_SEEDS),
        },
        n_observations=len(signals),
        metric_name="total_ret_pct_n_seats_desc_at_2000000_ntd",
        metric_value=n_seats_result["total_ret_pct"],
        status=status,
        source=__file__,
        notes=(
            f"測同日搶資金優先序規則(n_seats_desc現行基準 vs fifo vs random×10 seeds)在"
            f"{TOTAL_CAPITAL_NTD:,}NTD資金規模下的差異。n_seats_desc總報酬"
            f"{n_seats_result['total_ret_pct']:.1f}% Sharpe{n_seats_result['sharpe_annualized']:.3f} "
            f"maxDD{n_seats_result['max_drawdown_pct']:.1f}%；fifo總報酬{fifo_result['total_ret_pct']:.1f}% "
            f"Sharpe{fifo_result['sharpe_annualized']:.3f}；random(n=10 seeds)總報酬"
            f"mean={random_total_ret.mean():.1f}% std={random_total_ret.std():.1f} "
            f"range=[{random_total_ret.min():.1f},{random_total_ret.max():.1f}]。結論：{verdict}"
        ),
        extra_metrics={
            "n_seats_desc_total_ret_pct": n_seats_result["total_ret_pct"],
            "n_seats_desc_sharpe": n_seats_result["sharpe_annualized"],
            "n_seats_desc_max_dd_pct": n_seats_result["max_drawdown_pct"],
            "n_seats_desc_skipped_for_capital": n_seats_result["skipped_for_capital"],
            "fifo_total_ret_pct": fifo_result["total_ret_pct"],
            "fifo_sharpe": fifo_result["sharpe_annualized"],
            "fifo_max_dd_pct": fifo_result["max_drawdown_pct"],
            "fifo_skipped_for_capital": fifo_result["skipped_for_capital"],
            "random_total_ret_pct_mean": float(random_total_ret.mean()),
            "random_total_ret_pct_std": float(random_total_ret.std()),
            "random_total_ret_pct_min": float(random_total_ret.min()),
            "random_total_ret_pct_max": float(random_total_ret.max()),
            "random_sharpe_mean": float(random_sharpe.mean()),
            "random_sharpe_std": float(random_sharpe.std()),
            "random_max_dd_pct_mean": float(random_dd.mean()),
            "random_skipped_for_capital_mean": float(random_skipped.mean()),
        },
        tags=["dayflip-short", "post-dump", "long-side", "capital-priority-rule", "robustness-check"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
