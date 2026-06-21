"""Tests for copytrade_leg_bucket_horizon."""

from __future__ import annotations

import sqlite3
import unittest

from research.backtest.copytrade_leg_bucket_horizon import (
    _kruskal_wallis,
    _mann_whitney_two_sample,
    batch_for_horizon,
    build_policy_signal_days,
    default_l1_policies,
    evaluate_l1_f1,
    evaluate_l1_h3,
    leg_count_bucket,
)


class TestLegBucketHorizon(unittest.TestCase):
    def test_leg_count_bucket(self) -> None:
        self.assertEqual(leg_count_bucket(1), "1")
        self.assertEqual(leg_count_bucket(4), "2-4")
        self.assertEqual(leg_count_bucket(7), "5-10")
        self.assertEqual(leg_count_bucket(15), "11+")

    def test_batch_for_horizon(self) -> None:
        self.assertEqual(
            batch_for_horizon(9, matrix_batch="m20", extended_batch="x45"),
            "m20",
        )
        self.assertEqual(
            batch_for_horizon(27, matrix_batch="m20", extended_batch="x45"),
            "x45",
        )

    def test_evaluate_l1_f1_insufficient(self) -> None:
        conn = sqlite3.connect(":memory:")
        out = evaluate_l1_f1(conn)
        self.assertEqual(out["verdict"], "insufficient_n")

    def test_evaluate_l1_h3_empty_db(self) -> None:
        conn = sqlite3.connect(":memory:")
        out = evaluate_l1_h3(conn)
        self.assertEqual(out["hypothesis_id"], "L1-H3")
        self.assertFalse(out["interaction_supported"])
        self.assertEqual(out["verdict"], "no_clear_interaction")

    def test_mann_whitney_insufficient(self) -> None:
        self.assertIsNone(_mann_whitney_two_sample([1.0, 2.0], [3.0, 4.0]))

    def test_kruskal_wallis_insufficient(self) -> None:
        self.assertIsNone(_kruskal_wallis([[1.0, 2.0]]))

    def test_default_l1_policies(self) -> None:
        policies = default_l1_policies(
            [{"bucket_value": "5-10", "sweet_spot_h": 20}]
        )
        ids = [p["policy_id"] for p in policies]
        self.assertIn("P1_uniform_h9", ids)
        self.assertIn("P2_extend_5_10", ids)
        p2 = next(p for p in policies if p["policy_id"] == "P2_extend_5_10")
        self.assertEqual(p2["bucket_h"]["5-10"], 20)

    def test_build_policy_signal_days_empty(self) -> None:
        conn = sqlite3.connect(":memory:")
        policy = default_l1_policies()[0]
        days, meta = build_policy_signal_days(conn, policy)
        self.assertEqual(days, [])
        self.assertEqual(meta["n_universe"], 0)


if __name__ == "__main__":
    unittest.main()
