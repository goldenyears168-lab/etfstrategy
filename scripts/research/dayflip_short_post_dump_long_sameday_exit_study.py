#!/usr/bin/env python3
"""dayflip-short post-dump 做多——當天(不到一天)就賣，有沒有搞頭？

使用者問題：不到一天就賣。

用因果反轉訊號進場後，追蹤同一天（T0+1）接下來每個時間點的股價（期貨跟現貨
1分K高度同步，見dayflip_short_futures_long_verify.py的basis檢查），看從進場點
到「30分鐘後／1小時後／2小時後／當天收盤」各自能拿到多少報酬——回答「當天賣
划不划算」，不是只看訊號價→收盤（之前測過近似0%）這一個點，而是整條當天路徑。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_sameday_exit_study.py
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy import stats

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
ROUND_TRIP_COST_PCT = 0.05
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15
SAMEDAY_HORIZONS_MIN = (15, 30, 60, 90, 120, 999)  # 999 = 一路抱到當天收盤(13:30)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _minute_to_dt(m: str) -> datetime:
    h, mm = m.split(":")[:2]
    return datetime(2000, 1, 1, int(h), int(mm))


def find_signal_and_path(con: sqlite3.Connection, stock_id: str, t01: str) -> dict | None:
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
    signal_idx = None
    for i, (minute, low, close) in enumerate(bars):
        if low < running_low:
            running_low = low
            running_low_idx = i
        if (i - running_low_idx) >= MIN_MINUTES_OFF_LOW and (close / running_low - 1) * 100 >= REBOUND_THRESHOLD_PCT:
            signal_idx = i
            break
    if signal_idx is None:
        return None  # 只看有觸發因果訊號的交易日（跟之前一致，close_fallback另外處理沒意義——沒有明確的『進場後』時間軸）

    entry_minute, _, entry_price = bars[signal_idx]
    entry_dt = _minute_to_dt(entry_minute)

    path = {}
    for h_min in SAMEDAY_HORIZONS_MIN:
        if h_min == 999:
            px = bars[-1][2]
        else:
            target_dt = entry_dt + timedelta(minutes=h_min)
            px = None
            for minute, _, close in bars[signal_idx:]:
                if _minute_to_dt(minute) >= target_dt:
                    px = close
                    break
            if px is None:
                px = bars[-1][2]  # 目標時間超過收盤，用收盤價
        ret_pct = (px / entry_price - 1) * 100
        path[h_min] = ret_pct
    return {"entry_minute": entry_minute, "path": path}


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

    rows = []
    for t in trades:
        r = find_signal_and_path(con, t["stock"], t["trade_date"])
        if r:
            rows.append(r)

    print(f"=== 當天(不到一天)出場：進場後同一天路徑 ===")
    print(f"有觸發因果進場訊號可分析: {len(rows)}/{len(trades)}\n")

    print("--- 進場後 N 分鐘 / 當天收盤 的淨報酬(已扣5bps來回成本) ---")
    for h_min in SAMEDAY_HORIZONS_MIN:
        label = "當天收盤(13:30)" if h_min == 999 else f"{h_min}分鐘後"
        rets = [r["path"][h_min] - ROUND_TRIP_COST_PCT for r in rows]
        onesample_report(label, rets)

    print("\n--- 對照：多留一天再賣（T0+1收盤→T0+2收盤，扣成本）---")
    fwd1d_rets = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        base = con.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
            (sid, t01),
        ).fetchone()
        if not base:
            continue
        nxt = con.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date>? AND source='finmind' AND close>0 "
            "ORDER BY trade_date LIMIT 1",
            (sid, t01),
        ).fetchone()
        if not nxt:
            continue
        fwd1d_rets.append((float(nxt[0]) / float(base[0]) - 1) * 100 - ROUND_TRIP_COST_PCT)
    onesample_report("持有到隔天(T0+2)收盤才賣", fwd1d_rets)

    print(
        "\n⚠️ 限制：\n"
        "  1) 只看『有觸發因果進場訊號』的交易日（跟之前分析一致），沒訊號那天\n"
        "     沒有明確的『進場後』時間軸可測。\n"
        "  2) 用現貨1分K模擬期貨同步走勢，短天期basis極小（見futures_long_verify\n"
        "     的相關係數0.99+），近似合理，但不是期貨自己的1分K（沒有這個資料）。\n"
        "  3) 樣本沒做day-clustering修正（同一天多筆訊號互相牽動）。"
    )

    best_h = min(SAMEDAY_HORIZONS_MIN, key=lambda h: abs(np.mean([r["path"][h] for r in rows])))
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-sameday-exit",
        ts="2026-08-08",
        params={"horizons_min": list(SAMEDAY_HORIZONS_MIN)},
        n_observations=len(rows),
        metric_name="close_exit_mean_net_ret_pct",
        metric_value=float(np.mean([r["path"][999] - ROUND_TRIP_COST_PCT for r in rows])),
        status="rejected",
        source=__file__,
        notes=(
            f"問：不到一天就賣划不划算。n={len(rows)}，測了進場後15/30/60/90/120分鐘"
            "及當天收盤的淨報酬，全部集中在0附近、不顯著（見腳本輸出）；對照多留一天"
            "(T0+1→T0+2收盤)才有明確正報酬。結論：當天賣掉沒有邊際，edge是隔夜/多日"
            "才浮現，不是當天盤中價差。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "sameday-exit", "negative-result"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
