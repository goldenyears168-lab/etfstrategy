#!/usr/bin/env python3
"""6% vs 6.5% vs 7% 三門檻比較——使用者問「還是6.5%呢」，用同一套
day_pool重建 + walk-forward + block bootstrap方法（沿用
dayflip_short_fgap_threshold_full_sweep.py / dayflip_short_6v7_threshold_bootstrap.py
同一套規則：開盤進場、-2%觸價回補或13:45收盤平倉、5bps成本、
pick_rule=smallest_qualifying_gap），只是門檻改成三個一起比。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_65_midpoint_check.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from order.dayflip_short_signal import build_candidates, last_close

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
N_BOOTSTRAP = 5000
THRESHOLDS = (6.0, 6.5, 7.0)


def main() -> None:
    import csv

    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    print(f"=== 重建{len(signal_dates)}個訊號日候選池 ===")
    day_pool: dict[str, list[dict]] = {}
    for i, t0 in enumerate(signal_dates):
        candidates = build_candidates(t0)
        rows = []
        for c in candidates:
            t0_close = last_close(c.stock_id, t0)
            m = fut_cache.get(c.stock_id) or {}
            dates_sorted = sorted(m)
            if t0 not in dates_sorted:
                continue
            idx = dates_sorted.index(t0)
            if idx + 1 >= len(dates_sorted):
                continue
            t01 = dates_sorted[idx + 1]
            row = m.get(t01)
            if not row or t0_close is None or t0_close <= 0:
                continue
            open_px, close_px, _high, low_px = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            if open_px <= 0:
                continue
            fgap = (open_px / t0_close - 1) * 100
            target = open_px * (1 - COVER_TARGET_PCT)
            exit_px = target if low_px <= target else close_px
            net_ret = (open_px - exit_px) / open_px * 100 - ROUND_TRIP_COST_PCT
            rows.append({"fgap": fgap, "net_ret_pct": net_ret})
        day_pool[t0] = rows
        if (i + 1) % 20 == 0:
            print(f"  進度 {i + 1}/{len(signal_dates)}")
    print("完成\n")

    def day_pick(t0: str, threshold: float) -> float | None:
        qualifying = [r for r in day_pool.get(t0, []) if r["fgap"] >= threshold]
        if not qualifying:
            return None
        return min(qualifying, key=lambda r: r["fgap"])["net_ret_pct"]

    def sharpe_like(rets: list[float]) -> float:
        arr = np.array(rets)
        if len(arr) < 2 or arr.std() == 0:
            return float("nan")
        return float(arr.mean() / arr.std())

    print("=== 全樣本(74天)基準比較 ===")
    rets = {}
    for th in THRESHOLDS:
        r = [x for t0 in signal_dates if (x := day_pick(t0, th)) is not None]
        rets[th] = r
        print(f"{th}%門檻 n={len(r)} sharpe={sharpe_like(r):.3f} 均pnl={np.mean(r):+.3f}% "
              f"win={np.mean([1 if x > 0 else 0 for x in r]) * 100:.1f}%")
    print()

    print(f"=== Block bootstrap（重抽{N_BOOTSTRAP}次） ===")
    rng = np.random.default_rng(20260810)
    pairs = [(6.0, 6.5), (6.5, 7.0), (6.0, 7.0)]
    for lo, hi in pairs:
        diffs = []
        wins = 0
        for _ in range(N_BOOTSTRAP):
            sample_dates = rng.choice(signal_dates, size=len(signal_dates), replace=True)
            rlo = [x for t0 in sample_dates if (x := day_pick(t0, lo)) is not None]
            rhi = [x for t0 in sample_dates if (x := day_pick(t0, hi)) is not None]
            if len(rlo) < 5 or len(rhi) < 5:
                continue
            slo, shi = sharpe_like(rlo), sharpe_like(rhi)
            if np.isnan(slo) or np.isnan(shi):
                continue
            diffs.append(shi - slo)
            if shi > slo:
                wins += 1
        diffs = np.array(diffs)
        ci_lo, ci_hi = np.percentile(diffs, 5), np.percentile(diffs, 95)
        print(f"{hi}% vs {lo}%：贏比例={wins/len(diffs)*100:.1f}% "
              f"diff mean={diffs.mean():+.3f} 5th={ci_lo:+.3f} 95th={ci_hi:+.3f} "
              f"CI不含0={ci_lo > 0}")

    print("\n=== 三門檻並列（全樣本） ===")
    for th in THRESHOLDS:
        r = rets[th]
        print(f"  {th}%: n={len(r)} sharpe={sharpe_like(r):.3f} mean={np.mean(r):+.3f}%")


if __name__ == "__main__":
    main()
