#!/usr/bin/env python3
"""Flat-default + bracket 架構——選項E（擺動點停損+通道中線出場），因果、逐bar版。

跟選項A/B causal版結構相同（含已修好的同根bar邊界bug：event消費用
`state_for_event_check` snapshot、最後一根bar同根成交+立刻重跑exit-check）。

⚠️ 中線出場要用『這一根』的cur_upper/cur_lower（single-shift，本根bar自己的通道值，
在append本根high/low之前算出——等同批次版`dataset["Upper"].iat[i]`），不是
`self.prev_upper`（那是雙重shift、給event偵測用的i-1值）。這是選項E因果版特有、
選項A/B都沒有的新索引陷阱，混用會讓median exit的因果版跟批次版對不上。
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


class CausalFlatBracketEngineOptionE:
    def __init__(
        self,
        window: int,
        atr_threshold: float,
        swing_lookback: int,
        swing_buffer_pts: float,
        target_pts: float,
        time_stop_bars: int,
        use_median_exit: bool,
        cooldown: int = 8,
    ):
        self.window = window
        self.atr_threshold = atr_threshold
        self.swing_lookback = swing_lookback
        self.swing_buffer_pts = swing_buffer_pts
        self.target_pts = target_pts
        self.time_stop_bars = time_stop_bars
        self.use_median_exit = use_median_exit
        self.cooldown = cooldown

        self.highs: deque[float] = deque(maxlen=window)
        self.lows: deque[float] = deque(maxlen=window)
        # 給swing停損用的短窗buffer（跟通道window分開，maxlen=swing_lookback）
        self.recent_highs: deque[float] = deque(maxlen=swing_lookback)
        self.recent_lows: deque[float] = deque(maxlen=swing_lookback)
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
        # recent_highs/recent_lows此時已含成交當根（在on_bar main body先append才呼叫這裡）
        if direction == "short":
            swing_extreme = max(self.recent_highs)
            stop_price = swing_extreme + self.swing_buffer_pts
            target_price = entry_price - self.target_pts
        else:
            swing_extreme = min(self.recent_lows)
            stop_price = swing_extreme - self.swing_buffer_pts
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

    def _try_exit(self, h: float, l: float, c: float, t, session_end: bool, mid: float | None) -> dict | None:
        if self.pos is None:
            return None
        if self.pos["direction"] == "short":
            stop_touched = h >= self.pos["stop_price"]
            target_touched = l <= self.pos["target_price"]
            median_touched = self.use_median_exit and mid is not None and l <= mid
        else:
            stop_touched = l <= self.pos["stop_price"]
            target_touched = h >= self.pos["target_price"]
            median_touched = self.use_median_exit and mid is not None and h >= mid

        exit_reason = exit_price = None
        if stop_touched and (target_touched or median_touched):
            self.n_same_bar_ambiguous += 1
            exit_reason, exit_price = "stop", self.pos["stop_price"]
        elif stop_touched:
            exit_reason, exit_price = "stop", self.pos["stop_price"]
        elif median_touched:
            exit_reason, exit_price = "median_exit", mid
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

        # (1) 先更新短窗swing buffer（含本根）——進場用的swing極值一定要含成交當根。
        self.recent_highs.append(h)
        self.recent_lows.append(l)

        # (2) 若有pending entry排定在這一根成交。
        if self.pending_entry is not None and self.bar_idx == self.pending_entry["fill_at"]:
            self._open_position(self.pending_entry["direction"], o, t, self.bar_idx)
            self.pending_entry = None

        # (3) 更新通道 rolling buffer，取得這一根『single-shift』的cur_upper/cur_lower
        # （批次版 dataset["Upper"].iat[i] 語意，中線出場要用這個，不是雙重shift的
        # self.prev_upper）。
        cur_atr = self._update_atr(h, l, c)
        cur_upper = max(self.highs) if len(self.highs) == self.window else None
        cur_lower = min(self.lows) if len(self.lows) == self.window else None
        self.highs.append(h)
        self.lows.append(l)
        mid = (cur_upper + cur_lower) / 2.0 if cur_upper is not None and cur_lower is not None else None

        # (4) 若持倉中，檢查這一根（含剛成交當根）是否觸發出場。
        fired = self._try_exit(h, l, c, t, session_end, mid)

        # (5) 事件偵測——跟選項A/B完全相同的雙重shift/cooldown-on-event語意，
        # 用self.prev_upper（i-1值），不是這一根的cur_upper。
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
                        self._open_position(direction, o, t, self.bar_idx)
                        fired = self._try_exit(h, l, c, t, session_end, mid) or fired
                    else:
                        self.pending_entry = dict(fill_at=self.bar_idx + FILL_LAG_BARS, direction=direction)
                else:
                    self.n_skipped_in_position += 1

        self.prev_upper, self.prev_lower = cur_upper, cur_lower
        return dict(bar_idx=self.bar_idx, trade=fired)


def run_causal_block_optione(
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
    engine = CausalFlatBracketEngineOptionE(
        window, atr_threshold, swing_lookback, swing_buffer_pts, target_pts,
        time_stop_bars, use_median_exit, cooldown=cooldown,
    )
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
