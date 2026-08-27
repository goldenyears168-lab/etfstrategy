#!/usr/bin/env python3
"""dayflip-short post-dump 做多——來回分段入場（多腳位），賺多次划不划算？

規則：
  第一腳：跟移動停利sweep一樣，因果反轉訊號進場（沒觸發用收盤），移動停利
          TRAIL_PCT（用trailing_stop_sweep驗證過、樣本外全數勝過基準線的5%）出場。
  再進場：出場後追蹤期貨日收盤，一旦「從出場後最高點回檔≥REENTRY_PULLBACK%，
          且當天收盤>前一天收盤（簡單的日頻反轉確認）」就視為再進場點；同樣用
          移動停利出場。最多 MAX_LEGS 腳，總窗口 WINDOW_DAYS 個交易日（原始訊號
          算起）內找不到再進場點就停止。
  比較：把同一個訊號的所有腳位淨報酬加總，跟「只做第一腳、抱好抱滿移動停利」
  的單腳版本比——多腳到底有沒有多賺，還是白忙一場（多付好幾次5bps成本）。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_multileg_reentry.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0  # 移動停利sweep驗證過、樣本外全數勝過基準線的區間中段
WINDOW_DAYS = 15  # 原始訊號起算的總觀察窗口
MAX_LEGS = 4
REENTRY_PULLBACK_PCT = 3.0
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_entry_price(con: sqlite3.Connection, stock_id: str, t01: str) -> tuple[float, str] | None:
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
        if (i - running_low_idx) >= MIN_MINUTES_OFF_LOW and (close / running_low - 1) * 100 >= REBOUND_THRESHOLD_PCT:
            return close, "intraday_signal"
    return bars[-1][2], "close_fallback"


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def simulate_multileg(m: dict, dates: list[str], i0: int, entry_frac_of_close: float) -> list[dict]:
    """回傳這個訊號在 WINDOW_DAYS 天內所有腳位的紀錄（>=1筆，第一腳一定會有）。"""
    legs = []
    fut_close_t01 = float(m[dates[i0]][1])
    if fut_close_t01 <= 0:
        return legs
    entry_px = fut_close_t01 * entry_frac_of_close
    entry_offset = 0  # 相對原始訊號日的交易日偏移

    while len(legs) < MAX_LEGS and entry_offset < WINDOW_DAYS:
        peak = entry_px
        exit_offset = None
        exit_px = None
        for h in range(entry_offset + 1, WINDOW_DAYS + 1):
            if i0 + h >= len(dates):
                break
            px = float(m[dates[i0 + h]][1])
            if px <= 0:
                break
            peak = max(peak, px)
            pullback = (peak - px) / peak * 100
            if pullback >= TRAIL_PCT or h == WINDOW_DAYS:
                exit_offset, exit_px = h, px
                break
        if exit_offset is None or exit_px is None:
            break
        raw_ret = (exit_px / entry_px - 1) * 100
        net_ret = raw_ret - ROUND_TRIP_COST_PCT
        legs.append({"entry_offset": entry_offset, "exit_offset": exit_offset, "net_ret_pct": net_ret})

        # 找下一個再進場點：從出場後開始，追蹤『出場後最高收盤』，回檔到位+今天收盤>昨天收盤
        if exit_offset >= WINDOW_DAYS:
            break
        reentry_peak = exit_px
        prev_px = exit_px
        found = False
        for h in range(exit_offset + 1, WINDOW_DAYS + 1):
            if i0 + h >= len(dates):
                break
            px = float(m[dates[i0 + h]][1])
            if px <= 0:
                break
            reentry_peak = max(reentry_peak, px)
            pullback = (reentry_peak - px) / reentry_peak * 100
            if pullback >= REENTRY_PULLBACK_PCT and px > prev_px:
                entry_px = px
                entry_offset = h
                found = True
                break
            prev_px = px
        if not found:
            break
    return legs


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
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    single_leg_totals, multi_leg_totals, n_legs_dist = [], [], []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        entry = find_entry_price(con, sid, t01)
        if entry is None:
            continue
        entry_price, _ = entry
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        entry_frac = entry_price / day_close

        m = fut_cache.get(sid) or {}
        dates = sorted(m)
        if t01 not in dates:
            continue
        i0 = dates.index(t01)
        if i0 + WINDOW_DAYS >= len(dates):
            continue

        legs = simulate_multileg(m, dates, i0, entry_frac)
        if not legs:
            continue
        multi_leg_totals.append(sum(leg["net_ret_pct"] for leg in legs))
        single_leg_totals.append(legs[0]["net_ret_pct"])
        n_legs_dist.append(len(legs))

    print(f"=== 多腳位來回進場 vs 單腳（移動停利{TRAIL_PCT:.0f}%）===")
    print(f"可分析: {len(multi_leg_totals)}/{len(trades)}（{WINDOW_DAYS}日窗口內至少完成第一腳）\n")

    from collections import Counter
    leg_counts = Counter(n_legs_dist)
    print("--- 每個訊號實際做了幾腳 ---")
    for k in sorted(leg_counts):
        print(f"  {k}腳: {leg_counts[k]} 個訊號 ({leg_counts[k]/len(n_legs_dist)*100:.0f}%)")

    print("\n--- 單腳（只做第一腳，抱好抱滿移動停利）---")
    onesample_report("單腳總淨報酬%", single_leg_totals)

    print(f"\n--- 多腳合計（最多{MAX_LEGS}腳，同一{WINDOW_DAYS}日窗口內）---")
    onesample_report("多腳合計淨報酬%", multi_leg_totals)

    diff = [m_ - s for m_, s in zip(multi_leg_totals, single_leg_totals)]
    print("\n--- 多腳 - 單腳 差異（正值=多做幾腳真的有多賺）---")
    onesample_report("多腳-單腳 差異%", diff)

    print(
        "\n⚠️ 限制：\n"
        "  1) 再進場規則是日頻簡化版（回檔到位+一天收紅），不是像第一腳那樣用1分K\n"
        "     因果訊號——第二腳以後的入場時機精確度比第一腳低。\n"
        "  2) 沒有做資金/保證金排程模擬；多腳位代表資金需要能連續動用，若前一腳\n"
        "     的資金被綁住到出場才能用於下一腳，實際能不能這樣操作要看保證金水位。\n"
        "  3) TRAIL_PCT/REENTRY_PULLBACK_PCT 用先前研究挑的中段值，沒有針對多腳\n"
        "     情境重新sweep+walk-forward驗證。"
    )

    n = len(multi_leg_totals)
    mean_diff = float(np.mean(diff)) if diff else float("nan")
    _, p_diff = stats.wilcoxon(diff) if n >= 10 else (None, float("nan"))
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-multileg-reentry",
        ts="2026-08-09",
        params={
            "trail_pct": TRAIL_PCT, "window_days": WINDOW_DAYS, "max_legs": MAX_LEGS,
            "reentry_pullback_pct": REENTRY_PULLBACK_PCT,
        },
        n_observations=n,
        metric_name="multileg_minus_singleleg_mean_pct",
        metric_value=mean_diff,
        status="kept" if mean_diff == mean_diff and mean_diff > 0 and p_diff < 0.05 else "rejected",
        source=__file__,
        notes=(
            f"多腳來回進場 vs 單腳移動停利，n={n}。多腳-單腳平均差異={mean_diff:+.3f}%，"
            f"p={p_diff:.4f}（未做資金排程模擬、再進場規則為日頻簡化版，見腳本輸出限制）。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "multileg", "reentry"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
