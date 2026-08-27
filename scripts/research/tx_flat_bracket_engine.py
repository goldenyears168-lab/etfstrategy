#!/usr/bin/env python3
"""Flat-default + bracket 架構——批次向量化版本（Phase 1 引擎建置）。

背景：`reports/research/tx-donchian-regime/flat-default-bracket-design.md`（第十七輪
pre-registered 設計）。現行 always-in 翻倉架構有兩個致命問題（第十六輪證實）：session尾
記帳缺口、veto路徑依賴。本引擎改成 flat-default（預設空手）+ 明確 bracket（固定停損/停利/
time-stop），架構層面根除這兩個問題：

  1. 記帳缺口：進場當下就鎖定停損/停利/time-stop，session 邊界一定強制平倉
     （reason="session_end_forced"），不存在「忘記結算」的可能。
  2. 路徑依賴：訊號產生層（breakout event）純粹是價格+ATR+cooldown的函數，完全不讀取
     position state；已成交交易的進出場價格不依賴任何「前一筆交易怎麼結束」。唯一殘留的
     依賴是「機會可得性」（若訊號觸發時仍在持倉，這筆訊號直接跳過，不排隊）——這跟舊架構
     的路徑依賴是不同等級的問題（見設計文件 1.2 節）。

嚴格遵守設計文件第2節列出的六個已知死法（H-SC-EDGE-REVERT/H-RECOVERY/H-SC-WIDTH-PERCENTILE/
H-SC-DISTANCE-RATIO/H-SC-SAR-CONVERTER/H-SC-STAT-SIGNIFICANCE-GAP）的避法：
  - 出場價只有一個確定性規則（停損/停利/time_stop/session_end 四選一，同根bar內stop/target
    都觸及時保守假設stop優先），不存在「挑對交易方有利的候選價」。
  - 停損/停利/time_stop 三個門檻在進場當下一次鎖定，之後不重算、不讀未來資料。
  - 停損距離與停利距離是兩個獨立參數，不綁在同一個幾何量上等比例縮放。

Donchian突破訊號本身（window/ATR濾網/cooldown）直接沿用 `tx_channel_canonical_engine.py`
已驗證的雙重shift語意（訊號層用 Upper/Lower 的 .iat[i-1]，即批次shift(1)之後再手動lag一根），
不重新設計——第7~13輪已充分驗證訊號方向性/時機品質，問題出在舊架構的出場/記帳，不是訊號本身。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD, calculate_atr  # noqa: E402

# 硬性規格——跟 canonical engine 一致，不可配置。
FILL_LAG_BARS = 1
COST_PTS_PER_TRADE = 5.9


def compute_signal_events(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    cooldown: int,
) -> pd.DataFrame | None:
    """訊號產生層——純粹是價格+ATR+cooldown的函數，完全不讀取position state。

    回傳含 'event' 欄位的 dataframe：0=無事件，1=向上突破（fade空單事件），
    -1=向下突破（fade多單事件）。event 只在「這一根才觸發」的bar上標記（不是持續狀態），
    cooldown 綁的是「上一次事件bar」，不是「上一次進場bar」——這是設計文件1.3節要求的
    語意，用來保證這一層完全獨立於下游的進場/持倉決策。

    回傳 None 代表資料不足以起算（暖身期後不足20根bar，跟 canonical engine 門檻一致）。
    """
    dataset = df.copy()
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return None

    dataset["event"] = 0
    last_event_idx = -cooldown
    for i in range(1, len(dataset)):
        if dataset["ATR"].iat[i] < atr_threshold:
            continue
        if i - last_event_idx < cooldown:
            continue
        close = dataset["Close"].iat[i]
        if close > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("event")] = 1
            last_event_idx = i
        elif close < dataset["Lower"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("event")] = -1
            last_event_idx = i
    return dataset


def simulate_bracket_block(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    stop_pts: float,
    target_pts: float,
    time_stop_bars: int,
    cooldown: int = 8,
) -> tuple[list[dict], dict]:
    """單一 (day, session, window) 區塊。回傳 (trades, stats)。

    stats 含對帳不變量所需的三桶計數：n_events == n_entered + n_skipped_in_position，
    且 n_entered == len(trades)（每筆進場保證恰好產生一筆已平倉交易，因為 session 最後一根
    一定強制平倉）——這是 Phase 1 對帳測試要斷言的核心不變量。
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


def run_portfolio_bracket(
    days: list[str],
    all_bars_with_sess: dict[str, pd.DataFrame],
    windows: list[int],
    atr_threshold: float,
    stop_pts: float,
    target_pts: float,
    time_stop_bars: int,
    cooldown: int = 8,
) -> dict:
    """多天 × 多 session × 多 window 組合，回傳彙總統計＋對帳不變量檢查結果。"""
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
                trades, stats = simulate_bracket_block(
                    seg, w, atr_threshold, stop_pts, target_pts, time_stop_bars, cooldown=cooldown
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
