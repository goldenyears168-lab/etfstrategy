#!/usr/bin/env python3
"""嚴謹版硬止損模擬——上一支腳本(dayflip_post_dump_long_capital_sizing_
recommendation.py)的apply_hard_stop()是事後近似（只把最終結果比停損點差的
交易clamp到停損價），沒有處理「中途觸及停損、之後反彈回正」這種情況，導致
止損越緊、數字越無腦變好，是方法論瑕疵不是真發現。

這裡改成真正逐日重算：對每一筆訊號，用futures_daily_cache從entry_day開始
逐日往前走(最多MAX_HOLD_DAYS天)，每天同時檢查「進場價起算跌幅是否觸及硬
止損」跟「原本的移動停利(峰值回檔5%)」，兩者誰先觸發就用哪個當天的收盤價
出場——這樣硬止損如果中途觸發，之後的反彈就真的追不到了，不會像近似法
那樣偷偷保留最終的正報酬。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_post_dump_long_hard_stop_rigorous.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

TOTAL_CAPITAL_NTD = 300_000.0
MARGIN_RATE = 0.135
LOT_SHARES = 2000
FGAP_FLOOR = 4.0
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
ROUND_TRIP_COST_PCT = 0.05


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def resimulate_trade(fut_cache: dict, stock_id: str, entry_day: str, entry_px: float,
                      hard_stop_pct: float | None) -> dict | None:
    """逐日重算：每天同時檢查硬止損(entry_px起算)跟移動停利(峰值起算5%)，
    誰先觸發用誰。hard_stop_pct=None時完全比照原本simulate_trailing()邏輯，
    當作sanity check應該重現原始結果。"""
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if entry_day not in dates:
        return None
    i0 = dates.index(entry_day)
    if i0 + MAX_HOLD_DAYS >= len(dates):
        return None
    if entry_px <= 0:
        return None
    peak = entry_px
    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        loss_from_entry = (entry_px - px) / entry_px * 100
        pullback_from_peak = (peak - px) / peak * 100
        hit_hard_stop = hard_stop_pct is not None and loss_from_entry >= hard_stop_pct
        hit_trailing = pullback_from_peak >= TRAIL_PCT
        if hit_hard_stop or hit_trailing or h == MAX_HOLD_DAYS:
            net_ret = (px / entry_px - 1) * 100 - ROUND_TRIP_COST_PCT
            reason = "hard_stop" if hit_hard_stop else ("trailing" if hit_trailing else "max_hold")
            return {"ret": net_ret, "hold_days": h, "exit_day": d, "exit_reason": reason}
    return None


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
    orig_trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    orig_trades = [t for t in orig_trades if t["fgap"] >= FGAP_FLOOR]
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    print(f"fgap>=4%子集: n={len(orig_trades)}\n")

    print("=== Sanity check：hard_stop_pct=None應重現原始simulate_trailing()結果 ===")
    n_match, n_mismatch, mismatches = 0, 0, []
    for t in orig_trades:
        r = resimulate_trade(fut_cache, t["stock_id"], t["entry_day"], t["entry_px"], None)
        if r is None:
            continue
        if abs(r["ret"] - t["ret"]) < 0.01 and r["exit_day"] == t["exit_day"]:
            n_match += 1
        else:
            n_mismatch += 1
            mismatches.append((t["stock_id"], t["entry_day"], t["ret"], r["ret"]))
    print(f"match={n_match} mismatch={n_mismatch}")
    if mismatches:
        print("前5筆不符:", mismatches[:5])
    print()

    print(f"{'止損%':>8}{'最差交易%':>10}{'觸發止損筆數':>12}{'進場筆數':>8}{'總報酬%':>10}{'最大回撤%':>10}{'Sharpe':>9}")
    for stop_pct in (None, 8.0, 5.0, 4.0, 3.0, 2.0, 1.0):
        resim = []
        n_stopped = 0
        for t in orig_trades:
            r = resimulate_trade(fut_cache, t["stock_id"], t["entry_day"], t["entry_px"], stop_pct)
            if r is None:
                continue
            if r["exit_reason"] == "hard_stop":
                n_stopped += 1
            resim.append({**t, "ret": r["ret"], "exit_day": r["exit_day"]})
        worst = min(x["ret"] for x in resim)
        port = run_portfolio(resim, single_name_cap_pct=100.0)
        label = "無" if stop_pct is None else f"{stop_pct:.0f}"
        print(f"{label:>8}{worst:>10.1f}{n_stopped:>12}{port['n_entered']:>8}"
              f"{port['total_ret_pct']:>10.1f}{port['max_dd_pct']:>10.1f}{port['sharpe_like_daily']:>9.3f}")


if __name__ == "__main__":
    main()
