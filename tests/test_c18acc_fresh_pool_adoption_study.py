"""C18acc fresh-only vs POOL1 adoption study · verdict + render."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_fresh_pool_adoption_study import (
    _verdict,
    render_fresh_pool_adoption_md,
)


class FreshPoolAdoptionStudyTests(unittest.TestCase):
    def _payload_stub(self, *, adopt: bool) -> dict:
        if adopt:
            b_ex, c_ex = 6.0, 6.4
            b_oos, c_oos = 6.5, 6.8
        else:
            b_ex, c_ex = 6.0, 5.5
            b_oos, c_oos = 6.5, 5.9
        baseline = {
            "periods": [{"stock_id": "2330", "entry_date": "2025-06-01"}],
            "stats": {"n_legs": 100, "mean_excess_pct": b_ex},
        }
        challenger = {
            "periods": [{"stock_id": "2330", "entry_date": "2025-06-01"}],
            "stats": {"n_legs": 99, "mean_excess_pct": c_ex},
        }
        slices = {
            "full": {
                "baseline": {"n_legs": 100, "mean_excess_pct": b_ex},
                "challenger": {"n_legs": 99, "mean_excess_pct": c_ex},
            },
            "is": {
                "baseline": {"n_legs": 70, "mean_excess_pct": 5.8},
                "challenger": {"n_legs": 69, "mean_excess_pct": 6.0},
            },
            "oos": {
                "baseline": {"n_legs": 30, "mean_excess_pct": b_oos},
                "challenger": {"n_legs": 30, "mean_excess_pct": c_oos},
            },
        }
        return {
            "baseline": baseline,
            "challenger": challenger,
            "slices": slices,
            "pool_stats": {"pool1_median_per_day": 1.0, "fresh_median_per_day": 1.0},
        }

    def test_verdict_adopts_fresh_when_oos_non_inferior(self) -> None:
        stub = self._payload_stub(adopt=True)
        v = _verdict(
            baseline=stub["baseline"],
            challenger=stub["challenger"],
            slices=stub["slices"],
            pool_stats=stub["pool_stats"],
        )
        self.assertTrue(v["adopt_fresh_only"])
        self.assertEqual(v["recommended_candidate_pool"], "fresh")

    def test_verdict_rejects_fresh_when_oos_underperforms(self) -> None:
        stub = self._payload_stub(adopt=False)
        v = _verdict(
            baseline=stub["baseline"],
            challenger=stub["challenger"],
            slices=stub["slices"],
            pool_stats=stub["pool_stats"],
        )
        self.assertFalse(v["adopt_fresh_only"])
        self.assertEqual(v["recommended_candidate_pool"], "fresh_union_accel")

    def test_render_md_contains_adoption_line(self) -> None:
        stub = self._payload_stub(adopt=True)
        v = _verdict(
            baseline=stub["baseline"],
            challenger=stub["challenger"],
            slices=stub["slices"],
            pool_stats=stub["pool_stats"],
        )
        md = render_fresh_pool_adoption_md(
            {
                "date_start": "2025-01-02",
                "date_end": "2026-07-09",
                "is_end": "2025-12-31",
                "oos_start": "2026-01-02",
                "confirm_bars": 2,
                "n_slots": 3,
                "pool_composition": stub["pool_stats"],
                "baseline": {
                    "stats": stub["baseline"]["stats"],
                    "slices": {k: v["baseline"] for k, v in stub["slices"].items()},
                },
                "challenger": {
                    "stats": stub["challenger"]["stats"],
                    "slices": {k: v["challenger"] for k, v in stub["slices"].items()},
                },
                "verdict": v,
            }
        )
        self.assertIn("fresh-only vs POOL1", md)
        self.assertIn("candidate_pool", md)


if __name__ == "__main__":
    unittest.main()
