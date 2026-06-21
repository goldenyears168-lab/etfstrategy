"""regime_interpret · interpretation smoke tests."""

from __future__ import annotations

import unittest

from regime_interpret import interpret_breadth_level, interpret_market_structure


class TestRegimeInterpret(unittest.TestCase):
    def test_breadth_overbought_copy(self) -> None:
        text = interpret_breadth_level(
            {
                "available": True,
                "display": "Overbought · 過熱 (>80%)",
                "breadth_zone_200": "overbought",
                "pct_above_50": 82.6,
                "pct_above_200": 94.4,
                "participation_gap": -11.8,
                "pct50_delta_5d": 11.8,
                "pct200_delta_5d": 2.7,
                "divergence_flag": False,
            }
        )
        self.assertIn("Overbought", text)
        self.assertIn("背離", text)

    def test_breadth_impulse_copy(self) -> None:
        from regime_interpret import interpret_breadth_impulse

        text = interpret_breadth_impulse(
            {
                "available": True,
                "zweig_ema_pct": 58.0,
                "deemer_bam_today": True,
                "thrust_active": True,
                "thrust_days_remaining": 10,
                "thrust_hold_days": 42,
            }
        )
        self.assertIn("Deemer BAM", text)
        self.assertIn("Thrust 窗口進行中", text)

    def test_market_structure_with_rhythm(self) -> None:
        text = interpret_market_structure(
            {
                "available": True,
                "display": "Overbought · 過熱 (>80%)",
                "breadth_zone_200": "overbought",
                "pct_above_200": 94.4,
                "participation_gap": -11.8,
                "rhythm": {
                    "available": True,
                    "zweig_ema_pct": 58.9,
                    "display": "Mid · 中等 (50–58%)",
                    "zweig_ema_tier": "mid",
                },
                "impulse": {"available": True, "thrust_active": False},
            },
            {"available": True, "stage": 2, "stage_name": "advancing"},
            {"available": True, "rotation_health_pct": 45.0},
            {"available": True, "pass_pct": 51.9},
            bench="IX0001",
        )
        self.assertIn("Zweig EMA rhythm", text)
        self.assertIn("高位慣性", text)

    def test_market_structure(self) -> None:
        text = interpret_market_structure(
            {"available": True, "display": "Overbought · 過熱 (>80%)", "breadth_zone_200": "overbought",
             "pct_above_200": 94.4, "participation_gap": -11.8},
            {"available": True, "stage": 2, "stage_name": "advancing"},
            {"available": True, "rotation_health_pct": 45.0},
            {"available": True, "pass_pct": 51.9},
            bench="IX0001",
        )
        self.assertIn("Weinstein Stage 2", text)
        self.assertIn("Minervini template pass rate", text)


if __name__ == "__main__":
    unittest.main()
