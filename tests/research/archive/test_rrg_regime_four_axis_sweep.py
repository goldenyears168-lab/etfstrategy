"""Tests for RRG × Regime four-axis Phase 1 sweep."""

from __future__ import annotations

import unittest

from research.backtest.archive.rrg_regime_four_axis_sweep import (
    _apply_gate,
    _event_metrics,
    _sweep_one,
    phase1_gate_specs,
)


def _evt(
    *,
    d: str = "2024-06-03",
    s2: bool = True,
    s0: bool = False,
    s3: bool = False,
    bench_b0: bool = True,
    breadth_ns: bool = True,
    nxt: float = 0.5,
    excess: float = 0.2,
) -> dict:
    return {
        "date": d,
        "stock_id": "2330",
        "s2_setup_core": s2,
        "s0_fresh_flip": s0,
        "s3_burst": s3,
        "end_q": "improving",
        "bench_b0": bench_b0,
        "bench_bucket": "b0" if bench_b0 else "neg",
        "breadth_neutral_strong": breadth_ns,
        "breadth_overbought": not breadth_ns,
        "stage2_bucket": "high",
        "weinstein_stage2": True,
        "universe_improving_pct": 45.0,
        "universe_rotation_health_pct": 55.0,
        "nxt_ret": nxt,
        "excess_nxt": excess,
        "bench_today_ret": 0.1 if bench_b0 else -0.5,
    }


class TestRrgRegimeFourAxisSweep(unittest.TestCase):
    def test_apply_gate_s2_b0(self) -> None:
        events = [
            _evt(s2=True, bench_b0=True),
            _evt(s2=True, bench_b0=False, d="2024-06-04"),
            _evt(s2=False, bench_b0=True, d="2024-06-05"),
        ]
        specs = [s for s in phase1_gate_specs() if s.gate_id == "G-B0" and s.cohort == "S2"]
        self.assertEqual(len(specs), 1)
        out = _apply_gate(events, cohort="S2", predicate=specs[0].predicate)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["bench_b0"])

    def test_event_metrics(self) -> None:
        m = _event_metrics([_evt(nxt=1.0), _evt(nxt=-0.5, excess=-0.8)])
        self.assertEqual(m["n_events"], 2)
        self.assertEqual(m["mean_nxt_ret_pct"], 0.25)
        self.assertEqual(m["win_rate"], 0.5)

    def test_sweep_one_improves_with_gate(self) -> None:
        events = [
            _evt(d="2024-06-03", bench_b0=True, nxt=1.0, excess=0.8),
            _evt(d="2024-06-04", bench_b0=False, nxt=-1.0, excess=-1.2),
            _evt(d="2025-06-03", bench_b0=True, nxt=0.6, excess=0.4),
            _evt(d="2026-06-03", bench_b0=False, nxt=-0.8, excess=-1.0),
        ]
        specs = {f"{s.gate_id}:{s.cohort}": s for s in phase1_gate_specs()}
        r0 = _sweep_one(
            events,
            specs["R0:S2"],
            splits={"FULL": (None, None), "IS": ("2024-01-01", "2025-12-31"), "OOS": ("2026-01-01", None)},
        )
        g0 = _sweep_one(
            events,
            specs["G-B0:S2"],
            splits={"FULL": (None, None), "IS": ("2024-01-01", "2025-12-31"), "OOS": ("2026-01-01", None)},
        )
        self.assertEqual(r0["FULL"]["n_events"], 4)
        self.assertEqual(g0["FULL"]["n_events"], 2)
        self.assertGreater(
            float(g0["FULL"]["mean_nxt_ret_pct"] or -999),
            float(r0["FULL"]["mean_nxt_ret_pct"] or -999),
        )


if __name__ == "__main__":
    unittest.main()
