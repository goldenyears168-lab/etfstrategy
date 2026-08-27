#!/usr/bin/env python3
"""Flat-default + bracket 架構——選項B（ATR倍數停損），批次向量化版本。

跟 `tx_flat_bracket_engine.py`（選項A固定點數）唯一的差異：stop_price/target_price 不是
常數，是 `entry_price ± k_stop×ATR[fill_idx]` / `entry_price ∓ k_target×ATR[fill_idx]`——
用『進場那一根』的ATR值算距離，進場當下鎖定，之後不重算（跟選項A同一條硬性規格：三個
出場門檻進場當下一次鎖定）。理論上這會讓停損距離隨volatility regime自動縮放，是設計文件
（`flat-default-bracket-design.md` 3節）點名用來解決選項A「固定點數在750天內price×3的
regime跨度上不穩健」這個已證實失敗模式的候選方案。

k_stop/k_target 網格改用經驗校準值，不用設計文件裡的建議值{0.75,1.0,1.5,2.0}——那組數字
明顯是假設ATR量級是幾百點時才合理，實測83天樣本 ATR(20) p5=11.2/p50=28.0/p95=77.9，
若用0.75~2.0的k只會得到8~156pt的停損（比選項A已知太窄、全負的<200pt網格還窄），不校準
直接套用只是浪費一輪跑數。

事件層/進場層/出場層其餘邏輯（雙重shift訊號、cooldown綁事件非持倉、同根bar stop/target
都觸及時保守假設stop優先、session_end強制平倉）逐行照抄 `tx_flat_bracket_engine.py`，
只有stop_price/target_price這兩行的計算方式不同——刻意保持成獨立檔案（不共用一個帶
if/else的函式），比照這條研究線一貫做法：每個選項的規格獨立成檔，方便日後對照，不因為
共用抽象而在其中一個選項身上引入另一個選項的回歸風險。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_flat_bracket_engine import FILL_LAG_BARS, COST_PTS_PER_TRADE, compute_signal_events  # noqa: E402


def simulate_bracket_block_optionb(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    k_stop: float,
    k_target: float,
    time_stop_bars: int,
    cooldown: int = 8,
) -> tuple[list[dict], dict]:
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
                entry_atr = dataset["ATR"].iat[fill_idx]
                stop_pts = k_stop * entry_atr
                target_pts = k_target * entry_atr
                direction = "short" if ev == 1 else "long"
                if direction == "short":
                    stop_price = entry_price + stop_pts
                    target_price = entry_price - target_pts
                else:
                    stop_price = entry_price - stop_pts
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
            if pos["direction"] == "short":
                stop_touched = high >= pos["stop_price"]
                target_touched = low <= pos["target_price"]
            else:
                stop_touched = low <= pos["stop_price"]
                target_touched = high >= pos["target_price"]

            exit_reason = exit_price = None
            if stop_touched and target_touched:
                same_bar_ambiguous += 1
                exit_reason, exit_price = "stop", pos["stop_price"]
            elif stop_touched:
                exit_reason, exit_price = "stop", pos["stop_price"]
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
                        "entry_atr": entry_atr if pos is not None else None,
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


def run_portfolio_bracket_optionb(
    days: list[str],
    all_bars_with_sess: dict[str, pd.DataFrame],
    windows: list[int],
    atr_threshold: float,
    k_stop: float,
    k_target: float,
    time_stop_bars: int,
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
                trades, stats = simulate_bracket_block_optionb(
                    seg, w, atr_threshold, k_stop, k_target, time_stop_bars, cooldown=cooldown
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
