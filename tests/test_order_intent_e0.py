"""E0：pre-trade、approve、sync health。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from investment_policy import InvestmentPolicy
from pre_trade_check import (
    STATUS_BLOCKED,
    STATUS_DRAFT,
    IntentDraft,
    apply_pre_trade_checks,
    assess_sync_health,
)
import sqlite3

from order_intent_engine import _format_limit_price_line
from stock_db import (
    INTENT_VERSION_DEFAULT,
    approve_order_intents,
    connect,
    upsert_order_intents,
    upsert_pm_watchlist,
    upsert_portfolio_weights,
)


def _seed_watchlist(conn: sqlite3.Connection, as_of: str = "2026-06-04") -> None:
    upsert_pm_watchlist(
        conn,
        [
            {
                "stock_id": "2330",
                "as_of_date": as_of,
                "score_version": "p4-v2",
                "stock_name": "台積電",
                "investment_score": 72.0,
                "watchlist": "首要觀察",
                "entry_signal": "突破",
                "entry_tags_json": "[]",
                "chip_tag": "法人中性",
                "pm_bucket": "突破",
                "flow_score": 70,
                "chip_score": 50,
                "tech_score": 60,
                "catalyst_score": 0,
                "fundamental_score": 50,
                "note": "",
            },
            {
                "stock_id": "2454",
                "as_of_date": as_of,
                "score_version": "p4-v2",
                "stock_name": "聯發科",
                "investment_score": 40.0,
                "watchlist": "不列入",
                "entry_signal": "乖離過大",
                "entry_tags_json": "[]",
                "chip_tag": "法人中性",
                "pm_bucket": "回避",
                "flow_score": 40,
                "chip_score": 50,
                "tech_score": 40,
                "catalyst_score": 0,
                "fundamental_score": 50,
                "note": "",
            },
        ],
    )
    upsert_portfolio_weights(
        conn,
        [
            {
                "stock_id": "2330",
                "as_of_date": as_of,
                "score_version": "p4-v2",
                "stock_name": "台積電",
                "watchlist": "首要觀察",
                "position_score": 70,
                "risk_score": 60,
                "portfolio_weight_pct": 25,
                "suggested_ntd": 25000,
                "capital_ntd": 100000,
                "entry_signal": "突破",
                "entry_tags_json": "[]",
                "pm_bucket": "突破",
                "note": "",
            },
        ],
    )


class TestOrderIntentE0(unittest.TestCase):
    def test_sync_health_requires_prior_close(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            _seed_watchlist(conn, as_of="2026-06-05")
            ips = InvestmentPolicy.from_dict({"require_evening_sync_ok": True})
            bad = assess_sync_health(conn, trade_date="2026-06-05", ips=ips)
            self.assertFalse(bad.ok)
            good = assess_sync_health(conn, trade_date="2026-06-06", ips=ips)
            self.assertTrue(good.ok)
            conn.close()

    def test_pre_trade_blocks_avoid_bucket(self) -> None:
        ips = InvestmentPolicy.from_dict({})
        sync = type("S", (), {"ok": True, "as_of_date": "2026-06-04", "message": "ok"})()
        drafts = [
            IntentDraft(
                trade_date="2026-06-05",
                as_of_date="2026-06-04",
                stock_id="2454",
                stock_name="聯發科",
                side="BUY",
                ref_price=1200.0,
                limit_price=1200.0,
                qty=1,
                suggested_ntd=25000,
                pm_bucket="回避",
                entry_signal="乖離過大",
                entry_tags_json="[]",
                benchmark_type="prev_close",
                benchmark_price=1200.0,
                stop_price=None,
                target_price=None,
                score_version="p4-v2",
                investment_score=40,
                chip_tag="法人中性",
            )
        ]
        ctx = apply_pre_trade_checks(drafts, ips=ips, sync=sync, tsm_adr_pct=0.0)
        self.assertEqual(ctx.intents[0].status, STATUS_BLOCKED)

    def test_approve_only_draft_without_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            trade = "2026-06-05"
            upsert_order_intents(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "trade_date": trade,
                        "intent_version": INTENT_VERSION_DEFAULT,
                        "as_of_date": "2026-06-04",
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "side": "BUY",
                        "ref_price": 1000.0,
                        "limit_price": 1000.0,
                        "qty": 25,
                        "suggested_ntd": 25000,
                        "pm_bucket": "突破",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "benchmark_type": "prev_close",
                        "benchmark_price": 1000.0,
                        "stop_price": 970.0,
                        "target_price": 1045.0,
                        "order_type_planned": "pending_open",
                        "open_price": None,
                        "order_type_effective": None,
                        "status": STATUS_DRAFT,
                        "block_reason": "",
                        "ips_version": "ips-v1",
                        "chip_tag": "法人中性",
                        "investment_score": 72.0,
                    },
                    {
                        "stock_id": "2454",
                        "trade_date": trade,
                        "intent_version": INTENT_VERSION_DEFAULT,
                        "as_of_date": "2026-06-04",
                        "score_version": "p4-v2",
                        "stock_name": "聯發科",
                        "side": "BUY",
                        "ref_price": 1200.0,
                        "limit_price": 1200.0,
                        "qty": 1,
                        "suggested_ntd": 25000,
                        "pm_bucket": "回避",
                        "entry_signal": "乖離過大",
                        "entry_tags_json": "[]",
                        "benchmark_type": "prev_close",
                        "benchmark_price": 1200.0,
                        "stop_price": None,
                        "target_price": None,
                        "order_type_planned": "pending_open",
                        "open_price": None,
                        "order_type_effective": None,
                        "status": STATUS_BLOCKED,
                        "block_reason": "pm_bucket=回避",
                        "ips_version": "ips-v1",
                        "chip_tag": "法人中性",
                        "investment_score": 40.0,
                    },
                ],
            )
            n = approve_order_intents(conn, trade_date=trade)
            self.assertEqual(n, 1)
            conn.close()


class TestLimitPriceFormat(unittest.TestCase):
    def test_intraday_shows_snapshot_and_limit(self) -> None:
        it = IntentDraft(
            trade_date="2026-06-05",
            as_of_date="2026-06-04",
            stock_id="6223",
            stock_name="旺矽科技",
            side="BUY",
            ref_price=5480.0,
            limit_price=5480.0,
            qty=1,
            suggested_ntd=25000,
            pm_bucket="觀望",
            entry_signal="觀望",
            entry_tags_json="[]",
            benchmark_type="prev_close",
            benchmark_price=6070.0,
            stop_price=5370.0,
            target_price=None,
            score_version="p4-v2",
            investment_score=63.2,
            chip_tag="",
            price_snapshot=5760.0,
            open_gap_pct=-5.11,
        )
        line = _format_limit_price_line(it, evaluation_mode="intraday")
        self.assertIn("現價 5,760", line)
        self.assertIn("限價 5,480", line)
        self.assertIn("距限價 +5.11%", line)
        self.assertIn("gap昨收 -5.11%", line)

    def test_pre_open_shows_limit_only(self) -> None:
        it = IntentDraft(
            trade_date="2026-06-05",
            as_of_date="2026-06-04",
            stock_id="2330",
            stock_name="台積電",
            side="BUY",
            ref_price=2275.0,
            limit_price=2275.0,
            qty=8,
            suggested_ntd=100000,
            pm_bucket="突破",
            entry_signal="突破",
            entry_tags_json="[]",
            benchmark_type="prev_close",
            benchmark_price=2380.0,
            stop_price=2205.0,
            target_price=None,
            score_version="p4-v2",
            investment_score=62.7,
            chip_tag="",
        )
        line = _format_limit_price_line(it, evaluation_mode="pre_open")
        self.assertEqual(line, "2330 台積電  限價 2,275")


if __name__ == "__main__":
    unittest.main()
