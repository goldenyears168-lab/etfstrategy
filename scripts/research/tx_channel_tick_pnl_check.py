#!/usr/bin/env python3
"""tick_validation.py 發現快取1分K跟tick重新resample的1分K有實質落差（平均1.3pt/根、
單根最大38pt）。這支腳本直接測：用tick-derived bars重跑w233/多window組合的日盤訊號，
跟同一批天數、用快取bars跑出來的結果比較損益差多少——這才是真正回答『資料落差會不會
吃掉edge』的問題，不是只停在『資料有差異』這個描述性層次。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_days  # noqa: E402
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_channel_tick_validation import load_cached_day_bars, load_front_month_ticks, resample_to_1min  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COOLDOWN = 8
SLEEVES = [34, 55, 89]
W233 = 233


def run_sleeve(df: pd.DataFrame, window: int, atr_threshold: float) -> list[dict]:
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return []
    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return []
    dataset["Signal"] = 1
    short, last_entry = False, -COOLDOWN
    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
    _, trades = simulate_pnl_realistic(dataset, fill_lag_bars=FILL_LAG_BARS, cost_pts_per_trade=COST_PTS_PER_TRADE)
    return trades


def run_config(bars_by_day: dict, days: list[str], windows: list[int], atr_threshold: float) -> dict:
    daily_pnls, all_trades = [], []
    for day in days:
        df = bars_by_day.get(day)
        if df is None or df.empty:
            daily_pnls.append(0.0)
            continue
        day_trades = []
        for w in windows:
            day_trades.extend(run_sleeve(df, w, atr_threshold))
        daily_pnls.append(sum(t["pnl"] for t in day_trades))
        all_trades.extend(day_trades)
    arr = np.array(daily_pnls)
    return dict(total_pnl=float(arr.sum()), total_trades=len(all_trades), n_days=len(arr),
                positive_days=int((arr > 0).sum()), daily_pnls=arr.tolist())


def main() -> None:
    all_days = load_days()
    sample_days = all_days[::5]
    print(f"樣本天數: {len(sample_days)}\n")

    cache_bars, tick_bars = {}, {}
    for date in sample_days:
        cb = load_cached_day_bars(date)
        if not cb.empty:
            cache_bars[date] = cb
        ticks = load_front_month_ticks(date)
        if ticks is not None:
            tb = resample_to_1min(ticks)
            if not tb.empty:
                tick_bars[date] = tb

    common_days = sorted(set(cache_bars) & set(tick_bars))
    print(f"兩邊都有效的天數: {len(common_days)}\n")

    atr_th_cache = compute_global_atr_threshold(common_days, cache_bars)
    atr_th_tick = compute_global_atr_threshold(common_days, tick_bars)
    print(f"ATR門檻 (cache-derived)={atr_th_cache:.2f}  (tick-derived)={atr_th_tick:.2f}\n")

    print("=== w233（日盤） ===")
    r_cache = run_config(cache_bars, common_days, [W233], atr_th_cache)
    r_tick = run_config(tick_bars, common_days, [W233], atr_th_tick)
    print(f"cache版: trades={r_cache['total_trades']}  net={r_cache['total_pnl']:,.1f}pt  正報酬天={r_cache['positive_days']}/{r_cache['n_days']}")
    print(f"tick版:  trades={r_tick['total_trades']}  net={r_tick['total_pnl']:,.1f}pt  正報酬天={r_tick['positive_days']}/{r_tick['n_days']}")

    print("\n=== w34+w55+w89（日盤） ===")
    r_cache_p = run_config(cache_bars, common_days, SLEEVES, atr_th_cache)
    r_tick_p = run_config(tick_bars, common_days, SLEEVES, atr_th_tick)
    print(f"cache版: trades={r_cache_p['total_trades']}  net={r_cache_p['total_pnl']:,.1f}pt  正報酬天={r_cache_p['positive_days']}/{r_cache_p['n_days']}")
    print(f"tick版:  trades={r_tick_p['total_trades']}  net={r_tick_p['total_pnl']:,.1f}pt  正報酬天={r_tick_p['positive_days']}/{r_tick_p['n_days']}")

    # 逐日比較，找出訊號本身是否一致（不是只看總和）
    print("\n=== 逐日損益比較（w233） ===")
    cache_daily = dict(zip(common_days, r_cache["daily_pnls"]))
    tick_daily = dict(zip(common_days, r_tick["daily_pnls"]))
    n_same_sign = 0
    for d in common_days:
        c, t = cache_daily[d], tick_daily[d]
        same = (c > 0) == (t > 0) or (c == 0 and t == 0)
        n_same_sign += int(same)
        print(f"  {d}: cache={c:>8.1f}pt  tick={t:>8.1f}pt  同號={'Y' if same else 'N'}")
    print(f"\n方向一致天數: {n_same_sign}/{len(common_days)}")

    out = dict(
        sample_days=sample_days, common_days=common_days,
        w233=dict(cache=r_cache, tick=r_tick),
        portfolio=dict(cache=r_cache_p, tick=r_tick_p),
        atr_threshold=dict(cache=atr_th_cache, tick=atr_th_tick),
        daily_comparison=dict(cache=cache_daily, tick=tick_daily),
    )
    out_path = OUT_DIR / "tick_pnl_check_results.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
