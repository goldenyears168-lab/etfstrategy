#!/usr/bin/env python3
"""dayflip-short post-dump 做多——用大盤(0050)校正找「真正的相對低點」.

使用者提醒：找相對低點要跟台指期校正才準。台指期本身沒有可回補的分K資料
（FinMind這個方案只有日頻期貨價+tick，沒有1分K期貨資料集，另外aggregate tick
是更大的工程），改用 0050（跟大盤同步性極高，前幾輪已驗證拿來當多日超額報酬
的基準）當校正對象，1分K覆蓋率已經是74/74（T0+1所有交易日）。

原本的因果訊號只看個股自己的價格走勢；這裡改成看「個股相對大盤的超額報酬」
走勢——個股自己的低點，如果那個時間點大盤也在跌，不算真的個股利空出盡，只是
跟著大盤跌；只有當個股「相對大盤」的表現觸底、然後相對反彈，才是真正籌碼面
（分點買超）在起作用的訊號。

新規則：追蹤「個股相對0050的累積超額報酬」（自T0+1開盤起算）的每分鐘最低點，
反彈幅度（相對超額報酬，不是個股自己的原始報酬）達門檻+已過一段時間，才算訊號
確認。用同一套移動停利5%+walk-forward，跟原本(純個股價格)版本比較。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_market_relative_entry_signal.py
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
MIN_MINUTES_OFF_LOW = 15
BENCH = "0050"
EXCESS_REBOUND_CANDIDATES_PCT = (0.5, 1.0, 1.5, 2.0, 3.0)
RAW_REBOUND_PCT = 1.5  # 舊版(純個股價格)訊號的門檻，當對照組


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def find_relative_signal(
    stock_closes: dict[str, float], bench_closes: dict[str, float], excess_threshold_pct: float,
) -> tuple[str, float] | None:
    common_minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(common_minutes) < 50:
        return None
    stock_open = stock_closes[common_minutes[0]]
    bench_open = bench_closes[common_minutes[0]]
    if stock_open <= 0 or bench_open <= 0:
        return None

    excess_series = []
    for m in common_minutes:
        stock_ret = (stock_closes[m] / stock_open - 1) * 100
        bench_ret = (bench_closes[m] / bench_open - 1) * 100
        excess_series.append((m, stock_ret - bench_ret))

    running_min = excess_series[0][1]
    running_min_idx = 0
    for i, (m, ex) in enumerate(excess_series):
        if ex < running_min:
            running_min = ex
            running_min_idx = i
        rebound = ex - running_min  # 已經是超額報酬的差，直接相減
        if (i - running_min_idx) >= MIN_MINUTES_OFF_LOW and rebound >= excess_threshold_pct:
            return m, stock_closes[m]
    return None


def find_raw_signal(stock_closes: dict[str, float]) -> tuple[str, float] | None:
    minutes = sorted(stock_closes)
    if len(minutes) < 50:
        return None
    running_low = stock_closes[minutes[0]]
    running_low_idx = 0
    for i, m in enumerate(minutes):
        px = stock_closes[m]
        if px < running_low:
            running_low = px
            running_low_idx = i
        if (i - running_low_idx) >= MIN_MINUTES_OFF_LOW and (px / running_low - 1) * 100 >= RAW_REBOUND_PCT:
            return m, px
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def simulate_trailing(fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float) -> dict | None:
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
            raw_ret = (px / fut_entry - 1) * 100
            return {"net_ret_pct": raw_ret - ROUND_TRIP_COST_PCT}
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
        stock_closes = load_minute_closes(con, sid, t01)
        bench_closes = load_minute_closes(con, BENCH, t01)
        if len(stock_closes) < 50 or len(bench_closes) < 50:
            continue
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        prepared.append({"stock": sid, "trade_date": t01, "day_close": day_close,
                          "stock_closes": stock_closes, "bench_closes": bench_closes})

    print(f"=== 個股原始價格訊號 vs 個股相對0050超額訊號 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    def run(dates_set, use_relative, excess_threshold=None):
        recs, no_signal, signal_minutes = [], 0, []
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            if use_relative:
                sig = find_relative_signal(p["stock_closes"], p["bench_closes"], excess_threshold)
            else:
                sig = find_raw_signal(p["stock_closes"])
            if sig is None:
                no_signal += 1
                continue
            minute, px = sig
            signal_minutes.append(minute)
            r = simulate_trailing(fut_cache, p["stock"], p["trade_date"], px / p["day_close"])
            if r:
                recs.append(r["net_ret_pct"])
        return metrics(recs), no_signal, signal_minutes

    print("--- 訓練期：原始個股訊號 vs 相對超額訊號(掃門檻) ---")
    raw_train, raw_nosig, _ = run(train_dates, False)
    print(f"[原始個股價格訊號] n={raw_train.get('n',0)} 無訊號跳過={raw_nosig} "
          f"勝率={raw_train.get('win_rate',0)*100:.0f}% 平均淨報酬={raw_train.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={raw_train.get('sharpe_like',float('nan')):.3f}")

    train_by_th = {}
    for th in EXCESS_REBOUND_CANDIDATES_PCT:
        m, nosig, _ = run(train_dates, True, th)
        train_by_th[th] = m
        print(f"[相對超額門檻{th:.1f}%] n={m.get('n',0)} 無訊號跳過={nosig} "
              f"勝率={m.get('win_rate',0)*100:.0f}% 平均淨報酬={m.get('mean_ret_pct',0):+.3f}% "
              f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}")

    best_th = max(EXCESS_REBOUND_CANDIDATES_PCT, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出：相對超額門檻 {best_th:.1f}%（sharpe_like最高）")

    print(f"\n--- 樣本外測試期：原始訊號 vs 相對超額訊號({best_th:.1f}%) ---\n")
    raw_test, raw_test_nosig, _ = run(test_dates, False)
    rel_test, rel_test_nosig, _ = run(test_dates, True, best_th)
    print(f"[原始個股價格訊號]   n={raw_test.get('n',0)} 無訊號跳過={raw_test_nosig} "
          f"勝率={raw_test.get('win_rate',0)*100:.0f}% 平均淨報酬={raw_test.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={raw_test.get('sharpe_like',float('nan')):.3f} profit_factor={raw_test.get('profit_factor',0):.2f}")
    print(f"[相對超額訊號{best_th:.1f}%]   n={rel_test.get('n',0)} 無訊號跳過={rel_test_nosig} "
          f"勝率={rel_test.get('win_rate',0)*100:.0f}% 平均淨報酬={rel_test.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={rel_test.get('sharpe_like',float('nan')):.3f} profit_factor={rel_test.get('profit_factor',0):.2f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 用0050取代台指期——FinMind這個方案沒有期貨1分K資料集，0050是既有\n"
        "     驗證過同步性夠高的替代品，不是台指期本身。\n"
        "  2) 相對超額訊號要求兩邊(個股+0050)同時有1分K資料，覆蓋率天然比原始版低。\n"
        "  3) 同前幾輪：同一份in-sample清單時間切分、沒做資金排程模擬。"
    )

    survives = rel_test.get("mean_ret_pct", -999) > raw_test.get("mean_ret_pct", -999)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="market-relative-entry-signal-vs-raw",
        ts="2026-08-09",
        params={"excess_threshold_candidates_pct": list(EXCESS_REBOUND_CANDIDATES_PCT),
                "chosen_excess_threshold_pct": best_th, "benchmark": BENCH},
        n_observations=rel_test.get("n", 0),
        metric_name="oos_mean_ret_pct_relative_vs_raw",
        metric_value=rel_test.get("mean_ret_pct", float("nan")) - raw_test.get("mean_ret_pct", float("nan")),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"個股相對0050超額報酬版反轉訊號 vs 原始個股價格版，訓練期挑{best_th:.1f}%門檻。"
            f"樣本外：相對版{rel_test.get('mean_ret_pct',0):+.3f}% vs 原始版"
            f"{raw_test.get('mean_ret_pct',0):+.3f}%——{'相對版較好' if survives else '沒有改善'}。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "market-relative", "0050-proxy"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
