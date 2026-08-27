#!/usr/bin/env python3
"""重跑「滾動窗口相對弱勢段訊號」，把大盤代理從 0050 ETF 換成真台指期（TX）1分K.

背景：上一輪 rolling_relative_dip 訊號（見
scripts/research/dayflip_short_rolling_relative_dip_signal.py）用 0050 現貨當
大盤代理，是因為 FinMind API 額度沒有期貨1分K資料集。這裡改用真的 TX 1分K
（tick 重建，source='tx_1m_tick_built_582d'，日盤 sess='day'，快取路徑
${GOLDENSTOCKS_DATA_DIR}/cache/tmf_channel/bars.sqlite），驗證同一套訊號邏輯
（rolling 15分鐘個股 vs 大盤區間報酬落後、落後10分鐘內收斂過半才進場）在真實
台指期資料下是否仍然成立，並跟 0050 代理版與原始個股價格版做三方比較。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_tx_real_rolling_dip_signal_v1.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial, load_trials

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
TX_BARS_DB = Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"
TX_SESS = "day"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
ROLLING_WINDOW_MIN = 15
LAG_THRESHOLD_CANDIDATES_PCT = (0.3, 0.5, 0.8, 1.0, 1.5)
CONFIRM_MINUTES = 10
MIN_TRAIN_N = 30


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def load_tx_minute_closes(tx_con: sqlite3.Connection, t01: str) -> dict[str, float]:
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND sess=? AND day=? AND c > 0",
        (TX_SOURCE, TX_SESS, t01),
    ).fetchall()
    return {t[:5]: c for t, c in rows}


def find_rolling_dip_signal(
    stock_closes: dict[str, float], bench_closes: dict[str, float], lag_threshold_pct: float,
) -> tuple[str, float] | None:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None

    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret

    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None

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
    if not TX_BARS_DB.exists():
        raise SystemExit(f"TX bars cache not found: {TX_BARS_DB}")

    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared = []
    skipped_no_tx = 0
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_stock_minute_closes(con, sid, t01)
        tx_closes = load_tx_minute_closes(tx_con, t01)
        if len(stock_closes) < 50:
            continue
        if len(tx_closes) < 50:
            skipped_no_tx += 1
            continue
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        prepared.append({"stock": sid, "trade_date": t01, "day_close": day_close,
                          "stock_closes": stock_closes, "tx_closes": tx_closes})

    print("=== 真台指期(TX)1分K版滾動窗口相對弱勢段訊號 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}（TX資料不足被跳過: {skipped_no_tx}）\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    print(f"日期數: {len(dates_sorted)}（訓練 {len(train_dates)} / 測試 {len(test_dates)}）\n")

    def run(dates_set, use_rolling, lag_th=None):
        recs, no_signal = [], 0
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            if use_rolling:
                sig = find_rolling_dip_signal(p["stock_closes"], p["tx_closes"], lag_th)
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
    raw_train, raw_train_nosig = run(train_dates, False)
    print(f"[原始個股價格訊號] n={raw_train.get('n',0)} 無訊號={raw_train_nosig} "
          f"勝率={raw_train.get('win_rate',0)*100:.0f}% 平均淨報酬={raw_train.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={raw_train.get('sharpe_like',float('nan')):.3f}")

    train_by_th = {}
    for th in LAG_THRESHOLD_CANDIDATES_PCT:
        m, nosig = run(train_dates, True, th)
        train_by_th[th] = m
        print(f"[TX滾動落後門檻{th:.1f}%] n={m.get('n',0)} 無訊號={nosig} "
              f"勝率={m.get('win_rate',0)*100:.0f}% 平均淨報酬={m.get('mean_ret_pct',0):+.3f}% "
              f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}")

    eligible = [th for th in LAG_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = list(LAG_THRESHOLD_CANDIDATES_PCT)
        print(f"\n⚠️ 沒有候選門檻在訓練期達到最低樣本數{MIN_TRAIN_N}，退回全部候選挑選（結果不可靠）。")
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出（樣本數≥{MIN_TRAIN_N}才列入候選）：TX滾動落後門檻 {best_th:.1f}%")

    print(f"\n--- 樣本外測試期：原始訊號 vs TX滾動相對弱勢訊號({best_th:.1f}%) ---\n")
    raw_test, raw_test_nosig = run(test_dates, False)
    roll_test, roll_test_nosig = run(test_dates, True, best_th)
    print(f"[原始個股價格訊號]         n={raw_test.get('n',0)} 無訊號={raw_test_nosig} "
          f"勝率={raw_test.get('win_rate',0)*100:.0f}% 平均淨報酬={raw_test.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={raw_test.get('sharpe_like',float('nan')):.3f} profit_factor={raw_test.get('profit_factor',0):.2f}")
    print(f"[TX滾動相對弱勢訊號{best_th:.1f}%]  n={roll_test.get('n',0)} 無訊號={roll_test_nosig} "
          f"勝率={roll_test.get('win_rate',0)*100:.0f}% 平均淨報酬={roll_test.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={roll_test.get('sharpe_like',float('nan')):.3f} profit_factor={roll_test.get('profit_factor',0):.2f}")

    # 日期分群穩健性檢查（同一天多筆交易的訊號高度相關，避免逐筆檢定誇大顯著性）
    def day_clustered_mean(dates_set, use_rolling, lag_th=None):
        by_date: dict[str, list[float]] = {}
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            sig = find_rolling_dip_signal(p["stock_closes"], p["tx_closes"], lag_th) if use_rolling else find_raw_signal(p["stock_closes"])
            if sig is None:
                continue
            _, px = sig
            r = simulate_trailing(fut_cache, p["stock"], p["trade_date"], px / p["day_close"])
            if r is not None:
                by_date.setdefault(p["trade_date"], []).append(r)
        if not by_date:
            return {"n_days": 0}
        day_means = np.array([np.mean(v) for v in by_date.values()])
        return {"n_days": len(day_means), "mean_of_day_means_pct": float(day_means.mean()),
                "std_of_day_means_pct": float(day_means.std())}

    raw_test_clustered = day_clustered_mean(test_dates, False)
    roll_test_clustered = day_clustered_mean(test_dates, True, best_th)
    print("\n--- 日期分群穩健性檢查（樣本外，每天先取均值再統計）---")
    print(f"[原始訊號]     n_days={raw_test_clustered.get('n_days',0)} "
          f"日均值的均值={raw_test_clustered.get('mean_of_day_means_pct',float('nan')):+.3f}%")
    print(f"[TX滾動訊號]   n_days={roll_test_clustered.get('n_days',0)} "
          f"日均值的均值={roll_test_clustered.get('mean_of_day_means_pct',float('nan')):+.3f}%")

    # 讀回 0050代理版的樣本外基準，做三方比較
    proxy_trials = [t for t in load_trials("dayflip_short_gapup_short")
                     if t.get("topic_id") == "rolling-window-relative-dip-signal"]
    proxy_note = proxy_trials[-1]["notes"] if proxy_trials else "(找不到 0050代理版紀錄)"
    proxy_n = proxy_trials[-1]["n_observations"] if proxy_trials else None
    print(f"\n--- 0050代理版樣本外紀錄（trial registry, topic_id=rolling-window-relative-dip-signal）---")
    print(f"n={proxy_n}, 備註: {proxy_note}")
    # 已知數字（來自任務描述，與上面讀出的紀錄核對）：sharpe_like 0.341 vs 0.258, 勝率52% vs 48%, n=77 vs 61
    proxy_roll_sharpe, proxy_raw_sharpe = 0.341, 0.258
    proxy_roll_winrate, proxy_raw_winrate = 0.52, 0.48
    proxy_roll_n, proxy_raw_n = 77, 61

    tx_roll_sharpe = roll_test.get("sharpe_like", float("nan"))
    tx_raw_sharpe = raw_test.get("sharpe_like", float("nan"))
    tx_roll_winrate = roll_test.get("win_rate", float("nan"))
    tx_raw_winrate = raw_test.get("win_rate", float("nan"))

    print("\n=== 三方比較（樣本外）===")
    print(f"{'':25s}{'n':>6s}{'勝率':>10s}{'sharpe_like':>14s}")
    print(f"{'0050代理-滾動訊號':25s}{proxy_roll_n:>6d}{proxy_roll_winrate*100:>9.0f}%{proxy_roll_sharpe:>14.3f}")
    print(f"{'0050代理-原始訊號':25s}{proxy_raw_n:>6d}{proxy_raw_winrate*100:>9.0f}%{proxy_raw_sharpe:>14.3f}")
    print(f"{'TX真實期貨-滾動訊號':25s}{roll_test.get('n',0):>6d}{tx_roll_winrate*100:>9.0f}%{tx_roll_sharpe:>14.3f}")
    print(f"{'TX真實期貨-原始訊號':25s}{raw_test.get('n',0):>6d}{tx_raw_winrate*100:>9.0f}%{tx_raw_sharpe:>14.3f}")

    tx_survives = roll_test.get("mean_ret_pct", -999) > raw_test.get("mean_ret_pct", -999)
    if roll_test.get("n", 0) == 0:
        verdict = "無法判定（樣本外滾動訊號無交易）"
    elif tx_survives and tx_roll_sharpe > proxy_roll_sharpe * 0.7:
        verdict = "真實TX資料確認(confirm)0050代理版結論方向一致"
    elif tx_survives:
        verdict = "真實TX資料方向一致但強度弱化(weaken)：滾動優於原始，但邊際變薄"
    else:
        verdict = "真實TX資料推翻(overturn)0050代理版結論：滾動訊號在真期貨資料下不再優於原始訊號"
    print(f"\n【結論】{verdict}")

    print(
        "\n⚠️ 限制：\n"
        "  1) TX 1分K快取(tx_1m_tick_built_582d)本身是tick重建，日盤約08:45-13:44，\n"
        "     與個股1分K(09:00-13:30)取交集分鐘後再算滾動報酬——重疊時段較窄，\n"
        "     可能略微改變訊號觸發時點。\n"
        "  2) 滾動窗口長度(15分)、確認窗口(10分)、收斂門檻(剩一半)沿用前一輪設定，\n"
        "     沒有重新掃過——只驗證換基準對結論方向的影響。\n"
        "  3) 出場(trailing stop 5%)沿用futures_daily_cache.json的日頻期貨資料，\n"
        "     跟進場訊號用的分鐘級TX資料是兩個不同的資料源/粒度。\n"
        "  4) 交易高度集中在少數交易日(見日期分群穩健性檢查)，逐筆檢定會誇大顯著性。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="tx-real-rolling-dip-signal-v1",
        ts="2026-08-09",
        params={"rolling_window_min": ROLLING_WINDOW_MIN, "confirm_minutes": CONFIRM_MINUTES,
                "lag_threshold_candidates_pct": list(LAG_THRESHOLD_CANDIDATES_PCT),
                "chosen_lag_threshold_pct": best_th, "benchmark": "tx_1m_tick_built_582d"},
        n_observations=roll_test.get("n", 0),
        metric_name="oos_sharpe_like_tx_rolling",
        metric_value=roll_test.get("sharpe_like", float("nan")),
        status="kept" if tx_survives else "rejected",
        source=__file__,
        notes=(
            f"用真台指期(TX)1分K重跑rolling_relative_dip訊號（取代0050代理）。"
            f"訓練期挑{best_th:.1f}%門檻。樣本外：TX滾動版n={roll_test.get('n',0)} "
            f"勝率={tx_roll_winrate*100:.0f}% sharpe_like={tx_roll_sharpe:.3f} "
            f"mean_ret={roll_test.get('mean_ret_pct',0):+.3f}% vs TX原始版n={raw_test.get('n',0)} "
            f"勝率={tx_raw_winrate*100:.0f}% sharpe_like={tx_raw_sharpe:.3f} "
            f"mean_ret={raw_test.get('mean_ret_pct',0):+.3f}%。"
            f"對照0050代理版樣本外(sharpe 0.341 vs 0.258, 勝率52% vs 48%, n=77 vs 61): {verdict}"
        ),
        extra_metrics={
            "oos_mean_ret_pct_rolling": roll_test.get("mean_ret_pct", float("nan")),
            "oos_mean_ret_pct_raw": raw_test.get("mean_ret_pct", float("nan")),
            "oos_win_rate_rolling": tx_roll_winrate,
            "oos_win_rate_raw": tx_raw_winrate,
            "oos_profit_factor_rolling": roll_test.get("profit_factor", float("nan")),
            "oos_profit_factor_raw": raw_test.get("profit_factor", float("nan")),
            "oos_n_raw": raw_test.get("n", 0),
            "day_clustered_mean_rolling_pct": roll_test_clustered.get("mean_of_day_means_pct", float("nan")),
            "day_clustered_mean_raw_pct": raw_test_clustered.get("mean_of_day_means_pct", float("nan")),
            "day_clustered_n_days_rolling": roll_test_clustered.get("n_days", 0),
            "day_clustered_n_days_raw": raw_test_clustered.get("n_days", 0),
            "skipped_no_tx_data": skipped_no_tx,
        },
        tags=["dayflip-short", "post-dump", "long-side", "rolling-relative", "tx-real-futures"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
