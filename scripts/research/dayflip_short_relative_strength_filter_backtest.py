#!/usr/bin/env python3
"""把「T0+1相對大盤強度」當篩選條件，疊加進移動停利規則——正式walk-forward驗證.

上一輪發現：T0+1全天相對0050表現越強（不是越弱）的訊號股，後續移動停利報酬越好
（Spearman rho=+0.306, p<0.0001，倒貨最輕1/3 vs 最兇1/3 p=0.0006）。

這裡的限制：相對強度要等T0+1當天收盤才能確認（不是盤中即時可算的），所以這個
篩選規則的進場點改成『T0+1收盤』，不是之前驗證過的盤中因果反轉訊號——兩者不能
直接套在一起用（因果訊號在收盤前就要決定進場，但這時候還不知道全天相對表現）。

規則：
  篩選：T0+1收盤時，個股當日報酬 - 0050當日報酬 ≥ 門檻（training期挑）
  進場：符合篩選才進場，T0+1收盤價
  出場：移動停利5%（沿用前幾輪驗證過的設定），最長10日

對照組：
  (a) 不篩選，一樣T0+1收盤進場（看篩選本身的邊際貢獻）
  (b) 原本驗證過的因果訊號版本（盤中訊號/收盤補進場，不篩選）——看『用相對強度
      篩選但收盤進場』跟『不篩選但盤中訊號進場』誰整體比較好

Walk-forward：70/30時間切分，訓練期挑篩選門檻，測試期驗證。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_relative_strength_filter_backtest.py
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
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15
BENCH = "0050"
FILTER_THRESHOLD_CANDIDATES_PCT = (2.0, 4.0, 6.0, 8.0, 10.0)  # 相對0050超額報酬門檻


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_signal_entry(con: sqlite3.Connection, stock_id: str, t01: str) -> tuple[float, str] | None:
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


def _close_on(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def _prev_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date<? AND source='finmind' AND close>0 "
        "ORDER BY trade_date DESC LIMIT 1",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def simulate_trailing(fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float) -> float | None:
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
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            return (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
    return None


def metrics(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    arr = np.array(rets)
    win_rate = float(np.mean(arr > 0))
    mean_ret = float(arr.mean())
    std_ret = float(arr.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    gains = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    pf = float(gains / losses) if losses > 0 else float("inf")
    return {"n": len(arr), "win_rate": win_rate, "mean_ret_pct": mean_ret, "sharpe_like": sharpe_like, "profit_factor": pf}


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        sig = find_signal_entry(con, sid, t01)
        day_close = _close_on(con, sid, t01)
        stock_prev = _prev_close(con, sid, t01)
        bench_now = _close_on(con, BENCH, t01)
        bench_prev = _prev_close(con, BENCH, t01)
        if sig is None or day_close is None or not stock_prev or not bench_now or not bench_prev:
            continue
        sig_price, sig_kind = sig
        relative_strength = (day_close / stock_prev - 1) * 100 - (bench_now / bench_prev - 1) * 100

        ret_close_entry = simulate_trailing(fut_cache, sid, t01, 1.0)  # 進場frac=1.0 → 收盤進場
        ret_signal_entry = simulate_trailing(fut_cache, sid, t01, sig_price / day_close)
        if ret_close_entry is None or ret_signal_entry is None:
            continue
        prepared.append({
            "stock": sid, "trade_date": t01, "relative_strength": relative_strength,
            "ret_close_entry": ret_close_entry, "ret_signal_entry": ret_signal_entry,
        })

    print(f"=== 相對強度篩選 + 移動停利5% —— walk-forward 正式驗證 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    def run(dates_set, threshold, use_signal_entry):
        recs = []
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            if threshold is not None and p["relative_strength"] < threshold:
                continue
            recs.append(p["ret_signal_entry"] if use_signal_entry else p["ret_close_entry"])
        return metrics(recs)

    print("--- 訓練期：掃篩選門檻(收盤進場) ---")
    base_train = run(train_dates, None, False)
    print(f"[不篩選,收盤進場] n={base_train.get('n',0)} 勝率={base_train.get('win_rate',0)*100:.0f}% "
          f"平均淨報酬={base_train.get('mean_ret_pct',0):+.3f}% sharpe_like={base_train.get('sharpe_like',float('nan')):.3f}")
    baseline_signal_train = run(train_dates, None, True)
    print(f"[不篩選,因果訊號進場(原規則)] n={baseline_signal_train.get('n',0)} "
          f"勝率={baseline_signal_train.get('win_rate',0)*100:.0f}% "
          f"平均淨報酬={baseline_signal_train.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={baseline_signal_train.get('sharpe_like',float('nan')):.3f}\n")

    train_by_th = {}
    for th in FILTER_THRESHOLD_CANDIDATES_PCT:
        m = run(train_dates, th, False)
        train_by_th[th] = m
        print(f"[篩選≥{th:.0f}%,收盤進場] n={m.get('n',0)} 勝率={m.get('win_rate',0)*100:.0f}% "
              f"平均淨報酬={m.get('mean_ret_pct',0):+.3f}% sharpe_like={m.get('sharpe_like',float('nan')):.3f}")

    # 2026-08-09 code review 修正：原本直接用sharpe_like挑門檻，選中≥10%（訓練期
    # 只有n=4筆）——4筆的sharpe_like本來就不穩定，是典型小樣本假訊號。加最小樣本數
    # 門檻(30筆)，避免選到統計上不可信的極端門檻。
    MIN_TRAIN_N = 30
    eligible = [th for th in FILTER_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = list(FILTER_THRESHOLD_CANDIDATES_PCT)
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出：相對強度門檻 ≥{best_th:.0f}%（sharpe_like最高）")

    print(f"\n=== 樣本外測試期：三種規則對照 ===\n")
    base_test = run(test_dates, None, False)
    signal_test = run(test_dates, None, True)
    filtered_test = run(test_dates, best_th, False)
    print(f"[不篩選,收盤進場(基準)]         n={base_test.get('n',0)} 勝率={base_test.get('win_rate',0)*100:.0f}% "
          f"平均淨報酬={base_test.get('mean_ret_pct',0):+.3f}% sharpe_like={base_test.get('sharpe_like',float('nan')):.3f} "
          f"profit_factor={base_test.get('profit_factor',0):.2f}")
    print(f"[不篩選,因果訊號進場(原規則)]    n={signal_test.get('n',0)} 勝率={signal_test.get('win_rate',0)*100:.0f}% "
          f"平均淨報酬={signal_test.get('mean_ret_pct',0):+.3f}% sharpe_like={signal_test.get('sharpe_like',float('nan')):.3f} "
          f"profit_factor={signal_test.get('profit_factor',0):.2f}")
    print(f"[篩選≥{best_th:.0f}%,收盤進場(新規則)] n={filtered_test.get('n',0)} 勝率={filtered_test.get('win_rate',0)*100:.0f}% "
          f"平均淨報酬={filtered_test.get('mean_ret_pct',0):+.3f}% sharpe_like={filtered_test.get('sharpe_like',float('nan')):.3f} "
          f"profit_factor={filtered_test.get('profit_factor',0):.2f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 相對強度篩選要等T0+1收盤才能確認，這個規則因此改成收盤進場，跟\n"
        "     原本盤中因果訊號進場（能比收盤更早進場）不是同一種操作方式，兩者\n"
        "     互斥、不能疊加——這是這個篩選規則本質上的取捨。\n"
        "  2) 篩選會讓成交數大幅下降（門檻越高篩越少），實際能不能維持足夠訊號\n"
        "     頻率要看門檻選多高。\n"
        "  3) 同前幾輪：同一份in-sample清單時間切分、沒做資金排程模擬。"
    )

    survives = filtered_test.get("mean_ret_pct", -999) > base_test.get("mean_ret_pct", -999)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="relative-strength-filter-formal-backtest",
        ts="2026-08-09",
        params={"filter_threshold_candidates_pct": list(FILTER_THRESHOLD_CANDIDATES_PCT),
                "chosen_threshold_pct": best_th, "benchmark": BENCH, "entry": "close_only"},
        n_observations=filtered_test.get("n", 0),
        metric_name="oos_mean_ret_pct_filtered_vs_unfiltered",
        metric_value=filtered_test.get("mean_ret_pct", float("nan")) - base_test.get("mean_ret_pct", float("nan")),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"相對強度篩選(≥{best_th:.0f}%)+收盤進場+移動停利5%，walk-forward正式驗證。"
            f"樣本外(n={filtered_test.get('n',0)})：篩選版{filtered_test.get('mean_ret_pct',0):+.3f}% vs "
            f"不篩選收盤版{base_test.get('mean_ret_pct',0):+.3f}% vs 原因果訊號版"
            f"{signal_test.get('mean_ret_pct',0):+.3f}%。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "relative-strength-filter", "formal-backtest"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
