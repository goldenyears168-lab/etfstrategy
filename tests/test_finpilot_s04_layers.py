"""finpilot_s04_layers · Mom lookback 參數化。"""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.finpilot_s04_layers import S04LayerSpec, S04_LAYER_SPECS, _mom_series, select_s04_layer


class TestFinpilotS04MomLookback(unittest.TestCase):
    def test_mom_series_respects_lookback(self) -> None:
        idx = pd.date_range("2026-01-01", periods=80, freq="B")
        close = pd.DataFrame(
            {"2330": range(100, 180)},
            index=[d.strftime("%Y-%m-%d") for d in idx],
        )
        signal = close.index[-1]
        m5 = _mom_series(close, signal, lookback=5)
        m60 = _mom_series(close, signal, lookback=60)
        self.assertIsNotNone(m5)
        self.assertIsNotNone(m60)
        self.assertAlmostEqual(float(m5["2330"]), 179 / 175)
        self.assertAlmostEqual(float(m60["2330"]), 179 / 120)

    def test_select_s04_layer_mom10(self) -> None:
        idx = pd.date_range("2026-01-01", periods=30, freq="B")
        dates = [d.strftime("%Y-%m-%d") for d in idx]
        close = pd.DataFrame(
            {
                "A": [10 + i for i in range(30)],
                "B": [20 - i for i in range(30)],
            },
            index=dates,
        )
        spec = S04LayerSpec("T", "test top1", "mom_top10", mom_top_n=1)
        fund = {
            "A": {"roe_latest_q": 5.0},
            "B": {"roe_latest_q": 5.0},
        }
        picks, meta = select_s04_layer(
            spec,
            signal_date=dates[-1],
            close=close,
            fund_snap=fund,
            mom_lookback=10,
        )
        self.assertEqual(picks, ["A"])
        self.assertEqual(meta["mom_lookback"], 10)


if __name__ == "__main__":
    unittest.main()
