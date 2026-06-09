"""P3：sync_fundamentals 解析邏輯（無網路）。"""

from __future__ import annotations

import unittest

from sync_fundamentals import parse_financial_rows, parse_per_rows, parse_revenue_rows


class TestParseFundamentals(unittest.TestCase):
    def test_per_latest(self) -> None:
        rows = [
            {"date": "2026-06-01", "PER": 20.0, "PBR": 3.0, "dividend_yield": 2.5},
            {"date": "2026-06-04", "PER": 22.0, "PBR": 3.2, "dividend_yield": 2.6},
        ]
        out = parse_per_rows(rows)
        assert out is not None
        self.assertEqual(out["pe"], 22.0)
        self.assertEqual(out["as_of_date"], "2026-06-04")

    def test_revenue_yoy_and_accel(self) -> None:
        rows = [
            {"revenue_year": 2025, "revenue_month": 3, "revenue": 80.0},
            {"revenue_year": 2025, "revenue_month": 4, "revenue": 100.0},
            {"revenue_year": 2026, "revenue_month": 3, "revenue": 110.0},
            {"revenue_year": 2026, "revenue_month": 4, "revenue": 130.0},
        ]
        hist, stats = parse_revenue_rows(rows)
        self.assertEqual(len(hist), 4)
        assert stats is not None
        self.assertEqual(stats["revenue_yoy_pct"], 30.0)
        self.assertIsNotNone(stats["revenue_mom_accel_pp"])

    def test_financial_eps_consensus(self) -> None:
        rows = [
            {"date": "2025-03-31", "type": "EPS", "value": 1.0},
            {"date": "2026-03-31", "type": "EPS", "value": 1.5},
            {
                "date": "2026-03-31",
                "type": "IncomeFromContinuingOperations",
                "value": 100.0,
            },
            {
                "date": "2026-03-31",
                "type": "EquityAttributableToOwnersOfParent",
                "value": 500.0,
            },
        ]
        hist, derived, _ = parse_financial_rows(rows)
        self.assertEqual(derived["eps_latest_q"], 1.5)
        self.assertEqual(derived["consensus_eps"], 1.0)
        self.assertGreater(len(hist), 0)


if __name__ == "__main__":
    unittest.main()
