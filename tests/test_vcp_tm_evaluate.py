"""Integration tests for evaluate_vcp_tm pipeline."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from vcp_tm.evaluate import evaluate_vcp_tm, evaluate_vcp_tm_diagnostic
from vcp_tm.params import VcpTmParams


def _synthetic_uptrend(n: int = 260) -> pd.DataFrame:
    np.random.seed(42)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    trend = np.linspace(80, 150, n)
    noise = np.random.randn(n) * 1.5
    close = trend + noise
    high = close + np.abs(np.random.randn(n)) * 0.8
    low = close - np.abs(np.random.randn(n)) * 0.8
    vol = 800_000 + np.random.rand(n) * 200_000
    return pd.DataFrame(
        {
            "date": dates,
            "Open": close,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": vol,
        }
    )


class VcpTmEvaluateTests(unittest.TestCase):
    def test_evaluate_vcp_tm_returns_contract_fields(self):
        df = _synthetic_uptrend()
        r = evaluate_vcp_tm(df, df, params=VcpTmParams(trend_min_score=71.0))
        for key in (
            "composite_score",
            "execution_state",
            "entry_ready",
            "rating",
            "pattern_type",
        ):
            self.assertIn(key, r)

    def test_evaluate_vcp_tm_diagnostic_on_short_data(self):
        df = _synthetic_uptrend(100)
        r = evaluate_vcp_tm_diagnostic(df, df)
        self.assertFalse(r["passed"])
        self.assertEqual(r["reject_stage"], "bars")


if __name__ == "__main__":
    unittest.main()
