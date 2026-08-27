#!/usr/bin/env python3
"""Beta校正離均差斜率——設計A(靜態beta)。

使用者假設：現行「離均差」(個股滾動15分鐘報酬 - 大盤滾動15分鐘報酬)是原始值，
沒有考慮到不同股票對大盤的敏感度(beta)本來就不同。這裡測「除以T0前60個交易日
daily return對0050 OLS回歸算出的beta」之後的離均差斜率是否比未校正版本(今晚
已測NOT_SUPPORTED: train IC=-0.159 p=0.059, test IC=-0.003 p=0.980)更有訊號。

方法(跟dayflip_post_dump_long_deviation_slope_signal.py同一套窗口定義，只加
beta正規化這一步，避免多重比較)：
1. 對每筆訊號，用stock_daily_bars抓T0之前60個交易日的daily return，對0050
   同期daily return做OLS取斜率當beta；樣本不足60天則跳過此筆(誠實記錄)。
2. dev(t) = 個股滾動15分鐘報酬(t) - 大盤滾動15分鐘報酬(t)（大盤=0050 1分K，
   跟現行rolling_relative_dip同一個15分鐘窗口）。
3. dev_adj(t) = dev(t) / max(beta, 0.3)（下限保護避免除以極小值爆掉，誠實
   記錄套用下限保護的筆數）。
4. 對dev_adj在進場前15分鐘窗口做線性回歸取斜率，當候選特徵，跟後續報酬(ret)
   做Spearman IC，同一套walk-forward(前70%/後30%) + permutation test(3000次)。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_post_dump_long_beta_adjusted_deviation_slope.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

import stock_db
from stock_db.kbar import load_kbar_day_bars

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
BENCH = "0050"
ROLLING_WINDOW_MIN = 15  # 跟現行rolling_relative_dip同一個窗口
SLOPE_WINDOW_MIN = 15  # 進場前這段時間的離均差序列拿來算斜率
BETA_LOOKBACK_DAYS = 60  # T0之前60個交易日算beta
BETA_FLOOR = 0.3  # beta下限保護，避免除以極小值爆掉


def load_minute_closes(con: sqlite3.Connection, stock_id: str, day: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, day)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def load_daily_closes(con: sqlite3.Connection, stock_id: str, before_date: str, n: int) -> list[tuple[str, float]]:
    """取before_date之前(不含)最近n個交易日的(trade_date, close)，finmind優先、
    yfinance補缺(避免0050在來源切換邊界重複日期造成誤差)，依日期升冪回傳。"""
    cur = con.execute(
        """
        SELECT trade_date, close, source
        FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date < ? AND close IS NOT NULL AND close > 0
        ORDER BY trade_date DESC
        """,
        (stock_id, before_date),
    )
    by_date: dict[str, float] = {}
    for row in cur:
        d = row["trade_date"]
        if d in by_date and row["source"] != "finmind":
            continue
        by_date[d] = row["close"]
    dates_sorted = sorted(by_date, reverse=True)[:n]
    dates_sorted.sort()
    return [(d, by_date[d]) for d in dates_sorted]


def compute_beta(con: sqlite3.Connection, stock_id: str, t0: str) -> tuple[float | None, int]:
    """回傳(beta, 可用交易日數)。樣本不足BETA_LOOKBACK_DAYS天回傳(None, n)。"""
    stock_series = load_daily_closes(con, stock_id, t0, BETA_LOOKBACK_DAYS + 1)
    bench_series = load_daily_closes(con, BENCH, t0, BETA_LOOKBACK_DAYS + 1)
    stock_map = dict(stock_series)
    bench_map = dict(bench_series)
    common_dates = sorted(set(stock_map) & set(bench_map))
    if len(common_dates) < BETA_LOOKBACK_DAYS + 1:
        return None, len(common_dates)
    closes_s = [stock_map[d] for d in common_dates]
    closes_b = [bench_map[d] for d in common_dates]
    rets_s = np.diff(closes_s) / np.array(closes_s[:-1])
    rets_b = np.diff(closes_b) / np.array(closes_b[:-1])
    if len(rets_s) < BETA_LOOKBACK_DAYS:
        return None, len(rets_s)
    if np.std(rets_b) < 1e-12:
        return None, len(rets_s)
    beta = float(np.polyfit(rets_b, rets_s, 1)[0])
    return beta, len(rets_s)


def deviation_series(stock_closes: dict, bench_closes: dict) -> dict[str, float]:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    out = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        out[m] = stock_ret - bench_ret
    return out


def entry_window_slope(dev: dict[str, float], entry_minute: str) -> float | None:
    entry_dt = datetime.strptime(entry_minute, "%H:%M")
    start_dt = entry_dt - timedelta(minutes=SLOPE_WINDOW_MIN)
    window_minutes = sorted(m for m in dev if start_dt.strftime("%H:%M") <= m <= entry_minute)
    if len(window_minutes) < 5:
        return None
    xs = np.arange(len(window_minutes))
    ys = np.array([dev[m] for m in window_minutes])
    slope = float(np.polyfit(xs, ys, 1)[0])
    return slope


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    sub = [t for t in trades if t["fgap"] >= 4.0]
    print(f"候選池(fgap>=4%): {len(sub)}筆\n")

    enriched = []
    n_missing_kbar = 0
    n_missing_beta = 0
    n_floor_applied = 0
    betas_raw = []
    for t in sub:
        beta, n_days = compute_beta(con, t["stock_id"], t["t0"])
        if beta is None:
            n_missing_beta += 1
            continue
        betas_raw.append(beta)

        stock_closes = load_minute_closes(con, t["stock_id"], t["entry_day"])
        bench_closes = load_minute_closes(con, BENCH, t["entry_day"])
        if len(stock_closes) < 30 or len(bench_closes) < 30:
            n_missing_kbar += 1
            continue
        dev = deviation_series(stock_closes, bench_closes)
        slope = entry_window_slope(dev, t["entry_minute"])
        if slope is None:
            n_missing_kbar += 1
            continue

        beta_used = beta
        if beta <= 0 or beta < BETA_FLOOR:
            beta_used = BETA_FLOOR
            n_floor_applied += 1
        dev_adj_slope = slope / beta_used

        enriched.append({**t, "beta": beta, "dev_slope": slope, "dev_adj_slope": dev_adj_slope})
    con.close()

    print(f"beta不足{BETA_LOOKBACK_DAYS}個交易日跳過: {n_missing_beta}筆")
    print(f"1分K資料缺失/視窗不足跳過: {n_missing_kbar}筆")
    print(f"可用樣本: {len(enriched)}/{len(sub)}筆")
    print(f"beta下限保護(beta<={BETA_FLOOR}套用{BETA_FLOOR})套用筆數: {n_floor_applied}/{len(enriched)}筆")
    if betas_raw:
        arr = np.array(betas_raw)
        print(
            f"raw beta分布: min={arr.min():.3f} p25={np.percentile(arr,25):.3f} "
            f"median={np.median(arr):.3f} p75={np.percentile(arr,75):.3f} max={arr.max():.3f}\n"
        )

    enriched_sorted = sorted(enriched, key=lambda t: (t["entry_day"], t["entry_minute"]))
    n_train = int(len(enriched_sorted) * 0.7)
    train, test = enriched_sorted[:n_train], enriched_sorted[n_train:]

    print("=== Beta校正離均差斜率(dev_adj_slope) vs 後續報酬 ===")
    results = {}
    for label, group in [("full", enriched_sorted), ("train", train), ("test", test)]:
        xs = np.array([t["dev_adj_slope"] for t in group])
        ys = np.array([t["ret"] for t in group])
        ic, pval = spearmanr(xs, ys)
        results[label] = (float(ic), float(pval), len(group))
        print(
            f"{label}(n={len(group)}): IC={ic:.3f} p={pval:.3f} "
            f"分布[{xs.min():.3f},{xs.max():.3f}] median={np.median(xs):.3f}"
        )

    print("\n=== Permutation test（全樣本） ===")
    xs = np.array([t["dev_adj_slope"] for t in enriched_sorted])
    ys = np.array([t["ret"] for t in enriched_sorted])
    real_ic, _ = spearmanr(xs, ys)
    rng = np.random.default_rng(20260811)
    perm_ics = []
    for _ in range(3000):
        shuffled = rng.permutation(ys)
        perm_ic, _ = spearmanr(xs, shuffled)
        perm_ics.append(abs(perm_ic))
    perm_p = float(np.mean(np.array(perm_ics) >= abs(real_ic)))
    print(f"real IC={real_ic:.3f}  permutation p={perm_p:.3f} (3000次重抽)")

    print("\n=== 對照組：未校正dev_slope(同一批可用樣本) ===")
    for label, group in [("full", enriched_sorted), ("train", train), ("test", test)]:
        xs = np.array([t["dev_slope"] for t in group])
        ys = np.array([t["ret"] for t in group])
        ic, pval = spearmanr(xs, ys)
        print(f"{label}(n={len(group)}): IC={ic:.3f} p={pval:.3f}")

    out_path = ROOT / "reports/research/dayflip_fgap_calibration/beta_adjusted_deviation_slope_results.json"
    out_path.write_text(
        json.dumps(
            {
                "n_candidates": len(sub),
                "n_missing_beta": n_missing_beta,
                "n_missing_kbar": n_missing_kbar,
                "n_usable": len(enriched),
                "n_beta_floor_applied": n_floor_applied,
                "full": results["full"],
                "train": results["train"],
                "test": results["test"],
                "permutation_p": perm_p,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n結果已存: {out_path}")


if __name__ == "__main__":
    main()
