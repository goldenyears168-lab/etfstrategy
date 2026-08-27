#!/usr/bin/env python3
"""現行已部署的gap_seat_rank_blend_w075排序法，floor改6%會不會更好——之前
只在floor=7%下測過blend權重，floor=6%×blend組合還沒測過.

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_floor6_with_blend_rule.py
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
GAP_RANK_WEIGHT = 0.75
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


def pick_blend(qual: list[dict]) -> dict:
    by_gap = sorted(qual, key=lambda r: r["fgap"])
    gap_rank = {id(r): i + 1 for i, r in enumerate(by_gap)}
    by_seats = sorted(qual, key=lambda r: -r["n_seats"])
    seat_rank = {id(r): i + 1 for i, r in enumerate(by_seats)}
    return min(qual, key=lambda r: GAP_RANK_WEIGHT * gap_rank[id(r)] + (1 - GAP_RANK_WEIGHT) * seat_rank[id(r)])


def day_ret(day_pool: dict, t0: str, floor: float) -> float | None:
    qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= floor]
    if not qual:
        return None
    best = pick_blend(qual)
    return net_ret_for_entry(best, best["open_px"])


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    day_pool = json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))
    n_train = int(len(signal_dates) * 0.7)
    train, test = signal_dates[:n_train], signal_dates[n_train:]

    print(f"{'floor':>6}{'n':>6}{'全樣本sharpe':>13}{'全樣本mean%':>12}"
          f"{'train sharpe':>13}{'test sharpe':>12}{'test mean%':>11}")
    for floor in (6.0, 6.5, 7.0):
        r_all = [x for t0 in signal_dates if (x := day_ret(day_pool, t0, floor)) is not None]
        r_train = [x for t0 in train if (x := day_ret(day_pool, t0, floor)) is not None]
        r_test = [x for t0 in test if (x := day_ret(day_pool, t0, floor)) is not None]
        print(f"{floor:>6.1f}{len(r_all):>6}{sharpe_like(r_all):>13.3f}{np.mean(r_all):>12.3f}"
              f"{sharpe_like(r_train):>13.3f}{sharpe_like(r_test):>12.3f}{np.mean(r_test):>11.3f}")

    print(f"\n=== Block bootstrap：floor=6.0/6.5 vs 現行floor=7.0，重抽{N_BOOTSTRAP}次 ===")
    rng = np.random.default_rng(20260810)
    for floor in (6.0, 6.5):
        diffs, wins = [], 0
        for _ in range(N_BOOTSTRAP):
            sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
            rf = [x for t0 in sample if (x := day_ret(day_pool, t0, floor)) is not None]
            r7 = [x for t0 in sample if (x := day_ret(day_pool, t0, 7.0)) is not None]
            if len(rf) < 5 or len(r7) < 5:
                continue
            sf, s7 = sharpe_like(rf), sharpe_like(r7)
            if np.isnan(sf) or np.isnan(s7):
                continue
            diffs.append(sf - s7)
            if sf > s7:
                wins += 1
        diffs = np.array(diffs)
        print(f"floor={floor} 贏 floor=7.0 比例: {wins/len(diffs)*100:.1f}% diff mean={diffs.mean():+.3f} "
              f"5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
