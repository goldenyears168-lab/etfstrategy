#!/usr/bin/env python3
"""6%(現行) vs 7%(GAPUP_SHORT_SIZING.md原本建議) 固定門檻——block bootstrap
穩健性檢查，不是只看單一次70/30切分的數字.

背景：dayflip_short_fgap_threshold_full_sweep.py的固定門檻掃描顯示7%門檻
在train(sharpe 0.942)跟test(sharpe 1.329)都優於現行6%(train 0.606/test
0.763)，而且自適應搜尋自己收斂到『不用夜盤調整，7%固定最好』——但今天
已經看過太多次『單一次切分看起來贏、換個角度就消失』，這裡用block bootstrap
（對74個訊號日重複抽樣、重建整個當天的候選池挑選過程）看這個6% vs 7%的
差異在重抽樣下有多穩定，不只信一次70/30切分。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_6v7_threshold_bootstrap.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from order.dayflip_short_signal import build_candidates, last_close

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
N_BOOTSTRAP = 5000


def main() -> None:
    import csv

    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    print(f"=== 重建{len(signal_dates)}個訊號日候選池（沿用threshold_full_sweep同一套邏輯）===")
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

    # 全樣本(不切train/test)基準比較
    rets_6 = [r for t0 in signal_dates if (r := day_pick(t0, 6.0)) is not None]
    rets_7 = [r for t0 in signal_dates if (r := day_pick(t0, 7.0)) is not None]
    print(f"全樣本(74天)：6%門檻 n={len(rets_6)} sharpe={sharpe_like(rets_6):.3f} 均pnl={np.mean(rets_6):+.3f}%")
    print(f"全樣本(74天)：7%門檻 n={len(rets_7)} sharpe={sharpe_like(rets_7):.3f} 均pnl={np.mean(rets_7):+.3f}%\n")

    print(f"=== Block bootstrap（重抽{N_BOOTSTRAP}次，每次對74個訊號日做取後放回抽樣）===")
    rng = np.random.default_rng(20260810)
    diffs = []
    wins_7_over_6 = 0
    for _ in range(N_BOOTSTRAP):
        sample_dates = rng.choice(signal_dates, size=len(signal_dates), replace=True)
        r6 = [r for t0 in sample_dates if (r := day_pick(t0, 6.0)) is not None]
        r7 = [r for t0 in sample_dates if (r := day_pick(t0, 7.0)) is not None]
        if len(r6) < 5 or len(r7) < 5:
            continue
        s6, s7 = sharpe_like(r6), sharpe_like(r7)
        if np.isnan(s6) or np.isnan(s7):
            continue
        diffs.append(s7 - s6)
        if s7 > s6:
            wins_7_over_6 += 1

    diffs = np.array(diffs)
    print(f"有效重抽次數: {len(diffs)}/{N_BOOTSTRAP}")
    print(f"7%門檻贏過6%門檻(sharpe更高)的比例: {wins_7_over_6/len(diffs)*100:.1f}%")
    print(f"sharpe差異(7%-6%)分布: mean={diffs.mean():+.3f} std={diffs.std():.3f} "
          f"5th百分位={np.percentile(diffs,5):+.3f} 95th百分位={np.percentile(diffs,95):+.3f}")
    ci_excludes_zero = np.percentile(diffs, 5) > 0
    print(f"90% bootstrap信賴區間是否完全大於0（不含0）: {ci_excludes_zero}\n")

    if wins_7_over_6 / len(diffs) >= 0.80 and ci_excludes_zero:
        verdict = "相對穩健——7%在多數重抽樣本下都優於6%，值得認真考慮"
    elif wins_7_over_6 / len(diffs) >= 0.65:
        verdict = "有傾向性但不夠壓倒性——7%多數情況下較好，但不是穩定到能忽略雜訊"
    else:
        verdict = "不夠穩健——7%贏6%的比例不夠高，今天稍早那次70/30切分結果可能只是運氣"
    print(f"=== 結論：{verdict} ===")

    print(
        "\n⚠️ 限制：\n"
        "  1) block bootstrap對訊號『日期』重抽（取後放回），保留了同一天\n"
        "     的候選池完整性，但不是真正獨立的新樣本，本質上仍是同一份\n"
        "     74天歷史資料的重複利用，不能取代真正的樣本外(如未來3-6個月\n"
        "     新資料)驗證。\n"
        "  2) 沒有做多重比較校正——這是今天對6%/7%這組特定比較做的第一次\n"
        "     bootstrap，不像前面自適應搜尋那樣掃了幾百組，多重比較風險\n"
        "     相對低，但仍建議上線前用未來新資料再次確認。"
    )


if __name__ == "__main__":
    main()
