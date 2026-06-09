"""TSM ADR 弱勢：折讓加深 + 科技縮倉（不 block）。"""

from __future__ import annotations

import unittest

from investment_policy import InvestmentPolicy
from order_intent_engine import _compute_size_scale
from pre_trade_check import (
    IntentDraft,
    STATUS_BLOCKED,
    STATUS_DRAFT,
    apply_pre_trade_checks,
)


class TestTsmWeakScale(unittest.TestCase):
    def test_compute_size_scale_for_tech_when_tsm_weak(self) -> None:
        ips = InvestmentPolicy.from_dict(
            {"adr_weak_size_scale": 0.7, "tsm_adr_block_new_tech_pct": -2.0}
        )
        scale = _compute_size_scale(
            open_gap_pct=None,
            ips=ips,
            tsm_adr_pct=-2.24,
            stock_id="2330",
        )
        self.assertEqual(scale, 0.7)

    def test_non_tech_unscaled_when_tsm_weak(self) -> None:
        ips = InvestmentPolicy.from_dict(
            {"adr_weak_size_scale": 0.7, "tsm_adr_block_new_tech_pct": -2.0}
        )
        scale = _compute_size_scale(
            open_gap_pct=None,
            ips=ips,
            tsm_adr_pct=-2.24,
            stock_id="1303",
        )
        self.assertEqual(scale, 1.0)

    def test_pre_trade_no_tsm_block_for_tech(self) -> None:
        ips = InvestmentPolicy.from_dict({})
        sync = type("S", (), {"ok": True, "as_of_date": "2026-06-04", "message": "ok"})()
        draft = IntentDraft(
            trade_date="2026-06-06",
            as_of_date="2026-06-04",
            stock_id="2330",
            stock_name="台積電",
            side="BUY",
            ref_price=2315.0,
            limit_price=2315.0,
            qty=5,
            suggested_ntd=20_000,
            pm_bucket="突破",
            entry_signal="突破",
            entry_tags_json="[]",
            benchmark_type="prev_close",
            benchmark_price=2385.0,
            stop_price=2310.0,
            target_price=2325.0,
            score_version="p4-v2",
            investment_score=72.0,
            chip_tag="",
            size_scale=0.7,
        )
        ctx = apply_pre_trade_checks([draft], ips=ips, sync=sync, tsm_adr_pct=-2.24)
        self.assertEqual(ctx.intents[0].status, STATUS_DRAFT)
        self.assertNotEqual(ctx.intents[0].status, STATUS_BLOCKED)


if __name__ == "__main__":
    unittest.main()
