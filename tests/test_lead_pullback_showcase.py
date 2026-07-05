from __future__ import annotations

import unittest

from research.backtest.lead_pullback_showcase import _mean_leg_excess, _trade_ledger


class LeadPullbackShowcaseTest(unittest.TestCase):
    def test_trade_ledger_cumulative(self) -> None:
        legs = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "seg_last": 3.2,
                "entry_px": 900.0,
                "exit_px": 918.0,
                "entry_date": "2026-04-01",
                "exit_date": "2026-04-08",
                "return_pct": 2.0,
                "bench_return_pct": 1.0,
                "pnl_ntd": 200.0,
                "slot_id": 0,
                "exit_reason": "hold_5d",
            },
            {
                "stock_id": "2317",
                "stock_name": "鴻海",
                "seg_last": 2.5,
                "entry_px": 150.0,
                "exit_px": 147.0,
                "entry_date": "2026-04-02",
                "exit_date": "2026-04-09",
                "return_pct": -2.0,
                "bench_return_pct": 0.5,
                "pnl_ntd": -200.0,
                "slot_id": 1,
                "exit_reason": "hold_5d",
            },
        ]
        rows = _trade_ledger(legs, total_capital=30_000.0)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["cum_return_pct"], 200 / 300, places=3)
        self.assertAlmostEqual(rows[1]["cum_return_pct"], 0.0, places=3)
        self.assertAlmostEqual(rows[0]["excess_pct"], 1.0, places=3)

    def test_mean_leg_excess(self) -> None:
        legs = [
            {"return_pct": 3.0, "bench_return_pct": 1.0},
            {"return_pct": -1.0, "bench_return_pct": 0.5},
        ]
        self.assertAlmostEqual(_mean_leg_excess(legs), 0.25, places=3)


if __name__ == "__main__":
    unittest.main()
