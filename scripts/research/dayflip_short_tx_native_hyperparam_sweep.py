#!/usr/bin/env python3
"""dayflip-short post-dump 做多——用真台指期(TX)1分K原生重掃「滾動相對弱勢」訊號的三個超參數.

背景：rolling_relative_dip 訊號的三個超參數 rolling_window_min=15、
lag_threshold_pct=0.3、confirm_minutes=10，原本是用 0050 ETF 當大盤代理
walk-forward sweep 調出來的（當時還不知道有真台指期(TX)1分K可用）。後來兩支
獨立腳本（tx-real-rolling-dip-signal-v1／v2）改用真 TX 1分K重建同一組固定
超參數(15/0.3/10)，樣本外 sharpe_like 分別是 0.335、0.371——確認了訊號在真
資料上依然成立，但兩者都只是「沿用 0050 調出來的超參數」，從未在真 TX 資料
上原生重掃過超參數本身。本腳本補這一塊：對 rolling_window_min ×
lag_threshold_pct × confirm_minutes 做完整 4×5×3=60 格網格 walk-forward
sweep，全程只用真 TX 1分K（不碰 0050 代理），檢驗原本挑的 15/0.3/10 是否
已經接近原生最優、還是原生調參能找到明顯更好的組合。

訊號規則（與 v1/v2 相同，僅超參數改為可變）：
  1) 用個股 1 分K + 台指期(TX) 1 分K，各自算「過去 rolling_window_min 分鐘」
     報酬率（個股收盤/window分鐘前收盤 - 1，TX同理），兩者相減 = rolling_lag
     （負值 = 這段窗口個股漲比 TX 少，局部相對弱勢痕跡）。
  2) 找當天 rolling_lag 最負、且低於門檻(-lag_threshold_pct%) 的那個時間點
     （worst point，全天全域最小值）。
  3) 要求 worst point 之後、真實時鐘 1~confirm_minutes 分鐘內，第一個
     rolling_lag 回升到超過 worst_val 的一半（負值的一半）的分鐘，即為訊號
     分鐘（回吐幅度收斂過半，確認局部拋壓正在被吸收）。
  4) 進場價 = 訊號分鐘的個股收盤價。

出場：移動停利 5%（進場後最高日收盤價回檔 ≥5% 出場，否則 10 個交易日時間
停損），沿用既有 `futures_daily_cache.json`（日頻期貨收盤；訊號是分鐘級，
出場是日頻級，兩個解析度接在一起，跟 v1/v2 做法一致）。

Walk-forward 方法：221 筆交易涵蓋 74 個相異 trade_date，依日期時序切
70% train / 30% test。**只在訓練期**對 60 格逐一計算：要求訓練期訊號數
n>=30 才列入候選（本研究線先前一輪曾用僅 4 筆訓練樣本選門檻、樣本外崩潰，
這裡照抄同一條護欄，避免重蹈覆轍）。候選中挑訓練期 sharpe_like（平均淨
報酬/淨報酬標準差，淨報酬=期貨報酬-5bps來回成本）最高的一組，**只**在
未碰過的測試期評估一次，報 n / 勝率 / 平均淨報酬 / sharpe_like /
profit_factor，並附日聚集(day-clustered)穩健性版（74天只有這麼多天，逐筆
統計會誇大顯著性）。

資料源：
  - 個股/大盤日內 1 分K：主 DB stock_db.DEFAULT_DB_PATH，經
    stock_db.kbar.load_kbar_day_bars()（finmind 優先、yahoo補洞，避免
    stock_kbar_1m 表 finmind/yahoo 兩源量級不同造成的重複列問題——直接
    SELECT 不會處理這個，必須走這個 helper）。
  - 真台指期(TX) 1 分K：GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/bars.sqlite，
    source='tx_1m_tick_built_582d'、sess='day'（日盤 ≈08:45-13:44），跟主
    DB 是不同的 sqlite 檔案，唯讀開啟。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_tx_native_hyperparam_sweep.py
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial, load_trials

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TX_BARS_DB = DATA_DIR / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
MIN_TRAIN_N = 30

ROLLING_WINDOW_MIN_GRID = (10, 15, 20, 30)
LAG_THRESHOLD_PCT_GRID = (0.2, 0.3, 0.5, 0.8, 1.0)
CONFIRM_MINUTES_GRID = (5, 10, 15)

BASELINE_TOPIC_IDS = ("tx-real-rolling-dip-signal-v1", "tx-real-rolling-dip-signal-v2")
BASELINE_PARAMS = (15, 0.3, 10)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    """個股當日 1 分K 收盤價，"HH:MM" -> close，限定股票交易時段 09:00-13:30。"""
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def load_tx_minute_closes(tx_con: sqlite3.Connection, t01: str) -> dict[str, float]:
    """真台指期日盤 1 分K 收盤價，"HH:MM" -> close。"""
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND sess='day' AND day=? AND c IS NOT NULL AND c>0",
        (TX_SOURCE, t01),
    ).fetchall()
    return {t[:5]: float(c) for t, c in rows}


def _minute_to_int(hhmm: str) -> int:
    h, m = hhmm[:5].split(":")
    return int(h) * 60 + int(m)


def _int_to_minute(v: int) -> str:
    return f"{v // 60:02d}:{v % 60:02d}"


def compute_rolling_lag(
    stock_closes: dict[str, float], tx_closes: dict[str, float], window_min: int,
) -> dict[str, float] | None:
    """個股與 TX 各自算過去 window_min 分鐘報酬率之差，"HH:MM" -> rolling_lag(%)。
    anchor 用真實時鐘分鐘 m-window_min 去查表；任一邊缺該分鐘就整根跳過（不做
    前值填補），跟 v1/v2 一致的實作選擇。"""
    common = sorted(set(stock_closes) & set(tx_closes), key=_minute_to_int)
    if len(common) < 50:
        return None
    common_set = set(common)
    lag: dict[str, float] = {}
    for m in common:
        m_int = _minute_to_int(m)
        anchor = _int_to_minute(m_int - window_min)
        if anchor not in common_set:
            continue
        stock_ret = (stock_closes[m] / stock_closes[anchor] - 1) * 100
        tx_ret = (tx_closes[m] / tx_closes[anchor] - 1) * 100
        lag[m] = stock_ret - tx_ret
    return lag or None


def find_signal(
    rolling_lag: dict[str, float],
    stock_closes: dict[str, float],
    lag_threshold_pct: float,
    confirm_minutes: int,
) -> tuple[str, float] | None:
    """worst point = 全天低於門檻且最負的單一時間點（全域最小值）；訊號分鐘 =
    worst point 之後、真實時鐘 1~confirm_minutes 分鐘內，第一個 rolling_lag
    回升超過 worst_val*0.5 的分鐘。"""
    lag_minutes = sorted(rolling_lag, key=_minute_to_int)
    worst_m, worst_val = None, 0.0
    for m in lag_minutes:
        if rolling_lag[m] < -lag_threshold_pct and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_m = m
    if worst_m is None:
        return None
    worst_int = _minute_to_int(worst_m)
    candidates = [m for m in lag_minutes if 0 < _minute_to_int(m) - worst_int <= confirm_minutes]
    for m in candidates:  # already time-sorted
        if rolling_lag[m] > worst_val * 0.5:
            return m, stock_closes[m]
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
    if fut_entry <= 0:
        return None
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


def day_clustered_metrics(records: list[tuple[str, float]]) -> dict:
    """同一 trade_date 的淨報酬取平均，壓成「一天一個觀察值」的穩健性檢查。"""
    by_date: dict[str, list[float]] = {}
    for d, r in records:
        by_date.setdefault(d, []).append(r)
    day_means = [float(np.mean(v)) for v in by_date.values()]
    return metrics(day_means)


def prepare_trades(con: sqlite3.Connection, tx_con: sqlite3.Connection, trades: list[dict]) -> tuple[list[dict], dict]:
    prepared = []
    skipped = {"no_tx": 0, "no_stock": 0, "no_close": 0}
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_stock_minute_closes(con, sid, t01)
        tx_closes = load_tx_minute_closes(tx_con, t01)
        if len(tx_closes) < 50:
            skipped["no_tx"] += 1
            continue
        if len(stock_closes) < 50:
            skipped["no_stock"] += 1
            continue
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            skipped["no_close"] += 1
            continue
        prepared.append({
            "stock": sid, "trade_date": t01, "day_close": day_close,
            "stock_closes": stock_closes, "tx_closes": tx_closes,
            "rolling_lag_by_window": {},
        })
    return prepared, skipped


def run_combo(
    prepared: list[dict], fut_cache: dict, dates_set: set,
    window_min: int, lag_threshold_pct: float, confirm_minutes: int,
) -> tuple[dict, dict, int]:
    recs: list[float] = []
    day_recs: list[tuple[str, float]] = []
    no_signal = 0
    for p in prepared:
        if p["trade_date"] not in dates_set:
            continue
        rolling_lag = p["rolling_lag_by_window"].get(window_min)
        if window_min not in p["rolling_lag_by_window"]:
            rolling_lag = compute_rolling_lag(p["stock_closes"], p["tx_closes"], window_min)
            p["rolling_lag_by_window"][window_min] = rolling_lag
        if rolling_lag is None:
            no_signal += 1
            continue
        sig = find_signal(rolling_lag, p["stock_closes"], lag_threshold_pct, confirm_minutes)
        if sig is None:
            no_signal += 1
            continue
        _, px = sig
        r = simulate_trailing(fut_cache, p["stock"], p["trade_date"], px / p["day_close"])
        if r is not None:
            recs.append(r)
            day_recs.append((p["trade_date"], r))
    return metrics(recs), day_clustered_metrics(day_recs), no_signal


def load_baseline_from_registry() -> list[dict]:
    trials = load_trials("dayflip_short_gapup_short")
    return [t for t in trials if t.get("topic_id") in BASELINE_TOPIC_IDS]


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared, skipped = prepare_trades(con, tx_con, trades)

    print("=== TX真台指期1分K原生超參數重掃：rolling_window_min × lag_threshold_pct × confirm_minutes ===")
    print(
        f"可分析: {len(prepared)}/{len(trades)} "
        f"(TX資料不足跳過={skipped['no_tx']}, 個股分K不足跳過={skipped['no_stock']}, "
        f"無日收盤跳過={skipped['no_close']})\n"
    )

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    print(
        f"日期切分（依 74 個相異 trade_date 時序切 70/30）：train {len(train_dates)} 天 "
        f"({min(train_dates)}~{max(train_dates)}) / test {len(test_dates)} 天 "
        f"({min(test_dates)}~{max(test_dates)})\n"
    )

    grid = [
        (w, th, cm)
        for w in ROLLING_WINDOW_MIN_GRID
        for th in LAG_THRESHOLD_PCT_GRID
        for cm in CONFIRM_MINUTES_GRID
    ]
    print(f"訓練期網格 sweep：{len(grid)} 格（window×threshold×confirm = "
          f"{len(ROLLING_WINDOW_MIN_GRID)}×{len(LAG_THRESHOLD_PCT_GRID)}×{len(CONFIRM_MINUTES_GRID)}）\n")

    train_results = []
    for w, th, cm in grid:
        m, dm, nosig = run_combo(prepared, fut_cache, train_dates, w, th, cm)
        eligible = m.get("n", 0) >= MIN_TRAIN_N
        train_results.append({
            "window": w, "threshold": th, "confirm": cm,
            "n": m.get("n", 0), "no_signal": nosig,
            "win_rate": m.get("win_rate", float("nan")),
            "mean_ret_pct": m.get("mean_ret_pct", float("nan")),
            "sharpe_like": m.get("sharpe_like", float("nan")),
            "profit_factor": m.get("profit_factor", float("nan")),
            "day_n": dm.get("n", 0),
            "day_sharpe_like": dm.get("sharpe_like", float("nan")),
            "eligible": eligible,
        })

    def sort_key(r: dict) -> float:
        s = r["sharpe_like"]
        return s if r["eligible"] and s == s else -999.0  # NaN-safe

    train_results_sorted = sorted(train_results, key=sort_key, reverse=True)

    print("--- 訓練期全 60 格 sweep 結果（依 sharpe_like 排序，*=樣本數<30 不列入候選）---")
    header = (
        f"{'window':>6} {'thresh%':>7} {'confirm':>7} {'n':>4} {'winrate':>7} "
        f"{'mean_ret%':>9} {'sharpe':>7} {'pf':>6} {'day_n':>5} {'day_sharpe':>10}"
    )
    print(header)
    for r in train_results_sorted:
        flag = "" if r["eligible"] else "*"
        print(
            f"{r['window']:>6} {r['threshold']:>7.1f} {r['confirm']:>7} {r['n']:>4}{flag:<0} "
            f"{r['win_rate']*100 if r['win_rate']==r['win_rate'] else float('nan'):>6.0f}% "
            f"{r['mean_ret_pct']:>+9.3f} {r['sharpe_like']:>7.3f} {r['profit_factor']:>6.2f} "
            f"{r['day_n']:>5} {r['day_sharpe_like']:>10.3f}"
        )

    eligible_results = [r for r in train_results if r["eligible"]]
    if not eligible_results:
        eligible_results = train_results
        print(
            f"\n⚠️ 沒有任何組合在訓練期達到最小樣本數({MIN_TRAIN_N})，退回用全部 60 格挑選"
            f"(可信度較低)。"
        )
    best = max(eligible_results, key=lambda r: r["sharpe_like"] if r["sharpe_like"] == r["sharpe_like"] else -999)
    print(
        f"\n訓練期挑出（樣本數≥{MIN_TRAIN_N}才列入候選，{len(eligible_results)}/60 格合格）："
        f"rolling_window_min={best['window']}, lag_threshold_pct={best['threshold']:.1f}, "
        f"confirm_minutes={best['confirm']} (train n={best['n']}, train sharpe_like={best['sharpe_like']:.3f})"
    )

    print(
        f"\n--- 樣本外測試期：window={best['window']}, threshold={best['threshold']:.1f}%, "
        f"confirm={best['confirm']}min（全程真TX，唯一一次碰測試期）---\n"
    )
    test_m, test_dm, test_nosig = run_combo(
        prepared, fut_cache, test_dates, best["window"], best["threshold"], best["confirm"]
    )
    print(
        f"[逐筆] n={test_m.get('n', 0)} 無訊號={test_nosig} "
        f"勝率={test_m.get('win_rate', 0) * 100:.0f}% 平均淨報酬={test_m.get('mean_ret_pct', 0):+.3f}% "
        f"sharpe_like={test_m.get('sharpe_like', float('nan')):.3f} "
        f"profit_factor={test_m.get('profit_factor', 0):.2f}"
    )
    print(
        f"[日聚集穩健性版] n={test_dm.get('n', 0)} "
        f"勝率={test_dm.get('win_rate', 0) * 100:.0f}% 平均淨報酬={test_dm.get('mean_ret_pct', 0):+.3f}% "
        f"sharpe_like={test_dm.get('sharpe_like', float('nan')):.3f} "
        f"profit_factor={test_dm.get('profit_factor', 0):.2f}"
    )

    print("\n--- 對照：既有 tx-real-rolling-dip-signal v1/v2（固定沿用0050調出來的15/0.3/10）---")
    baseline_records = load_baseline_from_registry()
    if not baseline_records:
        print("⚠️ 在 trial registry 找不到 tx-real-rolling-dip-signal-v1/v2 紀錄，無法比較。")
        baseline_best_sharpe = float("nan")
    else:
        for rec in baseline_records:
            print(
                f"  {rec['topic_id']}: n={rec.get('n_observations')} "
                f"{rec.get('metric_name')}={rec.get('metric_value'):.4f} status={rec.get('status')}"
            )
        baseline_best_sharpe = max(rec.get("metric_value", float("-inf")) for rec in baseline_records)

    baseline_sharpes_str = "/".join(f"{r.get('metric_value', float('nan')):.3f}" for r in baseline_records)
    chosen_params = (best["window"], best["threshold"], best["confirm"])
    matches_baseline_params = chosen_params == BASELINE_PARAMS
    test_sharpe = test_m.get("sharpe_like", float("nan"))
    survives = (test_m.get("mean_ret_pct", -999) or -999) > 0 and test_m.get("n", 0) > 0

    if matches_baseline_params:
        verdict = (
            f"原生 TX sweep 挑出的最佳組合就是原本的 15/0.3/10——0050 代理調出的超參數在真"
            f"TX 資料上原生重掃後仍是最優，沒有找到明顯更好的組合。"
        )
    elif survives and baseline_best_sharpe == baseline_best_sharpe and test_sharpe > baseline_best_sharpe:
        verdict = (
            f"原生 TX sweep 找到不同於 15/0.3/10 的組合"
            f"(window={best['window']}, threshold={best['threshold']:.1f}%, confirm={best['confirm']}min)，"
            f"樣本外 sharpe_like={test_sharpe:.3f} 優於既有 v1/v2 紀錄的 {baseline_best_sharpe:.3f}——"
            f"值得注意但仍是單一 walk-forward split 的結果，未經多重比較校正，不宜直接視為'找到更優超參數'"
            f"的定論，建議之後用 DSR / 多組 split 再驗證。"
        )
    elif survives:
        verdict = (
            f"原生 TX sweep 挑出不同於 15/0.3/10 的組合"
            f"(window={best['window']}, threshold={best['threshold']:.1f}%, confirm={best['confirm']}min)，"
            f"但樣本外 sharpe_like={test_sharpe:.3f} 並未優於既有 v1/v2 的 {baseline_best_sharpe:.3f}——"
            f"原本 15/0.3/10 的選擇看起來已經接近原生最優，重掃沒有帶來實質改善。"
        )
    else:
        verdict = (
            f"原生 TX sweep 挑出的組合在樣本外未能維持正報酬（sharpe_like={test_sharpe:.3f}）——"
            f"訓練期最佳未必能穩健推廣，維持既有 15/0.3/10 作為採用值，本次 sweep 結果不建議取代。"
        )
    print(f"\n=== 結論 ===\n{verdict}")

    status = "rejected" if not survives else ("kept" if matches_baseline_params or not (
        baseline_best_sharpe == baseline_best_sharpe and test_sharpe > baseline_best_sharpe
    ) else "superseded")

    n_eligible = len(eligible_results)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="tx-native-hyperparameter-sweep",
        ts="2026-08-09",
        params={
            "grid_rolling_window_min": list(ROLLING_WINDOW_MIN_GRID),
            "grid_lag_threshold_pct": list(LAG_THRESHOLD_PCT_GRID),
            "grid_confirm_minutes": list(CONFIRM_MINUTES_GRID),
            "n_grid_cells": len(grid),
            "min_train_n": MIN_TRAIN_N,
            "chosen_rolling_window_min": best["window"],
            "chosen_lag_threshold_pct": best["threshold"],
            "chosen_confirm_minutes": best["confirm"],
            "benchmark": "tx_1m_tick_built_582d",
        },
        n_observations=test_m.get("n", 0),
        metric_name="oos_sharpe_like",
        metric_value=test_sharpe if test_sharpe == test_sharpe else float("nan"),
        status=status,
        source=__file__,
        notes=(
            f"全程用真TX(tx_1m_tick_built_582d)原生重掃 rolling_window_min×lag_threshold_pct×"
            f"confirm_minutes 60格網格（不碰0050代理），取代先前v1/v2『沿用0050調出的15/0.3/10』"
            f"的做法。訓練期({len(train_dates)}天)挑出n≥{MIN_TRAIN_N}候選中sharpe_like最高者："
            f"window={best['window']}, threshold={best['threshold']:.1f}%, confirm={best['confirm']}min "
            f"(train n={best['n']}, train sharpe_like={best['sharpe_like']:.3f}，{n_eligible}/60格合格)。"
            f"樣本外(n={test_m.get('n', 0)})：勝率{test_m.get('win_rate', 0) * 100:.0f}%、"
            f"平均淨報酬{test_m.get('mean_ret_pct', 0):+.3f}%、sharpe_like={test_sharpe:.3f}、"
            f"profit_factor={test_m.get('profit_factor', 0):.2f}。日聚集穩健性版：n={test_dm.get('n', 0)}、"
            f"sharpe_like={test_dm.get('sharpe_like', float('nan')):.3f}。對照既有v1/v2樣本外sharpe_like"
            f"({baseline_sharpes_str})。"
            f"{verdict}"
        ),
        extra_metrics={
            "train_n": best["n"],
            "train_sharpe_like": best["sharpe_like"],
            "test_win_rate": test_m.get("win_rate", float("nan")),
            "test_mean_ret_pct": test_m.get("mean_ret_pct", float("nan")),
            "test_profit_factor": test_m.get("profit_factor", float("nan")),
            "test_n": test_m.get("n", 0),
            "day_clustered_n": test_dm.get("n", 0),
            "day_clustered_sharpe_like": test_dm.get("sharpe_like", float("nan")),
            "day_clustered_mean_ret_pct": test_dm.get("mean_ret_pct", float("nan")),
            "n_eligible_cells": n_eligible,
            "matches_baseline_params": matches_baseline_params,
            "baseline_best_sharpe_like": baseline_best_sharpe if baseline_best_sharpe == baseline_best_sharpe else None,
        },
        tags=["dayflip-short", "post-dump", "long-side", "rolling-relative", "tx-real-futures", "hyperparameter-sweep"],
    )
    print(
        "\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl, "
        "topic_id=tx-native-hyperparameter-sweep)"
    )

    con.close()
    tx_con.close()


if __name__ == "__main__":
    main()
