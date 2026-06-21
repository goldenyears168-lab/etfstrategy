"""inst_flow_981a_overlap · leg 互補桶切分。"""

from __future__ import annotations

import unittest

from research.backtest.copytrade_backtest import CopytradeSignal
from research.backtest.inst_flow_981a_overlap import compute_overlap_stats, split_overlap_buckets


def _sig(day: str, sid: str) -> CopytradeSignal:
    return CopytradeSignal(
        signal_date=day,
        stock_id=sid,
        stock_name=sid,
        action="加码",
        share_delta=100.0,
        weight_delta=None,
        weight_pct_curr=None,
    )


class TestInstFlow981aOverlap(unittest.TestCase):
    def test_split_buckets_disjoint_union(self) -> None:
        inst = [_sig("2026-06-01", "2330"), _sig("2026-06-02", "2317")]
        etf = [_sig("2026-06-01", "2330"), _sig("2026-06-03", "2454")]
        buckets = split_overlap_buckets(inst, etf)
        self.assertEqual(len(buckets["both"]), 1)
        self.assertEqual(buckets["both"][0].stock_id, "2330")
        self.assertEqual(len(buckets["inst_only"]), 1)
        self.assertEqual(buckets["inst_only"][0].stock_id, "2317")
        self.assertEqual(len(buckets["etf_only"]), 1)
        self.assertEqual(buckets["etf_only"][0].stock_id, "2454")
        self.assertEqual(len(buckets["union"]), 3)

    def test_overlap_stats(self) -> None:
        inst = [_sig("2026-06-01", "2330"), _sig("2026-06-02", "2317")]
        etf = [_sig("2026-06-01", "2330"), _sig("2026-06-01", "2454")]
        stats = compute_overlap_stats(inst, etf)
        self.assertEqual(stats["both_legs"], 1)
        self.assertEqual(stats["inst_only_legs"], 1)
        self.assertEqual(stats["etf_only_legs"], 1)
        self.assertEqual(stats["both_days"], 1)
        self.assertEqual(stats["inst_only_days"], 1)


if __name__ == "__main__":
    unittest.main()
