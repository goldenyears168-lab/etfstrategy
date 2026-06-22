"""regime_daily_brief：memo 渲染與路徑。"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from regime_charts import RegimeChartPaths
from regime_daily_brief import render_regime_daily_markdown, write_regime_daily_reports
from stock_db import connect


SAMPLE_SNAP = {
    "as_of": "2026-06-18",
    "benchmark_code": "IX0001",
    "breadth_zone_200": {
        "available": True,
        "display": "Overbought · 過熱 (>80%)",
        "breadth_zone_200": "overbought",
        "pct_above_50": 82.6,
        "pct_above_200": 94.4,
        "participation_gap": -11.8,
        "pct50_delta_5d": 11.8,
        "pct200_delta_5d": 2.7,
        "divergence_flag": False,
        "n_valid": 125,
        "rhythm": {
            "available": True,
            "zweig_ema_pct": 62.3,
            "zweig_ema_tier": "high",
            "display": "High · 偏強 (≥58%)",
            "zweig_ema_delta_5d": 2.1,
        },
        "impulse": {
            "available": True,
            "deemer_ratio": 2.05,
            "zweig_thrust_today": False,
            "deemer_bam_today": True,
            "thrust_active": True,
            "thrust_days_remaining": 18,
            "thrust_hold_days": 42,
        },
    },
    "trend_posture": {
        "available": True,
        "stage": 2,
        "stage_name": "advancing",
        "trend_posture": "concentration",
        "weinstein": {
            "ma_slope_pct": 7.14,
            "extension_pct": 33.5,
            "higher_lows": True,
            "price_above_ma30": True,
        },
        "minervini": {
            "criteria": [True, True, True, True, True, True, True, False],
            "criteria_met": 7,
            "criteria_total": 8,
            "criteria_detail": {
                "c1_price_above_sma150_200": {"passed": True},
                "c8_rs_rank_above_70": {"passed": False},
            },
        },
    },
    "rrg_rotation": {
        "available": True,
        "universe_n": 149,
        "rotation_health_pct": 45.0,
        "leading_pct": 23.5,
        "improving_pct": 21.5,
        "weakening_pct": 14.1,
        "lagging_pct": 40.9,
        "dominant_label": "Lagging",
        "counts": {"leading": 35, "weakening": 21, "lagging": 61, "improving": 32},
        "pct": {"leading": 23.5, "weakening": 14.1, "lagging": 40.9, "improving": 21.5},
        "migrations": {"improving_to_leading": 3, "leading_to_weakening": 1,
                       "lagging_to_improving": 2, "weakening_to_lagging": 0},
        "ranked_symbols": [
            {"stock_id": "2330", "quadrant": "leading", "rs_ratio": 105.2,
             "rs_momentum": 102.1, "tail_dir": "↗ up-right"},
            {"stock_id": "2454", "quadrant": "improving", "rs_ratio": 99.1,
             "rs_momentum": 101.3, "tail_dir": "↑ down-left"},
        ],
    },
    "stage2_participation": {
        "available": True,
        "pass_pct": 51.9,
        "pass_delta_5d": 10.8,
        "universe_n": 149,
        "min_criteria": 7,
        "criteria_total": 8,
        "note": "bulk scan ≥7/8 (RS omitted)",
    },
}


class TestRegimeDailyBriefRender(unittest.TestCase):
    def test_render_memo_sections(self) -> None:
        charts = RegimeChartPaths(
            breadth_spark="axis/breadth/spark.svg",
            weinstein_weekly="axis/trend/weinstein_weekly.svg",
            rrg_scatter="axis/rrg/scatter.svg",
            participation_spark="axis/stage2/participation_spark.svg",
        )
        md = render_regime_daily_markdown(
            copy.deepcopy(SAMPLE_SNAP), ref="2026-06-18", bench="IX0001", charts=charts
        )
        self.assertIn("# Market environment memo · 2026-06-18", md)
        self.assertIn("Regime four-axis diagnostic", md)
        self.assertIn("## Daily synopsis", md)
        self.assertIn("**Notes**", md)
        self.assertIn("Stage 2 · advancing（上升）", md)
        self.assertIn("| Criterion | 說明 | Pass |", md)
        self.assertIn("Improving → Leading", md)
        self.assertIn("RRG symbol table", md)
        self.assertIn("1A · Breadth level", md)
        self.assertIn("1B · Zweig EMA rhythm tier", md)
        self.assertIn("1C · Breadth impulse", md)
        self.assertIn("2330", md)
        self.assertIn("axis/trend/weinstein_weekly.svg", md)
        self.assertNotIn("## Artifacts", md)
        self.assertNotIn("## Readout", md)


class TestRegimeDailyBrief(unittest.TestCase):
    @patch("regime_daily_brief.write_regime_charts", return_value=RegimeChartPaths())
    @patch("regime_daily_brief.build_regime_snapshot", return_value=copy.deepcopy(SAMPLE_SNAP))
    @patch(
        "regime_daily_brief.render_regime_daily_markdown",
        return_value="# Market environment memo · 2026-06-20\n",
    )
    def test_write_reports_stays_in_temp_dir(self, _mock_render, _mock_snap, _mock_charts) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            conn = connect(tmp_root / "t.db")
            track_dir = tmp_root / "daily" / "regime"
            path = write_regime_daily_reports(
                conn,
                track_dir=track_dir,
                as_of="2026-06-20",
                quiet=True,
            )
            self.assertTrue(path.is_file())
            dated = track_dir / "snapshots" / "20260620" / "daily_brief.md"
            self.assertTrue(dated.is_file())
            conn.close()


if __name__ == "__main__":
    unittest.main()
