#!/usr/bin/env python3
"""Flat-default + bracket 架構——因果、逐bar版（Phase 1 對帳的另一半）。

沿用第十三輪 `CausalDonchianEngine` 已驗證（83/83等價）的暖身/雙重shift處理手法
（`_update_atr` 回傳『不含本根』值、cur_upper/cur_lower 用『append本根之前』的buffer算、
`prev_upper`/`prev_lower` 在每根bar處理完才更新——這個「這一輪算完才寫回prev_*」的順序
正是雙重shift語意的來源），只替換掉訊號判斷之後的部位管理邏輯（原本是always-in翻倉，
這裡改成flat-default+bracket）。拿掉RSI相關追蹤（新架構訊號層不用RSI，見設計文件第4節）。

呼叫端規約：對每個 (day, session) 區塊，逐根餵 bar，最後一根呼叫時要傳
`on_bar(bar, session_end=True)`——這是新架構「session邊界一定強制平倉」硬性規格在因果
版的落地方式，對應批次版 `simulate_bracket_block` 裡 `i == n-1` 那個分支。

⚠️ 兩個容易漏掉的同根bar邊界情況（都是跟批次版對帳時抓出來的真實bug，不是假設）：
  1. exit-check若跑在event-check之前，同一根bar內剛平倉又馬上被同一根的event重新進場，
     批次版不會這樣做（批次版的event-check用的是『這一根開始前』的state）——因此這裡用
     `state_for_event_check` snapshot 在本根一開始就鎖住，事件消費判斷一律讀這個值。
  2. 批次版對「最後一根bar才觸發的事件」用 `fill_idx = min(i+FILL_LAG_BARS, n-1)` 夾住，
     等於同一根bar立刻成交＋立刻強制平倉；因果版逐bar餵資料、不知道『下一根』存不存在，
     若照樣把成交排到 bar_idx+1，這根bar之後就沒有下一根bar可以觸發成交——這筆事件會
     從『已進場』『被跳過』兩桶裡都消失，破壞對帳不變量。修法：event-check發現
     `session_end=True`（呼叫端保證這是餵進來的最後一根）時，直接用本根Open同根成交，
     成交後立刻對本根重跑一次exit-check（entry_idx==本根bar_idx，跟批次版entry_idx==i
     時『同一輪迭代i內』就會跑到exit-check是同一件事）。
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD  # noqa: E402

FILL_LAG_BARS = 1
COST_PTS_PER_TRADE = 5.9


class CausalFlatBracketEngine:
    def __init__(
        self,
        window: int,
        atr_threshold: float,
        stop_pts: float,
        target_pts: float,
        time_stop_bars: int,
        cooldown: int = 8,
    ):
        self.window = window
        self.atr_threshold = atr_threshold
        self.stop_pts = stop_pts
        self.target_pts = target_pts
        self.time_stop_bars = time_stop_bars
        self.cooldown = cooldown

        self.highs: deque[float] = deque(maxlen=window)
        self.lows: deque[float] = deque(maxlen=window)
        self.tr_hist: deque[float] = deque(maxlen=ATR_PERIOD)
        self.prev_close: float | None = None
        self.prev_upper: float | None = None
        self.prev_lower: float | None = None

        self.bar_idx = -1
        self.last_event_idx = -cooldown
        self.state = "FLAT"
        self.pos: dict | None = None
        self.pending_entry: dict | None = None

        self.trades: list[dict] = []
        self.n_events = 0
        self.n_entered = 0
        self.n_skipped_in_position = 0
        self.n_same_bar_ambiguous = 0

    def _update_atr(self, high: float, low: float, close: float) -> float | None:
        pre_value = (sum(self.tr_hist) / len(self.tr_hist)) if len(self.tr_hist) == ATR_PERIOD else None
        if self.prev_close is not None:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        else:
            tr = high - low
        self.tr_hist.append(tr)
        self.prev_close = close
        return pre_value

    def _open_position(self, direction: str, entry_price: float, entry_time, fill_bar_idx: int) -> None:
        if direction == "short":
            stop_price = entry_price + self.stop_pts
            target_price = entry_price - self.target_pts
        else:
            stop_price = entry_price - self.stop_pts
            target_price = entry_price + self.target_pts
        self.pos = dict(
            direction=direction,
            entry_price=entry_price,
            entry_time=entry_time,
            stop_price=stop_price,
            target_price=target_price,
            time_stop_idx=fill_bar_idx + self.time_stop_bars,
        )
        self.n_entered += 1

    def _try_exit(self, h: float, l: float, c: float, t, session_end: bool) -> dict | None:
        """若持倉中，檢查這一根是否觸發出場；回傳這次觸發的trade dict或None。"""
        if self.pos is None:
            return None
        if self.pos["direction"] == "short":
            stop_touched = h >= self.pos["stop_price"]
            target_touched = l <= self.pos["target_price"]
        else:
            stop_touched = l <= self.pos["stop_price"]
            target_touched = h >= self.pos["target_price"]

        exit_reason = exit_price = None
        if stop_touched and target_touched:
            self.n_same_bar_ambiguous += 1
            exit_reason, exit_price = "stop", self.pos["stop_price"]
        elif stop_touched:
            exit_reason, exit_price = "stop", self.pos["stop_price"]
        elif target_touched:
            exit_reason, exit_price = "target", self.pos["target_price"]
        elif self.bar_idx >= self.pos["time_stop_idx"]:
            exit_reason, exit_price = "time_stop", c
        elif session_end:
            exit_reason, exit_price = "session_end_forced", c

        if exit_reason is None:
            return None
        pnl_pts = (
            (exit_price - self.pos["entry_price"])
            if self.pos["direction"] == "long"
            else (self.pos["entry_price"] - exit_price)
        )
        fired = dict(
            direction=self.pos["direction"],
            entry_time=self.pos["entry_time"],
            exit_time=t,
            entry_price=self.pos["entry_price"],
            exit_price=exit_price,
            pnl=pnl_pts - COST_PTS_PER_TRADE,
            reason=exit_reason,
        )
        self.trades.append(fired)
        self.state = "FLAT"
        self.pos = None
        return fired

    def on_bar(self, bar: dict, session_end: bool = False) -> dict:
        self.bar_idx += 1
        o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]
        t = bar["Datetime"]
        fired = None

        # 批次版每個迭代 i：先用『這一根開始前』的state決定event能不能開新倉，之後才跑
        # exit-check（有可能在同一根把state改回FLAT）。這裡先snapshot，事件消費判斷一律
        # 讀這個snapshot，不讀本根exit-check之後可能已經翻新的self.state——否則因果版會
        # 在「這一根剛好平倉」的那根bar多算一次批次版不會算的重新進場（同根平倉同根進場）。
        state_for_event_check = self.state

        # (1) 若有pending entry排定在這一根成交，先成交、鎖定 stop/target/time_stop。
        if self.pending_entry is not None and self.bar_idx == self.pending_entry["fill_at"]:
            self._open_position(self.pending_entry["direction"], o, t, self.bar_idx)
            self.pending_entry = None

        # (2) 若持倉中，檢查這一根（含剛成交當根）是否觸發出場。
        fired = self._try_exit(h, l, c, t, session_end)

        # (3) 更新 ATR / 通道 rolling buffer（沿用第十三輪已驗證手法）。
        cur_atr = self._update_atr(h, l, c)
        cur_upper = max(self.highs) if len(self.highs) == self.window else None
        cur_lower = min(self.lows) if len(self.lows) == self.window else None
        self.highs.append(h)
        self.lows.append(l)

        # (4) 用『前一根』的Upper/Lower（雙重shift語意）判斷這一根是否有breakout事件——
        # 訊號層完全不讀取 self.state/self.pos，純粹是價格+ATR+cooldown的函數。
        ready = cur_atr is not None and self.prev_upper is not None
        if ready and cur_atr >= self.atr_threshold and (self.bar_idx - self.last_event_idx >= self.cooldown):
            direction = None
            if c > self.prev_upper:
                direction = "short"
            elif c < self.prev_lower:
                direction = "long"
            if direction is not None:
                self.n_events += 1
                self.last_event_idx = self.bar_idx
                if state_for_event_check == "FLAT" and self.pending_entry is None:
                    # 狀態在偵測到事件的當下立刻翻轉（不是等成交那根才翻），跟批次版
                    # 「event與state轉換在同一次迭代同步發生」的語意對齊。
                    self.state = direction
                    if session_end:
                        # 批次版 fill_idx = min(i+FILL_LAG_BARS, n-1) 夾住——這是餵進來的
                        # 最後一根，沒有下一根可以排pending，直接用本根Open同根成交，
                        # 成交後立刻對本根重跑一次exit-check（呼應批次版entry_idx==i==n-1
                        # 時，同一輪迭代i內exit-check馬上就會評估這個新倉位）。
                        self._open_position(direction, o, t, self.bar_idx)
                        fired = self._try_exit(h, l, c, t, session_end) or fired
                    else:
                        self.pending_entry = dict(fill_at=self.bar_idx + FILL_LAG_BARS, direction=direction)
                else:
                    self.n_skipped_in_position += 1

        self.prev_upper, self.prev_lower = cur_upper, cur_lower
        return dict(bar_idx=self.bar_idx, trade=fired)


def run_causal_block(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    stop_pts: float,
    target_pts: float,
    time_stop_bars: int,
    cooldown: int = 8,
) -> tuple[list[dict], dict]:
    engine = CausalFlatBracketEngine(window, atr_threshold, stop_pts, target_pts, time_stop_bars, cooldown=cooldown)
    n = len(df)
    for idx, row in enumerate(df.itertuples(index=False)):
        engine.on_bar(
            dict(Datetime=row.Datetime, Open=row.Open, High=row.High, Low=row.Low, Close=row.Close),
            session_end=(idx == n - 1),
        )
    stats = dict(
        n_events=engine.n_events,
        n_entered=engine.n_entered,
        n_skipped_in_position=engine.n_skipped_in_position,
        n_same_bar_ambiguous=engine.n_same_bar_ambiguous,
    )
    return engine.trades, stats
