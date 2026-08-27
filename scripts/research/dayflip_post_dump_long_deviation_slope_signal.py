#!/usr/bin/env python3
"""離均差變化率研究——使用者問「台指沒跌、個股在跌，離均差增加，這個變化率
有關係嗎」。現行rolling_relative_dip是二元判斷(跌破0.3%門檻→10分鐘後收斂
過半就進場)，這裡改測「進場前離均差收斂的斜率」本身能不能預測後續表現。

方法：對219筆訊號(fgap>=4%)，用個股1分K + 0050(大盤代理，historical覆蓋
最完整)1分K，在進場前15分鐘窗口內，逐分鐘算「離均差(t) = 個股滾動15分鐘
報酬(t) - 大盤滾動15分鐘報酬(t)」，對這個序列做線性回歸取斜率當候選特徵，
跟後續交易報酬做Spearman IC + walk-forward + permutation test（同一套今晚
一貫的驗證方法，只測1個特徵，避免多重比較）。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_post_dump_long_deviation_slope_signal.py
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
ROLLING_WINDOW_MIN = 15  # 跟現行rolling_relative_dip同一個窗口，才可比較
SLOPE_WINDOW_MIN = 15  # 進場前這段時間的離均差序列拿來算斜率


def load_minute_closes(con: sqlite3.Connection, stock_id: str, day: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, day)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def deviation_series(stock_closes: dict, bench_closes: dict) -> dict[str, float]:
    """跟find_rolling_dip_signal()同一套rolling_lag定義，但回傳整條序列不是
    只找觸發點——才能對進場前一段窗口算斜率。"""
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
    """進場前SLOPE_WINDOW_MIN分鐘的離均差序列，線性回歸斜率(每分鐘變化%)。
    正斜率=離均差正在擴大(個股相對走弱加劇)，負斜率=正在收斂(相對走強/
    回補中)——注意rolling_dip訊號本身要求進場前有一段落後段+收斂過半，
    所以理論上這裡多數應該是負斜率(收斂中)，測的是"收斂快慢"這個連續量。"""
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

    enriched = []
    n_missing = 0
    for t in sub:
        stock_closes = load_minute_closes(con, t["stock_id"], t["entry_day"])
        bench_closes = load_minute_closes(con, BENCH, t["entry_day"])
        if len(stock_closes) < 30 or len(bench_closes) < 30:
            n_missing += 1
            continue
        dev = deviation_series(stock_closes, bench_closes)
        slope = entry_window_slope(dev, t["entry_minute"])
        if slope is None:
            n_missing += 1
            continue
        enriched.append({**t, "dev_slope": slope})
    con.close()
    print(f"可比對: {len(enriched)}/{len(sub)}筆 (缺{n_missing}筆)\n")

    print("=== 進場前15分鐘離均差斜率 vs 後續報酬 ===")
    enriched_sorted = sorted(enriched, key=lambda t: (t["entry_day"], t["entry_minute"]))
    n_train = int(len(enriched_sorted) * 0.7)
    train, test = enriched_sorted[:n_train], enriched_sorted[n_train:]
    for label, group in [("全樣本", enriched_sorted), ("train(前70%)", train), ("test(後30%)", test)]:
        xs = np.array([t["dev_slope"] for t in group])
        ys = np.array([t["ret"] for t in group])
        ic, pval = spearmanr(xs, ys)
        print(f"{label}: n={len(group)} IC={ic:.3f} p={pval:.3f} "
              f"斜率分布[{xs.min():.3f},{xs.max():.3f}] median={np.median(xs):.3f}")

    print("\n=== Permutation test（全樣本） ===")
    xs = np.array([t["dev_slope"] for t in enriched_sorted])
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

    print("\n=== 快慢收斂分組對照（斜率中位數切兩半） ===")
    med = np.median(xs)
    fast = [t["ret"] for t in enriched_sorted if t["dev_slope"] < med]
    slow = [t["ret"] for t in enriched_sorted if t["dev_slope"] >= med]
    print(f"斜率較負(收斂較快/較陡)組: n={len(fast)} 均報酬={np.mean(fast):+.2f}%")
    print(f"斜率較高(收斂較慢/較平)組: n={len(slow)} 均報酬={np.mean(slow):+.2f}%")


if __name__ == "__main__":
    main()
