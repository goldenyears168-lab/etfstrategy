#!/usr/bin/env python3
"""分點層級賺賠拆解——使用者問「有沒有見到哪些分點特別會贏或特別會輸」.

跟個股拆解不同：一筆交易背後可能有多個分點同時買超同一檔股票，所以一個
分點會出現在多筆不同股票的交易紀錄裡，樣本量比個股拆解更大（219筆長邊
交易背後可能有更多分點-交易配對，因為n_seats平均>1）。

方法：對每筆已知交易(短邊day_pool 7-9%子集、長邊fgap>=4%子集)，重新呼叫
build_candidates()取得該候選當時實際參與的分點清單(seats tuple，不是只有
數量)，展開成「分點-交易」配對，按分點聚合勝率/均報酬——跟個股拆解一樣
先做描述性統計，再用walk-forward驗證避免重蹈覆轍(今晚已經因為沒驗證吃過
虧：外資買賣超PIT問題、離均差斜率train/test反轉)。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_branch_winloss_scan.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from order.dayflip_short_signal import build_candidates

ROOT = Path(__file__).resolve().parents[2]
LONG_RESULTS = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
DAY_POOL = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
FGAP_FLOOR_LONG = 4.0


def build_seat_map(t0_dates: set[str]) -> dict[tuple[str, str], tuple[str, ...]]:
    """(t0, stock_id) -> seats tuple，重新呼叫build_candidates()取得。"""
    out = {}
    for i, t0 in enumerate(sorted(t0_dates)):
        try:
            cands = build_candidates(t0)
        except Exception as ex:  # noqa: BLE001
            print(f"  {t0}: build_candidates失敗 {ex}")
            continue
        for c in cands:
            out[(t0, c.stock_id)] = c.seats
        if (i + 1) % 20 == 0:
            print(f"  進度 {i + 1}/{len(t0_dates)}")
    return out


def analyze(trades: list[dict], seat_map: dict, label: str) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    by_branch: dict[str, list[float]] = {}
    n_no_seats = 0
    for t in trades:
        seats = seat_map.get((t["t0"], t["stock_id"]))
        if not seats:
            n_no_seats += 1
            continue
        for tid in seats:
            by_branch.setdefault(tid, []).append(t["ret"])
    print(f"可對應分點資料: {len(trades) - n_no_seats}/{len(trades)}筆 (缺{n_no_seats}筆)")
    print(f"共涉及 {len(by_branch)} 個不同分點\n")

    stats = [(tid, len(r), np.mean(r), np.mean([1 if x > 0 else 0 for x in r]) * 100)
              for tid, r in by_branch.items()]
    stats_min3 = [s for s in stats if s[1] >= 3]
    stats_min3.sort(key=lambda x: -x[2])
    print(f"{'分點':<10}{'筆數':>6}{'均報酬%':>10}{'勝率%':>8}")
    print("--- 表現最好(n>=3) ---")
    for tid, n, mean, win in stats_min3[:8]:
        print(f"{tid:<10}{n:>6}{mean:>10.2f}{win:>8.1f}")
    print("--- 表現最差(n>=3) ---")
    for tid, n, mean, win in stats_min3[-8:]:
        print(f"{tid:<10}{n:>6}{mean:>10.2f}{win:>8.1f}")

    # walk-forward驗證（用交易的t0排序切70/30，看train選出的極端分點在test期是否持續）
    trades_sorted = sorted(trades, key=lambda t: t["t0"])
    n_train = int(len(trades_sorted) * 0.7)
    train_t0s = {t["t0"] for t in trades_sorted[:n_train]}
    test_trades = trades_sorted[n_train:]

    by_branch_train: dict[str, list[float]] = {}
    for t in trades_sorted[:n_train]:
        seats = seat_map.get((t["t0"], t["stock_id"]))
        if not seats:
            continue
        for tid in seats:
            by_branch_train.setdefault(tid, []).append(t["ret"])
    train_stats = [(tid, len(r), np.mean(r)) for tid, r in by_branch_train.items() if len(r) >= 3]
    if not train_stats:
        print("\ntrain期沒有任何分點達到n>=3，無法做walk-forward驗證")
        return
    train_stats.sort(key=lambda x: -x[2])
    top_branches = {s[0] for s in train_stats[:5]}
    train_stats.sort(key=lambda x: x[2])
    bottom_branches = {s[0] for s in train_stats[:5]}

    def test_group_stats(branch_set):
        rets = []
        for t in test_trades:
            seats = seat_map.get((t["t0"], t["stock_id"]))
            if seats and any(s in branch_set for s in seats):
                rets.append(t["ret"])
        return rets

    top_test = test_group_stats(top_branches)
    bottom_test = test_group_stats(bottom_branches)
    all_test = [t["ret"] for t in test_trades]
    print(f"\n=== Walk-forward驗證 ===")
    print(f"train選出前5分點: {sorted(top_branches)}")
    print(f"  這些分點在test期: n={len(top_test)} 均報酬={np.mean(top_test) if top_test else float('nan'):+.2f}%")
    print(f"train選出後5分點: {sorted(bottom_branches)}")
    print(f"  這些分點在test期: n={len(bottom_test)} 均報酬={np.mean(bottom_test) if bottom_test else float('nan'):+.2f}%")
    print(f"test期全部: n={len(all_test)} 均報酬={np.mean(all_test):+.2f}%")


def main() -> None:
    long_trades_raw = json.loads(LONG_RESULTS.read_text(encoding="utf-8"))
    long_trades = [t for t in long_trades_raw if t["fgap"] >= FGAP_FLOOR_LONG]

    day_pool = json.loads(DAY_POOL.read_text(encoding="utf-8"))

    def net_ret(r, entry_px):
        target = entry_px * 0.98
        exit_px = target if r["low_px"] <= target else r["close_px"]
        return (entry_px - exit_px) / entry_px * 100 - 0.05

    GAP_RANK_WEIGHT = 0.75

    def pick_blend(qual):
        by_gap = sorted(qual, key=lambda r: r["fgap"])
        gap_rank = {id(r): i + 1 for i, r in enumerate(by_gap)}
        by_seats = sorted(qual, key=lambda r: -r["n_seats"])
        seat_rank = {id(r): i + 1 for i, r in enumerate(by_seats)}
        return min(qual, key=lambda r: GAP_RANK_WEIGHT * gap_rank[id(r)] + (1 - GAP_RANK_WEIGHT) * seat_rank[id(r)])

    short_trades = []
    for t0, pool in sorted(day_pool.items()):
        qual = [r for r in pool if 7.0 <= r["fgap"] < 9.0]
        if not qual:
            continue
        best = pick_blend(qual)
        short_trades.append({"t0": t0, "stock_id": best["stock_id"], "ret": net_ret(best, best["open_px"])})

    all_t0_dates = {t["t0"] for t in long_trades} | {t["t0"] for t in short_trades}
    print(f"重新查{len(all_t0_dates)}個訊號日的分點清單(呼叫build_candidates)...")
    seat_map = build_seat_map(all_t0_dates)
    print("完成\n")

    analyze(long_trades, seat_map, "多單(fgap>=4%) 分點拆解")
    analyze(short_trades, seat_map, "空單(fgap 7-9%,現行部署) 分點拆解")


if __name__ == "__main__":
    main()
