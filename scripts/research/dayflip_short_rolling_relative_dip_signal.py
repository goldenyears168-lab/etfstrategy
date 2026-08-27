#!/usr/bin/env python3
"""用「某一段（局部區間）漲比大盤少」當賣壓痕跡，不是「自開盤累積落後大盤」.

使用者更精確的說法：不是個股跌破大盤（那是累積概念），是看『同樣在漲的情況下，
某一段時間漲得比大盤少』——這是局部（rolling window）相對弱勢，不是自開盤累積
的相對弱勢。上一輪用「自T0+1開盤起累積超額報酬的低點+反彈確認」沒有比較好，
這裡換一種更貼近使用者描述的做法：用滾動窗口（rolling window，例如過去15分鐘）
比較個股 vs 0050 的區間報酬，找『個股區間報酬明顯輸給大盤區間報酬』的那一段，
那段結束、輸的幅度開始收斂，就是賣壓正在消化的訊號。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_rolling_relative_dip_signal.py
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
BENCH = "0050"
ROLLING_WINDOW_MIN = 15  # 「某一段」的長度
LAG_THRESHOLD_CANDIDATES_PCT = (0.3, 0.5, 0.8, 1.0, 1.5)  # 區間報酬落後大盤多少算「有痕跡」
CONFIRM_MINUTES = 10  # 落後段結束後，再觀察這麼久確認收斂


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


def find_rolling_dip_signal(
    stock_closes: dict[str, float], bench_closes: dict[str, float], lag_threshold_pct: float,
) -> tuple[str, float] | None:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None

    # 每分鐘的『過去ROLLING_WINDOW_MIN分鐘 個股報酬 - 大盤報酬』
    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret  # 負值=這段個股漲比大盤少（賣壓痕跡）

    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None

    # 找最明顯的落後段（rolling_lag最負的點），要求之後CONFIRM_MINUTES分鐘落後幅度收斂
    worst_idx = None
    worst_val = 0.0
    for i, m in enumerate(lag_minutes):
        if rolling_lag[m] < -lag_threshold_pct and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_idx = i
    if worst_idx is None:
        return None

    worst_minute = lag_minutes[worst_idx]
    for i in range(worst_idx + 1, len(lag_minutes)):
        m = lag_minutes[i]
        elapsed = i - worst_idx
        if elapsed >= CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
            # 落後幅度收斂到剩不到一半，視為賣壓消化完成
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
        if (i - running_low_idx) >= 15 and (px / running_low - 1) * 100 >= 1.5:
            return m, px
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
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
        stock_closes = load_minute_closes(con, sid, t01)
        bench_closes = load_minute_closes(con, BENCH, t01)
        if len(stock_closes) < 50 or len(bench_closes) < 50:
            continue
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        prepared.append({"stock": sid, "trade_date": t01, "day_close": day_close,
                          "stock_closes": stock_closes, "bench_closes": bench_closes})

    print(f"=== 滾動窗口相對弱勢段訊號（'某段漲比大盤少'）===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    def run(dates_set, use_rolling, lag_th=None):
        recs, no_signal = [], 0
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            if use_rolling:
                sig = find_rolling_dip_signal(p["stock_closes"], p["bench_closes"], lag_th)
            else:
                sig = find_raw_signal(p["stock_closes"])
            if sig is None:
                no_signal += 1
                continue
            _, px = sig
            r = simulate_trailing(fut_cache, p["stock"], p["trade_date"], px / p["day_close"])
            if r is not None:
                recs.append(r)
        return metrics(recs), no_signal

    print("--- 訓練期 ---")
    raw_train, raw_nosig = run(train_dates, False)
    print(f"[原始個股價格訊號] n={raw_train.get('n',0)} 無訊號={raw_nosig} "
          f"勝率={raw_train.get('win_rate',0)*100:.0f}% 平均淨報酬={raw_train.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={raw_train.get('sharpe_like',float('nan')):.3f}")

    MIN_TRAIN_N = 30
    train_by_th = {}
    for th in LAG_THRESHOLD_CANDIDATES_PCT:
        m, nosig = run(train_dates, True, th)
        train_by_th[th] = m
        print(f"[滾動落後門檻{th:.1f}%] n={m.get('n',0)} 無訊號={nosig} "
              f"勝率={m.get('win_rate',0)*100:.0f}% 平均淨報酬={m.get('mean_ret_pct',0):+.3f}% "
              f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}")

    eligible = [th for th in LAG_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = list(LAG_THRESHOLD_CANDIDATES_PCT)
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出（樣本數≥{MIN_TRAIN_N}才列入候選）：滾動落後門檻 {best_th:.1f}%")

    print(f"\n--- 樣本外測試期：原始訊號 vs 滾動相對弱勢訊號({best_th:.1f}%) ---\n")
    raw_test, raw_test_nosig = run(test_dates, False)
    roll_test, roll_test_nosig = run(test_dates, True, best_th)
    print(f"[原始個股價格訊號]       n={raw_test.get('n',0)} 無訊號={raw_test_nosig} "
          f"勝率={raw_test.get('win_rate',0)*100:.0f}% 平均淨報酬={raw_test.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={raw_test.get('sharpe_like',float('nan')):.3f} profit_factor={raw_test.get('profit_factor',0):.2f}")
    print(f"[滾動相對弱勢訊號{best_th:.1f}%] n={roll_test.get('n',0)} 無訊號={roll_test_nosig} "
          f"勝率={roll_test.get('win_rate',0)*100:.0f}% 平均淨報酬={roll_test.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={roll_test.get('sharpe_like',float('nan')):.3f} profit_factor={roll_test.get('profit_factor',0):.2f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 用0050取代台指期（同前幾輪，FinMind無期貨1分K資料集）。\n"
        "  2) 滾動窗口長度(15分)、確認窗口(10分)、收斂門檻(剩一半)都是單次選定，\n"
        "     沒有各自掃過sweep——只驗證了『滾動局部落後』這個概念方向對不對，\n"
        "     還沒精細調參。\n"
        "  3) 同前幾輪：同一份in-sample清單時間切分、沒做資金排程模擬。"
    )

    survives = roll_test.get("mean_ret_pct", -999) > raw_test.get("mean_ret_pct", -999)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="rolling-window-relative-dip-signal",
        ts="2026-08-09",
        params={"rolling_window_min": ROLLING_WINDOW_MIN, "confirm_minutes": CONFIRM_MINUTES,
                "lag_threshold_candidates_pct": list(LAG_THRESHOLD_CANDIDATES_PCT),
                "chosen_lag_threshold_pct": best_th, "benchmark": BENCH},
        n_observations=roll_test.get("n", 0),
        metric_name="oos_mean_ret_pct_rolling_vs_raw",
        metric_value=roll_test.get("mean_ret_pct", float("nan")) - raw_test.get("mean_ret_pct", float("nan")),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"滾動窗口相對弱勢段訊號(使用者精確化的假說：局部區間漲比大盤少=賣壓痕跡)，"
            f"訓練期挑{best_th:.1f}%門檻。樣本外：滾動版{roll_test.get('mean_ret_pct',0):+.3f}% "
            f"vs 原始個股價格版{raw_test.get('mean_ret_pct',0):+.3f}%——"
            f"{'滾動版較好' if survives else '沒有改善'}。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "rolling-relative", "0050-proxy"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
