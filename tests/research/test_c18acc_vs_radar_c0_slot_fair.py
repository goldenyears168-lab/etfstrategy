"""Unit tests for radar-C0 slot fair helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_vs_radar_c0_slot_fair import (
    _adopt,
    render_c18acc_vs_radar_c0_slot_fair_md,
)


class TestRadarC0SlotFair(unittest.TestCase):
    def test_adopt_go(self) -> None:
        self.assertEqual(_adopt(n_oos=10, d_oos=0.6, d_is=0.0), "GO")

    def test_adopt_nogo(self) -> None:
        self.assertEqual(_adopt(n_oos=10, d_oos=0.1, d_is=0.0), "NO_GO")

    def test_render(self) -> None:
        md = render_c18acc_vs_radar_c0_slot_fair_md(
            {
                "topic": "c18acc-vs-radar-c0-slot-fair",
                "date_start": "2025-01-02",
                "date_end": "2026-07-16",
                "n_trade_dates": 10,
                "is_end": "x",
                "oos_start": "y",
                "variants": [
                    {
                        "variant_id": "B0_live",
                        "slices": {
                            "full": {
                                "n_legs": 1,
                                "mean_excess_pct": 1.0,
                                "compound_slot_pct": 10.0,
                            },
                            "oos": {"mean_excess_pct": 1.0},
                        },
                        "delta_oos_excess_pp": None,
                        "adopt": None,
                        "case_6505_legs": [],
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


if __name__ == "__main__":
    unittest.main()
