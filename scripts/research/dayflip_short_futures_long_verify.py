#!/usr/bin/env python3
"""dayflip-short：post-dump 做多訊號，換成用「個股期貨真實價格」驗證還成不成立.

使用者問題：如果收到訊號是個股期貨做多也是一樣嗎。

之前的分析全部用「標的股票」的現貨收盤價（stock_daily_bars）算報酬，但
dayflip-short 的空單其實是透過個股期貨執行——期貨跟現貨之間有 basis（隨融資成本/
除權息預期漂移），多日報酬不一定完全等於現貨報酬。這裡改用
reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json
（該策略自己維護的期貨日OHLCV+量快取，[open, close, high, low, volume_lots]）
直接算期貨版的遠期報酬，並比對期貨量能是否夠大讓多單真的能進出。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_futures_long_verify.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats

import stock_db
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
_BENCH = "0050"
FWD_HORIZONS = (1, 3, 5)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _close_on(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    rows = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        m = fut_cache.get(sid) or {}
        dates = sorted(m)
        if t01 not in dates:
            continue
        i0 = dates.index(t01)
        if i0 + max(FWD_HORIZONS) >= len(dates):
            continue
        fut_base_close = float(m[t01][1])
        if fut_base_close <= 0:
            continue

        stock_base = _close_on(con, sid, t01)
        if stock_base is None:
            continue

        bench_base = _close_on(con, _BENCH, t01)
        if bench_base is None:
            continue

        fwd_dates = dates[i0 + 1 : i0 + 1 + max(FWD_HORIZONS)]
        row = {"stock": sid, "trade_date": t01, "vol_t01_lots": float(m[t01][4])}
        ok = True
        for h in FWD_HORIZONS:
            fd = fwd_dates[h - 1]
            fut_fwd = float(m[fd][1])
            fut_vol = float(m[fd][4])
            stock_fwd = _close_on(con, sid, fd)
            bench_fwd = _close_on(con, _BENCH, fd)
            if fut_fwd <= 0 or stock_fwd is None or bench_fwd is None:
                ok = False
                break
            row[f"fut_ret_{h}d_pct"] = (fut_fwd / fut_base_close - 1) * 100
            row[f"stock_ret_{h}d_pct"] = (stock_fwd / stock_base - 1) * 100
            row[f"fut_excess_{h}d_pct"] = row[f"fut_ret_{h}d_pct"] - (bench_fwd / bench_base - 1) * 100
            row[f"fut_vol_{h}d_lots"] = fut_vol
        if ok:
            rows.append(row)

    print(f"=== 期貨版 post-dump 做多驗證 ===")
    print(f"221筆交易中，期貨日快取(futures_daily_cache.json)可查且未來{max(FWD_HORIZONS)}天資料齊全: {len(rows)}\n")

    print("--- 期貨報酬 vs 現貨報酬 是否一致（basis 檢查）---")
    for h in FWD_HORIZONS:
        fut = np.array([r[f"fut_ret_{h}d_pct"] for r in rows])
        stock = np.array([r[f"stock_ret_{h}d_pct"] for r in rows])
        diff = fut - stock
        corr = np.corrcoef(fut, stock)[0, 1]
        print(
            f"  +{h}日: 期貨-現貨報酬差 mean={diff.mean():+.3f}%±{diff.std():.3f}% · "
            f"相關係數={corr:.3f} (n={len(rows)})"
        )

    print("\n--- 期貨版超額報酬(已扣0050) ---")
    for h in FWD_HORIZONS:
        excess = np.array([r[f"fut_excess_{h}d_pct"] for r in rows])
        stat_fn = stats.wilcoxon if len(excess) < 500 else lambda a: stats.ttest_1samp(a, 0.0)
        _, p = stat_fn(excess)
        print(
            f"  +{h}日: mean={excess.mean():+.3f}±{excess.std():.3f} median={np.median(excess):+.3f} "
            f"(n={len(excess)}, %>0={np.mean(excess>0)*100:.0f}%) · p={p:.4f}"
        )

    print("\n--- 期貨流動性（口數，避免多單掛不進去/出不來）---")
    vol_t01 = np.array([r["vol_t01_lots"] for r in rows])
    print(
        f"  T0+1當天期貨成交量: mean={vol_t01.mean():.0f}口 median={np.median(vol_t01):.0f}口 "
        f"· 10%分位={np.percentile(vol_t01, 10):.0f}口"
    )
    thin = sum(1 for v in vol_t01 if v < 1000)
    print(f"  當天量<1000口的交易: {thin}/{len(rows)} ({thin/len(rows)*100:.0f}%)——量太薄，多單較難不打價")

    excess_5d = [r["fut_excess_5d_pct"] for r in rows]
    _, p5 = stats.wilcoxon(excess_5d)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="futures-native-long-verification",
        ts="2026-08-08",
        params={"horizons_days": list(FWD_HORIZONS), "source": "futures_daily_cache.json"},
        n_observations=len(rows),
        metric_name="fut_excess_ret_5d_mean_pct_vs_0050",
        metric_value=float(np.mean(excess_5d)),
        status="kept" if p5 < 0.05 else "rejected",
        source=__file__,
        notes=(
            f"用期貨自己的日OHLCV(不是現貨股價)重驗post-dump做多訊號，n={len(rows)}。"
            f"+5日期貨超額報酬p={p5:.4f}。同時檢查期貨量能是否足夠讓多單實際能進出。"
        ),
        tags=["dayflip-short", "futures-native", "long-side", "liquidity"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
