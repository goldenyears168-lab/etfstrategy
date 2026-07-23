"""Tests for c18acc_entry_rank_study helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_entry_rank_study import (
    _bucket_for_zone,
    _jaccard,
    _norm_map,
    _wma_composite_from_point,
    decide_r2_adoption,
    evaluate_g3_breadth,
)


class TestEntryRankHelpers(unittest.TestCase):
    def test_jaccard(self) -> None:
        self.assertAlmostEqual(_jaccard({"a", "b", "c"}, {"a", "b", "c"}), 1.0)
        self.assertAlmostEqual(_jaccard({"a", "b"}, {"c", "d"}), 0.0)
        self.assertAlmostEqual(_jaccard({"a", "b"}, {"b", "c"}), 1 / 3)

    def test_norm_map(self) -> None:
        out = _norm_map({"a": 0.0, "b": 10.0})
        self.assertAlmostEqual(out["a"], 0.0)
        self.assertAlmostEqual(out["b"], 1.0)

    def test_wma_composite(self) -> None:
        pt = {
            "w20": {"rs_momentum": 102.0, "rs_ratio": 101.0},
            "w5": {"rs_ratio": 100.5},
            "w3": {"rs_ratio": 99.8},
        }
        v = _wma_composite_from_point(pt)
        self.assertIsNotNone(v)
        self.assertGreater(float(v), 0.0)

    def test_bucket_for_zone(self) -> None:
        self.assertEqual(_bucket_for_zone("strong"), "risk_on")
        self.assertEqual(_bucket_for_zone("weak"), "risk_off")
        self.assertEqual(_bucket_for_zone("neutral"), "neutral")

    def test_g3_and_adoption(self) -> None:
        def _legs(zone: str, mean: float, n: int = 5) -> list[dict]:
            return [{"excess_pct": mean, "breadth_zone_200": zone} for _ in range(n)]

        r0 = _legs("strong", 1.0) + _legs("neutral", 0.5)
        r2 = _legs("strong", 2.0) + _legs("neutral", 0.4)
        g3 = evaluate_g3_breadth(r0_periods=r0, r2_periods=r2)
        self.assertTrue(g3["g3_pass"])
        d = decide_r2_adoption(
            phase1_vs={
                "pass_strict": True,
                "delta_oos_excess_pp": 0.66,
                "delta_is_excess_pp": 0.02,
                "delta_full_excess_pp": 0.24,
            },
            g3=g3,
        )
        self.assertTrue(d["recommend_adopt"])
        self.assertEqual(d["outcome"], "GO_ADOPT_R2")


if __name__ == "__main__":
    unittest.main()
