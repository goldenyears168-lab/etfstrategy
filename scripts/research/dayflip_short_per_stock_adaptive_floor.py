#!/usr/bin/env python3
"""個股自適應門檻——使用者要求「研究根據個股自適應」而非全體股票共用固定7%.

設計理由：今天稍早測過的『市場regime自適應』（VIX/夜盤動能/當下台指期）全部
NOT_SUPPORTED，但那些是『時間軸』自適應（同一檔股票在不同天用不同門檻）。
使用者這次要的是『橫截面』自適應（同一天不同股票用不同門檻），是不同的維度，
沒有被今天稍早的NOT_SUPPORTED結論排除。

關鍵：避免重蹈小樣本覆轍——不能直接用74個訊號日裡『這檔股票過去的訊號表現』
來校準（多數股票在樣本裡只出現1-3次，直接算會嚴重過擬合）。改用『該股票自己
的歷史跳空波動度』（T0之前200個交易日的股票自身隔夜跳空|open/prev_close-1|
平均值，來自stock_daily_bars，樣本量是完整價格史，不是稀疏的訊號日子集，
獨立於本策略的訊號本身，不會用同一組資料校準又驗證）當個股特徵，把7%門檻
正規化成：adaptive_threshold = 7% × (own_gap_vol / universe_median_gap_vol)。

沿用day_pool_full_74d.json快取（候選池/fgap/開高低收），另外查DB算每檔候選
股票自己的200日隔夜跳空波動度。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_per_stock_adaptive_floor.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import stock_db

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
DAY_POOL_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
GAP_VOL_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/per_stock_gap_vol_cache.json"
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
LOOKBACK_DAYS = 200
N_BOOTSTRAP = 3000
K_SWEEP = (0.5, 0.75, 1.0, 1.25, 1.5)  # adaptive_threshold = 7% × k × (own_gap_vol/universe_median)


def net_ret_for_entry(r: dict, entry_px: float) -> float:
    target = entry_px * (1 - COVER_TARGET_PCT)
    exit_px = target if r["low_px"] <= target else r["close_px"]
    return (entry_px - exit_px) / entry_px * 100 - ROUND_TRIP_COST_PCT


def sharpe_like(rets: list[float]) -> float:
    arr = np.array(rets)
    if len(arr) < 2 or arr.std() == 0:
        return float("nan")
    return float(arr.mean() / arr.std())


def compute_gap_vol(con: sqlite3.Connection, stock_id: str, t0: str) -> float | None:
    lo = (date.fromisoformat(t0) - timedelta(days=LOOKBACK_DAYS + 60)).isoformat()
    rows = con.execute(
        "SELECT trade_date, open, close FROM stock_daily_bars "
        "WHERE source='finmind' AND stock_id=? AND trade_date BETWEEN ? AND ? "
        "AND open>0 AND close>0 ORDER BY trade_date",
        (stock_id, lo, t0),
    ).fetchall()
    if len(rows) < 40:
        return None
    rows = rows[-LOOKBACK_DAYS:]
    gaps = []
    prev_close = None
    for _d, o, c in rows:
        if prev_close is not None and prev_close > 0:
            gaps.append(abs(o / prev_close - 1) * 100)
        prev_close = c
    if len(gaps) < 30:
        return None
    return float(np.mean(gaps))


def build_gap_vol_cache(day_pool: dict) -> dict[str, float | None]:
    if GAP_VOL_CACHE.exists():
        print(f"讀取快取 {GAP_VOL_CACHE}")
        return json.loads(GAP_VOL_CACHE.read_text(encoding="utf-8"))

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    cache: dict[str, float | None] = {}
    keys = set()
    for t0, pool in day_pool.items():
        for r in pool:
            keys.add((r["stock_id"], t0))
    print(f"計算{len(keys)}組(stock_id,t0)的200日自身隔夜跳空波動度…")
    for i, (sid, t0) in enumerate(sorted(keys)):
        cache[f"{sid}|{t0}"] = compute_gap_vol(con, sid, t0)
        if (i + 1) % 50 == 0:
            print(f"  進度 {i + 1}/{len(keys)}")
    con.close()
    GAP_VOL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GAP_VOL_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    day_pool = json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))
    gap_vol_cache = build_gap_vol_cache(day_pool)

    all_vols = [v for v in gap_vol_cache.values() if v is not None]
    universe_median = float(np.median(all_vols))
    print(f"\n候選股自身隔夜跳空波動度：median={universe_median:.3f}% "
          f"(n={len(all_vols)}, 缺值{sum(1 for v in gap_vol_cache.values() if v is None)}組)\n")

    def enrich(t0: str) -> list[dict]:
        out = []
        for r in day_pool.get(t0, []):
            v = gap_vol_cache.get(f"{r['stock_id']}|{t0}")
            out.append({**r, "gap_vol": v})
        return out

    def day_ret_fixed(t0: str, floor: float = 7.0) -> float | None:
        qual = [r for r in enrich(t0) if r["fgap"] >= floor]
        if not qual:
            return None
        best = min(qual, key=lambda r: r["fgap"])
        return net_ret_for_entry(best, best["open_px"])

    def day_ret_adaptive(t0: str, k: float, hard_floor: float = 4.0) -> float | None:
        pool = enrich(t0)
        qual = []
        for r in pool:
            if r["gap_vol"] is None:
                continue
            eff_threshold = max(hard_floor, 7.0 * k * (r["gap_vol"] / universe_median))
            if r["fgap"] >= eff_threshold:
                qual.append(r)
        if not qual:
            return None
        best = min(qual, key=lambda r: r["fgap"])
        return net_ret_for_entry(best, best["open_px"])

    n_train = int(len(signal_dates) * 0.7)
    train, test = signal_dates[:n_train], signal_dates[n_train:]

    print(f"{'design':<28}{'n':>6}{'全樣本sharpe':>13}{'全樣本mean%':>12}"
          f"{'train sharpe':>13}{'test sharpe':>12}{'test mean%':>11}")
    r_all = [x for t0 in signal_dates if (x := day_ret_fixed(t0)) is not None]
    r_train = [x for t0 in train if (x := day_ret_fixed(t0)) is not None]
    r_test = [x for t0 in test if (x := day_ret_fixed(t0)) is not None]
    print(f"{'現行(固定7%)':<28}{len(r_all):>6}{sharpe_like(r_all):>13.3f}{np.mean(r_all):>12.3f}"
          f"{sharpe_like(r_train):>13.3f}{sharpe_like(r_test):>12.3f}{np.mean(r_test):>11.3f}")

    for k in K_SWEEP:
        r_all = [x for t0 in signal_dates if (x := day_ret_adaptive(t0, k)) is not None]
        r_train = [x for t0 in train if (x := day_ret_adaptive(t0, k)) is not None]
        r_test = [x for t0 in test if (x := day_ret_adaptive(t0, k)) is not None]
        print(f"{'個股自適應k=' + str(k):<28}{len(r_all):>6}{sharpe_like(r_all):>13.3f}{np.mean(r_all):>12.3f}"
              f"{sharpe_like(r_train):>13.3f}{sharpe_like(r_test):>12.3f}{np.mean(r_test):>11.3f}")

    print(f"\n=== Block bootstrap：每個k vs 現行固定7%，重抽{N_BOOTSTRAP}次 ===")
    rng = np.random.default_rng(20260810)
    for k in K_SWEEP:
        diffs, wins = [], 0
        for _ in range(N_BOOTSTRAP):
            sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
            ra = [x for t0 in sample if (x := day_ret_fixed(t0)) is not None]
            rk = [x for t0 in sample if (x := day_ret_adaptive(t0, k)) is not None]
            if len(ra) < 5 or len(rk) < 5:
                continue
            sa, sk = sharpe_like(ra), sharpe_like(rk)
            if np.isnan(sa) or np.isnan(sk):
                continue
            diffs.append(sk - sa)
            if sk > sa:
                wins += 1
        diffs = np.array(diffs)
        print(f"k={k} 贏 現行固定7% 比例: {wins/len(diffs)*100:.1f}% diff mean={diffs.mean():+.3f} "
              f"5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
