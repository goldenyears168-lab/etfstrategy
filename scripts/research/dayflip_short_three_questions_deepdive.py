#!/usr/bin/env python3
"""2026-08-10 三個懸而未決的設計問題，一次跑完（同一份day_pool重建，避免
三次重複呼叫build_candidates()）：

  Q1 7-8%區間+回落8%成交：gap>=7才候選，但>8%時模擬「掛在8%價位的限價單」
     成交（entry_px = min(實際開盤, T0收盤×1.08)），對比「不設上限、直接
     追市價進場」與「硬性拒絕>8%（當天不交易）」。
  Q2（席位排序）跳空最小 vs 席數最多 的walk-forward對照——今天稍早只看到
     全樣本t-stat排名（single_pick.json），沒做train/test，可能重蹈自適應
     搜尋那種『全樣本顯著、切開就消失』的坑，這裡補上。
  Q3 等權全押（FROZEN_SPEC原始設計）vs 單押最小跳空（現行部署）——
     全樣本 + walk-forward 對照，日報酬用當日所有合格候選的簡單平均。

方法沿用今天稍早的規則：開盤進場、-2%觸價回補或13:45收盤平倉、5bps成本。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_three_questions_deepdive.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from order.dayflip_short_signal import build_candidates, last_close

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DAY_POOL_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
FGAP_FLOOR = 7.0
FGAP_CAP = 8.0
N_BOOTSTRAP = 3000


def build_day_pool() -> dict[str, list[dict]]:
    if DAY_POOL_CACHE.exists():
        print(f"讀取快取 {DAY_POOL_CACHE}")
        return json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))

    import csv

    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    print(f"=== 重建{len(signal_dates)}個訊號日候選池（含n_seats，供Q2用）===")
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
            rows.append({
                "stock_id": c.stock_id, "fgap": fgap, "n_seats": c.n_seats,
                "t0_close": t0_close, "open_px": open_px, "close_px": close_px, "low_px": low_px,
            })
        day_pool[t0] = rows
        if (i + 1) % 20 == 0:
            print(f"  進度 {i + 1}/{len(signal_dates)}")
    print("完成\n")
    DAY_POOL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DAY_POOL_CACHE.write_text(json.dumps(day_pool, ensure_ascii=False), encoding="utf-8")
    return day_pool


def net_ret_for_entry(r: dict, entry_px: float) -> float:
    target = entry_px * (1 - COVER_TARGET_PCT)
    exit_px = target if r["low_px"] <= target else r["close_px"]
    return (entry_px - exit_px) / entry_px * 100 - ROUND_TRIP_COST_PCT


def sharpe_like(rets: list[float]) -> float:
    arr = np.array(rets)
    if len(arr) < 2 or arr.std() == 0:
        return float("nan")
    return float(arr.mean() / arr.std())


def split_train_test(dates: list[str], frac: float = 0.7) -> tuple[list[str], list[str]]:
    n = int(len(dates) * frac)
    return dates[:n], dates[n:]


def q1_band_cap(day_pool: dict[str, list[dict]], signal_dates: list[str]) -> None:
    print("\n" + "=" * 70)
    print("Q1: 7-8%區間 vs 現行(僅下限7%,追市價) vs 硬拒>8%")
    print("=" * 70)

    def pick_current(t0: str) -> float | None:
        qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]
        if not qual:
            return None
        best = min(qual, key=lambda r: r["fgap"])
        return net_ret_for_entry(best, best["open_px"])

    def pick_band_reject(t0: str) -> float | None:
        qual = [r for r in day_pool.get(t0, []) if FGAP_FLOOR <= r["fgap"] <= FGAP_CAP]
        if not qual:
            return None
        best = min(qual, key=lambda r: r["fgap"])
        return net_ret_for_entry(best, best["open_px"])

    def pick_band_fallback(t0: str) -> float | None:
        qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]
        if not qual:
            return None
        best = min(qual, key=lambda r: r["fgap"])
        entry_px = best["open_px"] if best["fgap"] <= FGAP_CAP else best["t0_close"] * (1 + FGAP_CAP / 100)
        return net_ret_for_entry(best, entry_px)

    variants = {"現行(僅下限7%,追市價)": pick_current, "7-8%區間(硬拒>8%)": pick_band_reject,
                "7%下限+回落8%成交": pick_band_fallback}
    results = {}
    for name, fn in variants.items():
        r = [x for t0 in signal_dates if (x := fn(t0)) is not None]
        results[name] = r
        print(f"{name:<20} n={len(r):>3} sharpe={sharpe_like(r):>6.3f} 均pnl={np.mean(r):+.3f}% "
              f"win={np.mean([1 if x > 0 else 0 for x in r]) * 100:.1f}%")

    n_gt8 = sum(1 for t0 in signal_dates
                for r in day_pool.get(t0, [])
                if r["fgap"] >= FGAP_FLOOR and r["fgap"] > FGAP_CAP
                and r == min([x for x in day_pool[t0] if x["fgap"] >= FGAP_FLOOR], key=lambda x: x["fgap"]))
    print(f"\n>8%的『當日最小合格跳空』出現次數: {n_gt8}/{len(signal_dates)}天"
          f"（樣本太小時band的差異多半是雜訊，不是訊號）")

    rng = np.random.default_rng(20260810)
    for a, b in [("現行(僅下限7%,追市價)", "7%下限+回落8%成交"),
                 ("現行(僅下限7%,追市價)", "7-8%區間(硬拒>8%)")]:
        wins = 0
        diffs = []
        for _ in range(N_BOOTSTRAP):
            sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
            ra = [x for t0 in sample if (x := variants[a](t0)) is not None]
            rb = [x for t0 in sample if (x := variants[b](t0)) is not None]
            if len(ra) < 5 or len(rb) < 5:
                continue
            sa, sb = sharpe_like(ra), sharpe_like(rb)
            if np.isnan(sa) or np.isnan(sb):
                continue
            diffs.append(sb - sa)
            if sb > sa:
                wins += 1
        diffs = np.array(diffs)
        print(f"\n『{b}』贏『{a}』比例: {wins/len(diffs)*100:.1f}% "
              f"diff mean={diffs.mean():+.3f} 5th={np.percentile(diffs,5):+.3f} "
              f"95th={np.percentile(diffs,95):+.3f}")


def q2_pickrule_walkforward(day_pool: dict[str, list[dict]], signal_dates: list[str]) -> None:
    print("\n" + "=" * 70)
    print("Q2: 跳空最小(現行) vs 席數最多 — walk-forward對照")
    print("=" * 70)

    def pick(t0: str, rule: str) -> float | None:
        qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]
        if not qual:
            return None
        if rule == "跳空最小":
            best = min(qual, key=lambda r: r["fgap"])
        else:
            best = max(qual, key=lambda r: (r["n_seats"], -r["fgap"]))
        return net_ret_for_entry(best, best["open_px"])

    train, test = split_train_test(signal_dates)
    for label, dates in [("全樣本(74天)", signal_dates), ("train(前70%)", train), ("test(後30%)", test)]:
        row = []
        for rule in ("跳空最小", "席數最多"):
            r = [x for t0 in dates if (x := pick(t0, rule)) is not None]
            row.append(f"{rule}: n={len(r):>3} sharpe={sharpe_like(r):>6.3f} mean={np.mean(r):+.3f}%")
        print(f"{label:<14}" + " | ".join(row))

    rng = np.random.default_rng(20260810)
    diffs, wins = [], 0
    for _ in range(N_BOOTSTRAP):
        sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
        r_gap = [x for t0 in sample if (x := pick(t0, "跳空最小")) is not None]
        r_seat = [x for t0 in sample if (x := pick(t0, "席數最多")) is not None]
        if len(r_gap) < 5 or len(r_seat) < 5:
            continue
        s_gap, s_seat = sharpe_like(r_gap), sharpe_like(r_seat)
        if np.isnan(s_gap) or np.isnan(s_seat):
            continue
        diffs.append(s_seat - s_gap)
        if s_seat > s_gap:
            wins += 1
    diffs = np.array(diffs)
    print(f"\n『席數最多』贏『跳空最小』比例: {wins/len(diffs)*100:.1f}% "
          f"diff mean={diffs.mean():+.3f} 5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


def q3_equal_weight_all(day_pool: dict[str, list[dict]], signal_dates: list[str]) -> None:
    print("\n" + "=" * 70)
    print("Q3: 等權全押(FROZEN_SPEC原始設計) vs 單押最小跳空(現行部署) — walk-forward對照")
    print("=" * 70)

    def daily_ret_single_pick(t0: str) -> float | None:
        qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]
        if not qual:
            return None
        best = min(qual, key=lambda r: r["fgap"])
        return net_ret_for_entry(best, best["open_px"])

    def daily_ret_equal_weight(t0: str) -> float | None:
        qual = [r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]
        if not qual:
            return None
        rets = [net_ret_for_entry(r, r["open_px"]) for r in qual]
        return float(np.mean(rets))

    train, test = split_train_test(signal_dates)
    for label, dates in [("全樣本(74天)", signal_dates), ("train(前70%)", train), ("test(後30%)", test)]:
        r_single = [x for t0 in dates if (x := daily_ret_single_pick(t0)) is not None]
        r_ew = [x for t0 in dates if (x := daily_ret_equal_weight(t0)) is not None]
        n_candidates = [len([r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR]) for t0 in dates]
        avg_n = np.mean([n for n in n_candidates if n > 0]) if any(n_candidates) else 0
        print(f"{label:<14}單押: n={len(r_single):>3} sharpe={sharpe_like(r_single):>6.3f} mean={np.mean(r_single):+.3f}%"
              f"  |  等權全押: n={len(r_ew):>3} sharpe={sharpe_like(r_ew):>6.3f} mean={np.mean(r_ew):+.3f}%"
              f"  (平均每天{avg_n:.1f}檔合格候選)")

    rng = np.random.default_rng(20260810)
    diffs, wins = [], 0
    for _ in range(N_BOOTSTRAP):
        sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
        r_single = [x for t0 in sample if (x := daily_ret_single_pick(t0)) is not None]
        r_ew = [x for t0 in sample if (x := daily_ret_equal_weight(t0)) is not None]
        if len(r_single) < 5 or len(r_ew) < 5:
            continue
        s_single, s_ew = sharpe_like(r_single), sharpe_like(r_ew)
        if np.isnan(s_single) or np.isnan(s_ew):
            continue
        diffs.append(s_ew - s_single)
        if s_ew > s_single:
            wins += 1
    diffs = np.array(diffs)
    print(f"\n『等權全押』贏『單押最小跳空』比例: {wins/len(diffs)*100:.1f}% "
          f"diff mean={diffs.mean():+.3f} 5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")
    print("\n(注意：等權全押每天動用資金≈候選數×單押的分配額，資金/保證金需求會顯著提高，"
          "sharpe/報酬率不能直接類比成『資金效率』相同的兩個方案)")


def main() -> None:
    day_pool = build_day_pool()
    import csv
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})

    q1_band_cap(day_pool, signal_dates)
    q2_pickrule_walkforward(day_pool, signal_dates)
    q3_equal_weight_all(day_pool, signal_dates)


if __name__ == "__main__":
    main()
