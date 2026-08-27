#!/usr/bin/env python3
"""dayflip-short post-dump 做多——移動停利（trailing stop）掃描.

跟固定停利（一觸價就全出）不同：移動停利讓獲利部位持續參與上漲，只在價格從
「進場後最高收盤」回檔達門檻時才出場——目的是解決上次停利研究的問題（固定停利
在強動能延續期把大部分報酬讓掉）。

規則：進場後每天追蹤「進場以來最高期貨收盤價」(peak)；當天收盤價比 peak 回檔
≥ trail_pct 就出場；上限 MAX_HOLD_DAYS 天還沒觸發回檔就強制出場（時間停損兜底）。
只用日頻期貨收盤（跟前幾輪一致，沒有期貨自己的1分K）。

Walk-forward：跟之前的正式回測/停利sweep同一個70/30時間切分。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_trailing_stop_sweep.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

ROUND_TRIP_COST_PCT = 0.05
MAX_HOLD_DAYS = 10
TRAIL_CANDIDATES_PCT = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
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


def simulate_trailing(
    fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float, trail_pct: float | None,
) -> dict | None:
    """trail_pct=None 時純時間停損(MAX_HOLD_DAYS日)當基準線。"""
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t01 not in dates:
        return None
    i0 = dates.index(t01)
    if i0 + MAX_HOLD_DAYS >= len(dates):
        return None
    fut_close_t01 = float(m[t01][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac_of_close

    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback_pct = (peak - px) / peak * 100
        triggered = trail_pct is not None and pullback_pct >= trail_pct
        if triggered or h == MAX_HOLD_DAYS:
            raw_ret = (px / fut_entry - 1) * 100
            net_ret = raw_ret - ROUND_TRIP_COST_PCT
            return {
                "hold_days": h, "raw_ret_pct": raw_ret, "net_ret_pct": net_ret,
                "triggered": triggered, "peak_ret_pct": (peak / fut_entry - 1) * 100,
            }
    return None


def metrics(records: list[dict]) -> dict:
    if not records:
        return {"n": 0}
    rets = np.array([r["net_ret_pct"] for r in records])
    holds = np.array([r["hold_days"] for r in records])
    peaks = np.array([r["peak_ret_pct"] for r in records])
    n = len(rets)
    win_rate = float(np.mean(rets > 0))
    mean_ret = float(rets.mean())
    std_ret = float(rets.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    gains = rets[rets > 0].sum()
    losses = -rets[rets < 0].sum()
    profit_factor = float(gains / losses) if losses > 0 else float("inf")
    avg_hold = float(holds.mean())
    # 「有沒有拉高峰後回吐太多」——峰值報酬 vs 實際出場報酬的差距
    give_back = float((peaks - rets).mean())
    return {
        "n": n, "win_rate": win_rate, "mean_ret_pct": mean_ret, "std_ret_pct": std_ret,
        "sharpe_like": sharpe_like, "profit_factor": profit_factor, "avg_hold_days": avg_hold,
        "avg_peak_ret_pct": float(peaks.mean()), "avg_giveback_pct": give_back,
    }


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        entry = find_entry_price(con, sid, t01)
        if entry is None:
            continue
        entry_price, entry_kind = entry
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        prepared.append({"stock": sid, "trade_date": t01, "entry_frac": entry_price / day_close})

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    def run_group(dates_set, trail):
        recs = []
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            r = simulate_trailing(fut_cache, p["stock"], p["trade_date"], p["entry_frac"], trail)
            if r:
                recs.append(r)
        return metrics(recs)

    print(f"=== 移動停利 sweep（訓練期，MAX_HOLD_DAYS={MAX_HOLD_DAYS}）===\n")
    base = run_group(train_dates, None)
    print(
        f"[純時間停損{MAX_HOLD_DAYS}日，無移動停利] n={base.get('n',0)} 勝率={base.get('win_rate',0)*100:.0f}% "
        f"平均淨報酬={base.get('mean_ret_pct',0):+.3f}% sharpe_like={base.get('sharpe_like',float('nan')):.3f} "
        f"平均持有={base.get('avg_hold_days',0):.2f}日"
    )
    print()

    train_by_trail = {}
    for trail in TRAIL_CANDIDATES_PCT:
        m = run_group(train_dates, trail)
        train_by_trail[trail] = m
        print(
            f"移動停利{trail:.0f}%: n={m.get('n',0)} 勝率={m.get('win_rate',0)*100:.0f}% "
            f"平均淨報酬={m.get('mean_ret_pct',0):+.3f}% sharpe_like={m.get('sharpe_like',float('nan')):.3f} "
            f"平均持有={m.get('avg_hold_days',0):.2f}日 平均峰值報酬={m.get('avg_peak_ret_pct',0):+.3f}% "
            f"平均回吐={m.get('avg_giveback_pct',0):.3f}% profit_factor={m.get('profit_factor',0):.2f}"
        )

    best_trail = max(TRAIL_CANDIDATES_PCT, key=lambda tr: train_by_trail[tr].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出：移動停利 {best_trail:.0f}%（sharpe_like最高）")

    print(f"\n=== 樣本外測試期：移動停利{best_trail:.0f}% vs 純時間停損{MAX_HOLD_DAYS}日 對照 ===\n")
    test_base = run_group(test_dates, None)
    test_trail = run_group(test_dates, best_trail)
    print(
        f"[純時間停損{MAX_HOLD_DAYS}日] n={test_base.get('n',0)} 勝率={test_base.get('win_rate',0)*100:.0f}% "
        f"平均淨報酬={test_base.get('mean_ret_pct',0):+.3f}% sharpe_like={test_base.get('sharpe_like',float('nan')):.3f}"
    )
    print(
        f"[移動停利{best_trail:.0f}%]   n={test_trail.get('n',0)} 勝率={test_trail.get('win_rate',0)*100:.0f}% "
        f"平均淨報酬={test_trail.get('mean_ret_pct',0):+.3f}% sharpe_like={test_trail.get('sharpe_like',float('nan')):.3f} "
        f"平均持有={test_trail.get('avg_hold_days',0):.2f}日"
    )

    print("\n--- 事後檢查：全部移動停利門檻在樣本外的實際表現 ---")
    for trail in TRAIL_CANDIDATES_PCT:
        m = run_group(test_dates, trail)
        print(
            f"  移動停利{trail:.0f}%: n={m.get('n',0)} 平均淨報酬={m.get('mean_ret_pct',0):+.3f}% "
            f"sharpe_like={m.get('sharpe_like',float('nan')):.3f} 平均持有={m.get('avg_hold_days',0):.2f}日"
        )

    print(
        "\n⚠️ 限制：同前幾輪——日頻期貨收盤判斷(非盤中即時)、同一份in-sample訊號股\n"
        "   清單時間切分、沒做資金/保證金排程模擬、5bps成本未經滑價實測。"
    )

    survives = test_trail.get("mean_ret_pct", 0) > test_base.get("mean_ret_pct", 0)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-trailing-stop-sweep",
        ts="2026-08-09",
        params={"trail_candidates_pct": list(TRAIL_CANDIDATES_PCT), "max_hold_days": MAX_HOLD_DAYS, "chosen_trail_pct": best_trail},
        n_observations=test_trail.get("n", 0),
        metric_name="oos_mean_ret_pct_vs_baseline",
        metric_value=test_trail.get("mean_ret_pct", float("nan")) - test_base.get("mean_ret_pct", float("nan")),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"移動停利sweep，訓練期挑{best_trail:.0f}%。樣本外(n={test_trail.get('n',0)})："
            f"平均淨報酬{test_trail.get('mean_ret_pct',0):+.3f}% vs 基準線(純時間停損"
            f"{MAX_HOLD_DAYS}日){test_base.get('mean_ret_pct',0):+.3f}%——"
            f"{'贏過' if survives else '沒贏過'}基準線。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "trailing-stop"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
