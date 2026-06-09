"""P3：L8.5 預期差反例 + 子分邏輯。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from expectation_engine import (
    L85Inputs,
    L8Inputs,
    compute_expectation_subscore,
    compute_fundamental_subscore,
    gap_to_subscore,
)
from stock_db import (
    connect,
    upsert_stock_consensus,
    upsert_stock_fundamental,
)


class TestExpectationGap(unittest.TestCase):
    def test_roe_beat_raises_score(self) -> None:
        """ROE 15% vs 共識 8% → 預期差利多（PRD §9.2 反例）。"""
        res = compute_expectation_subscore(
            L85Inputs(
                actual_roe=15.0,
                consensus_roe=8.0,
                actual_eps=None,
                consensus_eps=None,
                revenue_yoy_pct=None,
                revenue_mom_accel_pp=None,
            )
        )
        self.assertEqual(res.status, "OK")
        self.assertGreater(res.score, 50.0)
        self.assertEqual(res.score, gap_to_subscore(7.0, scale=2.5))

    def test_roe_miss_lowers_score(self) -> None:
        """ROE 34% vs 共識 38% → 預期差利空（PRD §9.2 反例）。"""
        res = compute_expectation_subscore(
            L85Inputs(
                actual_roe=34.0,
                consensus_roe=38.0,
                actual_eps=None,
                consensus_eps=None,
                revenue_yoy_pct=None,
                revenue_mom_accel_pp=None,
            )
        )
        self.assertEqual(res.status, "OK")
        self.assertLess(res.score, 50.0)
        self.assertEqual(res.score, gap_to_subscore(-4.0, scale=2.5))

    def test_revenue_accel_boost(self) -> None:
        res = compute_expectation_subscore(
            L85Inputs(None, None, None, None, None, revenue_mom_accel_pp=5.0)
        )
        self.assertGreater(res.score, 60.0)

    def test_missing_data_neutral(self) -> None:
        res = compute_expectation_subscore(
            L85Inputs(None, None, None, None, None, None)
        )
        self.assertEqual(res.status, "DATA_MISSING")
        self.assertEqual(res.score, 50.0)


class TestFundamentalSubscore(unittest.TestCase):
    def test_pe_percentile_in_pool(self) -> None:
        low = compute_fundamental_subscore(
            L8Inputs(pe=10.0, pb=None, roe_ttm=20.0, dividend_yield=None),
            pe_pool=[10.0, 20.0, 30.0],
            roe_pool=[20.0],
        )
        high = compute_fundamental_subscore(
            L8Inputs(pe=30.0, pb=None, roe_ttm=20.0, dividend_yield=None),
            pe_pool=[10.0, 20.0, 30.0],
            roe_pool=[20.0],
        )
        self.assertGreater(low.score, high.score)


class TestLoadFromDb(unittest.TestCase):
    def test_fundamental_and_consensus_drive_score(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            upsert_stock_fundamental(
                conn,
                [
                    {
                        "stock_id": "TEST",
                        "as_of_date": "2026-06-04",
                        "pe": 12.0,
                        "pb": 1.5,
                        "roe_ttm": 34.0,
                        "eps_ttm": 4.0,
                        "eps_latest_q": 1.1,
                        "roe_latest_q": 34.0,
                        "dividend_yield": 3.0,
                        "revenue_yoy_pct": 10.0,
                        "revenue_mom_accel_pp": 2.0,
                        "source": "test",
                    }
                ],
            )
            upsert_stock_consensus(
                conn,
                [
                    {
                        "stock_id": "TEST",
                        "as_of_date": "2026-06-04",
                        "metric": "roe",
                        "consensus_value": 38.0,
                        "source": "test",
                    }
                ],
            )
            from expectation_engine import (
                build_l85_inputs,
                build_l8_inputs,
                load_latest_consensus_map,
                load_latest_fundamental_map,
            )

            fund = load_latest_fundamental_map(conn, ["TEST"])["TEST"]
            cons = load_latest_consensus_map(conn, ["TEST"])
            exp = compute_expectation_subscore(build_l85_inputs(fund, cons["TEST"]))
            self.assertLess(exp.score, 50.0)
            fund_sc = compute_fundamental_subscore(
                build_l8_inputs(fund), pe_pool=[12.0, 25.0], roe_pool=[34.0, 10.0]
            )
            self.assertEqual(fund_sc.status, "OK")
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
