#!/usr/bin/env python3
"""dayflip-short post-dump 做多——停利點 vs 固定持有期，哪個好？多少停利點最好？

使用者問題：設定目標停利點就賣可不可以、提早拿回資金、多少停利點最好。

跟固定3日持有比，加停利規則：進場後逐日檢查期貨收盤價，漲幅達門檻就出場（用
futures_daily_cache.json的日收盤，不是即時盤中價，所以是保守估計——實際盤中觸價
出場理論上比這裡算的更好，因為不用等到收盤）；超過上限天數(5日)還沒觸價就用
時間停損出場。

同時報「資金效率」：平均持有天數、以及「淨報酬% / 平均持有天數」（capital-day
normalized），因為停利提早出場真正的價值是讓同一筆資金能接下一個訊號，不是單純
比較單筆報酬率高低。

Walk-forward 跟正式回測用同一個 70/30 時間切分——訓練期掃過一輪停利門檻，
挑資金效率最高的一組，樣本外期只跑那一組。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_takeprofit_sweep.py
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
MAX_HOLD_DAYS = 5  # 時間停損上限；前次正式回測3/5日都測過，5日給停利規則多一點空間觸價
TP_CANDIDATES_PCT = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
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


def simulate_tp(
    fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float, tp_pct: float | None,
) -> dict | None:
    """tp_pct=None 時純時間停損（MAX_HOLD_DAYS 日）當對照組。"""
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

    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        raw_ret = (px / fut_entry - 1) * 100
        hit_tp = tp_pct is not None and raw_ret >= tp_pct
        if hit_tp or h == MAX_HOLD_DAYS:
            net_ret = raw_ret - ROUND_TRIP_COST_PCT
            return {"hold_days": h, "raw_ret_pct": raw_ret, "net_ret_pct": net_ret, "hit_tp": hit_tp}
    return None


def metrics(records: list[dict]) -> dict:
    if not records:
        return {"n": 0}
    rets = np.array([r["net_ret_pct"] for r in records])
    holds = np.array([r["hold_days"] for r in records])
    n = len(rets)
    win_rate = float(np.mean(rets > 0))
    mean_ret = float(rets.mean())
    std_ret = float(rets.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    gains = rets[rets > 0].sum()
    losses = -rets[rets < 0].sum()
    profit_factor = float(gains / losses) if losses > 0 else float("inf")
    avg_hold = float(holds.mean())
    ret_per_capital_day = mean_ret / avg_hold if avg_hold > 0 else float("nan")
    tp_hit_rate = float(np.mean([r["hit_tp"] for r in records]))
    return {
        "n": n, "win_rate": win_rate, "mean_ret_pct": mean_ret, "std_ret_pct": std_ret,
        "sharpe_like": sharpe_like, "profit_factor": profit_factor, "avg_hold_days": avg_hold,
        "ret_per_capital_day": ret_per_capital_day, "tp_hit_rate": tp_hit_rate,
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
        prepared.append({
            "stock": sid, "trade_date": t01, "entry_kind": entry_kind,
            "entry_frac": entry_price / day_close,
        })

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    print("=== 停利點 sweep（訓練期）：固定5日時間停損當基準線，跟各停利門檻比 ===\n")

    def run_group(dates_set, tp):
        recs = []
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            r = simulate_tp(fut_cache, p["stock"], p["trade_date"], p["entry_frac"], tp)
            if r:
                recs.append(r)
        return metrics(recs)

    base = run_group(train_dates, None)
    print(
        f"[純時間停損{MAX_HOLD_DAYS}日，無停利] n={base.get('n',0)} 勝率={base.get('win_rate',0)*100:.0f}% "
        f"平均淨報酬={base.get('mean_ret_pct',0):+.3f}% 平均持有={base.get('avg_hold_days',0):.2f}日 "
        f"報酬/資金天={base.get('ret_per_capital_day',0):+.4f}%/天 sharpe_like={base.get('sharpe_like',float('nan')):.3f}"
    )
    print()

    train_by_tp = {}
    for tp in TP_CANDIDATES_PCT:
        m = run_group(train_dates, tp)
        train_by_tp[tp] = m
        print(
            f"停利{tp:.0f}%: n={m.get('n',0)} 勝率={m.get('win_rate',0)*100:.0f}% "
            f"平均淨報酬={m.get('mean_ret_pct',0):+.3f}% 觸價率={m.get('tp_hit_rate',0)*100:.0f}% "
            f"平均持有={m.get('avg_hold_days',0):.2f}日 報酬/資金天={m.get('ret_per_capital_day',0):+.4f}%/天 "
            f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}"
        )

    best_tp = max(TP_CANDIDATES_PCT, key=lambda tp: train_by_tp[tp].get("ret_per_capital_day", -999) or -999)
    print(f"\n訓練期挑出：停利 {best_tp:.0f}%（報酬/資金天 最高——資金效率最佳，不是單筆報酬最高）")

    print(f"\n=== 樣本外測試期：停利{best_tp:.0f}% vs 純時間停損5日 對照 ===\n")
    test_base = run_group(test_dates, None)
    test_tp = run_group(test_dates, best_tp)
    print(
        f"[純時間停損5日] n={test_base.get('n',0)} 勝率={test_base.get('win_rate',0)*100:.0f}% "
        f"平均淨報酬={test_base.get('mean_ret_pct',0):+.3f}% 平均持有={test_base.get('avg_hold_days',0):.2f}日 "
        f"報酬/資金天={test_base.get('ret_per_capital_day',0):+.4f}%/天"
    )
    print(
        f"[停利{best_tp:.0f}%]     n={test_tp.get('n',0)} 勝率={test_tp.get('win_rate',0)*100:.0f}% "
        f"平均淨報酬={test_tp.get('mean_ret_pct',0):+.3f}% 觸價率={test_tp.get('tp_hit_rate',0)*100:.0f}% "
        f"平均持有={test_tp.get('avg_hold_days',0):.2f}日 報酬/資金天={test_tp.get('ret_per_capital_day',0):+.4f}%/天"
    )

    speedup = (
        test_base.get("avg_hold_days", 0) / test_tp.get("avg_hold_days", 1)
        if test_tp.get("avg_hold_days", 0) > 0 else float("nan")
    )
    print(f"\n資金周轉速度提升：{speedup:.2f}x（同一筆錢，停利版本理論上可以多做{speedup:.2f}倍筆交易）")

    print(
        "\n⚠️ 限制：\n"
        "  1) 停利判斷用期貨『日收盤』檢查，不是盤中即時觸價——實際上盤中價格衝過\n"
        "     門檻馬上出場理論上比這裡估的更快、更好（這裡是保守估計，不是樂觀高估）。\n"
        "  2) 跟正式回測一樣：同一份in-sample訊號股清單時間切分，不是全新樣本外資料；\n"
        "     沒做真的資金/保證金排程模擬。\n"
        "  3) 用『報酬/資金天』選最佳停利點，這個指標假設資金會立刻投入下一筆訊號——\n"
        "     如果訊號本來就不夠多、資金常常閒置，那提早出場不一定真的有用。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-takeprofit-sweep",
        ts="2026-08-08",
        params={"tp_candidates_pct": list(TP_CANDIDATES_PCT), "max_hold_days": MAX_HOLD_DAYS, "chosen_tp_pct": best_tp},
        n_observations=test_tp.get("n", 0),
        metric_name="oos_ret_per_capital_day",
        metric_value=test_tp.get("ret_per_capital_day", float("nan")),
        status="kept" if test_tp.get("n", 0) >= 10 and test_tp.get("mean_ret_pct", 0) > 0 else "rejected",
        source=__file__,
        notes=(
            f"停利點sweep，訓練期挑出{best_tp:.0f}%（報酬/資金天最高）。樣本外(n={test_tp.get('n',0)})："
            f"平均淨報酬{test_tp.get('mean_ret_pct',0):+.3f}%、平均持有{test_tp.get('avg_hold_days',0):.2f}日"
            f"（純時間停損版{test_base.get('avg_hold_days',0):.2f}日），資金周轉速度{speedup:.2f}x。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "take-profit", "capital-efficiency"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
