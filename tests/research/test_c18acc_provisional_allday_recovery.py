"""Unit tests for provisional ALLDAY recovery verdict helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_provisional_allday_recovery import (
    _adopt,
    render_provisional_allday_recovery_md,
)


class TestProvisionalAlldayRecovery(unittest.TestCase):
    def test_adopt_go(self) -> None:
        self.assertEqual(_adopt(n_oos=10, d_oos=0.6, d_is=0.0), "GO")

    def test_adopt_nogo_oos(self) -> None:
        self.assertEqual(_adopt(n_oos=10, d_oos=0.2, d_is=0.0), "NO_GO")

    def test_adopt_nogo_is(self) -> None:
        self.assertEqual(_adopt(n_oos=10, d_oos=0.8, d_is=-0.5), "NO_GO")

    def test_render(self) -> None:
        md = render_provisional_allday_recovery_md(
            {
                "topic": "c18acc-provisional-allday-recovery",
                "date_start": "2025-01-02",
                "date_end": "2026-07-16",
                "n_trade_dates": 370,
                "is_end": "2025-12-01",
                "oos_start": "2025-12-02",
                "clocks": ["09:30", "13:00"],
                "variants": [
                    {
                        "variant_id": "B0_live_E1300",
                        "confirm_bars": 1,
                        "slices": {
                            "full": {
                                "n_legs": 100,
                                "mean_excess_pct": 3.0,
                                "compound_slot_pct": 400.0,
                            },
                            "oos": {"mean_excess_pct": 2.5},
                        },
                        "delta_oos_excess_pp": None,
                        "adopt": None,
                    }
                ],
                "verdict": {
                    "summary": "NO_GO",
                    "decision": "KEEP_LIVE_E1300",
                    "winner_variant": None,
                    "order_action": "keep",
                },
            }
        )
        self.assertIn("KEEP_LIVE_E1300", md)
        self.assertIn("Phase 1", md)


if __name__ == "__main__":
    unittest.main()
