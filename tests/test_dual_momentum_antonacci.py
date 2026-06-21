"""Tests for Gary Antonacci dual momentum TW backtest."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.dual_momentum_antonacci import (
    _allocate,
    _mom_12m_at,
    _pick_gem_asset,
    run_strategy_backtest,
)


class DualMomentumAntonacciTests(unittest.TestCase):
    def _fake_close(self, n: int = 400) -> pd.DataFrame:
        idx = pd.date_range("2018-01-02", periods=n, freq="B").strftime("%Y-%m-%d").tolist()
        up = [100.0 + i for i in range(n)]
        bond = [100.0 + i * 0.02 for i in range(n)]
        return pd.DataFrame(
            {"0050": up, "00646": [v * 0.9 for v in up], "00720B": bond},
            index=idx,
        )

    def test_mom_12m_positive_on_uptrend(self) -> None:
        close = self._fake_close()
        m = _mom_12m_at(close["0050"], close.index[-1])
        self.assertIsNotNone(m)
        assert m is not None
        self.assertGreater(m, 0)

    def test_circuit_breaker_de_risks_on_negative_12m(self) -> None:
        asset, exp, note, abs_ok = _allocate(
            "abs_circuit_breaker",
            mom_0050=-0.05,
            mom_00646=0.1,
            mom_bond=0.02,
            rf_annual=0.015,
            risk_off_exposure=0.2,
        )
        self.assertEqual(asset, "0050")
        self.assertAlmostEqual(exp, 0.2)
        self.assertFalse(abs_ok)
        self.assertIn("12M<0", note)

    def test_gem_picks_intl_on_relative_strength(self) -> None:
        asset, note = _pick_gem_asset(0.10, 0.15, 0.02, rf_annual=0.015)
        self.assertEqual(asset, "00646")
        self.assertIn("intl", note)

    def test_backtest_runs(self) -> None:
        close = self._fake_close()
        res = run_strategy_backtest(
            close,
            strategy="abs_circuit_breaker",
            start_date=str(close.index[253]),
        )
        self.assertGreater(len(res.daily), 50)
        self.assertIn("total_return_pct", res.stats)


if __name__ == "__main__":
    unittest.main()
