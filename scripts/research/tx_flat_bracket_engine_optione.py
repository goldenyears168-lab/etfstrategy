#!/usr/bin/env python3
"""Flat-default + bracket 架構——選項E（擺動點停損+通道中線出場），批次向量化版本。

取材自使用者要求重讀的原始文章：
  - Linear Regression Channel（Raff Regression Channel，forextraininggroup.com）：
    進場規則是「停損設在造成反彈那根K棒的高/低點」（swing-based stop，動態、跟隨
    市場結構，不是固定距離或ATR倍數）；出場規則之一是「跌破/突破中線就出場」
    （median-line exit），文章給的兩組真實交易範例顯示這個出場有時比撐到對側通道
    更好（2016年5-6月EUR/USD空單範例：第三筆用中線出場躲過了緊接著發生的完全反轉）。

跟選項A（固定點數）/選項B（ATR倍數）最大的差異：停損不再是「進場當下鎖定一個固定
距離」，而是「進場當下鎖定一個緊貼最近擺動點的動態價位」——距離本身由最近K根bar的
實際高低點決定，不是任何倍數關係。這跟design doc明文排除的選項C（通道對側邊界當
停損，H-SC-SAR-CONVERTER/H-SC-WIDTH-PERCENTILE死掉的設計族群）是不同幾何：選項C
用的是89根bar的完整通道邊界（慢、系統性），這裡用的是K=5~20根bar的區域性擺動點
（快、貼近進場點），design doc本身在H-TXFB-OPTION-B-ATR-BRACKET-EDGE的do_not裡
明確保留了這個方向未被證偽。

中線出場沿用文章的「median line」概念：mid_channel = (Upper+Lower)/2，用訊號層
已經算好的（single-shift）Upper/Lower，不額外算。同根bar若stop與median同時觸及，
保守假設stop優先；若median與target同時觸及，保守假設只拿到中線這個較小的獲利
（不假設拿到target這個較大獲利）——跟選項A/B「同根bar優先假設對交易方不利」的
保守慣例一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_flat_bracket_engine import FILL_LAG_BARS, COST_PTS_PER_TRADE, compute_signal_events  # noqa: E402


def simulate_bracket_block_optione(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    swing_lookback: int,
    swing_buffer_pts: float,
    target_pts: float,
    time_stop_bars: int,
    use_median_exit: bool,
    cooldown: int = 8,
) -> tuple[list[dict], dict]:
    """swing_lookback：進場前回看幾根bar找擺動極值（含成交當根）。
    swing_buffer_pts：擺動極值外再加的緩衝距離（避免剛好觸價就出場的雜訊）。
    target_pts：固定點數停利距離（跟選項A同語意，停損換掉，停利先沿用固定點數
    以隔離變數——只測『換停損幾何』本身的效果）。
    use_median_exit：是否啟用中線出場。
    """
    dataset = compute_signal_events(df, window, atr_threshold, cooldown)
    if dataset is None:
        return [], dict(n_events=0, n_entered=0, n_skipped_in_position=0, n_same_bar_ambiguous=0)

    n = len(dataset)
    trades: list[dict] = []
    state = "FLAT"
    pos: dict | None = None
    n_events = 0
    n_entered = 0
    n_skipped = 0
    same_bar_ambiguous = 0

    for i in range(n):
        ev = dataset["event"].iat[i]
        if ev != 0:
            n_events += 1
            if state == "FLAT":
                fill_idx = min(i + FILL_LAG_BARS, n - 1)
                entry_price = dataset["Open"].iat[fill_idx]
                entry_time = dataset["Datetime"].iat[fill_idx]
                direction = "short" if ev == 1 else "long"

                lb_start = max(0, fill_idx - swing_lookback + 1)
                if direction == "short":
                    swing_extreme = dataset["High"].iloc[lb_start : fill_idx + 1].max()
                    stop_price = swing_extreme + swing_buffer_pts
                    target_price = entry_price - target_pts
                else:
                    swing_extreme = dataset["Low"].iloc[lb_start : fill_idx + 1].min()
                    stop_price = swing_extreme - swing_buffer_pts
                    target_price = entry_price + target_pts

                pos = dict(
                    direction=direction,
                    entry_idx=fill_idx,
                    entry_price=entry_price,
                    entry_time=entry_time,
                    stop_price=stop_price,
                    target_price=target_price,
                    time_stop_idx=fill_idx + time_stop_bars,
                )
                state = direction
                n_entered += 1
            else:
                n_skipped += 1

        if pos is not None and i >= pos["entry_idx"]:
            high = dataset["High"].iat[i]
            low = dataset["Low"].iat[i]
            upper_i = dataset["Upper"].iat[i]
            lower_i = dataset["Lower"].iat[i]
            mid = (upper_i + lower_i) / 2.0 if pd.notna(upper_i) and pd.notna(lower_i) else None

            if pos["direction"] == "short":
                stop_touched = high >= pos["stop_price"]
                target_touched = low <= pos["target_price"]
                median_touched = use_median_exit and mid is not None and low <= mid
            else:
                stop_touched = low <= pos["stop_price"]
                target_touched = high >= pos["target_price"]
                median_touched = use_median_exit and mid is not None and high >= mid

            exit_reason = exit_price = None
            if stop_touched and (target_touched or median_touched):
                same_bar_ambiguous += 1
                exit_reason, exit_price = "stop", pos["stop_price"]
            elif stop_touched:
                exit_reason, exit_price = "stop", pos["stop_price"]
            elif median_touched:
                # 同根bar median跟target都觸及時，保守假設只拿到中線這個較小獲利
                exit_reason, exit_price = "median_exit", mid
            elif target_touched:
                exit_reason, exit_price = "target", pos["target_price"]
            elif i >= pos["time_stop_idx"]:
                exit_reason, exit_price = "time_stop", dataset["Close"].iat[i]
            elif i == n - 1:
                exit_reason, exit_price = "session_end_forced", dataset["Close"].iat[i]

            if exit_reason is not None:
                pnl_pts = (
                    (exit_price - pos["entry_price"])
                    if pos["direction"] == "long"
                    else (pos["entry_price"] - exit_price)
                )
                trades.append(
                    {
                        "direction": pos["direction"],
                        "entry_time": pos["entry_time"],
                        "exit_time": dataset["Datetime"].iat[i],
                        "entry_price": pos["entry_price"],
                        "exit_price": exit_price,
                        "pnl": pnl_pts - COST_PTS_PER_TRADE,
                        "reason": exit_reason,
                    }
                )
                state = "FLAT"
                pos = None

    stats = dict(
        n_events=n_events,
        n_entered=n_entered,
        n_skipped_in_position=n_skipped,
        n_same_bar_ambiguous=same_bar_ambiguous,
    )
    return trades, stats


def run_portfolio_bracket_optione(
    days: list[str],
    all_bars_with_sess: dict[str, pd.DataFrame],
    windows: list[int],
    atr_threshold: float,
    swing_lookback: int,
    swing_buffer_pts: float,
    target_pts: float,
    time_stop_bars: int,
    use_median_exit: bool,
    cooldown: int = 8,
) -> dict:
    all_trades: list[dict] = []
    agg_stats = dict(n_events=0, n_entered=0, n_skipped_in_position=0, n_same_bar_ambiguous=0)
    by_day: dict[str, float] = {}

    for day in days:
        day_bars = all_bars_with_sess[day]
        day_pnl = 0.0
        for sess in ("day", "night"):
            seg = day_bars[day_bars["sess"] == sess].reset_index(drop=True)
            if seg.empty:
                continue
            for w in windows:
                trades, stats = simulate_bracket_block_optione(
                    seg, w, atr_threshold, swing_lookback, swing_buffer_pts,
                    target_pts, time_stop_bars, use_median_exit, cooldown=cooldown,
                )
                for t in trades:
                    t["day"] = day
                    t["sess"] = sess
                    t["window"] = w
                all_trades.extend(trades)
                day_pnl += sum(t["pnl"] for t in trades)
                for k in agg_stats:
                    agg_stats[k] += stats[k]
        by_day[day] = day_pnl

    total_pnl = sum(t["pnl"] for t in all_trades)
    invariant_ok = agg_stats["n_events"] == agg_stats["n_entered"] + agg_stats["n_skipped_in_position"]
    invariant_ok = invariant_ok and agg_stats["n_entered"] == len(all_trades)
    return {
        "total_pnl": total_pnl,
        "trades": all_trades,
        "n_trades": len(all_trades),
        "by_day": by_day,
        "stats": agg_stats,
        "invariant_ok": invariant_ok,
    }
