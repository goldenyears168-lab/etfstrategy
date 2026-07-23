"""Unit tests for range5 × day-up study helpers (no DB)."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_range_dayup_study import (
    CLOSEOUT_SPECS,
    _discover_on_seg1,
    _mean,
    _pred_from_cand,
    _summarize,
    _two_by_two,
    _variant_eval,
    render_range_dayup_closeout_md,
    render_range_dayup_md,
)


def _row(
    *,
    entry: str,
    tp: float,
    range5: float,
    day_up: float,
    range5_atr: float | None = 3.0,
) -> dict:
    return {
        "stock_id": "2330",
        "entry_date": entry,
        "tp_only_ret_pct": tp,
        "range5_pct": range5,
        "range5_atr": range5_atr,
        "day_up_pct": day_up,
        "from_open_pct": day_up,
    }


class TestHelpers(unittest.TestCase):
    def test_summarize_delta_and_drop_top3(self):
        rets = [10.0, 8.0, 6.0, 1.0, 0.0, -1.0]
        s = _summarize(rets, baseline=2.0)
        self.assertEqual(s["n"], 6)
        self.assertAlmostEqual(s["mean_pct"], _mean(rets))
        self.assertIsNotNone(s["delta_drop_top3_pp"])

    def test_two_by_two_hi_vol_up_cell(self):
        rows = []
        # HI vol + up → strong
        for i in range(12):
            rows.append(_row(entry="2025-01-02", tp=5.0, range5=15.0, day_up=1.0))
        # others weak
        for i in range(12):
            rows.append(_row(entry="2025-01-02", tp=0.0, range5=15.0, day_up=-1.0))
        for i in range(12):
            rows.append(_row(entry="2025-01-02", tp=0.5, range5=5.0, day_up=1.0))
        for i in range(12):
            rows.append(_row(entry="2025-01-02", tp=0.5, range5=5.0, day_up=-1.0))
        tw = _two_by_two(rows, vol_key="range5_pct", baseline=1.0)
        hi_up = next(c for c in tw["cells"] if c["vol"] == "HI" and c["day_up"] == "Y")
        self.assertEqual(hi_up["n"], 12)
        self.assertGreater(hi_up["mean_pct"], 4.0)

    def test_seg1_discovery_ranks_positive_delta(self):
        rows = []
        # seg1: high range + up works
        for i in range(10):
            rows.append(_row(entry="2024-05-02", tp=6.0, range5=14.0, day_up=1.0))
        for i in range(20):
            rows.append(_row(entry="2024-05-02", tp=1.0, range5=6.0, day_up=-0.5))
        # filler other segs
        for i in range(10):
            rows.append(_row(entry="2025-05-02", tp=2.0, range5=14.0, day_up=1.0))
        ranked = _discover_on_seg1(rows)
        self.assertTrue(ranked)
        self.assertGreater(ranked[0]["delta_seg1_pp"], 0)

    def test_pred_from_cand_range_and_atr(self):
        r = _row(entry="2024-05-02", tp=1.0, range5=12.0, day_up=0.2, range5_atr=3.5)
        pred_r = _pred_from_cand(
            {"family": "range5", "range_thr": 12.0, "day_up_thr": 0.0, "id": "x"}
        )
        pred_a = _pred_from_cand(
            {"family": "range5_atr", "range_atr_thr": 3.0, "day_up_thr": 0.0, "id": "y"}
        )
        self.assertTrue(pred_r(r))
        self.assertTrue(pred_a(r))
        self.assertFalse(
            _pred_from_cand(
                {"family": "range5", "range_thr": 15.0, "day_up_thr": 0.0, "id": "z"}
            )(r)
        )

    def test_variant_gate(self):
        rows = [_row(entry="2025-01-02", tp=5.0, range5=12.0, day_up=1.0) for _ in range(30)]
        rows += [_row(entry="2025-01-02", tp=0.0, range5=5.0, day_up=-1.0) for _ in range(30)]
        baseline = _mean([float(r["tp_only_ret_pct"]) for r in rows])
        v = _variant_eval(
            rows,
            vid="r>=12&up>0",
            label="t",
            pred=lambda r: float(r["range5_pct"]) >= 12 and float(r["day_up_pct"]) > 0,
            baseline=baseline,
            min_n=30,
            lift_pp=2.0,
        )
        self.assertTrue(v["passes_gate"])

    def test_render_md_contains_verdict(self):
        md = render_range_dayup_md(
            {
                "verdict": "rejected",
                "n_annotated": 10,
                "date_start": "2024-01-02",
                "date_end": "2026-07-01",
                "baseline_mean_tp_pct": 2.0,
                "exit": "champion",
                "hypothesis_id": "H-ENTRY-RANGE-DAYUP-1",
                "lift_pp_gate": 2.0,
                "min_bucket_n": 30,
                "inductive": {"range5_bins": [], "day_up_alone": [], "two_by_two_range5": {"cells": []}},
                "deductive": {"seg1_discovery_ranked": [], "variants": [], "best_variant": {}},
                "schema": "abc_v3_f1_range_dayup-v1",
            }
        )
        self.assertIn("Verdict: rejected", md)
        self.assertIn("H-ENTRY-RANGE-DAYUP-1", md)

    def test_closeout_specs_include_named_and_defat(self):
        ids = {vid for vid, _label, _pred in CLOSEOUT_SPECS}
        self.assertIn("r12_up0", ids)
        self.assertIn("r12_up_band_3", ids)
        self.assertIn("r12_up0_range_cap20", ids)

    def test_closeout_band_pred(self):
        pred = next(p for vid, _l, p in CLOSEOUT_SPECS if vid == "r12_up_band_3")
        ok = _row(entry="2025-01-02", tp=1.0, range5=13.0, day_up=1.5)
        hi = _row(entry="2025-01-02", tp=1.0, range5=13.0, day_up=4.0)
        self.assertTrue(pred(ok))
        self.assertFalse(pred(hi))

    def test_render_closeout_md(self):
        md = render_range_dayup_closeout_md(
            {
                "verdict": "rejected",
                "n_annotated": 10,
                "date_start": "a",
                "date_end": "b",
                "baseline_mean_tp_pct": 2.0,
                "baseline_median_tp_pct": 0.5,
                "hypothesis_id": "H-ENTRY-RANGE-DAYUP-2",
                "goal": "goal text",
                "variants": [],
                "best_variant": {},
                "next_step": "Order-layer champion TP",
                "schema": "abc_v3_f1_range_dayup_closeout-v1",
            }
        )
        self.assertIn("H-ENTRY-RANGE-DAYUP-2", md)
        self.assertIn("Order-layer champion TP", md)


if __name__ == "__main__":
    unittest.main()
