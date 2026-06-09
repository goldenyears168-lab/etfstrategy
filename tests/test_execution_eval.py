"""execution_eval：核准保護、pre_open、auction gap 重算。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution_eval import resolve_evaluation_prices, run_evaluation
from investment_policy import InvestmentPolicy
from order_intent_engine import run_generate
from pre_trade_check import STATUS_APPROVED, STATUS_DRAFT
from stock_db import (
    INTENT_VERSION_DEFAULT,
    connect,
    count_approved_order_intents,
    upsert_order_intents,
    upsert_pm_watchlist,
    upsert_portfolio_weights,
    upsert_stock_daily_bars,
)


def _seed_bars(conn, stock_id: str = "2330", as_of: str = "2026-06-04") -> None:
    upsert_stock_daily_bars(
        conn,
        [
            {
                "stock_id": stock_id,
                "trade_date": as_of,
                "open": 1000.0,
                "high": 1010.0,
                "low": 990.0,
                "close": 1000.0,
                "volume": 1000,
                "source": "finmind",
            },
        ],
    )


def _seed_watchlist(conn, as_of: str = "2026-06-04") -> None:
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
                "portfolio_weight_pct": 20,
                "suggested_ntd": 20000,
                "capital_ntd": 100000,
                "entry_signal": "突破",
                "entry_tags_json": "[]",
                "pm_bucket": "突破",
                "note": "",
            },
        ],
    )


class TestExecutionEval(unittest.TestCase):
    def test_persist_blocked_when_approved_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            trade = "2026-06-06"
            _seed_watchlist(conn, as_of="2026-06-04")
            _seed_bars(conn)
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
                        "status": STATUS_APPROVED,
                        "block_reason": "",
                        "ips_version": "ips-v1",
                        "chip_tag": "法人中性",
                        "investment_score": 72.0,
                        "evaluation_mode": "pre_open",
                        "price_source": "last_close",
                        "eval_run_id": "old",
                    },
                ],
            )
            ips = InvestmentPolicy.from_dict({})
            code, ctx = run_generate(
                conn,
                trade_date=trade,
                ips=ips,
                pre_trade=True,
                quiet=True,
                persist=True,
            )
            self.assertEqual(code, 2)
            self.assertIsNone(ctx)
            self.assertEqual(
                count_approved_order_intents(conn, trade_date=trade),
                1,
            )
            conn.close()

    def test_force_regenerate_demotes_approved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            trade = "2026-06-06"
            _seed_watchlist(conn, as_of="2026-06-04")
            _seed_bars(conn)
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
                        "status": STATUS_APPROVED,
                        "block_reason": "",
                        "ips_version": "ips-v1",
                        "chip_tag": "法人中性",
                        "investment_score": 72.0,
                    },
                ],
            )
            ips = InvestmentPolicy.from_dict({"min_risk_reward": 1.0})
            code, ctx = run_generate(
                conn,
                trade_date=trade,
                ips=ips,
                pre_trade=True,
                quiet=True,
                force_regenerate=True,
                evaluation_mode="pre_open",
                price_source="last_close",
                eval_run_id="test-run",
                persist=True,
            )
            self.assertEqual(code, 0)
            self.assertIsNotNone(ctx)
            self.assertEqual(
                count_approved_order_intents(conn, trade_date=trade),
                0,
            )
            row = conn.execute(
                "SELECT status, evaluation_mode FROM order_intents WHERE stock_id='2330'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], STATUS_DRAFT)
            self.assertEqual(row["evaluation_mode"], "pre_open")
            conn.close()

    def test_auction_requires_prices(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            _seed_watchlist(conn, as_of="2026-06-04")
            ips = InvestmentPolicy.from_dict({})
            code = run_evaluation(
                conn,
                trade_date="2026-06-06",
                ips=ips,
                mode="auction",
                persist=True,
                preview=False,
                force_regenerate=False,
                prices="",
                quiet=True,
            )
            self.assertEqual(code, 2)
            conn.close()

    def test_auction_updates_ref_with_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            trade = "2026-06-06"
            _seed_watchlist(conn, as_of="2026-06-04")
            _seed_bars(conn, as_of="2026-06-04")
            ips = InvestmentPolicy.from_dict(
                {
                    "min_risk_reward": 1.0,
                    "max_open_gap_pct": 10.0,
                }
            )
            code = run_evaluation(
                conn,
                trade_date=trade,
                ips=ips,
                mode="auction",
                persist=True,
                preview=False,
                force_regenerate=False,
                prices="2330=1050",
                quiet=True,
            )
            self.assertEqual(code, 0)
            row = conn.execute(
                """
                SELECT ref_price, price_snapshot, evaluation_mode, size_scale
                FROM order_intents WHERE stock_id='2330'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["evaluation_mode"], "auction")
            self.assertEqual(row["price_snapshot"], 1050.0)
            self.assertLess(row["ref_price"], 1050.0)
            conn.close()

    def test_auction_gap_shrinks_qty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            trade = "2026-06-06"
            _seed_watchlist(conn, as_of="2026-06-04")
            _seed_bars(conn, as_of="2026-06-04")
            ips = InvestmentPolicy.from_dict(
                {
                    "min_risk_reward": 1.0,
                    "max_open_gap_pct": 3.0,
                    "gap_size_multiplier": 0.5,
                }
            )
            code = run_evaluation(
                conn,
                trade_date=trade,
                ips=ips,
                mode="auction",
                persist=True,
                preview=False,
                force_regenerate=False,
                prices="2330=1050",
                quiet=True,
            )
            self.assertEqual(code, 0)
            row = conn.execute(
                "SELECT qty, size_scale FROM order_intents WHERE stock_id='2330'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["size_scale"], 0.5)
            self.assertLess(row["qty"], 20)
            conn.close()


    @patch("execution_eval.resolve_snapshot_prices")
    def test_auction_finmind_price_source(self, mock_resolve) -> None:
        mock_resolve.return_value = (
            {"2330": 1010.0},
            "finmind_tick",
            [],
        )
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            trade = "2026-06-06"
            _seed_watchlist(conn, as_of="2026-06-04")
            _seed_bars(conn)
            ips = InvestmentPolicy.from_dict({"min_risk_reward": 1.0})
            code = run_evaluation(
                conn,
                trade_date=trade,
                ips=ips,
                mode="auction",
                persist=True,
                preview=False,
                force_regenerate=False,
                prices="",
                quiet=True,
                price_source_pref="finmind",
            )
            self.assertEqual(code, 0)
            row = conn.execute(
                "SELECT price_source FROM order_intents WHERE stock_id='2330'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["price_source"], "finmind_tick")
            conn.close()

    def test_resolve_evaluation_prices_manual_requires_prices(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            _seed_watchlist(conn)
            ips = InvestmentPolicy.from_dict({})
            _, _, _, err = resolve_evaluation_prices(
                conn,
                mode="auction",
                ips=ips,
                prices_arg="",
                price_source_pref="manual",
            )
            self.assertEqual(err, 2)
            conn.close()


if __name__ == "__main__":
    unittest.main()
