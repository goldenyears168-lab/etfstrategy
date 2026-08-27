#!/usr/bin/env python3
"""TX/TMF Donchian channel 研究線的唯一共用回測引擎——收斂第十六輪發現的記帳缺口修正。

背景（見 `reports/research/tx-donchian-regime/README.md` 第十三、十五、十六輪）：這條研究線
至今所有腳本（`tx_channel_recalibrate.py`、`tx_channel_multiwindow_portfolio.py`、
`tx_channel_daynight_split.py` 等）都各自複製貼上同一份 `simulate_pnl_realistic`
（定義於 `tx_channel_geometry_realism_check.py`），而這份邏輯有一個致命缺陷：每個
(day, session, window) 區塊「最後一次翻倉之後、一直持有到收盤」的未平倉部位從未被結算，
被靜默丟棄。`tx_channel_session_end_accounting.py` 手動修過一次（force-close），並量化出
這個缺陷有多嚴重：83天資料上，498個被丟棄的部位合計 -92,174.2pt，把 w34+w55+w89 組合
原本 +79,863.9pt 的正報酬直接翻成 -12,310.3pt。

這支模組把訊號生成（逐行複製 `tx_channel_recalibrate.run_combo_on_day` 的狀態機）與
force-close 修正後的結算邏輯收斂成唯一一份實作，讓以後的腳本 import 這裡、不要再各自
複製貼上一份可能又漏掉 force-close 的版本。

硬性規格（不可配置、寫死——這是要解決的核心問題）：
  - 成交延遲：訊號在 bar 收盤確定，成交在 `bar_idx + 1` 的開盤價（FILL_LAG_BARS 恆為 1）
  - 成本：每筆交易 5.9pt（來回，不可配置，跟 production TMF 看板實測值一致）
  - Session 尾端強制平倉：每個 (day, session, window) 區塊跑完，若還有未平倉部位，用該
    區塊最後一根 bar 的收盤價強制平倉、一樣扣成本，這筆交易一定會出現在回傳的 trades
    list 裡，標記 `reason="session_end_forceclose"`；正常訊號出場標記 `reason="signal_exit"`。
  - Day/night 各自獨立起算，不跨 session 滾動（呼叫端要把資料先用
    `tx_channel_daynight_split.load_day_bars_with_sess` 讀入、依 `sess` 欄位切開後
    分別餵給 `simulate_block`）。

可配置參數（訊號邏輯本身的參數）：window（Donchian 回看期）、cooldown（預設8）、
atr_threshold（ATR零波動濾網門檻，建議用 `tx_channel_recalibrate.compute_global_atr_threshold`
算）、use_rsi_exit（是否啟用RSI提前出場，預設False）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_donchian_intraday_faithful import RSI_EXIT  # noqa: E402

# 硬性規格——不可配置，故意寫死，防止以後又有腳本"忘記"實作對的版本。
FILL_LAG_BARS = 1
COST_PTS_PER_TRADE = 5.9


def _build_signal(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    cooldown: int,
    use_rsi_exit: bool,
) -> pd.DataFrame | None:
    """逐行複製 `tx_channel_recalibrate.run_combo_on_day` 的訊號狀態機（Donchian突破淡出、
    cooldown閘、ATR零波動濾網、可選RSI提前出場）。不得重新設計，只能忠實移植。

    回傳 None 代表資料不足以起算（暖身期後不足20根bar）。
    """
    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return None

    dataset["Signal"] = 1
    dataset["Entry"] = 0
    short, last_entry = False, -cooldown

    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        rsi = dataset["RSI"].iat[i]
        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= cooldown) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if use_rsi_exit:
                exit_cond = exit_cond or (rsi < RSI_EXIT)
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    return dataset


def _simulate_fills(dataset: pd.DataFrame) -> tuple[list[dict], int, float, pd.Timestamp]:
    """逐行複製 `simulate_pnl_realistic(fill_lag_bars=1)`（`tx_channel_geometry_realism_check.py`）
    的翻倉結算邏輯——訊號在 bar 收盤確定、成交延到下一根 bar 開盤。**不含 force-close**，
    只回傳已經翻倉平掉的交易，外加收盤時仍持有的未平倉部位狀態（給 `simulate_block` 拿去
    force-close）。
    """
    trades: list[dict] = []
    n = len(dataset)
    pos_dir = dataset["Signal"].iat[0]

    fill_idx0 = min(FILL_LAG_BARS, n - 1)
    entry_price = dataset["Open"].iat[fill_idx0]
    entry_time = dataset["Datetime"].iat[fill_idx0]
    pending: dict | None = None

    for i in range(1, n):
        sig = dataset["Signal"].iat[i]

        if pending is not None and i == pending["fill_at"]:
            fill_price = dataset["Open"].iat[i]
            pnl_pts = (fill_price - entry_price) if pos_dir == 1 else (entry_price - fill_price)
            trades.append(
                {
                    "direction": "long" if pos_dir == 1 else "short",
                    "entry_time": entry_time,
                    "exit_time": dataset["Datetime"].iat[i],
                    "entry_price": entry_price,
                    "exit_price": fill_price,
                    "pnl": pnl_pts - COST_PTS_PER_TRADE,
                    "reason": "signal_exit",
                }
            )
            pos_dir = pending["new_dir"]
            entry_price = fill_price
            entry_time = dataset["Datetime"].iat[i]
            pending = None

        if sig != pos_dir and pending is None:
            pending = {"fill_at": min(i + FILL_LAG_BARS, n - 1), "new_dir": sig}

    return trades, pos_dir, entry_price, entry_time


def simulate_block(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    cooldown: int = 8,
    use_rsi_exit: bool = False,
) -> list[dict]:
    """單一 (day, session, window) 區塊，回傳含 force-close 尾部位的完整 trades list。

    `df` 必須是單一 session（day 或 night）、單一交易日、依時間排序好的 OHLCV
    （欄位：Datetime/Open/High/Low/Close/Volume）——呼叫端負責先用
    `tx_channel_daynight_split.load_day_bars_with_sess` 讀入、依 `sess` 切開，
    這支函式本身不做跨 session 的邊界判斷。
    """
    dataset = _build_signal(df, window, atr_threshold, cooldown, use_rsi_exit)
    if dataset is None:
        return []

    trades, pos_dir, entry_price, entry_time = _simulate_fills(dataset)

    # session 尾端強制平倉：收盤時仍未平倉的部位，一定要進 trades list。
    n = len(dataset)
    last_close = dataset["Close"].iat[n - 1]
    pnl_pts = (last_close - entry_price) if pos_dir == 1 else (entry_price - last_close)
    trades.append(
        {
            "direction": "long" if pos_dir == 1 else "short",
            "entry_time": entry_time,
            "exit_time": dataset["Datetime"].iat[n - 1],
            "entry_price": entry_price,
            "exit_price": last_close,
            "pnl": pnl_pts - COST_PTS_PER_TRADE,
            "reason": "session_end_forceclose",
        }
    )
    return trades


def run_portfolio(
    days: list[str],
    all_bars_with_sess: dict[str, pd.DataFrame],
    windows: list[int],
    atr_threshold: float,
    cooldown: int = 8,
    use_rsi_exit: bool = False,
) -> dict:
    """多天 × 多 session × 多 window 組合，回傳彙總統計。

    `all_bars_with_sess[day]` 必須含 Datetime/Open/High/Low/Close/Volume/sess 欄位
    （`tx_channel_daynight_split.load_day_bars_with_sess` 的輸出格式）。day/night 各自
    獨立起算：每個 (day, sess, window) 各自呼叫一次 `simulate_block`，不合併成連續序列。
    """
    all_trades: list[dict] = []
    by_day: dict[str, float] = {}

    for day in days:
        day_bars = all_bars_with_sess[day]
        day_pnl = 0.0
        for sess in ("day", "night"):
            seg = day_bars[day_bars["sess"] == sess].reset_index(drop=True)
            if seg.empty:
                continue
            for w in windows:
                trades = simulate_block(seg, w, atr_threshold, cooldown=cooldown, use_rsi_exit=use_rsi_exit)
                for t in trades:
                    t["day"] = day
                    t["sess"] = sess
                    t["window"] = w
                all_trades.extend(trades)
                day_pnl += sum(t["pnl"] for t in trades)
        by_day[day] = day_pnl

    total_pnl = sum(t["pnl"] for t in all_trades)
    return {
        "total_pnl": total_pnl,
        "trades": all_trades,
        "n_trades": len(all_trades),
        "by_day": by_day,
    }
