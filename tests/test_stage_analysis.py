"""Stage Analysis — Weinstein weekly + Minervini 8-point Trend Template."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from stage_analysis import (
    MINERVINI_CRITERIA_TOTAL,
    classify_weinstein_stage,
    calculate_minervini_trend_template,
    ix_stage_to_regime_name,
)


def _uptrend_daily(n: int = 280, *, start: float = 100.0, step: float = 0.4) -> pd.DataFrame:
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    close = start + np.arange(n) * step
    return pd.DataFrame(
        {
            "date": dates,
            "Open": close,
            "High": close + 1.5,
            "Low": close - 0.8,
            "Close": close,
            "Volume": 1_000_000,
        }
    )


def _downtrend_daily(n: int = 280) -> pd.DataFrame:
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    close = 200.0 - np.arange(n) * 0.5
    return pd.DataFrame(
        {
            "date": dates,
            "Open": close,
            "High": close + 0.5,
            "Low": close - 1.5,
            "Close": close,
            "Volume": 1_000_000,
        }
    )


class StageAnalysisTests(unittest.TestCase):
    def test_weinstein_stage2_on_strong_uptrend(self) -> None:
        out = classify_weinstein_stage(_uptrend_daily())
        self.assertEqual(out["stage"], 2)
        self.assertTrue(out["price_above_ma30"])

    def test_weinstein_stage4_on_downtrend(self) -> None:
        out = classify_weinstein_stage(_downtrend_daily())
        self.assertIn(out["stage"], (3, 4))

    def test_minervini_all_eight_on_uptrend_with_rs(self) -> None:
        out = calculate_minervini_trend_template(_uptrend_daily(), rs_rank=85)
        self.assertEqual(out["criteria_met"], MINERVINI_CRITERIA_TOTAL)
        self.assertTrue(out["passed"])
        self.assertEqual(out["stage"], 2)

    def test_minervini_fails_without_rs(self) -> None:
        out = calculate_minervini_trend_template(_uptrend_daily(), rs_rank=None)
        self.assertFalse(out["passed"])
        self.assertEqual(out["criteria_met"], MINERVINI_CRITERIA_TOTAL - 1)

    def test_ix_regime_broadening_on_stage2(self) -> None:
        name = ix_stage_to_regime_name(2, trend_score=100.0, extension_pct=5.0)
        self.assertEqual(name, "broadening")

    def test_ix_regime_contraction_on_stage4(self) -> None:
        self.assertEqual(ix_stage_to_regime_name(4), "contraction")

    def test_classify_ix_trend_posture(self) -> None:
        from stage_analysis import classify_ix_trend_posture

        reg = classify_ix_trend_posture(_uptrend_daily())
        self.assertIn(
            reg["trend_posture"],
            ("broadening", "concentration", "transitional", "contraction"),
        )
        self.assertGreater(reg["stage"], 0)


if __name__ == "__main__":
    unittest.main()
