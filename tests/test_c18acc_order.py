"""Tests for C18acc order submit · sell-first · deferred swap buy."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from order.account_cap_gate import sort_actions_sell_first
from order.c18acc_order import process_c18acc_orders
from order.c18acc_order_ledger import C18accOrderLedger
from order.live_submit_guard import today_session_date


@dataclass
class _Act:
    kind: str
    stock_id: str
    stock_name: str = ""
    side: str = "buy"
    price: float = 100.0
    quantity_shares: int = 1
    note: str = ""
    counterparty_id: str | None = None
    counterparty_name: str | None = None
    pyramid_parent_cid: str | None = None
    slot: int | None = None


class TestC18accOrder(unittest.TestCase):
    def test_dry_run_returns_all_actions(self) -> None:
        from order.c18acc_order_config import C18accOrderConfig

        actions = [
            _Act("swap", "2330", side="sell", counterparty_id="2454"),
            _Act("swap", "2454", side="buy", counterparty_id="2330"),
        ]
        cfg = C18accOrderConfig(
            order_enabled=True,
            auto_submit=False,
            dry_run=True,
            budget_twd_per_slot=20_000,
            board_lot=False,
            price_type="chase_ask",
            market_type="intraday_odd",
            time_in_force="rod",
            user_def="rrg-mono-swap-accel",
            entry_confirm_bars=1,
            candidate_pool="fresh",
            avoid_spread_mixed=True,
            allow_pool_override=False,
            prior_ret_days=5,
            prior_ret_max=0.12,
            spread_gate_no_trade_before="13:00",
            poll_max_attempts=1,
            poll_interval_sec=0.0,
            submit_notify=False,
        )
        with patch("order.c18acc_order.load_c18acc_order_config", return_value=cfg):
            out = process_c18acc_orders(
                actions,
                session_date="2026-07-09",
                poll_minute="10:00",
                dry_run=True,
            )
        self.assertTrue(out["dry_run"])
        self.assertEqual(len(out["applied_actions"]), 2)
        self.assertEqual(out["priority"], "sell_first")

    def test_live_path_refuses_under_test_runner(self) -> None:
        """Regression · pytest/unittest must never reach Fubon (2026-07-13 / 2026-08-04).

        Uses a non-tradable fake symbol so a guard regression cannot hit a real
        name (2026-08-04 used 2377 and produced a live odd-lot fill).
        """
        from order.c18acc_order_config import C18accOrderConfig

        fake_cfg = C18accOrderConfig(
            order_enabled=True,
            auto_submit=True,
            dry_run=False,
            budget_twd_per_slot=20_000,
            board_lot=False,
            price_type="chase_ask",
            market_type="intraday_odd",
            time_in_force="rod",
            user_def="rrg-mono-swap-accel",
            entry_confirm_bars=1,
            candidate_pool="fresh",
            avoid_spread_mixed=True,
            allow_pool_override=False,
            prior_ret_days=5,
            prior_ret_max=0.12,
            spread_gate_no_trade_before="13:00",
            poll_max_attempts=1,
            poll_interval_sec=0.0,
            submit_notify=False,
        )
        with patch("order.c18acc_order.load_c18acc_order_config", return_value=fake_cfg):
            out = process_c18acc_orders(
                [_Act("entry", "ZZZZ", side="buy")],
                session_date=today_session_date(),
                poll_minute="10:00",
                dry_run=False,
            )
        self.assertEqual(out.get("status"), "error")
        self.assertIn("live_submit_blocked", str(out.get("reason") or ""))

    @patch("order.c18acc_order.load_c18acc_order_config")
    def test_live_path_refuses_when_master_off(self, mock_cfg) -> None:
        """Explicit dry_run=False must still die on ORDER_MASTER_ENABLED=0."""
        import os

        from order.c18acc_order_config import C18accOrderConfig

        mock_cfg.return_value = C18accOrderConfig(
            order_enabled=True,
            auto_submit=True,
            dry_run=False,
            budget_twd_per_slot=20_000,
            board_lot=False,
            price_type="chase_ask",
            market_type="intraday_odd",
            time_in_force="rod",
            user_def="rrg-mono-swap-accel",
            entry_confirm_bars=1,
            candidate_pool="fresh",
            avoid_spread_mixed=True,
            allow_pool_override=False,
            prior_ret_days=5,
            prior_ret_max=0.12,
            spread_gate_no_trade_before="13:00",
            poll_max_attempts=1,
            poll_interval_sec=0.0,
            submit_notify=False,
        )
        with patch("order.live_submit_guard.under_test_runner", return_value=False):
            with patch.dict(os.environ, {"ORDER_MASTER_ENABLED": "0"}, clear=False):
                out = process_c18acc_orders(
                    [_Act("entry", "ZZZZ", side="buy")],
                    session_date=today_session_date(),
                    poll_minute="10:00",
                    dry_run=False,
                )
        self.assertEqual(out.get("status"), "error")
        self.assertIn("ORDER_MASTER_ENABLED=0", str(out.get("reason") or ""))

    def test_sort_sell_before_buy(self) -> None:
        actions = [
            _Act("entry", "2454", side="buy"),
            _Act("max_hold_exit", "2330", side="sell"),
        ]
        ordered = sort_actions_sell_first(actions)
        self.assertEqual(ordered[0].kind, "max_hold_exit")

    def test_order_config_avoid_spread_mixed(self) -> None:
        from order.c18acc_order_config import load_c18acc_order_config
        from research.backtest.rrg_mono_score_swap_c import champion_score_swap_c_config

        cfg = load_c18acc_order_config()
        self.assertTrue(cfg.avoid_spread_mixed)
        self.assertEqual(cfg.spread_gate_no_trade_before, "13:00")
        self.assertEqual(cfg.candidate_pool, "fresh")
        self.assertEqual(champion_score_swap_c_config().candidate_pool, cfg.candidate_pool)
        self.assertEqual(champion_score_swap_c_config().no_trade_before, "13:00")

    @patch("order.c18acc_order.load_c18acc_order_config")
    def test_swap_buy_deferred_on_cash_shortfall(self, mock_cfg) -> None:
        from order.c18acc_order_config import C18accOrderConfig

        mock_cfg.return_value = C18accOrderConfig(
            order_enabled=True,
            auto_submit=True,
            dry_run=False,
            budget_twd_per_slot=20_000,
            board_lot=False,
            price_type="chase_ask",
            market_type="intraday_odd",
            time_in_force="rod",
            user_def="rrg-mono-swap-accel",
            entry_confirm_bars=2,
            candidate_pool="fresh",
            avoid_spread_mixed=True,
            allow_pool_override=False,
            prior_ret_days=5,
            prior_ret_max=0.12,
            spread_gate_no_trade_before="13:00",
            poll_max_attempts=1,
            poll_interval_sec=0.0,
            submit_notify=False,
        )
        ledger = C18accOrderLedger()
        with patch("order.c18acc_order.load_c18acc_order_ledger", return_value=ledger), patch(
            "order.c18acc_order.save_c18acc_order_ledger"
        ), patch("order.c18acc_order._submit_one") as submit_one, patch(
            "order.live_submit_guard.live_submit_block_reason", return_value=None
        ), patch(
            "order.fubon_subprocess.host_needs_fubon_venv", return_value=False
        ), patch(
            "order.fubon_subprocess.run_fubon_order_worker",
            side_effect=AssertionError("test must not call live Fubon worker"),
        ):

            def _side_effect(session, act, **kwargs):
                if act.side == "sell":
                    return {
                        "kind": act.kind,
                        "side": act.side,
                        "symbol": act.stock_id,
                        "status": "submitted",
                        "notional_twd": 20_000,
                        "quantity_shares": act.quantity_shares,
                    }
                return {
                    "kind": act.kind,
                    "side": act.side,
                    "symbol": act.stock_id,
                    "status": "deferred",
                    "reason": "insufficient_cash",
                    "queued_swap_buy": True,
                }

            submit_one.side_effect = _side_effect
            with patch("order.fubon_session.connect_fubon", return_value=object()), patch(
                "order.fubon_account.bank_remain",
                return_value={"available_balance": 5_000},
            ), patch("order.fubon_orders.holdings_shares_by_symbol", return_value={"2330": 1}):
                actions = [
                    _Act("swap", "2330", side="sell", counterparty_id="2454", quantity_shares=1),
                    _Act("swap", "2454", side="buy", counterparty_id="2330", quantity_shares=1),
                ]
                out = process_c18acc_orders(
                    actions,
                    session_date=today_session_date(),
                    poll_minute="10:00",
                    dry_run=False,
                )
        self.assertEqual(out["results"][0]["side"], "sell")
        self.assertEqual(out["results"][1]["status"], "deferred")
        self.assertEqual(submit_one.call_count, 2)
        self.assertEqual(submit_one.call_args_list[0][0][1].side, "sell")
        self.assertEqual(submit_one.call_args_list[1][0][1].side, "buy")


if __name__ == "__main__":
    unittest.main()
