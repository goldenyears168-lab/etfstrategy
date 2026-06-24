"""Tests for lens_daily_alert KPI aggregation."""

from __future__ import annotations

import unittest

from lens_alert_digest import build_lens_daily_alert_from_rows
from stock_daily_lens import LensRow


class LensAlertDigestTests(unittest.TestCase):
    def test_build_lens_daily_alert_kpi_counts(self) -> None:
        rows = [
            LensRow(
                trade_date="2026-06-22",
                stock_id="2330",
                highlight_tier="fire",
                delta_new_to_watchlist=True,
                consensus_add=True,
                lens_score=90,
                delta_any_signal=True,
                signal_convergence=4,
            ),
            LensRow(
                trade_date="2026-06-22",
                stock_id="2454",
                highlight_tier="watch",
                consensus_add=False,
                lens_score=70,
            ),
            LensRow(
                trade_date="2026-06-22",
                stock_id="3008",
                highlight_tier="fire",
                consensus_add=True,
                lens_score=80,
                delta_any_signal=True,
                signal_convergence=3,
            ),
        ]
        alert = build_lens_daily_alert_from_rows(rows, "2026-06-22", top_n=2)
        self.assertEqual(alert["total_count"], 3)
        self.assertEqual(alert["fire_count"], 2)
        self.assertEqual(alert["delta_new_count"], 1)
        self.assertEqual(alert["consensus_add_count"], 2)
        self.assertEqual(len(alert["items_json"]), 2)
        self.assertEqual(alert["items_json"][0]["stock_id"], "2330")


if __name__ == "__main__":
    unittest.main()
