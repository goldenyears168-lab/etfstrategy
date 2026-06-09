"""rule_limit_price：benchmark、評分折扣、波動微調。"""

from __future__ import annotations

import unittest

from investment_policy import InvestmentPolicy
from market_labels import ENTRY_BREAKOUT, ENTRY_PULLBACK, ENTRY_SKIP, ENTRY_WAIT
from rule_limit_price import (
    compute_execution_prices,
    compute_ref_price,
    round_twd_tick,
)
from stock_context import TechnicalSnapshot


def _ips() -> InvestmentPolicy:
    return InvestmentPolicy.from_dict({})


def _tech(
    close: float,
    *,
    ma20: float | None = None,
    high_52w: float | None = None,
    dist_from_52w_high_pct: float | None = -5.0,
    stock_id: str = "2330",
    **kwargs: object,
) -> TechnicalSnapshot:
    base = dict(
        stock_id=stock_id,
        trade_date="2026-06-04",
        close=close,
        ma20=ma20,
        ma60=None,
        dist_ma20_pct=None,
        dist_ma60_pct=None,
        high_52w=high_52w or close * 1.1,
        low_52w=close * 0.8,
        position_52w_pct=70.0,
        dist_from_52w_high_pct=dist_from_52w_high_pct,
        volume=1000,
        vol_avg_5d=900.0,
        vol_ratio_5d=1.1,
        vol_label="量增",
        atr14_pct=None,
        avg_range_pct_14d=None,
        realized_vol_pct_14d=None,
    )
    base.update(kwargs)
    return TechnicalSnapshot(**base)


class TestRuleLimitPrice(unittest.TestCase):
    def test_round_twd_tick(self) -> None:
        self.assertEqual(round_twd_tick(100.23), 100.0)
        self.assertEqual(round_twd_tick(50.04), 50.0)

    def test_pullback_uses_ma20(self) -> None:
        r = compute_ref_price(
            entry_signal=ENTRY_PULLBACK,
            pm_bucket="觀察",
            tech=_tech(1000.0, ma20=980.0),
            ips=_ips(),
        )
        self.assertEqual(r.benchmark_type, "ma20")
        self.assertEqual(r.ref_price, 980.0)

    def test_wait_below_prev_close(self) -> None:
        r = compute_ref_price(
            entry_signal=ENTRY_WAIT,
            pm_bucket="觀察",
            tech=_tech(1000.0),
            ips=_ips(),
        )
        self.assertEqual(r.benchmark_type, "prev_close")
        self.assertLess(r.ref_price, 1000.0)

    def test_skip_entry(self) -> None:
        r = compute_ref_price(
            entry_signal=ENTRY_SKIP,
            pm_bucket="觀察",
            tech=_tech(1000.0),
            ips=_ips(),
        )
        self.assertIsNone(r.ref_price)

    def test_breakout_min_close_and_high(self) -> None:
        r = compute_ref_price(
            entry_signal=ENTRY_BREAKOUT,
            pm_bucket="突破",
            tech=_tech(1000.0, high_52w=1010.0),
            ips=_ips(),
        )
        self.assertIsNotNone(r.ref_price)
        self.assertLessEqual(r.ref_price, 1000.0)

    def test_2330_today_ab_vol(self) -> None:
        """台積電：折讓限價不抬高；執行停損在限價下方。"""
        r = compute_ref_price(
            entry_signal=ENTRY_BREAKOUT,
            pm_bucket="突破",
            tech=_tech(
                2385.0,
                high_52w=2440.0,
                dist_from_52w_high_pct=-2.25,
                atr14_pct=2.29,
                avg_range_pct_14d=1.81,
                realized_vol_pct_14d=1.59,
            ),
            ips=_ips(),
            investment_score=62.7,
            beta=1.23,
            tx_gap_pct=0.17,
            tsm_adr_pct=1.88,
        )
        self.assertIsNotNone(r.ref_price)
        self.assertEqual(r.discount_pct, 3.0)
        self.assertEqual(r.ref_price, 2310.0)
        self.assertEqual(r.structural_stop_price, 2310.0)
        self.assertLess(r.stop_price or 0, r.ref_price or 0)
        self.assertNotIn("折讓上限", r.pricing_note or "")

    def test_execution_prices_stop_fixed_on_prev_close(self) -> None:
        """試撮上跳時結構停損仍錨在 as_of 昨收；執行停損在限價下方。"""
        tech = _tech(1000.0, high_52w=1010.0)
        r = compute_execution_prices(
            entry_signal=ENTRY_BREAKOUT,
            pm_bucket="突破",
            tech=tech,
            ips=_ips(),
            snapshot_price=1050.0,
            investment_score=72.0,
        )
        self.assertIsNotNone(r.ref_price)
        self.assertEqual(r.structural_stop_price, 970.0)
        self.assertLess(r.ref_price, 1050.0)
        self.assertLess(r.stop_price or 0, r.ref_price or 0)

    def test_wait_discount_not_clamped_above_structural_stop(self) -> None:
        """觀望：折讓深於結構停損時限價不抬高。"""
        r = compute_ref_price(
            entry_signal=ENTRY_WAIT,
            pm_bucket="觀察",
            tech=_tech(111.0),
            ips=_ips(),
            investment_score=55.7,
        )
        self.assertIsNotNone(r.ref_price)
        self.assertEqual(r.structural_stop_price, 108.5)
        self.assertLess(r.ref_price or 0, r.structural_stop_price or 0)
        self.assertLess(r.stop_price or 0, r.ref_price or 0)
        self.assertNotIn("折讓上限", r.pricing_note or "")

    def test_execution_prices_ref_below_structural_stop_ok(self) -> None:
        r = compute_execution_prices(
            entry_signal=ENTRY_WAIT,
            pm_bucket="觀察",
            tech=_tech(1000.0),
            ips=_ips(),
            snapshot_price=950.0,
            investment_score=72.0,
        )
        self.assertIsNotNone(r.ref_price)
        self.assertEqual(r.structural_stop_price, 980.0)
        self.assertLess(r.ref_price or 0, r.structural_stop_price or 0)
        self.assertLess(r.stop_price or 0, r.ref_price or 0)

    def test_6223_today_ab_vol(self) -> None:
        """旺矽：觀望高震盪 → 折讓限價低於結構停損，不抬高。"""
        r = compute_ref_price(
            entry_signal=ENTRY_WAIT,
            pm_bucket="觀察",
            tech=_tech(
                6070.0,
                dist_from_52w_high_pct=-8.93,
                stock_id="6223",
                atr14_pct=6.77,
                avg_range_pct_14d=6.4,
                realized_vol_pct_14d=4.5,
            ),
            ips=_ips(),
            investment_score=63.2,
            beta=1.1,
            tx_gap_pct=0.17,
            tsm_adr_pct=1.88,
        )
        self.assertIsNotNone(r.ref_price)
        self.assertEqual(r.discount_pct, 4.28)
        self.assertEqual(r.structural_stop_price, 5945.0)
        self.assertLess(r.ref_price or 0, r.structural_stop_price or 0)
        self.assertEqual(r.ref_price, 5810.0)
        self.assertNotIn("折讓上限", r.pricing_note or "")


if __name__ == "__main__":
    unittest.main()
