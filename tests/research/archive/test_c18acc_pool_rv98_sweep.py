"""Tests for C18acc POOL1 soft / additive RV-MV pool helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_pool_rv98_sweep import (
    mono_soft_rv_gate,
    pool_rv98_configs,
    render_pool_rv98_sweep_md,
)


def _feat(
    *,
    rv: float,
    mv: float,
    trend: str = "up_right",
    mono_up: bool = True,
    disp: float = 1.2,
    end_q: str = "improving",
) -> dict:
    return {
        "trend": trend,
        "mono_up": mono_up,
        "disp": disp,
        "rs_ratio": rv,
        "rs_momentum": mv,
        "end_q": end_q,
        "seg_last": disp,
        "segs": [1.0, 1.1, 1.2],
        "quadrants": [end_q] * 4,
    }


class TestMonoSoftRvGate(unittest.TestCase):
    def test_accepts_rv98_mv_above_100(self) -> None:
        self.assertTrue(mono_soft_rv_gate(_feat(rv=98.0, mv=100.5)))

    def test_rejects_rv_below_98(self) -> None:
        self.assertFalse(mono_soft_rv_gate(_feat(rv=97.9, mv=101.0)))

    def test_rejects_mv_le_100(self) -> None:
        self.assertFalse(mono_soft_rv_gate(_feat(rv=99.0, mv=100.0)))
        self.assertFalse(mono_soft_rv_gate(_feat(rv=99.0, mv=99.5)))

    def test_accepts_classic_leading(self) -> None:
        self.assertTrue(
            mono_soft_rv_gate(_feat(rv=101.0, mv=101.0, end_q="leading"))
        )

    def test_requires_mono_geometry(self) -> None:
        self.assertFalse(
            mono_soft_rv_gate(_feat(rv=99.0, mv=101.0, mono_up=False))
        )
        self.assertFalse(
            mono_soft_rv_gate(_feat(rv=99.0, mv=101.0, trend="up_left"))
        )
        self.assertFalse(
            mono_soft_rv_gate(_feat(rv=99.0, mv=101.0, disp=0.5))
        )

    def test_none_feat(self) -> None:
        self.assertFalse(mono_soft_rv_gate(None))

    def test_gt_compare_excludes_exact_98(self) -> None:
        self.assertFalse(
            mono_soft_rv_gate(
                _feat(rv=98.0, mv=103.0),
                rv_compare="gt",
                mv_min_exclusive=102.0,
            )
        )
        self.assertTrue(
            mono_soft_rv_gate(
                _feat(rv=98.1, mv=103.0),
                rv_compare="gt",
                mv_min_exclusive=102.0,
            )
        )

    def test_union_leading_keeps_weak_leading(self) -> None:
        # Leading mono_tier2: RV>100 MV>100 even if MV≤102
        lead = _feat(rv=101.0, mv=100.5, end_q="leading")
        self.assertTrue(
            mono_soft_rv_gate(
                lead,
                pool_mode="union_leading",
                rv_compare="gt",
                mv_min_exclusive=102.0,
            )
        )
        # Improving RV∈(98,100] MV∈(100,102] should NOT pass additive band
        weak = _feat(rv=99.0, mv=101.0, end_q="improving")
        self.assertFalse(
            mono_soft_rv_gate(
                weak,
                pool_mode="union_leading",
                rv_compare="gt",
                mv_min_exclusive=102.0,
            )
        )
        # Improving RV>98 MV>102 should pass
        strong_imp = _feat(rv=99.0, mv=102.5, end_q="improving")
        self.assertTrue(
            mono_soft_rv_gate(
                strong_imp,
                pool_mode="union_leading",
                rv_compare="gt",
                mv_min_exclusive=102.0,
            )
        )


class TestPoolRv98Configs(unittest.TestCase):
    def test_two_poll5m_variants(self) -> None:
        cfgs = pool_rv98_configs()
        ids = [c.variant_id for c in cfgs]
        self.assertEqual(ids, ["POOL1-P5M", "POOL1-RV98-P5M"])
        for c in cfgs:
            self.assertEqual(c.timing_mode, "poll_5m")
            self.assertEqual(c.poll_interval_min, 5)
            self.assertEqual(c.candidate_pool, "fresh_union_accel")
            self.assertTrue(c.accel_sell_bypass_on_spread_lost)

    def test_custom_challenger_id(self) -> None:
        cfgs = pool_rv98_configs(challenger_id="POOL1-ADD-X")
        self.assertEqual(cfgs[1].variant_id, "POOL1-ADD-X")


class TestRenderMd(unittest.TestCase):
    def test_render_contains_verdict(self) -> None:
        md = render_pool_rv98_sweep_md(
            {
                "date_start": "2020-01-02",
                "date_end": "2026-07-07",
                "timing_mode": "poll_5m",
                "is_end": "2025-12-31",
                "oos_h1": "2026-01-01..2026-06-30",
                "challenger_id": "POOL1-RV98-P5M",
                "gate": {
                    "baseline": "Leading",
                    "challenger": "RV≥98",
                    "pool_mode": "replace",
                    "mean_pool_size_baseline": 3.0,
                    "mean_pool_size_rv98": 3.5,
                    "mean_soft_only": 0.5,
                    "mean_soft_only_rv_band": 0.4,
                },
                "rv98_vs_baseline": {
                    "delta_excess_full_pp": 0.1,
                    "delta_legs_full": 2,
                    "delta_swaps_full": 1,
                    "delta_excess_oos_h1_pp": -0.05,
                    "delta_excess_oos_h2_pp": None,
                    "delta_cagr_full_pp": 1.2,
                },
                "verdict": {
                    "go_soft_rv": False,
                    "oos_h2_evaluated": True,
                    "summary": "NO_GO test",
                },
                "variants": {},
            }
        )
        self.assertIn("NO_GO_SOFT_RV", md)
        self.assertIn("poll_5m", md)

    def test_render_union_title(self) -> None:
        md = render_pool_rv98_sweep_md(
            {
                "date_start": "2020-01-02",
                "date_end": "2026-07-07",
                "timing_mode": "poll_5m",
                "is_end": "2025-12-31",
                "oos_h1": "2026-01-01..2026-06-30",
                "challenger_id": "POOL1-ADD-RVgt98-MV102-P5M",
                "gate": {
                    "baseline": "Leading",
                    "challenger": "Leading ∪ (RV>98 ∧ MV>102)",
                    "pool_mode": "union_leading",
                    "mean_pool_size_baseline": 0.9,
                    "mean_pool_size_challenger": 1.1,
                    "mean_soft_only": 0.2,
                    "mean_soft_only_rv_band": 0.2,
                },
                "rv98_vs_baseline": {},
                "verdict": {"go_soft_rv": False, "oos_h2_evaluated": True, "summary": "x"},
                "variants": {},
            }
        )
        self.assertIn("Leading ∪ (RV>98 ∧ MV>102)", md)


if __name__ == "__main__":
    unittest.main()
