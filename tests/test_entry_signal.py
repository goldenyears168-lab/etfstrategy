"""entry_signal 規則測試。"""

from __future__ import annotations

import unittest

from entry_signal import (
    classify_entry_context,
    classify_entry_context_batch,
    classify_entry_signal,
    has_strong_trend,
    overextended_min_pct,
)
from market_labels import (
    ENTRY_BREAKOUT,
    ENTRY_OVEREXTENDED,
    ENTRY_PULLBACK,
    ENTRY_SKIP,
    ENTRY_TAG_VOLUME,
    VOL_FLAT,
    VOL_UP,
)
from stock_context import TechnicalSnapshot


def _tech(**kwargs) -> TechnicalSnapshot:
    base = dict(
        stock_id="2330",
        trade_date="2026-06-04",
        close=1000.0,
        ma20=980.0,
        ma60=950.0,
        dist_ma20_pct=2.0,
        dist_ma60_pct=5.0,
        high_52w=1020.0,
        low_52w=800.0,
        position_52w_pct=50.0,
        dist_from_52w_high_pct=-2.0,
        volume=1000,
        vol_avg_5d=900.0,
        vol_ratio_5d=1.1,
        vol_label=VOL_FLAT,
    )
    base.update(kwargs)
    return TechnicalSnapshot(**base)


class TestEntrySignal(unittest.TestCase):
    def test_breakout(self) -> None:
        t = _tech(position_52w_pct=96.0, dist_from_52w_high_pct=-2.2)
        self.assertEqual(classify_entry_signal(t), ENTRY_BREAKOUT)

    def test_overextended(self) -> None:
        t = _tech(dist_ma20_pct=19.8)
        self.assertEqual(classify_entry_signal(t), ENTRY_OVEREXTENDED)

    def test_pullback(self) -> None:
        t = _tech(dist_ma20_pct=3.0, position_52w_pct=70.0)
        self.assertEqual(classify_entry_signal(t), ENTRY_PULLBACK)

    def test_skip_entry_on_reduce(self) -> None:
        t = _tech(position_52w_pct=96.0, dist_from_52w_high_pct=-2.0)
        self.assertEqual(classify_entry_signal(t, net_side="reduce"), ENTRY_SKIP)

    def test_strong_trend_tag(self) -> None:
        t = _tech(dist_ma60_pct=22.0, vol_label=VOL_UP, position_52w_pct=90.0)
        ctx = classify_entry_context(t, flow_score=70.0, chip_score=80.0)
        self.assertEqual(ctx.signal, ENTRY_OVEREXTENDED)
        self.assertIn(ENTRY_TAG_VOLUME, ctx.tags)
        self.assertTrue(has_strong_trend(t, flow_score=70.0, chip_score=80.0))


class TestRelativeOverextended(unittest.TestCase):
    def test_batch_only_extreme_marked(self) -> None:
        techs = [
            _tech(stock_id="A", dist_ma20_pct=25.0, dist_ma60_pct=20.0),
            _tech(stock_id="B", dist_ma20_pct=19.0, dist_ma60_pct=15.0),
            _tech(stock_id="C", dist_ma20_pct=14.0, dist_ma60_pct=10.0),
            _tech(stock_id="D", dist_ma20_pct=8.0, dist_ma60_pct=5.0),
        ]
        items = [(t.stock_id, t, None, None, None) for t in techs]
        ctx = classify_entry_context_batch(items)
        over = [sid for sid, c in ctx.items() if c.signal == ENTRY_OVEREXTENDED]
        self.assertIn("A", over)
        self.assertNotIn("D", over)
        self.assertLessEqual(len(over), 3)

    def test_overextended_min_pct_respects_abs_floor(self) -> None:
        self.assertGreaterEqual(overextended_min_pct([10.0, 11.0, 12.0]), 12.0)


if __name__ == "__main__":
    unittest.main()
