"""regime_daily_html · inline SVG smoke test."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from regime_charts import RegimeChartPaths
from regime_daily_html import _rich_text, render_regime_daily_html

SAMPLE = {
    "breadth_zone_200": {"available": True, "display": "Overbought · 過熱 (>80%)",
                         "pct_above_50": 82.6, "pct_above_200": 94.4, "participation_gap": -11.8,
                         "pct50_delta_5d": 1.0, "pct200_delta_5d": 0.5, "divergence_flag": False,
                         "breadth_zone_200": "overbought"},
    "trend_posture": {"available": False},
    "rrg_rotation": {
        "available": True,
        "rotation_health_pct": 45.0,
        "counts": {"leading": 1, "improving": 1, "weakening": 0, "lagging": 0},
        "pct": {"leading": 50.0, "improving": 50.0, "weakening": 0.0, "lagging": 0.0},
        "ranked_symbols": [
            {"stock_id": "2330", "quadrant": "leading", "rs_ratio": 105.2,
             "rs_momentum": 102.1, "tail_dir": "↗ up-right"},
        ],
    },
    "stage2_participation": {"available": False},
}


class TestRegimeDailyHtml(unittest.TestCase):
    def test_inlines_svg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = "axis/breadth/spark.svg"
            svg_path = root / rel
            svg_path.parent.mkdir(parents=True)
            svg_path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                '<rect width="10" height="10" fill="red"/></svg>',
                encoding="utf-8",
            )
            html_doc = render_regime_daily_html(
                SAMPLE,
                ref="2026-06-18",
                bench="IX0001",
                charts=RegimeChartPaths(breadth_spark=rel),
                track_dir=root,
            )
            self.assertIn("<svg", html_doc)
            self.assertIn('fill="red"', html_doc)
            self.assertIn("kpi-strip", html_doc)

    def test_embed_fragment(self) -> None:
        from regime_daily_html import render_regime_embed_html
        frag = render_regime_embed_html(
            SAMPLE, ref="2026-06-18", bench="IX0001",
            charts=RegimeChartPaths(), track_dir=Path("."),
        )
        self.assertIn('class="regime-embed"', frag)
        self.assertNotIn("<!DOCTYPE html>", frag)
        self.assertNotIn("Cursor", frag)

    def test_rich_text_and_rrg_table(self) -> None:
        self.assertIn("<strong>bold</strong>", _rich_text("pre **bold** post"))
        html_doc = render_regime_daily_html(
            SAMPLE,
            ref="2026-06-18",
            bench="IX0001",
            charts=RegimeChartPaths(),
            track_dir=Path("."),
        )
        self.assertIn("RRG symbol table", html_doc)
        self.assertIn("市場結構日報", html_doc)
        self.assertIn("2330", html_doc)
        self.assertNotIn("**bold**", html_doc)


if __name__ == "__main__":
    unittest.main()
