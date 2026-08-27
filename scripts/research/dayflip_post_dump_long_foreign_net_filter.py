#!/usr/bin/env python3
"""外資淨買超當額外品質過濾器——先前permutation test已驗證進場日外資淨買賣超
跟後續報酬正相關(IC=0.217, p=0.001)。這裡做兩件事：
  1) walk-forward(train前70%/test後30%)驗證這個IC不是全樣本巧合
  2) 實際套用「外資淨買超才進場」過濾器，跑資金池模擬看整體組合表現變化

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_post_dump_long_foreign_net_filter.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

import stock_db

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
TOTAL_CAPITAL_NTD = 300_000.0
MARGIN_RATE = 0.135
LOT_SHARES = 2000


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def run_portfolio(trades: list[dict], *, single_name_cap_pct: float = 100.0) -> dict:
    calendar = sorted({t["entry_day"] for t in trades} | {t["exit_day"] for t in trades})
    by_entry_day: dict[str, list[dict]] = {}
    for t in trades:
        by_entry_day.setdefault(t["entry_day"], []).append(t)
    open_positions: list[dict] = []
    margin_used = 0.0
    cap_per_name = TOTAL_CAPITAL_NTD * single_name_cap_pct / 100
    daily_nav: list[float] = []
    realized_pnl = 0.0
    n_entered = 0
    for day in calendar:
        still_open = []
        for p in open_positions:
            if p["exit_day"] == day:
                realized_pnl += p["margin"] * (p["ret"] / 100) / MARGIN_RATE
                margin_used -= p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open
        todays = sorted(by_entry_day.get(day, []), key=lambda t: -t["fgap"])
        for t in todays:
            margin = estimate_margin_ntd(t["entry_px"])
            if margin > cap_per_name or margin_used + margin > TOTAL_CAPITAL_NTD:
                continue
            margin_used += margin
            open_positions.append({"exit_day": t["exit_day"], "margin": margin, "ret": t["ret"]})
            n_entered += 1
        daily_nav.append(TOTAL_CAPITAL_NTD + realized_pnl)
    nav_arr = np.array(daily_nav)
    total_ret_pct = (nav_arr[-1] / TOTAL_CAPITAL_NTD - 1) * 100 if len(nav_arr) else 0.0
    running_max = np.maximum.accumulate(nav_arr) if len(nav_arr) else np.array([TOTAL_CAPITAL_NTD])
    drawdown = (nav_arr - running_max) / running_max * 100 if len(nav_arr) else np.array([0.0])
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0
    daily_rets = np.diff(nav_arr) / nav_arr[:-1] * 100 if len(nav_arr) > 1 else np.array([0.0])
    sharpe_like = float(daily_rets.mean() / daily_rets.std()) if daily_rets.std() > 0 else float("nan")
    return {"n_entered": n_entered, "total_ret_pct": total_ret_pct, "max_dd_pct": max_dd,
            "sharpe_like_daily": sharpe_like, "final_nav": float(nav_arr[-1]) if len(nav_arr) else TOTAL_CAPITAL_NTD}


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    sub = [t for t in trades if t["fgap"] >= 4.0]

    enriched = []
    for t in sub:
        row = con.execute(
            "SELECT foreign_net FROM stock_institutional_daily WHERE stock_id=? AND trade_date=?",
            (t["stock_id"], t["entry_day"]),
        ).fetchone()
        if row is None or row[0] is None:
            continue
        enriched.append({**t, "foreign_net": row[0]})
    con.close()
    print(f"可比對: {len(enriched)}/{len(sub)}筆\n")

    print("=== Walk-forward驗證：IC不是全樣本巧合 ===")
    enriched_sorted = sorted(enriched, key=lambda t: (t["entry_day"], t["entry_minute"]))
    n_train = int(len(enriched_sorted) * 0.7)
    train, test = enriched_sorted[:n_train], enriched_sorted[n_train:]
    for label, group in [("全樣本", enriched_sorted), ("train(前70%)", train), ("test(後30%)", test)]:
        xs = np.array([t["foreign_net"] for t in group])
        ys = np.array([t["ret"] for t in group])
        ic, pval = spearmanr(xs, ys)
        print(f"{label}: n={len(group)} IC={ic:.3f} p={pval:.3f}")

    print("\n=== 套用過濾器：外資淨買超(>=0)才進場，資金池模擬比較 ===")
    with_filter = [t for t in enriched if t["foreign_net"] >= 0]
    without_filter = enriched
    for label, group in [("無過濾(現行)", without_filter), ("外資淨買超>=0過濾", with_filter)]:
        rets = [t["ret"] for t in group]
        win = np.mean([1 if r > 0 else 0 for r in rets]) * 100
        port = run_portfolio(group, single_name_cap_pct=15.0)
        print(f"\n{label}: n={len(group)} 均報酬={np.mean(rets):+.2f}% 勝率={win:.1f}%")
        print(f"  資金池模擬(15%單股上限): 進場{port['n_entered']}筆 總報酬{port['total_ret_pct']:+.1f}% "
              f"最大回撤{port['max_dd_pct']:.1f}% Sharpe{port['sharpe_like_daily']:.3f}")

    print("\n=== Block bootstrap：過濾後 vs 無過濾，重抽3000次比較sharpe ===")
    rng = np.random.default_rng(20260811)
    wf_rets = np.array([t["ret"] for t in with_filter])
    nf_rets = np.array([t["ret"] for t in without_filter])

    def sharpe_like(arr):
        return arr.mean() / arr.std() if arr.std() > 0 else float("nan")

    diffs, wins = [], 0
    for _ in range(3000):
        s1 = rng.choice(nf_rets, size=len(nf_rets), replace=True)
        s2 = rng.choice(wf_rets, size=len(wf_rets), replace=True)
        sh1, sh2 = sharpe_like(s1), sharpe_like(s2)
        if np.isnan(sh1) or np.isnan(sh2):
            continue
        diffs.append(sh2 - sh1)
        if sh2 > sh1:
            wins += 1
    diffs = np.array(diffs)
    print(f"過濾後贏無過濾比例: {wins/len(diffs)*100:.1f}% diff mean={diffs.mean():+.3f} "
          f"5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
