#!/usr/bin/env python3
"""dayflip-short：T0+1（隔日沖倒貨/放空進場日）當天能不能抓到「賣壓已盡」的訊號？
還是乾脆隔天(T0+2)再買就好？

使用者問題：可以如何知道當天被倒貨了，有跡象嗎，還是隔天再買來得及嗎。

兩段分析：
  (1) 盤中「反轉確認」訊號：只用「當下已發生」的資料（因果、無未來函數）——追蹤
      到目前為止看到的當日最低價，當股價已經離開那個低點一段時間(≥15分鐘)且
      反彈超過門檻(≥1.5%)，才算「反轉確認」。跟 dayflip_short_post_dump_bounce_study.py
      裡「事後才知道的低點」不同，這個是每一分鐘都可以即時判斷的規則。看：
      多少比例的交易日當天真的會觸發、觸發時間分布、從觸發點買進的後續報酬。
  (2) 等到 T0+2 收盤才買，報酬會不會比 T0+1 收盤就買差很多——回答「來不來得及」。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_intraday_capitulation_signal_study.py
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import stats

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
_BENCH = "0050"

_BUCKETS = [
    ("09:00", "09:30", "開盤09:00-09:30"),
    ("09:30", "10:30", "早盤09:30-10:30"),
    ("10:30", "12:00", "盤中10:30-12:00"),
    ("12:00", "13:00", "午盤12:00-13:00"),
    ("13:00", "13:30", "尾盤13:00-13:30"),
]
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15
FWD_HORIZONS = (1, 3, 5)


def bucket_of(minute: str) -> str | None:
    for lo, hi, label in _BUCKETS:
        if lo <= minute < hi:
            return label
    if minute == "13:30":
        return _BUCKETS[-1][2]
    return None


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_causal_reversal_signal(con: sqlite3.Connection, stock_id: str, t01: str) -> dict | None:
    """逐分鐘掃描，只用『到當下為止』看過的資料判斷——不偷看未來。"""
    raw = load_kbar_day_bars(con, stock_id, t01)
    bars = [
        (b.minute[:5], b.low, b.close)
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.low and b.low > 0 and b.close
    ]
    if len(bars) < 50:
        return None

    running_low = bars[0][1]
    running_low_idx = 0
    for i, (minute, low, close) in enumerate(bars):
        if low < running_low:
            running_low = low
            running_low_idx = i
        minutes_off_low = i - running_low_idx
        rebound_pct = (close / running_low - 1) * 100
        if minutes_off_low >= MIN_MINUTES_OFF_LOW and rebound_pct >= REBOUND_THRESHOLD_PCT:
            return {
                "signal_bucket": bucket_of(minute),
                "signal_minute": minute,
                "signal_price": close,
                "day_close": bars[-1][2],
            }
    return None


def _close_on(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def next_trading_dates(con: sqlite3.Connection, stock_id: str, after: str, n: int) -> list[str]:
    rows = con.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE stock_id=? AND trade_date>? AND source='finmind' AND close>0
        ORDER BY trade_date LIMIT ?
        """,
        (stock_id, after, n),
    ).fetchall()
    return [str(r[0]) for r in rows]


def onesample_report(name: str, vals: list[float]) -> float | None:
    arr = np.array([v for v in vals if v == v])
    if len(arr) < 10:
        print(f"  {name}: 樣本不足(n={len(arr)})，跳過")
        return None
    stat_fn = stats.wilcoxon if len(arr) < 500 else lambda a: stats.ttest_1samp(a, 0.0)
    _, p = stat_fn(arr)
    print(
        f"  {name}: mean={arr.mean():+.3f}±{arr.std():.3f} median={np.median(arr):+.3f} "
        f"(n={len(arr)}, %>0={np.mean(arr>0)*100:.0f}%) · p={p:.4f}"
    )
    return p


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print(f"=== (1) T0+1 盤中因果反轉訊號（低點後反彈≥{REBOUND_THRESHOLD_PCT}% 且已過{MIN_MINUTES_OFF_LOW}分鐘）===\n")
    sig_rows = []
    n_checked = 0
    for t in trades:
        raw = load_kbar_day_bars(con, t["stock"], t["trade_date"])
        n_bars = len([b for b in raw if "09:00" <= b.minute[:5] <= "13:30"])
        if n_bars < 50:
            continue
        n_checked += 1
        sig = find_causal_reversal_signal(con, t["stock"], t["trade_date"])
        if sig:
            sig["stock"] = t["stock"]
            sig["trade_date"] = t["trade_date"]
            sig_rows.append(sig)

    print(f"當天有足夠1分K可判斷: {n_checked}/{len(trades)}")
    print(f"當天觸發訊號: {len(sig_rows)}/{n_checked} ({len(sig_rows)/n_checked*100:.0f}%)\n")

    bucket_counts = Counter(r["signal_bucket"] for r in sig_rows)
    print("--- 訊號觸發時段分布 ---")
    for _, _, label in _BUCKETS:
        n = bucket_counts.get(label, 0)
        print(f"  {label}: {n} 筆 ({n/len(sig_rows)*100:.1f}%)")

    print("\n--- 從訊號觸發價 到 T0+1收盤：還能再抓到多少反彈 ---")
    ret_sig_to_close = [(r["day_close"] / r["signal_price"] - 1) * 100 for r in sig_rows]
    onesample_report("訊號價→T0+1收盤 報酬%", ret_sig_to_close)

    print("\n--- 從訊號觸發價 起算的多日遠期報酬（原始，未扣大盤，跟收盤基準比對用）---")
    sig_fwd = []
    for r in sig_rows:
        fwd_dates = next_trading_dates(con, r["stock"], r["trade_date"], max(FWD_HORIZONS))
        if len(fwd_dates) < max(FWD_HORIZONS):
            continue
        fwd_closes = [_close_on(con, r["stock"], d) for d in fwd_dates]
        if any(c is None for c in fwd_closes):
            continue
        row = {"trade_date": r["trade_date"]}
        for h in FWD_HORIZONS:
            row[f"fwd_{h}d"] = (fwd_closes[h - 1] / r["signal_price"] - 1) * 100
        sig_fwd.append(row)
    for h in FWD_HORIZONS:
        onesample_report(f"訊號價→T0+1+{h}日 報酬%", [r[f"fwd_{h}d"] for r in sig_fwd])

    print(f"\n=== (2) 乾脆等 T0+2 收盤才買，跟 T0+1 收盤就買比較 ===\n")
    t01_anchor, t02_anchor = [], []
    for t in trades:
        base01 = _close_on(con, t["stock"], t["trade_date"])
        if base01 is None:
            continue
        fwd01 = next_trading_dates(con, t["stock"], t["trade_date"], max(FWD_HORIZONS) + 1)
        if len(fwd01) < max(FWD_HORIZONS) + 1:
            continue
        t02_date = fwd01[0]
        base02 = _close_on(con, t["stock"], t02_date)
        if base02 is None:
            continue
        closes01 = [_close_on(con, t["stock"], d) for d in fwd01]
        if any(c is None for c in closes01):
            continue

        bench01 = _close_on(con, _BENCH, t["trade_date"])
        bench_dates = fwd01
        bench_closes = [_close_on(con, _BENCH, d) for d in bench_dates]
        if bench01 is None or any(c is None for c in bench_closes):
            continue

        for h in FWD_HORIZONS:
            r01 = (closes01[h - 1] / base01 - 1) * 100
            b01 = (bench_closes[h - 1] / bench01 - 1) * 100
            t01_anchor.append({"h": h, "excess": r01 - b01})
        for h in FWD_HORIZONS:
            idx = h  # T0+2 anchor, so day h forward from t02 = fwd01[h] (one further than t01's h)
            if idx >= len(closes01):
                continue
            r02 = (closes01[idx] / base02 - 1) * 100
            b02 = (bench_closes[idx] / bench_closes[0] - 1) * 100
            t02_anchor.append({"h": h, "excess": r02 - b02})

    for h in FWD_HORIZONS:
        print(f"[+{h}日窗口]")
        onesample_report("  T0+1收盤買進，超額報酬%", [r["excess"] for r in t01_anchor if r["h"] == h])
        onesample_report("  T0+2收盤才買，超額報酬%", [r["excess"] for r in t02_anchor if r["h"] == h])

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="intraday-capitulation-signal-vs-wait-t02",
        ts="2026-08-08",
        params={
            "rebound_threshold_pct": REBOUND_THRESHOLD_PCT,
            "min_minutes_off_low": MIN_MINUTES_OFF_LOW,
        },
        n_observations=len(sig_rows),
        metric_name="signal_hit_rate",
        metric_value=len(sig_rows) / n_checked if n_checked else float("nan"),
        status="kept",
        source=__file__,
        notes=(
            "問：T0+1當天能不能即時判斷倒貨已盡、還是等T0+2買就好。用因果(無偷看未來)"
            f"規則(離低點{MIN_MINUTES_OFF_LOW}分鐘+反彈{REBOUND_THRESHOLD_PCT}%)當日觸發率"
            f"{len(sig_rows)}/{n_checked}。詳細比較見腳本輸出（訊號觸發時段分布、"
            "訊號價到後續報酬、T0+1 vs T0+2進場超額報酬對照）。"
        ),
        tags=["dayflip-short", "intraday-signal", "entry-timing", "causal"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
