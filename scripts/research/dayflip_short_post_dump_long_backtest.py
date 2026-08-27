#!/usr/bin/env python3
"""dayflip-short post-dump 做多（個股期貨）—— 正式回測，非單純事後統計.

規則（全部由前幾輪研究得出，見 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl）：
  進場：T0+1（分點訊號隔日沖倒貨/放空進場日）當天，用因果反轉訊號（離當日目前
        最低點≥15分鐘且反彈≥1.5%）進場做多個股期貨；當天沒觸發則收盤才進場。
  出場：固定持有 N 個交易日後，用期貨收盤價出場（N 由 walk-forward 訓練期挑，
        候選 {3, 5}——這兩個窗口是先前研究裡少數扣掉大盤/日層級聚合後仍顯著的）。
  成本：個股期貨來回 5bps（跟同一份研究 GAPUP_SHORT_SIZING.md 的假設一致）。
  部位：1口/筆，跟 dayflip-short 空單同規格，直接可比。

Walk-forward：74個不同trade_date按時間排序，前70%當訓練期（挑N），後30%當樣本外
測試期（只跑訓練期選出的N，不再調參）——避免用同一份資料選參數又拿來驗證。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_long_backtest.py
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

ROUND_TRIP_COST_PCT = 0.05  # 5bps，來回
CANDIDATE_HOLD_DAYS = (3, 5)
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15
LOT_SHARES = 2000  # 個股期貨每口股數（跟 src/order/dayflip_short_order.py 一致）


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_entry_price(con: sqlite3.Connection, stock_id: str, t01: str) -> tuple[float, str] | None:
    """因果反轉訊號進場價；沒觸發則收盤價進場。回傳 (股票現貨進場價, 進場方式)。"""
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


def simulate_trade(
    fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float, hold_days: int
) -> dict | None:
    """entry_frac_of_close：進場現貨價 / 當天現貨收盤價 —— 用來把股票進場點換算到期貨進場價
    （因為因果訊號的進場時點只有現貨1分K，期貨快取只有日頻；用同一天現貨的『進場價相對收盤的
    比例』去調整期貨收盤價，近似期貨在同一時點的價格——期貨跟現貨當日同步性極高，見
    dayflip_short_futures_long_verify.py 的 basis 檢查，相關係數0.987+）。"""
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t01 not in dates:
        return None
    i0 = dates.index(t01)
    if i0 + hold_days >= len(dates):
        return None
    fut_close_t01 = float(m[t01][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac_of_close
    exit_date = dates[i0 + hold_days]
    fut_exit = float(m[exit_date][1])
    if fut_exit <= 0:
        return None
    raw_ret_pct = (fut_exit / fut_entry - 1) * 100
    net_ret_pct = raw_ret_pct - ROUND_TRIP_COST_PCT
    pnl_ntd_per_lot = (fut_exit - fut_entry) * LOT_SHARES - (ROUND_TRIP_COST_PCT / 100 * fut_entry * LOT_SHARES)
    return {"exit_date": exit_date, "raw_ret_pct": raw_ret_pct, "net_ret_pct": net_ret_pct,
            "pnl_ntd_per_lot": pnl_ntd_per_lot}


def metrics(rets: list[float]) -> dict:
    arr = np.array(rets)
    n = len(arr)
    if n == 0:
        return {"n": 0}
    win_rate = float(np.mean(arr > 0))
    mean_ret = float(arr.mean())
    std_ret = float(arr.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    gains = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    profit_factor = float(gains / losses) if losses > 0 else float("inf")
    # 2026-08-08：刻意不報 equity curve / 累積報酬 / max drawdown——不管是逐筆
    # 加總還是逐筆複利，都得假設「全部資金依序、不重疊投入單筆交易」，但這批
    # 交易在同一個 22 天測試期就有 81 筆（很多天同一天多筆），資金/保證金重疊
    # 使用是常態，這個假設嚴重不成立，算出來的數字（曾經算出 -87% 回檔、也曾
    # 算出 +1975% 累積報酬）都是誤導。要有真的資金曲線，得先做真的部位/保證金
    # 排程模擬，不是這支腳本的範圍——只報不需要資金假設就成立的逐筆統計。
    return {
        "n": n, "win_rate": win_rate, "mean_ret_pct": mean_ret, "std_ret_pct": std_ret,
        "sharpe_like": sharpe_like, "profit_factor": profit_factor,
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
        entry_frac = entry_price / day_close
        prepared.append({"stock": sid, "trade_date": t01, "entry_kind": entry_kind, "entry_frac": entry_frac})

    print(f"=== dayflip-short post-dump 做多（個股期貨）正式回測 ===")
    print(f"候選: {len(prepared)}/{len(trades)}（缺1分K或現貨收盤價的剔除）\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    print(f"Walk-forward split: 訓練期 {len(train_dates)} 天（{dates_sorted[0]}~{dates_sorted[split_idx-1]}）"
          f"· 樣本外測試期 {len(test_dates)} 天（{dates_sorted[split_idx]}~{dates_sorted[-1]}）\n")

    print("--- 訓練期：{3,5}日持有比較，挑較好的一組 ---")
    train_results = {}
    for h in CANDIDATE_HOLD_DAYS:
        rets = []
        for p in prepared:
            if p["trade_date"] not in train_dates:
                continue
            sim = simulate_trade(fut_cache, p["stock"], p["trade_date"], p["entry_frac"], h)
            if sim:
                rets.append(sim["net_ret_pct"])
        m = metrics(rets)
        train_results[h] = m
        print(f"  持有{h}日: n={m.get('n',0)} 勝率={m.get('win_rate',0)*100:.0f}% "
              f"平均淨報酬={m.get('mean_ret_pct',0):+.3f}% sharpe_like={m.get('sharpe_like',float('nan')):.3f} "
              f"profit_factor={m.get('profit_factor',0):.2f}")

    best_h = max(CANDIDATE_HOLD_DAYS, key=lambda h: train_results[h].get("sharpe_like", -999) or -999)
    print(f"\n  訓練期挑出持有天數 = {best_h} 日（sharpe_like最高）")

    print(f"\n--- 樣本外測試期：只跑持有{best_h}日（不再調參）---")
    test_rets = []
    test_detail = []
    for p in prepared:
        if p["trade_date"] not in test_dates:
            continue
        sim = simulate_trade(fut_cache, p["stock"], p["trade_date"], p["entry_frac"], best_h)
        if sim:
            test_rets.append(sim["net_ret_pct"])
            test_detail.append({**p, **sim})
    test_m = metrics(test_rets)
    print(f"  n={test_m.get('n',0)} 勝率={test_m.get('win_rate',0)*100:.0f}% "
          f"平均淨報酬={test_m.get('mean_ret_pct',0):+.3f}% sharpe_like={test_m.get('sharpe_like',float('nan')):.3f} "
          f"profit_factor={test_m.get('profit_factor',0):.2f}")
    print(
        "  （不報 max drawdown/累積報酬：測試期22天內有81筆交易、資金與保證金重疊\n"
        "   使用是常態，沒做真的部位排程模擬前，任何單一資金曲線數字都會誤導。）"
    )

    entry_kinds = {}
    for p in prepared:
        if p["trade_date"] in test_dates:
            entry_kinds[p["entry_kind"]] = entry_kinds.get(p["entry_kind"], 0) + 1
    print(f"\n  測試期進場方式: {entry_kinds}")

    print(
        "\n⚠️ 重要限制：\n"
        "  1) 這是同一份 in-sample 訊號股清單的時間切分，不是全新的樣本外資料——\n"
        "     訓練/測試期共用同一套FROZEN_SPEC_V1.json篩選規則本身沒有再驗證。\n"
        "  2) 沒有模擬真實資金/保證金排程——同一天常有多筆訊號（測試期22天81筆），\n"
        "     實際能不能全部進場受總資金與保證金上限限制，這裡沒建模，只報逐筆\n"
        "     報酬統計，不報任何資金曲線/累積報酬數字。\n"
        "  3) 個股期貨5bps成本假設沿用GAPUP_SHORT_SIZING.md，實際滑價未實測。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-futures-formal-backtest",
        ts="2026-08-08",
        params={
            "hold_days_candidates": list(CANDIDATE_HOLD_DAYS), "chosen_hold_days": best_h,
            "cost_bps_roundtrip": ROUND_TRIP_COST_PCT * 100, "train_test_split": 0.7,
        },
        n_observations=test_m.get("n", 0),
        metric_name="oos_sharpe_like",
        metric_value=test_m.get("sharpe_like", float("nan")),
        status="kept" if test_m.get("n", 0) >= 10 and test_m.get("mean_ret_pct", 0) > 0 else "rejected",
        source=__file__,
        notes=(
            f"正式回測（walk-forward, 因果進場訊號, 個股期貨5bps成本）。訓練期選出持有{best_h}日。"
            f"樣本外(n={test_m.get('n',0)})：勝率{test_m.get('win_rate',0)*100:.0f}%，"
            f"平均淨報酬{test_m.get('mean_ret_pct',0):+.3f}%，sharpe_like={test_m.get('sharpe_like',float('nan')):.3f}。"
            "非全新樣本外資料、權益曲線非真實資金模擬，詳見腳本輸出限制說明。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "formal-backtest", "walk-forward"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


if __name__ == "__main__":
    main()
