#!/usr/bin/env python3
"""Flat-default + bracket 架構——選項B（ATR倍數停損），因果、逐bar版。

跟 `tx_flat_bracket_causal.py`（選項A）結構完全相同（含兩個已修好的同根bar邊界bug：
event消費用`state_for_event_check`snapshot、最後一根bar的事件同根成交+立刻重跑exit-check），
唯一差異是 `_open_position` 用進場當下的ATR（`cur_atr`在成交那根bar算出的『不含本根』值，
即批次版讀到的 `dataset["ATR"].iat[fill_idx]`）乘上 k_stop/k_target 決定停損/停利距離。
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


class CausalFlatBracketEngineOptionB:
    def __init__(
        self,
        window: int,
        atr_threshold: float,
        k_stop: float,
        k_target: float,
        time_stop_bars: int,
        cooldown: int = 8,
    ):
        self.window = window
        self.atr_threshold = atr_threshold
        self.k_stop = k_stop
        self.k_target = k_target
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

    def _open_position(self, direction: str, entry_price: float, entry_time, fill_bar_idx: int, entry_atr: float) -> None:
        stop_pts = self.k_stop * entry_atr
        target_pts = self.k_target * entry_atr
        if direction == "short":
            stop_price = entry_price + stop_pts
            target_price = entry_price - target_pts
        else:
            stop_price = entry_price - stop_pts
            target_price = entry_price + target_pts
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

        state_for_event_check = self.state

        # (1) 先算這一根的 ATR（『不含本根』pre-value）——批次版 dataset["ATR"].iat[fill_idx]
        # 就是用『第fill_idx列自己』的ATR（該值由calculate_atr的shift(1)語意保證只用到
        # fill_idx-1以前的TR），所以這裡必須是『這一根』算出來的cur_atr，不能是上一根
        # 留下的值——這是跟選項A（固定點數、沒有ATR依賴）最大的差異，順序提前到最前面
        # 才能讓後面(2)的pending fill正確對齊批次版entry_atr的索引。
        cur_atr = self._update_atr(h, l, c)
        cur_upper = max(self.highs) if len(self.highs) == self.window else None
        cur_lower = min(self.lows) if len(self.lows) == self.window else None
        self.highs.append(h)
        self.lows.append(l)

        # (2) 若有pending entry排定在這一根成交，用剛算好的『這一根』cur_atr算距離。
        if self.pending_entry is not None and self.bar_idx == self.pending_entry["fill_at"]:
            self._open_position(self.pending_entry["direction"], o, t, self.bar_idx, cur_atr)
            self.pending_entry = None

        # (3) 若持倉中，檢查這一根（含剛成交當根）是否觸發出場。
        fired = self._try_exit(h, l, c, t, session_end)

        # (4) 事件偵測——跟選項A完全相同的雙重shift/cooldown-on-event語意。
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
                    self.state = direction
                    if session_end:
                        self._open_position(direction, o, t, self.bar_idx, cur_atr)
                        fired = self._try_exit(h, l, c, t, session_end) or fired
                    else:
                        self.pending_entry = dict(fill_at=self.bar_idx + FILL_LAG_BARS, direction=direction)
                else:
                    self.n_skipped_in_position += 1

        self.prev_upper, self.prev_lower = cur_upper, cur_lower
        return dict(bar_idx=self.bar_idx, trade=fired)


def run_causal_block_optionb(
    df: pd.DataFrame,
    window: int,
    atr_threshold: float,
    k_stop: float,
    k_target: float,
    time_stop_bars: int,
    cooldown: int = 8,
) -> tuple[list[dict], dict]:
    engine = CausalFlatBracketEngineOptionB(window, atr_threshold, k_stop, k_target, time_stop_bars, cooldown=cooldown)
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
