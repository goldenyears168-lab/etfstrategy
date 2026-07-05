from __future__ import annotations

import unittest

from research.backtest.c18acc_ext_regime_stratification import (
    _bucket_for_zone,
    _summarize_saves,
    evaluate_g3_hypotheses,
    stratify_ext_legs,
)


class ExtRegimeStratificationTest(unittest.TestCase):
    def test_bucket_for_zone(self) -> None:
        self.assertEqual(_bucket_for_zone("strong"), "risk_on")
        self.assertEqual(_bucket_for_zone("overbought"), "risk_on")
        self.assertEqual(_bucket_for_zone("neutral"), "neutral")
        self.assertEqual(_bucket_for_zone("weak"), "risk_off")

    def test_summarize_saves_empty(self) -> None:
        self.assertEqual(_summarize_saves([]), {"n": 0})

    def test_g3_hypotheses_pass(self) -> None:
        legs = [
            {"save_vs_orig_pct": 2.0, "breadth_zone_200": "strong", "breadth_bucket": "risk_on", "flow_tape_regime": "多頭"},
            {"save_vs_orig_pct": 1.0, "breadth_zone_200": "overbought", "breadth_bucket": "risk_on", "flow_tape_regime": "多頭"},
            {"save_vs_orig_pct": -1.0, "breadth_zone_200": "neutral", "breadth_bucket": "neutral", "flow_tape_regime": "震盪"},
            {"save_vs_orig_pct": -2.0, "breadth_zone_200": "weak", "breadth_bucket": "risk_off", "flow_tape_regime": "空頭"},
        ]
        strat = stratify_ext_legs(legs)
        hypo = evaluate_g3_hypotheses(strat)
        self.assertTrue(hypo["H-G3-1"]["pass"])
        self.assertTrue(hypo["H-G3-2"]["pass"])


if __name__ == "__main__":
    unittest.main()
