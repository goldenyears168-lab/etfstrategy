"""regime_charts · SVG render smoke tests."""

from __future__ import annotations

import unittest

import pandas as pd

from regime_charts import (
    rank_rrg_points,
    render_breadth_spark_svg,
    render_rrg_scatter_svg,
    render_weinstein_weekly_svg,
    tail_direction_label,
)


class TestRegimeCharts(unittest.TestCase):
    def test_breadth_spark_svg(self) -> None:
        points = [
            {
                "d": "2026-06-01",
                "p50": 70.0,
                "p200": 80.0,
                "z200zh": "強勢",
                "c": "#1F8A65",
                "bench": 22000.0,
                "div": False,
            },
            {
                "d": "2026-06-18",
                "p50": 82.6,
                "p200": 94.4,
                "z200zh": "過熱",
                "c": "#C04848",
                "bench": 22500.0,
                "div": False,
            },
        ]
        svg = render_breadth_spark_svg(points)
        self.assertIn("<svg", svg)
        self.assertIn("94.4", svg)

    def test_rrg_scatter_svg(self) -> None:
        points = [
            {
                "stock_id": "2330",
                "rs_ratio": 102.0,
                "rs_momentum": 101.0,
                "quadrant": "leading",
                "trail": [(100.5, 99.8), (101.2, 100.4), (102.0, 101.0)],
            },
            {"stock_id": "2317", "rs_ratio": 98.0, "rs_momentum": 99.0, "quadrant": "lagging", "trail": []},
        ]
        svg = render_rrg_scatter_svg(points, as_of="2026-06-18")
        self.assertIn("<svg", svg)
        self.assertIn("2330", svg)
        self.assertIn("Leading", svg)

    def test_rrg_rank_and_tail(self) -> None:
        points = [
            {"stock_id": "2330", "rs_ratio": 105.0, "rs_momentum": 103.0,
             "quadrant": "leading", "trail": [(100.0, 99.0), (105.0, 103.0)]},
            {"stock_id": "2317", "rs_ratio": 98.0, "rs_momentum": 101.0,
             "quadrant": "improving", "trail": [(97.0, 100.0), (98.0, 101.0)]},
        ]
        ranked = rank_rrg_points(points, per_quadrant=5)
        self.assertEqual(ranked[0]["stock_id"], "2330")
        self.assertIn("tail_dir", ranked[0])
        self.assertEqual(tail_direction_label([(100.0, 99.0), (105.0, 103.0)]), "↗ up-right")

    def test_weinstein_ribbon(self) -> None:
        idx = pd.date_range("2024-01-01", periods=80, freq="W-FRI")
        close = pd.Series(range(100, 180), index=idx, dtype=float)
        df = pd.DataFrame({"Close": close, "Open": close, "High": close, "Low": close, "Volume": 1})
        svg = render_weinstein_weekly_svg(
            df, bench="IX0001", stage=2, stage_name="advancing"
        )
        self.assertIn("Stage ribbon", svg)
        self.assertIn('height="16"', svg)
        self.assertIn("S2 advancing", svg)


if __name__ == "__main__":
    unittest.main()
