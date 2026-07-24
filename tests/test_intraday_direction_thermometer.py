"""Unit tests · intraday direction thermometer (research helpers, no network)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from research.intraday_direction_thermometer import (
    Bar,
    ThermoConfig,
    fade_near_ext_from_bars,
    swing_1h_from_bars,
)


def _bars(n: int, *, start_hm: tuple[int, int] = (9, 0), trend: float = 0.0) -> list[Bar]:
    h, m = start_hm
    out: list[Bar] = []
    px = 100.0
    for i in range(n):
        ts = datetime(2026, 3, 2, h, m) + timedelta(minutes=5 * i)
        o = px
        px = px * (1.0 + trend)
        out.append(Bar(ts=ts, open=o, high=max(o, px) * 1.001, low=min(o, px) * 0.999, close=px))
    return out


class TestSwing1h(unittest.TestCase):
    def test_not_ready_before_threshold(self) -> None:
        cfg = ThermoConfig(swing_ready_bars=12, swing_lookback_bars=8)
        layer = swing_1h_from_bars(_bars(5), cfg)
        self.assertFalse(layer.ready)
        self.assertIsNone(layer.temp)

    def test_uptrend_positive_temp(self) -> None:
        cfg = ThermoConfig(swing_ready_bars=8, swing_lookback_bars=6)
        layer = swing_1h_from_bars(_bars(12, trend=0.002), cfg)
        self.assertTrue(layer.ready)
        self.assertIsNotNone(layer.temp)
        assert layer.temp is not None
        self.assertGreater(layer.temp, 0)


class TestFadeNearExt(unittest.TestCase):
    def test_near_high_fades(self) -> None:
        # Climb through midday; flat OHLC so close == day high.
        base = datetime(2026, 3, 2, 10, 30)
        rebuilt: list[Bar] = []
        px = 100.0
        for i in range(20):
            ts = base + timedelta(minutes=5 * i)
            o = px
            px = px * 1.004
            rebuilt.append(Bar(ts=ts, open=o, high=px, low=min(o, px), close=px))
        layer = fade_near_ext_from_bars(rebuilt, midday_only=True)
        self.assertTrue(layer.ready)
        self.assertEqual(layer.temp, -1)

    def test_midday_filter_neutral_outside(self) -> None:
        bars = _bars(12, start_hm=(9, 0), trend=0.004)
        layer = fade_near_ext_from_bars(bars, midday_only=True)
        self.assertTrue(layer.ready)
        self.assertEqual(layer.temp, 0)
        self.assertIn("非午盤", layer.label)


if __name__ == "__main__":
    unittest.main()
