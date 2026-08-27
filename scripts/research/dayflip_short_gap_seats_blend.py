#!/usr/bin/env python3
"""跳空最小(現行) × 席數最多 加權混合排序——使用者問「為什麼不是兩者加權採納」.

方法：對每天的合格候選池，各自算gap_rank(跳空由小到大排名，1=最小)跟
seat_rank(席數由多到少排名，1=最多)，combined_score = w*gap_rank + (1-w)*seat_rank，
挑combined_score最小的候選。w=1.0時退化成純跳空最小(現行)，w=0.0時退化成純
席數最多。掃w∈{0,0.25,0.5,0.75,1.0}，train(前70%)選最佳w，test(後30%)驗證，
另外對每個w都跑一次bootstrap跟現行(w=1.0)比較。

沿用day_pool_full_74d.json快取（同一份資料，不重跑build_candidates）。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_gap_seats_blend.py
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
FGAP_FLOOR = 7.0
N_BOOTSTRAP = 3000
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def net_ret_for_entry(r: dict, entry_px: float) -> float:
    target = entry_px * (1 - COVER_TARGET_PCT)
    exit_px = target if r["low_px"] <= target else r["close_px"]
    return (entry_px - exit_px) / entry_px * 100 - ROUND_TRIP_COST_PCT


def sharpe_like(rets: list[float]) -> float:
    arr = np.array(rets)
    if len(arr) < 2 or arr.std() == 0:
        return float("nan")
    return float(arr.mean() / arr.std())


def pick_blend(qual: list[dict], w: float) -> dict:
    by_gap = sorted(qual, key=lambda r: r["fgap"])
    gap_rank = {id(r): i + 1 for i, r in enumerate(by_gap)}
    by_seats = sorted(qual, key=lambda r: -r["n_seats"])
    seat_rank = {id(r): i + 1 for i, r in enumerate(by_seats)}
    return min(qual, key=lambda r: w * gap_rank[id(r)] + (1 - w) * seat_rank[id(r)])


def day_ret(day_pool: dict, t0: str, w: float) -> float | None:
    qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]
    if not qual:
        return None
    best = pick_blend(qual, w)
    return net_ret_for_entry(best, best["open_px"])


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    day_pool = json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))

    n_train = int(len(signal_dates) * 0.7)
    train, test = signal_dates[:n_train], signal_dates[n_train:]

    print("=== 掃描權重w（w=1.0純跳空最小=現行, w=0.0純席數最多）===")
    print(f"{'w':>6}{'全樣本n':>8}{'全樣本sharpe':>13}{'全樣本mean%':>12}"
          f"{'train sharpe':>13}{'test sharpe':>12}{'test mean%':>11}")
    for w in WEIGHTS:
        r_all = [x for t0 in signal_dates if (x := day_ret(day_pool, t0, w)) is not None]
        r_train = [x for t0 in train if (x := day_ret(day_pool, t0, w)) is not None]
        r_test = [x for t0 in test if (x := day_ret(day_pool, t0, w)) is not None]
        print(f"{w:>6.2f}{len(r_all):>8}{sharpe_like(r_all):>13.3f}{np.mean(r_all):>12.3f}"
              f"{sharpe_like(r_train):>13.3f}{sharpe_like(r_test):>12.3f}{np.mean(r_test):>11.3f}")

    # train選最佳w
    best_w, best_train_sharpe = None, -np.inf
    for w in WEIGHTS:
        r_train = [x for t0 in train if (x := day_ret(day_pool, t0, w)) is not None]
        s = sharpe_like(r_train)
        if not np.isnan(s) and s > best_train_sharpe:
            best_train_sharpe, best_w = s, w
    print(f"\ntrain選出最佳w={best_w}（train sharpe={best_train_sharpe:.3f}）")
    r_test_best = [x for t0 in test if (x := day_ret(day_pool, t0, best_w)) is not None]
    r_test_baseline = [x for t0 in test if (x := day_ret(day_pool, t0, 1.0)) is not None]
    print(f"test期：w={best_w} sharpe={sharpe_like(r_test_best):.3f} mean={np.mean(r_test_best):+.3f}%  "
          f"vs 現行(w=1.0) sharpe={sharpe_like(r_test_baseline):.3f} mean={np.mean(r_test_baseline):+.3f}%")

    print(f"\n=== Block bootstrap：每個w vs 現行(w=1.0)，重抽{N_BOOTSTRAP}次 ===")
    rng = np.random.default_rng(20260810)
    for w in WEIGHTS:
        if w == 1.0:
            continue
        diffs, wins = [], 0
        for _ in range(N_BOOTSTRAP):
            sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
            rw = [x for t0 in sample if (x := day_ret(day_pool, t0, w)) is not None]
            r1 = [x for t0 in sample if (x := day_ret(day_pool, t0, 1.0)) is not None]
            if len(rw) < 5 or len(r1) < 5:
                continue
            sw, s1 = sharpe_like(rw), sharpe_like(r1)
            if np.isnan(sw) or np.isnan(s1):
                continue
            diffs.append(sw - s1)
            if sw > s1:
                wins += 1
        diffs = np.array(diffs)
        print(f"w={w:.2f} 贏 現行(w=1.0) 比例: {wins/len(diffs)*100:.1f}% "
              f"diff mean={diffs.mean():+.3f} 5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
