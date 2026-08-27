#!/usr/bin/env python3
"""因果、逐bar版的 Donchian channel 引擎——距離下單層的第三個缺口。

先前10+輪全部是「一次讀進一整天K棒，用index迴圈跑」的離線批次版本，雖然邏輯上已經是
因果的（shift(1)、只看得到i-1以前的通道值），但不是「可以逐根即時餵資料進去」的形式。
這支腳本把同一套邏輯重寫成一個 class：外部一根一根餵K棒進來，內部自己維護所有需要的
狀態（Signal/position/cooldown/rolling buffer），每餵一根就回傳「這根有沒有訊號」。

驗證方式：拿同一批歷史資料，(a) 跑舊的批次向量化版本，(b) 逐根餵給這個因果class，
兩者的訊號/損益逐根比對，必須完全一致（bit-for-bit）才能證明這個「可即時化」的重寫
沒有引入新的邏輯錯誤——這是把離線研究轉成可以接即時資料源之前必須做的等價性檢查。

⚠️ 這支腳本只到「因果引擎本身」為止，刻意不含任何：即時資料源輪詢、下單呼叫、
launchd/job registry 註冊。要接上即時資料，下一步是寫一個 DataSource adapter 每分鐘
呼叫一次 `engine.on_bar(new_bar)`，但那已經進入下單層基礎設施的範疇，不在這輪授權內。
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_day_bars, load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402

RSI_PERIOD = 18
ATR_PERIOD = 20
COOLDOWN = 8


class CausalDonchianEngine:
    """逐bar因果版：外部呼叫 on_bar(bar) 一根一根餵，內部自己維護所有滾動狀態。

    跟批次版的唯一數學差異應該是0——這個class存在的目的純粹是把「一次看到整個
    DataFrame」的計算方式改寫成「只看得到目前為止收到的bar」的流式計算，邏輯本身
    (breakout/exit/cooldown/ATR濾網規則)完全不變。
    """

    def __init__(self, window: int, atr_threshold: float, cooldown: int = COOLDOWN):
        self.window = window
        self.atr_threshold = atr_threshold
        self.cooldown = cooldown

        # rolling buffers（因果：只存「已經收到」的bar）
        self.highs: deque[float] = deque(maxlen=window)
        self.lows: deque[float] = deque(maxlen=window)
        self.closes_rsi: deque[float] = deque(maxlen=RSI_PERIOD + 1)
        self.gains: deque[float] = deque(maxlen=RSI_PERIOD)
        self.losses: deque[float] = deque(maxlen=RSI_PERIOD)
        self.tr_hist: deque[float] = deque(maxlen=ATR_PERIOD)
        self.prev_close: float | None = None

        # 通道值：本次訊號判斷要用「前一根」算出來的Upper/Lower（shift(1)語意）
        self.prev_upper: float | None = None
        self.prev_lower: float | None = None

        self.signal = 1  # 1=多頭狀態, -1=淡出空單
        self.short = False
        self.last_entry_idx = -cooldown
        self.bar_idx = -1

        # 成交延遲：訊號在bar i確定，成交在bar i+FILL_LAG_BARS的開盤
        self.pending_fill: dict | None = None
        self.pos_dir = 1
        self.entry_price: float | None = None
        self.entry_time = None
        self.initial_entry_set = False
        self.first_valid_bar_idx: int | None = None
        self.trades: list[dict] = []
        self.equity = 0.0
        self.equity_curve: list[float] = []

    def _update_atr(self, high: float, low: float, close: float) -> float | None:
        """回傳『不含本根』的ATR——calculate_atr 批次版最後 .shift(1)，
        狀態機讀 ATR.iat[i] 沒有額外再shift，等於只用到本根以前的TR，本根自己的TR
        要等這次return之後才併入歷史，供下一根使用。"""
        pre_value = (sum(self.tr_hist) / len(self.tr_hist)) if len(self.tr_hist) == ATR_PERIOD else None
        if self.prev_close is not None:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        else:
            tr = high - low
        self.tr_hist.append(tr)
        self.prev_close = close
        return pre_value

    def _update_rsi(self, close: float) -> float | None:
        """同理：回傳『不含本根』的RSI，本根價格變化併入歷史供下一根使用。"""
        pre_value = None
        if len(self.gains) == RSI_PERIOD:
            avg_gain = sum(self.gains) / RSI_PERIOD
            avg_loss = sum(self.losses) / RSI_PERIOD
            pre_value = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        if self.closes_rsi:
            diff = close - self.closes_rsi[-1]
            self.gains.append(max(diff, 0.0))
            self.losses.append(max(-diff, 0.0))
        self.closes_rsi.append(close)
        return pre_value

    def on_bar(self, bar: dict) -> dict:
        """bar = {'Datetime', 'Open', 'High', 'Low', 'Close'}；回傳這一步的狀態快照。

        跟批次版一致的處理順序：先執行上一根排定的成交，再更新指標/通道，
        最後用『前一根』的Upper/Lower判斷這一根的訊號。
        """
        self.bar_idx += 1
        o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]

        fired_trade = None
        if self.pending_fill is not None and self.bar_idx == self.pending_fill["fill_at"]:
            fill_price = o
            new_dir = self.pending_fill["new_dir"]
            pnl_pts = (fill_price - self.entry_price) if self.pos_dir == 1 else (self.entry_price - fill_price)
            pnl = pnl_pts - COST_PTS_PER_TRADE
            self.equity += pnl
            fired_trade = dict(direction="long" if self.pos_dir == 1 else "short",
                                entry_time=self.entry_time, exit_time=bar["Datetime"],
                                entry_price=self.entry_price, exit_price=fill_price, pnl=pnl)
            self.trades.append(fired_trade)
            self.pos_dir = new_dir
            self.entry_price = fill_price
            self.entry_time = bar["Datetime"]
            self.pending_fill = None

        cur_atr = self._update_atr(h, l, c)
        cur_rsi = self._update_rsi(c)

        cur_upper = max(self.highs) if len(self.highs) == self.window else None
        cur_lower = min(self.lows) if len(self.lows) == self.window else None
        self.highs.append(h)
        self.lows.append(l)

        # 批次版用 dataset.dropna(subset=[Upper,Lower,RSI,ATR]) 決定「第一根有效列」
        # （這一根自己的cur_upper/cur_atr/cur_rsi都非空，不是看前一根），對應到 dataset2
        # row 0；初始合成部位的 entry_price 是 dataset["Open"].iat[min(fill_lag_bars,n-1)]，
        # 也就是 dataset2 row FILL_LAG_BARS——這裡用同一個基準重現，不能用原始bar_idx。
        row_valid_now = cur_upper is not None and cur_lower is not None and cur_atr is not None and cur_rsi is not None
        if row_valid_now and self.first_valid_bar_idx is None:
            self.first_valid_bar_idx = self.bar_idx
        if (
            not self.initial_entry_set
            and self.first_valid_bar_idx is not None
            and self.bar_idx == self.first_valid_bar_idx + FILL_LAG_BARS
        ):
            self.entry_price = o
            self.entry_time = bar["Datetime"]
            self.initial_entry_set = True

        # Upper/Lower：狀態機額外讀 .iat[i-1]（雙重shift），要用 self.prev_upper。
        # ATR/RSI：狀態機直接讀 .iat[i]（只有 calculate_atr/rsi 自己那次shift），
        # 用這裡的 cur_atr/cur_rsi（已經是『不含本根』的值）即可，不需要再多delay一層。
        ready = cur_atr is not None and cur_rsi is not None and self.prev_upper is not None
        if ready:
            if cur_atr < self.atr_threshold:
                pass  # signal 維持不變（跟批次版一致）
            elif (not self.short) and (self.bar_idx - self.last_entry_idx >= self.cooldown) and c > self.prev_upper:
                self.signal = -1
                self.short = True
                self.last_entry_idx = self.bar_idx
                if self.pending_fill is None:
                    self.pending_fill = dict(fill_at=self.bar_idx + FILL_LAG_BARS, new_dir=-1)
            else:
                exit_cond = c < self.prev_lower
                if self.short and exit_cond:
                    self.short = False
                    self.signal = 1
                    if self.pending_fill is None:
                        self.pending_fill = dict(fill_at=self.bar_idx + FILL_LAG_BARS, new_dir=1)

        self.prev_upper, self.prev_lower = cur_upper, cur_lower
        self.equity_curve.append(self.equity)
        return dict(bar_idx=self.bar_idx, signal=self.signal, equity=self.equity, trade=fired_trade)


def run_causal(df: pd.DataFrame, window: int, atr_threshold: float) -> tuple[list[dict], float]:
    engine = CausalDonchianEngine(window, atr_threshold)
    for _, row in df.iterrows():
        engine.on_bar(dict(Datetime=row["Datetime"], Open=row["Open"], High=row["High"],
                            Low=row["Low"], Close=row["Close"]))
    return engine.trades, engine.equity


def run_batch(df: pd.DataFrame, window: int, atr_threshold: float) -> tuple[list[dict], float]:
    """跟 tx_channel_recalibrate.run_combo_on_day 相同邏輯的向量化批次版，當作equivalence基準。"""
    from tx_channel_geometry_control import calculate_atr, calculate_rsi
    from tx_channel_geometry_realism_check import simulate_pnl_realistic

    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return [], 0.0

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
    total = sum(t["pnl"] for t in trades)
    return trades, total


def main() -> None:
    days = load_days()
    sample_days = days  # 全部83天做最終等價性驗證
    all_bars = {d: load_day_bars(d) for d in days}
    atr_threshold = compute_global_atr_threshold(days, all_bars)

    print(f"因果引擎 vs 批次版 等價性驗證（{len(sample_days)}天樣本，window=233）\n")
    n_match, n_mismatch = 0, 0
    for date in sample_days:
        df = all_bars[date]
        batch_trades, batch_total = run_batch(df, 233, atr_threshold)
        causal_trades, causal_total = run_causal(df, 233, atr_threshold)

        match = (
            len(batch_trades) == len(causal_trades)
            and abs(batch_total - causal_total) < 1e-6
            and all(abs(a["pnl"] - b["pnl"]) < 1e-6 for a, b in zip(batch_trades, causal_trades))
        )
        status = "✅一致" if match else "❌不一致"
        n_match += int(match)
        n_mismatch += int(not match)
        print(f"{date}: 批次={len(batch_trades)}筆/{batch_total:,.1f}pt  "
              f"因果={len(causal_trades)}筆/{causal_total:,.1f}pt  {status}")

    print(f"\n=== 結果：{n_match}/{len(sample_days)} 天完全一致 ===")
    if n_mismatch == 0:
        print("因果逐bar版跟批次向量化版數學上等價，可以安全地當作『可即時化』的基礎——")
        print("下一步接即時資料源只需要寫一個 adapter 每根新K棒呼叫一次 engine.on_bar()。")
    else:
        print("⚠️ 有不一致，因果版邏輯還有bug，不能當作批次版的等價替代，需要debug。")


if __name__ == "__main__":
    main()
