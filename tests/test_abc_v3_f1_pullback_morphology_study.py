"""Tests for abc_v3_f1_pullback_morphology_study."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_pullback_morphology_study import score_pullback_morphology


def _pt(
    *,
    minute: str,
    spread_class: str,
    spread_dist: float,
    w20_mv: float,
    w5_mv: float,
    w3_mv: float,
) -> dict:
    return {
        "minute": minute,
        "spread_class": spread_class,
        "spread_dist": spread_dist,
        "w20": {"rs_momentum": w20_mv, "rs_ratio": 102.0, "quadrant": "leading"},
        "w5": {"rs_momentum": w5_mv, "rs_ratio": 100.0, "quadrant": "weakening"},
        "w3": {"rs_momentum": w3_mv, "rs_ratio": 99.0, "quadrant": "lagging"},
    }


class TestScorePullbackMorphology(unittest.TestCase):
    def test_classic_morph_with_prior_poll(self) -> None:
        day = [
            _pt(
                minute="09:30",
                spread_class="aligned",
                spread_dist=1.0,
                w20_mv=101.0,
                w5_mv=100.5,
                w3_mv=100.0,
            ),
            _pt(
                minute="10:00",
                spread_class="pullback",
                spread_dist=2.5,
                w20_mv=101.2,
                w5_mv=99.8,
                w3_mv=99.5,
            ),
        ]
        m = score_pullback_morphology(day, entry_px=99.0, open_px=100.0)
        assert m is not None
        self.assertEqual(m["ref_source"], "prev_poll")
        self.assertTrue(m["fresh_onset"])
        self.assertTrue(m["deepening"])
        self.assertTrue(m["lead_firm"])
        self.assertTrue(m["short_soft"])
        self.assertTrue(m["px_dip"])
        self.assertTrue(m["classic_morph"])
        self.assertTrue(m["active_dip"])
        self.assertTrue(m["w20_gt_w5"])
        self.assertTrue(m["w20_gt_both"])

    def test_relative_cushion_when_both_down(self) -> None:
        day = [
            _pt(
                minute="09:30",
                spread_class="pullback",
                spread_dist=2.0,
                w20_mv=101.0,
                w5_mv=100.0,
                w3_mv=99.0,
            ),
            _pt(
                minute="10:00",
                spread_class="pullback",
                spread_dist=2.2,
                w20_mv=100.8,  # −0.2
                w5_mv=99.0,  # −1.0
                w3_mv=98.5,  # −0.5
            ),
        ]
        m = score_pullback_morphology(day, entry_px=50.0, open_px=50.0)
        assert m is not None
        self.assertFalse(m["lead_firm"])
        self.assertTrue(m["w20_gt_w5"])
        self.assertTrue(m["w20_gt_w3"])
        self.assertTrue(m["w20_rel_cushion"])

    def test_first_poll_uses_t_minus_1_ref(self) -> None:
        day = [
            _pt(
                minute="09:30",
                spread_class="pullback",
                spread_dist=3.0,
                w20_mv=101.5,
                w5_mv=99.0,
                w3_mv=98.5,
            )
        ]
        prior = _pt(
            minute="13:30",
            spread_class="aligned",
            spread_dist=1.5,
            w20_mv=101.0,
            w5_mv=100.0,
            w3_mv=99.5,
        )
        m = score_pullback_morphology(
            day, prior_ref=prior, ref_source="t_minus_1_eod", entry_px=50.0, open_px=51.0
        )
        assert m is not None
        self.assertEqual(m["ref_source"], "t_minus_1_eod")
        self.assertTrue(m["fresh_onset"])
        self.assertTrue(m["deepening"])
        self.assertTrue(m["lead_firm"])
        self.assertTrue(m["short_soft"])
        self.assertTrue(m["classic_morph"])


if __name__ == "__main__":
    unittest.main()
