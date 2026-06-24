"""Tests for RRG mono next-day expert confirmation entry detectors."""

from __future__ import annotations

import unittest

from research.backtest.rrg_mono_expert_entry import (
    bars_at_or_before,
    breakout_minute_at_or_above,
    compute_ema,
    compute_vwap_series,
    detect_bone_zone_entry,
    detect_expert_entry_after,
    detect_pivot_retest_entry,
    detect_vcp_expert_entry_after_breakout,
    detect_vwap_bounce_entry,
    detect_vwap_reclaim_entry,
)
from stock_db.kbar import KbarBar


def _bar(
    minute: str,
    o: float,
    h: float,
    lo: float,
    c: float,
    vol: int = 1000,
) -> KbarBar:
    return KbarBar(minute=minute, open=o, high=h, low=lo, close=c, volume=vol)


def _session_prefix(n: int = 25, base: float = 100.0) -> list[KbarBar]:
    """Flat warmup bars before 09:05 for EMA/VWAP seeding."""
    out: list[KbarBar] = []
    for i in range(n):
        mm = f"09:{i:02d}:00" if i < 60 else f"10:{i - 60:02d}:00"
        if mm < "09:05:00":
            px = base
            out.append(_bar(mm, px, px + 0.2, px - 0.1, px))
    return out


class TestRrgMonoExpertEntry(unittest.TestCase):
    def test_compute_ema_seed(self) -> None:
        closes = [float(i) for i in range(1, 21)]
        ema = compute_ema(closes, 9)
        self.assertIsNone(ema[7])
        self.assertAlmostEqual(ema[8], sum(closes[:9]) / 9.0)

    def test_vwap_reclaim_detects_first_bullish_cross(self) -> None:
        bars = _session_prefix(20, 100.0)
        bars.extend(
            [
                _bar("09:05:00", 100.0, 100.1, 99.5, 99.6),
                _bar("09:06:00", 99.6, 100.2, 99.4, 100.1),
            ]
        )
        trig = detect_vwap_reclaim_entry(tuple(bars))
        self.assertIsNotNone(trig)
        assert trig is not None
        self.assertEqual(trig.mode, "vwap_reclaim")
        self.assertEqual(trig.entry_minute, "09:06:00")
        self.assertAlmostEqual(trig.entry_px, 100.1)

    def test_vwap_bounce_needs_touch_then_next_bullish(self) -> None:
        bars = _session_prefix(20, 100.0)
        bars.extend(
            [
                _bar("09:05:00", 100.5, 100.8, 100.2, 100.6),
                _bar("09:06:00", 100.6, 100.7, 100.0, 100.5),
                _bar("09:07:00", 100.5, 101.0, 100.4, 100.9),
            ]
        )
        trig = detect_vwap_bounce_entry(tuple(bars))
        self.assertIsNotNone(trig)
        assert trig is not None
        self.assertEqual(trig.entry_minute, "09:07:00")
        self.assertAlmostEqual(trig.entry_px, 100.9)

    def test_bone_zone_pullback_and_confirm(self) -> None:
        # Warmup uptrend
        bars: list[KbarBar] = []
        for i in range(25):
            mm = f"09:{i:02d}:00"
            px = 100.0 + i * 0.15
            bars.append(_bar(mm, px, px + 0.2, px - 0.05, px))
        # Pull into band then bullish reclaim above fast EMA
        bars.append(_bar("09:25:00", 103.5, 103.6, 102.8, 103.0))
        bars.append(_bar("09:26:00", 103.0, 103.8, 102.9, 103.7))
        trig = detect_bone_zone_entry(tuple(bars))
        self.assertIsNotNone(trig)
        assert trig is not None
        self.assertEqual(trig.mode, "bone_zone")
        self.assertEqual(trig.entry_minute, "09:26:00")

    def test_no_trade_before_0905(self) -> None:
        bars = [
            _bar("09:00:00", 100.0, 100.5, 99.0, 100.2),
            _bar("09:04:00", 100.2, 101.0, 99.5, 100.8),
        ]
        self.assertIsNone(detect_vwap_reclaim_entry(tuple(bars)))

    def test_compute_vwap_series(self) -> None:
        bars = [_bar("09:05:00", 10, 12, 9, 11, 100), _bar("09:06:00", 11, 13, 10, 12, 200)]
        vwap = compute_vwap_series(bars)
        self.assertIsNotNone(vwap[0])
        self.assertIsNotNone(vwap[1])
        self.assertGreater(vwap[1], vwap[0])  # type: ignore[operator]

    def test_bars_at_or_before(self) -> None:
        bars = tuple(_bar(f"09:{30 + i:02d}:00", 100, 99, 101, 100) for i in range(5))
        sliced = bars_at_or_before(bars, "09:32")
        self.assertEqual(len(sliced), 3)

    def test_detect_expert_entry_after_respects_not_before(self) -> None:
        bars = _session_prefix(20, 100.0)
        bars.extend(
            [
                _bar("09:05:00", 100.0, 100.1, 99.5, 99.6),
                _bar("09:06:00", 99.6, 100.2, 99.4, 100.1),
            ]
        )
        seq = tuple(bars)
        self.assertIsNone(
            detect_expert_entry_after(
                "vwap_reclaim", seq, not_before_minute="09:07", at_or_before_minute="09:30"
            )
        )
        trig = detect_expert_entry_after(
            "vwap_reclaim", seq, not_before_minute="09:05", at_or_before_minute="09:30"
        )
        self.assertIsNotNone(trig)
        assert trig is not None
        self.assertEqual(trig.entry_minute, "09:06:00")


class TestVcpPivotRetestEntry(unittest.TestCase):
    def test_breakout_minute_skips_pre_0905(self) -> None:
        pivot = 100.0
        bars = [
            _bar("09:04:00", 99.0, 101.0, 98.5, 100.5),
            _bar("09:05:00", 99.8, 99.9, 99.5, 99.7),
        ]
        self.assertIsNone(breakout_minute_at_or_above(tuple(bars), pivot))
        self.assertEqual(
            breakout_minute_at_or_above(
                tuple(bars + [_bar("09:06:00", 99.7, 101.0, 99.6, 100.8)]), pivot
            ),
            "09:06:00",
        )

    def test_pivot_retest_after_breakout_pullback(self) -> None:
        pivot = 100.0
        bars = _session_prefix(20, 99.0)
        bars.extend(
            [
                _bar("09:05:00", 99.8, 100.5, 100.01, 100.3),  # breakout, low above pivot
                _bar("09:06:00", 100.3, 100.4, 100.1, 100.2),
                _bar("09:07:00", 100.2, 100.3, 99.5, 99.6),  # pullback touch, bearish
                _bar("09:08:00", 99.6, 100.6, 99.5, 100.4),  # reclaim
            ]
        )
        trig = detect_pivot_retest_entry(tuple(bars), pivot)
        self.assertIsNotNone(trig)
        assert trig is not None
        self.assertEqual(trig.mode, "pivot_retest")
        self.assertEqual(trig.entry_minute, "09:08:00")

    def test_vcp_expert_requires_breakout_first(self) -> None:
        pivot = 100.0
        bars = _session_prefix(20, 98.0)
        bars.extend([_bar("09:05:00", 98.0, 99.0, 97.8, 98.5)])
        self.assertIsNone(detect_vcp_expert_entry_after_breakout("vwap_reclaim", tuple(bars), pivot))


if __name__ == "__main__":
    unittest.main()
