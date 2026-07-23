"""RP-5 shape library study unit tests (pure logic, no DB)."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_shape_library_study import (
    _annualized_n,
    _filter_legs,
    _leg_key,
    _overlap_rate,
    default_shape_catalog,
    run_shape_library_study,
)


def _leg(
    *,
    sid: str = "2330",
    entry: str = "2025-01-02",
    tp: float = 9.0,
    gap20: float = 5.0,
    gap53: float = 0.5,
    poll: int = 1,
    z20: float | None = 1.0,
    mv3: float = 101.0,
    w3_quad: str = "improving",
) -> dict:
    return {
        "stock_id": sid,
        "entry_date": entry,
        "tp_ret_pct": tp,
        "return_pct": tp,
        "gate_poll_idx": poll,
        "gate_gap_w20_w5": gap20,
        "gate_gap_w5_w3": gap53,
        "gap_w20_w5_z": z20,
        "gap_w20_w5_pct": 70.0 if z20 and z20 > 0 else 30.0,
        "gap_w5_w3_z": 0.5,
        "rv3_rebound_z": -0.2,
        "entry_t0": {"w3_mv": mv3, "w5_mv": 101.0, "w3_rv": 99.6, "w20_rv": 100.0, "w3_quad": w3_quad},
    }


class TestShapeMatchers(unittest.TestCase):
    def test_catalog_has_at_least_15_shapes(self):
        self.assertGreaterEqual(len(default_shape_catalog()), 15)

    def test_gap_band_base_matches(self):
        shape = next(s for s in default_shape_catalog() if s.id == "gap_band_base_v1")
        self.assertTrue(shape.matcher(_leg(gap20=5.0, gap53=0.0, poll=1)))
        self.assertFalse(shape.matcher(_leg(gap20=1.0, gap53=0.0, poll=1)))

    def test_w3_improving_overlay_requires_base_and_mv(self):
        shape = next(s for s in default_shape_catalog() if s.id == "w3_improving_overlay")
        self.assertTrue(shape.matcher(_leg(mv3=101.0)))
        self.assertFalse(shape.matcher(_leg(mv3=99.0)))

    def test_gap_z_pos_timing(self):
        shape = next(s for s in default_shape_catalog() if s.id == "gap_w20_w5_z_pos")
        self.assertTrue(shape.matcher(_leg(z20=0.5)))
        self.assertFalse(shape.matcher(_leg(z20=-0.1)))


class TestPipelineHelpers(unittest.TestCase):
    def test_filter_legs_by_date(self):
        legs = [_leg(entry="2025-01-02"), _leg(entry="2025-01-03", sid="2317")]
        out = _filter_legs(legs, date_set={"2025-01-02"}, matcher=lambda l: True)
        self.assertEqual(len(out), 1)
        self.assertEqual(_leg_key(out[0]), ("2330", "2025-01-02"))

    def test_overlap_rate(self):
        a = {("2330", "2025-01-02"), ("2317", "2025-01-03")}
        b = {("2330", "2025-01-02"), ("2454", "2025-01-04")}
        self.assertAlmostEqual(_overlap_rate(a, b), 33.3, places=1)

    def test_annualized_n(self):
        self.assertAlmostEqual(_annualized_n(100, n_trading_days=252), 100.0)


class TestRunShapeLibraryStudy(unittest.TestCase):
    def _synthetic_legs(self, n_per_segment: int = 12) -> list[dict]:
        dates = [f"2024-{m:02d}-02" for m in range(1, 13)] + [
            f"2025-{m:02d}-02" for m in range(1, 13)
        ] + [f"2026-{m:02d}-02" for m in range(1, 7)]
        legs: list[dict] = []
        for i, d in enumerate(dates):
            for j in range(n_per_segment):
                tp = 10.0 if (i + j) % 3 == 0 else 2.0
                legs.append(
                    _leg(
                        sid=f"{2300 + j}",
                        entry=d,
                        tp=tp,
                        gap20=5.0 if j % 2 == 0 else 1.0,
                        poll=1 if j % 2 == 0 else 0,
                        z20=1.0 if j % 2 == 0 else -1.0,
                        mv3=101.0 if j % 4 == 0 else 99.0,
                    )
                )
        return legs

    def test_run_produces_schema_and_verdict(self):
        legs = self._synthetic_legs()
        payload = run_shape_library_study(
            legs,
            date_start=legs[0]["entry_date"],
            date_end=legs[-1]["entry_date"],
            target_pct=8.0,
            discovery_min_n=5,
            validation_min_n=5,
        )
        self.assertEqual(payload["schema"], "abc_v3_f1_shape_library-v1")
        self.assertIn(payload["verdict"], ("passed", "not_passed"))
        self.assertEqual(len(payload["shapes"]), len(default_shape_catalog()))
        self.assertIn("union", payload)
        self.assertIn("success_criteria", payload)


if __name__ == "__main__":
    unittest.main()
