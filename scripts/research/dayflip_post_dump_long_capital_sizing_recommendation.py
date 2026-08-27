#!/usr/bin/env python3
"""使用者要求「去頭去尾」看資金規劃、並評估止損設計.

背景：100%單股上限的組合模擬顯示總報酬+790%但最大回撤-45.9%，明顯被少數
極端交易主導（fgap>=4%子集裡最好+83.3%、最差-13.8%，遠超過5%移動停利
理論上該有的範圍——因為出場檢查是「每天收盤查一次」，個股期貨若隔日跳空
下殺超過5%，移動停利來不及攔，是這套機制對跳空/停損的已知盲點）。

方法：
  1) 去頭尾：對fgap>=4%子集做winsorize（極端值縮尾，不是直接刪除，比較不
     會因為刪除樣本而扭曲天數/資金排程邏輯），在1%/2%/5%三種縮尾幅度下
     重跑資金池模擬，掃single_name_cap_pct(15/25/40/60/80/100%)找風險/
     報酬平衡點。
  2) 加一道「進場價起算的硬止損」(不是移動停利，是絕對虧損%達到就出場)，
     測 5/8/10/15% 幾種門檻，看能不能把最差交易的尾部風險收斂。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_post_dump_long_capital_sizing_recommendation.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"

TOTAL_CAPITAL_NTD = 300_000.0
MARGIN_RATE = 0.135
LOT_SHARES = 2000
FGAP_FLOOR = 4.0


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def winsorize(rets: list[float], pct: float) -> list[float]:
    if not rets or pct <= 0:
        return rets
    lo, hi = np.percentile(rets, [pct, 100 - pct])
    return [min(max(r, lo), hi) for r in rets]


def run_portfolio(trades: list[dict], *, single_name_cap_pct: float) -> dict:
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
    n_skipped_capital = 0

    for day in calendar:
        still_open = []
        for p in open_positions:
            if p["exit_day"] == day:
                pnl = p["margin"] * (p["ret"] / 100) / MARGIN_RATE
                realized_pnl += pnl
                margin_used -= p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open

        todays = sorted(by_entry_day.get(day, []), key=lambda t: -t["fgap"])
        for t in todays:
            margin = estimate_margin_ntd(t["entry_px"])
            if margin > cap_per_name or margin_used + margin > TOTAL_CAPITAL_NTD:
                n_skipped_capital += 1
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

    return {
        "n_entered": n_entered, "n_skipped_capital": n_skipped_capital,
        "total_ret_pct": total_ret_pct, "max_dd_pct": max_dd, "sharpe_like_daily": sharpe_like,
        "final_nav": float(nav_arr[-1]) if len(nav_arr) else TOTAL_CAPITAL_NTD,
    }


def apply_hard_stop(trades: list[dict], stop_pct: float) -> list[dict]:
    """近似處理：若這筆交易原始ret已經比-stop_pct還差，代表過程中一定觸及過
    -stop_pct（不是嚴謹的逐日重算，是用『最終比停損點還差就視為在停損點出場』
    的保守近似——會低估一些「觸底反彈回來變正的」情況，是刻意保守的估計）。"""
    out = []
    for t in trades:
        if t["ret"] < -stop_pct:
            out.append({**t, "ret": -stop_pct - 0.05})
        else:
            out.append(t)
    return out


def main() -> None:
    all_trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    trades = [t for t in all_trades if t["fgap"] >= FGAP_FLOOR]
    print(f"fgap>=4%子集: n={len(trades)}\n")

    print("=== 第一部分：去頭尾（winsorize）後，掃單股上限找平衡點 ===")
    for wpct in (0.0, 1.0, 2.0, 5.0):
        print(f"\n--- winsorize={wpct}% ---")
        rets_w = winsorize([t["ret"] for t in trades], wpct)
        trades_w = [{**t, "ret": r} for t, r in zip(trades, rets_w)]
        print(f"{'單股上限%':>8}{'進場筆數':>8}{'總報酬%':>10}{'最大回撤%':>10}{'Sharpe':>9}")
        for cap_pct in (15, 25, 40, 60, 80, 100):
            r = run_portfolio(trades_w, single_name_cap_pct=cap_pct)
            print(f"{cap_pct:>8}{r['n_entered']:>8}{r['total_ret_pct']:>10.1f}"
                  f"{r['max_dd_pct']:>10.1f}{r['sharpe_like_daily']:>9.3f}")

    print("\n\n=== 第二部分：加硬止損(進場價起算的絕對%)，在單股上限100%下測試 ===")
    print(f"{'止損%':>8}{'最差交易%':>10}{'進場筆數':>8}{'總報酬%':>10}{'最大回撤%':>10}{'Sharpe':>9}")
    for stop_pct in (None, 15.0, 10.0, 8.0, 5.0):
        if stop_pct is None:
            trades_s = trades
            label = "無"
        else:
            trades_s = apply_hard_stop(trades, stop_pct)
            label = f"{stop_pct:.0f}"
        worst = min(t["ret"] for t in trades_s)
        r = run_portfolio(trades_s, single_name_cap_pct=100)
        print(f"{label:>8}{worst:>10.1f}{r['n_entered']:>8}{r['total_ret_pct']:>10.1f}"
              f"{r['max_dd_pct']:>10.1f}{r['sharpe_like_daily']:>9.3f}")


if __name__ == "__main__":
    main()
