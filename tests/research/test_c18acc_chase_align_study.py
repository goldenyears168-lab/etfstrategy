"""Unit tests for c18acc_chase_align_study helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.c18acc_chase_align_study import (
    ChaseGateSpec,
    _blocked_leg_autopsy,
    apply_chase_to_whitelist,
    chase_gate_specs,
)


class TestChaseGate(unittest.TestCase):
    def test_specs_include_combo(self) -> None:
        ids = {s.variant_id for s in chase_gate_specs()}
        self.assertIn("G_COMBO", ids)
        self.assertIn("G_RV105", ids)
        self.assertIn("G_GAP3", ids)

    def test_apply_blocks_high_rv(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(2, 12)]
        idx = pd.Index(dates)
        rs = pd.DataFrame(
            {"A": [100.0] * len(dates), "B": [100.0] * len(dates)},
            index=idx,
        )
        rs.loc["2026-01-09", "A"] = 110.0
        rs.loc["2026-01-09", "B"] = 101.0
        close = pd.DataFrame(
            {"A": [100.0 + i for i in range(len(dates))], "B": [50.0] * len(dates)},
            index=idx,
        )
        open_px = close.copy()
        base = {"2026-01-10": {"A", "B"}}
        spec = ChaseGateSpec("G_RV105", "rv", lambda f: (f.get("w20_rv") or 0) > 105)
        gated, meta = apply_chase_to_whitelist(
            base,
            close=close,
            open_px=open_px,
            rs_ratio=rs,
            trade_dates=["2026-01-10"],
            full_dates=dates,
            spec=spec,
        )
        self.assertEqual(gated["2026-01-10"], {"B"})
        self.assertEqual(meta["blocked_stock_days"], 1)

    def test_blocked_autopsy(self) -> None:
        baseline = [
            {"stock_id": "1", "entry_date": "2026-01-02", "return_pct": -10.0},
            {"stock_id": "2", "entry_date": "2026-01-03", "return_pct": 20.0},
        ]
        gated = [
            {"stock_id": "2", "entry_date": "2026-01-03", "return_pct": 20.0},
            {"stock_id": "3", "entry_date": "2026-01-04", "return_pct": 5.0},
        ]
        au = _blocked_leg_autopsy(baseline, gated)
        self.assertEqual(au["n_blocked"], 1)
        self.assertEqual(au["n_added_replacement"], 1)
        self.assertEqual(au["mean_ret_blocked"], -10.0)


if __name__ == "__main__":
    unittest.main()
