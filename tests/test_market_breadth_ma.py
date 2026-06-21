"""Tests for market_breadth_ma."""

from __future__ import annotations

import unittest

import pandas as pd

from market_breadth_ma import (
    BREADTH_ZONES_ORDER,
    classify_breadth_zone,
    compute_ma_breadth_frame,
    enrich_breadth_panel,
)


class TestMarketBreadthMa(unittest.TestCase):
    def test_classify_tv_zones(self) -> None:
        self.assertEqual(classify_breadth_zone(15), "oversold")
        self.assertEqual(classify_breadth_zone(30), "weak")
        self.assertEqual(classify_breadth_zone(50), "neutral")
        self.assertEqual(classify_breadth_zone(70), "strong")
        self.assertEqual(classify_breadth_zone(85), "overbought")

    def test_zone_order_complete(self) -> None:
        self.assertEqual(len(BREADTH_ZONES_ORDER), 5)

    def test_compute_synthetic_universe(self) -> None:
        dates = pd.date_range("2023-01-01", periods=260, freq="B").strftime("%Y-%m-%d")
        idx = list(dates)
        data = {f"S{i:02d}": [100 + i + d * 0.1 for d in range(len(idx))] for i in range(50)}
        close = pd.DataFrame(data, index=idx)
        frame = compute_ma_breadth_frame(close)
        last = frame.dropna(subset=["pct_above_50"]).iloc[-1]
        self.assertGreaterEqual(float(last["pct_above_50"]), 99.0)

    def test_enrich_adds_zones(self) -> None:
        frame = pd.DataFrame(
            {
                "trade_date": ["2024-06-01", "2024-06-02"],
                "pct_above_50": [70.0, 30.0],
                "pct_above_200": [65.0, 25.0],
                "n_valid_50": [100, 100],
                "n_valid_200": [100, 100],
            }
        )
        bench = pd.Series({"2024-06-01": 100.0, "2024-06-02": 105.0})
        panel = enrich_breadth_panel(frame, bench)
        self.assertEqual(panel.iloc[0]["zone_200"], "strong")
        self.assertEqual(panel.iloc[1]["zone_200"], "weak")


if __name__ == "__main__":
    unittest.main()
