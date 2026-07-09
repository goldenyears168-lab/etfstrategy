"""Tests for abc_v3_slot_sweep."""

from __future__ import annotations

import unittest

from research.backtest.archive.abc_v3_slot_sweep import (
    abc_v3_rank_score_v0,
    apply_score_spec,
    compute_abc_v3_rank_score,
    order_periods_for_fill,
    quintile_forward_table,
    slot_val_overlap_stats,
    spearman_ic,
    split_train_val_dates,
)


class TestAbcV3SlotSweep(unittest.TestCase):
    def test_rank_score_penalizes_0930(self) -> None:
        base = {
            "w20_w3_rv_spread": 4.0,
            "w20_rv": 103.0,
            "etf_hold_count": 2.0,
            "trade_signal": "lead_pullback_buy",
            "entry_poll_idx": 2,
        }
        s_base = abc_v3_rank_score_v0(base)
        s_930 = abc_v3_rank_score_v0({**base, "entry_poll_idx": 1})
        self.assertGreater(s_base, s_930)

    def test_topk_orders_by_score_within_day(self) -> None:
        rows = [
            {"entry_date": "2026-06-01", "entry_frame_idx": 10, "rank_score": 1.0, "stock_id": "A"},
            {"entry_date": "2026-06-01", "entry_frame_idx": 5, "rank_score": 3.0, "stock_id": "B"},
            {"entry_date": "2026-06-02", "entry_frame_idx": 1, "rank_score": 0.5, "stock_id": "C"},
        ]
        fifo = order_periods_for_fill(rows, fill_mode="fifo")
        self.assertEqual([r["stock_id"] for r in fifo], ["B", "A", "C"])
        topk = order_periods_for_fill(rows, fill_mode="topk_score")
        self.assertEqual([r["stock_id"] for r in topk], ["B", "A", "C"])

    def test_score_specs_and_quintiles(self) -> None:
        feats = {
            "w20_w3_rv_spread": 5.0,
            "w20_rv": 103.0,
            "etf_hold_count": 1.0,
            "trade_signal": "lead_pullback_buy",
            "entry_poll_idx": 2,
        }
        self.assertEqual(compute_abc_v3_rank_score(feats, spec_id="v0"), abc_v3_rank_score_v0(feats))
        spread_only = compute_abc_v3_rank_score(feats, spec_id="spread_only")
        self.assertAlmostEqual(spread_only, -5.0)

        rows = [
            {
                "entry_date": "2026-06-01",
                "entry_frame_idx": i,
                "features": {
                    **feats,
                    "w20_w3_rv_spread": -float(i),
                },
                "net_pct": float(i) * 0.5,
            }
            for i in range(10)
        ]
        scored = apply_score_spec(rows, spec_id="spread_only")
        q = quintile_forward_table(scored, min_bucket_n=2)
        self.assertTrue(q["monotonic"])
        self.assertIsNotNone(spearman_ic(scored))

    def test_train_val_split(self) -> None:
        dates = [f"2026-06-{d:02d}" for d in range(1, 91)]
        train, val = split_train_val_dates(dates, train_days=60, val_days=30)
        self.assertEqual(len(train), 60)
        self.assertEqual(len(val), 30)
        self.assertTrue(train[-1] < val[0])

    def test_slot_val_overlap_stats_subset(self) -> None:
        v3 = [
            {"stock_id": "A", "entry_date": "2026-06-10", "net_pct": 1.0},
            {"stock_id": "B", "entry_date": "2026-06-10", "net_pct": -1.0},
            {"stock_id": "C", "entry_date": "2026-06-11", "net_pct": 2.0},
        ]
        f1 = [
            {"stock_id": "A", "entry_date": "2026-06-10", "net_pct": 1.0},
            {"stock_id": "C", "entry_date": "2026-06-11", "net_pct": 2.0},
        ]
        out = slot_val_overlap_stats(v3, f1, ["2026-06-10", "2026-06-11"], n_slots=1)
        totals = out["totals"]
        self.assertEqual(totals["v3_signals"], 3)
        self.assertEqual(totals["f1_signals"], 2)
        self.assertEqual(totals["dropped_by_f1"], 1)
        self.assertEqual(totals["dropped_mean_ret_pct"], -1.0)
        self.assertEqual(totals["kept_mean_ret_pct"], 1.5)


if __name__ == "__main__":
    unittest.main()
