"""Unit tests · intraday direction thermometer (research helpers, no network)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from research.intraday_direction_thermometer import (
    Bar,
    ThermoConfig,
    fade_near_ext_from_bars,
    fuse_ta30m_bias,
    or_fail_break_temp,
    swing_1h_from_bars,
    ta30m_factors_from_bars,
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


class TestTa30mFactors(unittest.TestCase):
    def test_uptrend_mom_and_live_confluence(self) -> None:
        bars = _bars(12, trend=0.003)
        fac = ta30m_factors_from_bars(bars)
        self.assertTrue(fac["ready"])
        self.assertEqual(fac["mom30"], 1)
        temp = fuse_ta30m_bias(
            fac, mode="live_confluence", keys=("mom30", "vwap")
        )
        self.assertEqual(temp, 1)

    def test_and_requires_agreement(self) -> None:
        fac = {
            "ready": True,
            "mom30": 1,
            "vwap": -1,
            "hm": 11 * 60,
            "bars_elapsed": 20,
            "ret30_pct": 0.5,
            "vol_confirm": 1,
        }
        self.assertEqual(
            fuse_ta30m_bias(fac, mode="and", keys=("mom30", "vwap")), 0
        )
        fac["vwap"] = 1
        self.assertEqual(
            fuse_ta30m_bias(fac, mode="and", keys=("mom30", "vwap")), 1
        )


class TestOrFailBreak(unittest.TestCase):
    def _session_reject(self) -> list[Bar]:
        """OR 100–101 over first 6 bars; then upside wick+reclaim same bar."""
        out: list[Bar] = []
        base = datetime(2026, 3, 2, 9, 0)
        for i in range(6):
            ts = base + timedelta(minutes=5 * i)
            out.append(
                Bar(ts=ts, open=100.2, high=101.0, low=100.0, close=100.5, volume=1000)
            )
        ts = base + timedelta(minutes=5 * 6)
        out.append(
            Bar(ts=ts, open=100.8, high=101.4, low=100.6, close=100.7, volume=800)
        )
        return out

    def test_upside_fail_fades_short(self) -> None:
        bars = self._session_reject()
        self.assertEqual(or_fail_break_temp(bars, reclaim_within=1), -1)

    def test_weak_vol_filter(self) -> None:
        bars = self._session_reject()
        self.assertEqual(
            or_fail_break_temp(bars, reclaim_within=1, weak_vol_max_rvol=1.0), -1
        )
        bars[-1] = Bar(
            ts=bars[-1].ts,
            open=100.8,
            high=101.4,
            low=100.6,
            close=100.7,
            volume=2000,
        )
        self.assertEqual(
            or_fail_break_temp(bars, reclaim_within=1, weak_vol_max_rvol=1.0), 0
        )

    def test_no_signal_while_still_outside(self) -> None:
        bars = self._session_reject()
        bars[-1] = Bar(
            ts=bars[-1].ts,
            open=100.8,
            high=101.4,
            low=101.1,
            close=101.2,
            volume=800,
        )
        self.assertEqual(or_fail_break_temp(bars, reclaim_within=1), 0)

    def test_no_repeat_after_reclaim(self) -> None:
        bars = self._session_reject()
        base = bars[0].ts
        # one more inside bar after rejection — must not re-fire
        bars.append(
            Bar(
                ts=base + timedelta(minutes=5 * 7),
                open=100.6,
                high=100.8,
                low=100.4,
                close=100.5,
                volume=900,
            )
        )
        self.assertEqual(or_fail_break_temp(bars, reclaim_within=3), 0)

    def test_multibar_reclaim(self) -> None:
        out: list[Bar] = []
        base = datetime(2026, 3, 2, 9, 0)
        for i in range(6):
            out.append(
                Bar(
                    ts=base + timedelta(minutes=5 * i),
                    open=100.2,
                    high=101.0,
                    low=100.0,
                    close=100.5,
                    volume=1000,
                )
            )
        # close outside above OR
        out.append(
            Bar(
                ts=base + timedelta(minutes=5 * 6),
                open=100.8,
                high=101.5,
                low=100.9,
                close=101.3,
                volume=800,
            )
        )
        # reclaim inside
        out.append(
            Bar(
                ts=base + timedelta(minutes=5 * 7),
                open=101.0,
                high=101.1,
                low=100.4,
                close=100.6,
                volume=700,
            )
        )
        self.assertEqual(or_fail_break_temp(out, reclaim_within=2), -1)


if __name__ == "__main__":
    unittest.main()
