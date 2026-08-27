#!/usr/bin/env python3
"""設計E — 進場「後」beta校正離均差當早期預警(非進場前判斷)。

假設：現行5%移動停利只看個股自己的絕對價格，沒有排除「大盤也在跌、個股
只是跟跌」這種雜訊。這裡測試：進場後，如果beta校正過的離均差重新開始
擴大(個股相對大盤走弱的speed，扣掉beta影響後)，能不能比純價格的移動
停利更早偵測到問題。

方法：
1. 對每檔候選股票，用stock_daily_bars算「T0之前60個交易日」的beta——
   個股 vs 0050(大盤代理) 日報酬OLS回歸斜率，嚴格用trade_date < T0的
   資料(PIT安全，beta本身不是收盤後才公布的資料，用歷史日線沒有PIT問題，
   但仍嚴格排除T0當天，確保beta是"進場前已知"的資訊)。
2. 對每一筆訊號，用個股1分K + 0050 1分K，追蹤entry_minute之後到收盤
   (13:30，當天剩餘時間，不跨日)這段時間的beta校正離均差：
       dev_adj(t) = [個股相對entry_minute的累積報酬(%) -
                     beta * 大盤相對entry_minute的累積報酬(%)]
   這是用beta直接校正單一時點報酬，不是rolling window。
3. 候選特徵 = min(dev_adj(t))，t從entry_minute到當天收盤——代表進場後
   最糟的「扣掉beta後」相對弱勢程度。
4. 跟該筆交易最終報酬(ret，多天交易的最終結果)做Spearman IC，
   walk-forward(前70%train/後30%test，依entry_day+entry_minute排序)+
   permutation test(3000次重抽)。

PIT: beta嚴格用T0之前(不含T0)的日線；1分K本身即時無PIT問題；
entry_minute之後、收盤前的資料才拿來算特徵，跟訊號觸發時序一致(這是「進場後
早期預警」設計，特徵本身發生在entry之後，不是用未來資訊預測entry——
但要誠實承認：這個特徵在entry_minute當下並不可得，是「進場後才逐步觀察
到」的資訊，等同於盤中停利/停損規則的替代品，不是進場篩選訊號)。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_post_dump_long_intraday_beta_dev_earlywarning.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

import stock_db
from stock_db.kbar import load_kbar_day_bars

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
BENCH = "0050"
BETA_LOOKBACK_DAYS = 60
SESSION_CLOSE = "13:30"


def compute_beta(con: sqlite3.Connection, stock_id: str, t0: str) -> float | None:
    """個股 vs 0050 日報酬OLS beta，用T0之前(不含T0)最近BETA_LOOKBACK_DAYS個
    交易日的收盤價。PIT: 只用 trade_date < t0 的資料。"""
    rows_stock = con.execute(
        "SELECT trade_date, close FROM stock_daily_bars "
        "WHERE stock_id=? AND source='finmind' AND trade_date < ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (stock_id, t0, BETA_LOOKBACK_DAYS + 1),
    ).fetchall()
    rows_bench = con.execute(
        "SELECT trade_date, close FROM stock_daily_bars "
        "WHERE stock_id=? AND source='finmind' AND trade_date < ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (BENCH, t0, BETA_LOOKBACK_DAYS + 1),
    ).fetchall()
    if len(rows_stock) < 30 or len(rows_bench) < 30:
        return None
    stock_map = {r[0]: r[1] for r in rows_stock}
    bench_map = {r[0]: r[1] for r in rows_bench}
    dates = sorted(set(stock_map) & set(bench_map))
    if len(dates) < 30:
        return None
    stock_px = [stock_map[d] for d in dates]
    bench_px = [bench_map[d] for d in dates]
    stock_ret = np.diff(stock_px) / np.array(stock_px[:-1])
    bench_ret = np.diff(bench_px) / np.array(bench_px[:-1])
    if np.std(bench_ret) < 1e-9:
        return None
    beta = float(np.cov(stock_ret, bench_ret)[0, 1] / np.var(bench_ret))
    return beta


def load_minute_closes(con: sqlite3.Connection, stock_id: str, day: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, day)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def post_entry_min_dev_adj(
    stock_closes: dict[str, float], bench_closes: dict[str, float], entry_minute: str, beta: float
) -> tuple[float | None, int]:
    """entry_minute(含)到收盤這段時間，逐分鐘算beta校正離均差，回傳最小值
    與樣本點數(含entry_minute本身，dev_adj(entry_minute)=0)。"""
    minutes = sorted(m for m in (set(stock_closes) & set(bench_closes)) if m >= entry_minute)
    if entry_minute not in stock_closes or entry_minute not in bench_closes or len(minutes) < 5:
        return None, 0
    s0 = stock_closes[entry_minute]
    b0 = bench_closes[entry_minute]
    devs = []
    for m in minutes:
        stock_cum = (stock_closes[m] / s0 - 1) * 100
        bench_cum = (bench_closes[m] / b0 - 1) * 100
        devs.append(stock_cum - beta * bench_cum)
    return float(min(devs)), len(devs)


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    sub = [t for t in trades if t["fgap"] >= 4.0]

    beta_cache: dict[tuple[str, str], float | None] = {}
    enriched = []
    n_no_beta = 0
    n_no_intraday = 0
    for t in sub:
        key = (t["stock_id"], t["t0"])
        if key not in beta_cache:
            beta_cache[key] = compute_beta(con, t["stock_id"], t["t0"])
        beta = beta_cache[key]
        if beta is None:
            n_no_beta += 1
            continue
        stock_closes = load_minute_closes(con, t["stock_id"], t["entry_day"])
        bench_closes = load_minute_closes(con, BENCH, t["entry_day"])
        if len(stock_closes) < 5 or len(bench_closes) < 5:
            n_no_intraday += 1
            continue
        min_dev, n_pts = post_entry_min_dev_adj(stock_closes, bench_closes, t["entry_minute"], beta)
        if min_dev is None or n_pts < 5:
            n_no_intraday += 1
            continue
        enriched.append({**t, "beta": beta, "post_entry_min_dev_adj": min_dev, "n_pts": n_pts})
    con.close()

    n_sub = len(sub)
    n_usable = len(enriched)
    print(f"候選(fgap>=4%): {n_sub}筆")
    print(f"缺beta(60日資料不足): {n_no_beta}筆")
    print(f"缺當日盤中資料(entry_minute之後不足5個對齊分鐘): {n_no_intraday}筆")
    print(f"可用: {n_usable}筆\n")

    betas = np.array([t["beta"] for t in enriched])
    print(f"beta分布: min={betas.min():.2f} median={np.median(betas):.2f} max={betas.max():.2f}\n")

    enriched_sorted = sorted(enriched, key=lambda t: (t["entry_day"], t["entry_minute"]))
    n_train = int(len(enriched_sorted) * 0.7)
    train, test = enriched_sorted[:n_train], enriched_sorted[n_train:]

    results = {}
    print("=== post_entry_min_dev_adj (beta校正離均差進場後最小值) vs 最終報酬 ===")
    for label, group in [("全樣本", enriched_sorted), ("train(前70%)", train), ("test(後30%)", test)]:
        xs = np.array([t["post_entry_min_dev_adj"] for t in group])
        ys = np.array([t["ret"] for t in group])
        ic, pval = spearmanr(xs, ys)
        results[label] = (float(ic), float(pval), len(group))
        print(
            f"{label}: n={len(group)} IC={ic:.3f} p={pval:.3f} "
            f"特徵分布[{xs.min():.3f},{xs.max():.3f}] median={np.median(xs):.3f}"
        )

    print("\n=== Permutation test（全樣本，3000次重抽） ===")
    xs_full = np.array([t["post_entry_min_dev_adj"] for t in enriched_sorted])
    ys_full = np.array([t["ret"] for t in enriched_sorted])
    real_ic, _ = spearmanr(xs_full, ys_full)
    rng = np.random.default_rng(20260811)
    perm_ics = []
    for _ in range(3000):
        shuffled = rng.permutation(ys_full)
        perm_ic, _ = spearmanr(xs_full, shuffled)
        perm_ics.append(abs(perm_ic))
    perm_p = float(np.mean(np.array(perm_ics) >= abs(real_ic)))
    print(f"real IC={real_ic:.3f}  permutation p={perm_p:.3f}")

    print("\n=== 早期預警分組對照（特徵中位數切兩半） ===")
    med = np.median(xs_full)
    worse = [t["ret"] for t in enriched_sorted if t["post_entry_min_dev_adj"] < med]
    better = [t["ret"] for t in enriched_sorted if t["post_entry_min_dev_adj"] >= med]
    print(f"進場後最糟beta校正離均差更負(相對走弱更嚴重)組: n={len(worse)} 均報酬={np.mean(worse):+.2f}%")
    print(f"進場後最糟beta校正離均差較不負(相對走弱較輕)組: n={len(better)} 均報酬={np.mean(better):+.2f}%")

    print("\n=== 摘要(供StructuredOutput) ===")
    print(json.dumps({"n_usable": n_usable, "results": results, "permutation_p": perm_p}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
