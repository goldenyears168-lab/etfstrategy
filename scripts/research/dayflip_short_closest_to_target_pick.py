#!/usr/bin/env python3
"""挑「離7%目標最近」而非「7%以上最小」——使用者問：候選池若有6.9%(現行floor
以下、完全被排除)，該不該考慮它而不是被迫選8%這種floor以上但離目標更遠的.

方法：對每天的完整候選池（含fgap<7%的，day_pool已經有），測四種picker：
  A) 現行：fgap>=7% 篩選後挑最小的（等於「7%以上離7%最近」）
  B) 無floor：不篩選，直接挑|fgap-7%|最小的（可能選到遠低於7%的候選）
  C) 軟floor=5%：fgap>=5%才候選，挑|fgap-7%|最小的
  D) 軟floor=6%：fgap>=6%才候選，挑|fgap-7%|最小的

train(70%)/test(30%) walk-forward + block bootstrap，比照今天稍早的方法論。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_closest_to_target_pick.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
DAY_POOL_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
TARGET = 7.0
N_BOOTSTRAP = 3000


def net_ret_for_entry(r: dict, entry_px: float) -> float:
    target = entry_px * (1 - COVER_TARGET_PCT)
    exit_px = target if r["low_px"] <= target else r["close_px"]
    return (entry_px - exit_px) / entry_px * 100 - ROUND_TRIP_COST_PCT


def sharpe_like(rets: list[float]) -> float:
    arr = np.array(rets)
    if len(arr) < 2 or arr.std() == 0:
        return float("nan")
    return float(arr.mean() / arr.std())


PICKERS = {
    "A現行(floor7%,挑最小)": lambda pool: min(
        [r for r in pool if r["fgap"] >= 7.0], key=lambda r: r["fgap"], default=None),
    "B無floor(挑離7%最近)": lambda pool: min(
        pool, key=lambda r: abs(r["fgap"] - TARGET), default=None),
    "C軟floor5%(挑離7%最近)": lambda pool: min(
        [r for r in pool if r["fgap"] >= 5.0], key=lambda r: abs(r["fgap"] - TARGET), default=None),
    "D軟floor6%(挑離7%最近)": lambda pool: min(
        [r for r in pool if r["fgap"] >= 6.0], key=lambda r: abs(r["fgap"] - TARGET), default=None),
}


def day_ret(day_pool: dict, t0: str, picker_name: str) -> float | None:
    pool = day_pool.get(t0, [])
    if not pool:
        return None
    best = PICKERS[picker_name](pool)
    if best is None:
        return None
    return net_ret_for_entry(best, best["open_px"])


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    day_pool = json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))

    n_train = int(len(signal_dates) * 0.7)
    train, test = signal_dates[:n_train], signal_dates[n_train:]

    print("=== 全樣本/train/test 比較 ===")
    print(f"{'picker':<26}{'全樣本n':>8}{'全樣本sharpe':>13}{'全樣本mean%':>12}"
          f"{'train sharpe':>13}{'test sharpe':>12}{'test mean%':>11}")
    for name in PICKERS:
        r_all = [x for t0 in signal_dates if (x := day_ret(day_pool, t0, name)) is not None]
        r_train = [x for t0 in train if (x := day_ret(day_pool, t0, name)) is not None]
        r_test = [x for t0 in test if (x := day_ret(day_pool, t0, name)) is not None]
        print(f"{name:<26}{len(r_all):>8}{sharpe_like(r_all):>13.3f}{np.mean(r_all):>12.3f}"
              f"{sharpe_like(r_train):>13.3f}{sharpe_like(r_test):>12.3f}{np.mean(r_test):>11.3f}")

    # 有多少天，closest-to-target實際選到floor以下的候選（B/C/D跟A picks不同的天）
    diff_days = {name: 0 for name in PICKERS if name != "A現行(floor7%,挑最小)"}
    below_floor_days = {name: 0 for name in PICKERS if name != "A現行(floor7%,挑最小)"}
    for t0 in signal_dates:
        pool = day_pool.get(t0, [])
        if not pool:
            continue
        a_pick = PICKERS["A現行(floor7%,挑最小)"](pool)
        for name in diff_days:
            b_pick = PICKERS[name](pool)
            if b_pick is None:
                continue
            a_sid = a_pick["stock_id"] if a_pick else None
            if b_pick["stock_id"] != a_sid:
                diff_days[name] += 1
            if b_pick["fgap"] < 7.0:
                below_floor_days[name] += 1
    print(f"\n跟現行選到不同候選的天數: {diff_days}")
    print(f"實際選到floor(7%)以下候選的天數: {below_floor_days}")

    print(f"\n=== Block bootstrap（每個vs現行A，重抽{N_BOOTSTRAP}次） ===")
    rng = np.random.default_rng(20260810)
    for name in PICKERS:
        if name == "A現行(floor7%,挑最小)":
            continue
        diffs, wins = [], 0
        for _ in range(N_BOOTSTRAP):
            sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
            ra = [x for t0 in sample if (x := day_ret(day_pool, t0, "A現行(floor7%,挑最小)")) is not None]
            rb = [x for t0 in sample if (x := day_ret(day_pool, t0, name)) is not None]
            if len(ra) < 5 or len(rb) < 5:
                continue
            sa, sb = sharpe_like(ra), sharpe_like(rb)
            if np.isnan(sa) or np.isnan(sb):
                continue
            diffs.append(sb - sa)
            if sb > sa:
                wins += 1
        diffs = np.array(diffs)
        print(f"{name} 贏現行比例: {wins/len(diffs)*100:.1f}% diff mean={diffs.mean():+.3f} "
              f"5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
