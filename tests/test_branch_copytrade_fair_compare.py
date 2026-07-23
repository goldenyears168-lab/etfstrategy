"""branch_copytrade_fair_compare · ranking / summarize helpers."""

from __future__ import annotations

import unittest

from research.backtest.branch_copytrade_fair_compare import _sharpe_like, render_markdown


class FairCompareHelpersTest(unittest.TestCase):
    def test_sharpe_like_basic(self) -> None:
        self.assertIsNone(_sharpe_like([]))
        self.assertIsNone(_sharpe_like([1.0]))
        self.assertGreater(_sharpe_like([1.0, 2.0, 3.0]) or 0.0, 0.0)

    def test_render_markdown_contains_ranks(self) -> None:
        payload = {
            "generated_on": "2026-07-18",
            "protocol": {
                "window_start": "2024-07-01",
                "window_end": "2026-07-16",
                "oos_cut": "2025-07-01",
                "is_end": "2025-06-30",
                "entry": "L1_open",
                "hold_trading_days": 12,
                "n_slots": 3,
                "top_n": 1,
                "min_avg_volume_shares": 200_000.0,
                "cost_bps": 0.0,
                "amount_definition": "positive net shares × signal-day close",
                "density_target_is_signals": 80,
                "min_net_amounts_m_grid": [50.0, 100.0],
                "primary_rank": "oos_period_return_pct",
                "note": "per-branch amount thresholds",
            },
            "rank_density_matched_oos": [
                {
                    "label": "國票-安和",
                    "min_net_amount_m": 80.0,
                    "is_n_signals": 84,
                    "oos_n_cycles": 60,
                    "oos_period_return_pct": 168.0,
                    "oos_mean_return_pct": 2.5,
                    "oos_median_return_pct": 2.0,
                    "oos_win_rate_pct": 65.0,
                    "oos_sharpe_like": 0.47,
                    "oos_positive_month_pct": 66.0,
                }
            ],
            "rank_density_matched_full": [
                {
                    "label": "富邦-新店",
                    "min_net_amount_m": 80.0,
                    "full_n_cycles": 100,
                    "full_period_return_pct": 180.0,
                    "full_mean_return_pct": 2.0,
                    "oos_period_return_pct": 137.0,
                }
            ],
            "branches": [
                {
                    "label": "國票-安和",
                    "trader_id": "779Z",
                    "style": "community_winner",
                    "coverage": {
                        "n_days": 495,
                        "n_rows": 1,
                        "min_date": "2024-07-01",
                        "max_date": "2026-07-16",
                    },
                }
            ],
        }
        md = render_markdown(payload)
        self.assertIn("OOS period return%", md)
        self.assertIn("國票-安和", md)
        self.assertIn("富邦-新店", md)


if __name__ == "__main__":
    unittest.main()
