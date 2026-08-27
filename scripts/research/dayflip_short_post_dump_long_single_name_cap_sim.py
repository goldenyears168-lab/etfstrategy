#!/usr/bin/env python3
"""dayflip-short post-dump 做多——單一標的名目曝險上限測試.

2026-08-09 資金排程模擬追查「日報酬std/Sharpe偏高」的原因時發現：
run_simulation()（dayflip_short_post_dump_long_capital_simulation.py）完全沒有
單一標的集中度限制，同一檔股票如果連續幾個訊號日都觸發，會無上限疊倉。2026-02-02
那天NAV單日-27.7%，追查後發現主因是8299同時疊了兩筆部位（保證金合計佔總資金58.5%，
名目曝險約7.4倍保證金），週末一過股價從高點回落，未實現獲利蒸發造成的，不是訊號
本身在分散組合下的正常表現。

這裡加一個「單一標的名目曝險上限」（占總資金的比例），掃過幾個上限水準，跟
無上限（cap=1.0，即現行yaml的risk_controls: none基準）比較，看能不能在不大幅
犧牲報酬/Sharpe的前提下，把日報酬std壓低、把最差單日的量級縮小。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_post_dump_long_single_name_cap_sim.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from trial_registry import append_trial

from dayflip_short_post_dump_long_capital_simulation import (
    FUT_CACHE_PATH,
    MARGIN_RATE,
    MAX_HOLD_DAYS,
    ROUND_TRIP_COST_PCT,
    TRAIL_PCT,
    _t01_stock_close,
    build_calendar,
    estimate_margin_ntd,
    find_entry_price,
    load_trades,
)

ROOT = Path(__file__).resolve().parents[2]
TOTAL_CAPITAL_NTD = 2_000_000
PER_STOCK_CAP_FRACTIONS = (0.10, 0.15, 0.20, 0.30, 0.50, 1.0)  # 1.0 = 無上限（現行基準）


def run_simulation_with_cap(
    signals: list[dict], fut_cache: dict, calendar: list[str], total_capital: float, per_stock_cap_frac: float
) -> dict:
    per_stock_cap = total_capital * per_stock_cap_frac
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda s: -s["n_seats"])

    open_positions = []
    realized_pnl = 0.0
    skipped_for_capital = 0
    skipped_for_concentration = 0
    taken = 0
    nav_series = []

    for day_idx, day in enumerate(calendar):
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
            else:
                still_open.append(p)
        open_positions = still_open

        margin_used_by_stock: dict[str, float] = {}
        for p in open_positions:
            margin_used_by_stock[p["stock"]] = margin_used_by_stock.get(p["stock"], 0.0) + p["margin"]
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = total_capital - margin_used

        for s in signals_by_date.get(day, []):
            m = fut_cache.get(s["stock"]) or {}
            if day not in m:
                continue
            fut_close = float(m[day][1])
            if fut_close <= 0:
                continue
            entry_price = fut_close * s["entry_frac"]
            margin = estimate_margin_ntd(entry_price)
            stock_margin_after = margin_used_by_stock.get(s["stock"], 0.0) + margin
            if margin > available_margin:
                skipped_for_capital += 1
                continue
            if stock_margin_after > per_stock_cap:
                skipped_for_concentration += 1
                continue
            open_positions.append({
                "stock": s["stock"], "entry_price": entry_price, "entry_day_idx": day_idx,
                "margin": margin, "peak": entry_price,
            })
            available_margin -= margin
            margin_used_by_stock[s["stock"]] = stock_margin_after
            taken += 1

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
        "taken": taken,
        "skipped_for_capital": skipped_for_capital,
        "skipped_for_concentration": skipped_for_concentration,
        "total_ret_pct": total_ret_pct,
        "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe_annualized,
        "daily_ret_mean_pct": float(daily_ret.mean() * 100) if len(daily_ret) else 0.0,
        "daily_ret_std_pct": float(daily_ret.std() * 100) if len(daily_ret) else 0.0,
        "worst_day_pct": float(daily_ret.min() * 100) if len(daily_ret) else 0.0,
        "best_day_pct": float(daily_ret.max() * 100) if len(daily_ret) else 0.0,
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
    print(f"=== 單一標的名目曝險上限測試（總資金 {TOTAL_CAPITAL_NTD:,} NTD）===")
    print(f"訊號數: {len(signals)} · 交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）\n")

    print(
        f"{'單股上限%':>10} {'成交':>6} {'因資金跳過':>10} {'因集中度跳過':>12} "
        f"{'總報酬%':>10} {'年化Sharpe':>10} {'最大回檔%':>10} "
        f"{'日均報酬%':>10} {'日std%':>8} {'最差日%':>10} {'最好日%':>10}"
    )
    results = {}
    for frac in PER_STOCK_CAP_FRACTIONS:
        r = run_simulation_with_cap(signals, fut_cache, calendar, TOTAL_CAPITAL_NTD, frac)
        results[frac] = r
        label = "無上限" if frac >= 1.0 else f"{frac*100:.0f}%"
        print(
            f"{label:>10} {r['taken']:>6} {r['skipped_for_capital']:>10} {r['skipped_for_concentration']:>12} "
            f"{r['total_ret_pct']:>10.1f} {r['sharpe_annualized']:>10.3f} {r['max_drawdown_pct']:>10.1f} "
            f"{r['daily_ret_mean_pct']:>10.2f} {r['daily_ret_std_pct']:>8.2f} "
            f"{r['worst_day_pct']:>10.1f} {r['best_day_pct']:>10.1f}"
        )

    baseline = results[1.0]
    best_frac = min(
        (f for f in PER_STOCK_CAP_FRACTIONS if f < 1.0),
        key=lambda f: results[f]["max_drawdown_pct"],
    )
    best = results[best_frac]

    print(
        "\n⚠️ 限制：\n"
        "  1) 全樣本一次性掃描（非train/test walk-forward），因為這是機制性風控參數\n"
        "     不是要挑選『最優』數值去配適歷史資料，比照先前四輪風控測試的作法。\n"
        "  2) 集中度上限用『同一標的累計保證金 / 總資金』計算，沒有考慮同產業/\n"
        "     相關標的的跨股集中度。\n"
        "  3) 8299那筆極端案例是否代表性、還是單一離群事件，樣本內只出現這一次，\n"
        "     無法排除其他標的未來出現類似疊倉情境的機率。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-single-name-concentration-cap",
        ts="2026-08-09",
        params={
            "total_capital_ntd": TOTAL_CAPITAL_NTD,
            "per_stock_cap_fractions": list(PER_STOCK_CAP_FRACTIONS),
        },
        n_observations=len(signals),
        metric_name=f"max_drawdown_pct_at_{best_frac}",
        metric_value=best["max_drawdown_pct"],
        status="kept" if best["max_drawdown_pct"] > baseline["max_drawdown_pct"] else "rejected",
        source=__file__,
        notes=(
            f"追查2026-02-02單日-27.7%NAV異常（8299疊倉兩筆部位，保證金合計佔總資金"
            f"58.5%）後補的風控測試。無上限基準：total_ret={baseline['total_ret_pct']:.1f}%、"
            f"max_dd={baseline['max_drawdown_pct']:.1f}%、日std={baseline['daily_ret_std_pct']:.2f}%、"
            f"最差日={baseline['worst_day_pct']:.1f}%。最佳上限({best_frac*100:.0f}%)："
            f"total_ret={best['total_ret_pct']:.1f}%、max_dd={best['max_drawdown_pct']:.1f}%、"
            f"日std={best['daily_ret_std_pct']:.2f}%、最差日={best['worst_day_pct']:.1f}%。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "concentration-cap", "risk-control"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
