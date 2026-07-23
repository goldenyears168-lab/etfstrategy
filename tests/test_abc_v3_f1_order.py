"""Tests for ABC v3+f1 re-entry policy and order gates."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from abc_v3_f1_intent_bridge import ABC_V3_F1_POOL_ID, build_intent_payload, format_order_log_lines
from order.abc_v3_f1_config import AbcV3F1OrderConfig, load_abc_v3_f1_order_config
from order.abc_v3_f1_order import process_abc_v3_f1_orders
from order.abc_v3_f1_reentry import (
    AbcV3F1Ledger,
    evaluate_reentry,
    trading_days_between,
)


def _cfg(**overrides: object) -> AbcV3F1OrderConfig:
    base = dict(
        order_enabled=True,
        auto_submit=True,
        budget_twd=20000,
        max_entries_per_day=5,
        price_type="chase_ask",
        market_type="intraday_odd",
        time_in_force="rod",
        user_def="abc-v3-f1-pullback",
        cancel_open_buys=True,
        hold_days=3,
        reentry_discount_pct=2.0,
        reentry_min_trading_days=1,
        max_entries_per_symbol_rolling=3,
        rolling_lookback_trading_days=20,
        max_notional_per_symbol_twd=60000,
        poll_max_attempts=3,
        poll_interval_sec=0.0,
        notional_basis="submitted",
        submit_notify=True,
    )
    base.update(overrides)
    return AbcV3F1OrderConfig(**base)


TD = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]


@dataclass
class _Sig:
    stock_id: str
    stock_name: str = ""
    action: str = "observe"
    reason: str = "test"
    price: float = 100.0
    poll_minute: str = "10:00"
    pool_id: str = ABC_V3_F1_POOL_ID


class TestAbcV3F1Reentry(unittest.TestCase):
    def test_trading_days_between(self) -> None:
        self.assertEqual(trading_days_between(TD, "2026-07-07", "2026-07-09"), 2)

    def test_first_entry_allowed(self) -> None:
        ok, reason = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=240.0,
            cfg=_cfg(),
            ledger=AbcV3F1Ledger(),
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "first_entry")

    def test_same_day_duplicate_blocked(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[{"symbol": "6257", "session_date": "2026-07-09", "ask_price": 240.0}]
        )
        ok, reason = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=235.0,
            cfg=_cfg(),
            ledger=ledger,
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertFalse(ok)
        self.assertIn("same_day_duplicate", reason)

    def test_new_event_after_hold_window(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[{"symbol": "6257", "session_date": "2026-07-03", "ask_price": 250.0, "quantity_shares": 80}]
        )
        ok, reason = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=260.0,
            cfg=_cfg(),
            ledger=ledger,
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertTrue(ok)
        self.assertIn("new_event_after_hold", reason)

    def test_hold_window_addon_requires_discount(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[{"symbol": "6257", "session_date": "2026-07-07", "ask_price": 250.0, "quantity_shares": 80}]
        )
        ok, reason = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=248.0,
            cfg=_cfg(),
            ledger=ledger,
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertFalse(ok)
        self.assertIn("hold_window_no_discount", reason)

        ok2, reason2 = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=244.0,
            cfg=_cfg(),
            ledger=ledger,
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertTrue(ok2)
        self.assertIn("addon_discount", reason2)

    def test_max_notional_per_symbol(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[
                {"symbol": "6257", "session_date": "2026-07-01", "ask_price": 250.0, "quantity_shares": 100},
                {"symbol": "6257", "session_date": "2026-07-02", "ask_price": 250.0, "quantity_shares": 100},
            ]
        )
        ok, reason = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=200.0,
            cfg=_cfg(max_notional_per_symbol_twd=60000),
            ledger=ledger,
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertFalse(ok)
        self.assertIn("max_notional_per_symbol", reason)

    def test_max_notional_ignores_entries_outside_rolling_window(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[
                {"symbol": "6257", "session_date": "2026-06-01", "ask_price": 250.0, "quantity_shares": 200},
                {"symbol": "6257", "session_date": "2026-07-07", "ask_price": 250.0, "quantity_shares": 80},
            ]
        )
        ok, reason = evaluate_reentry(
            symbol="6257",
            session_date="2026-07-09",
            ask_price=244.0,
            cfg=_cfg(max_notional_per_symbol_twd=60000, rolling_lookback_trading_days=20),
            ledger=ledger,
            trading_dates=TD,
            available_cash=100000,
        )
        self.assertTrue(ok)
        self.assertIn("addon_discount", reason)

    def test_process_uses_current_poll_signals_not_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            intents_dir = Path(tmp) / "intents"
            intents_dir.mkdir()
            with patch.dict(
                os.environ,
                {
                    "ABC_V3_F1_ORDER_ENABLED": "1",
                    "ABC_V3_F1_AUTO_SUBMIT": "0",
                    "ABC_V3_F1_ORDER_FORCE_LEGACY": "1",
                },
                clear=False,
            ):
                with patch("order.abc_v3_f1_reentry.LEDGER_PATH", ledger_path):
                    with patch("abc_v3_f1_intent_bridge.INTENTS_DIR", intents_dir):
                        with patch("order.abc_v3_f1_reentry.load_tw_trading_dates", return_value=TD):
                            with patch(
                                "order.fubon_session.connect_fubon",
                                side_effect=RuntimeError("no cert"),
                            ):
                                results = process_abc_v3_f1_orders(
                                    session_date="2026-07-09",
                                    poll_minute="13:05",
                                    signals=[_Sig(stock_id="6257", price=241.5)],
                                    dry_run=True,
                                )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["reentry"], "first_entry")

    def test_load_config_max_entries_five(self) -> None:
        cfg = load_abc_v3_f1_order_config()
        self.assertEqual(cfg.max_entries_per_day, 10)
        self.assertEqual(cfg.budget_twd, 10000)
        self.assertEqual(cfg.max_notional_per_symbol_twd, 30000)
        # Order sleeve retired · defaults off unless FORCE_LEGACY
        self.assertFalse(cfg.order_enabled)
        self.assertFalse(cfg.auto_submit)

    def test_process_dry_run_with_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            intents_dir = Path(tmp) / "intents"
            intents_dir.mkdir()
            with patch.dict(
                os.environ,
                {
                    "ABC_V3_F1_ORDER_ENABLED": "1",
                    "ABC_V3_F1_AUTO_SUBMIT": "0",
                    "ABC_V3_F1_ORDER_FORCE_LEGACY": "1",
                },
                clear=False,
            ):
                with patch("order.abc_v3_f1_reentry.LEDGER_PATH", ledger_path):
                    with patch("abc_v3_f1_intent_bridge.INTENTS_DIR", intents_dir):
                        with patch("order.abc_v3_f1_reentry.load_tw_trading_dates", return_value=TD):
                            with patch(
                                "order.fubon_session.connect_fubon",
                                side_effect=RuntimeError("no cert"),
                            ):
                                results = process_abc_v3_f1_orders(
                                    session_date="2026-07-09",
                                    poll_minute="13:00",
                                    signals=[_Sig(stock_id="6257", price=241.5)],
                                    dry_run=True,
                                )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["reentry"], "first_entry")

    def test_format_order_log_shows_reentry(self) -> None:
        lines = format_order_log_lines(
            [
                {
                    "symbol": "6257",
                    "status": "submitted",
                    "lifecycle_status": "working",
                    "reentry": "new_event_after_hold:td_gap=4>3",
                    "quantity_shares": 82,
                    "ask_price": 241.5,
                    "notional_twd": 19803,
                    "daily_entries": 2,
                }
            ]
        )
        self.assertTrue(any("已送單" in ln for ln in lines))
        self.assertTrue(any("working" in ln for ln in lines))

    def test_submit_success_sets_notify_flag(self) -> None:
        import sys
        from unittest.mock import MagicMock

        fubon_stub = MagicMock()
        fubon_const = MagicMock()
        fubon_stub.constant = fubon_const
        module_patches = {
            "fubon_neo": fubon_stub,
            "fubon_neo.constant": fubon_const,
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            intents_dir = Path(tmp) / "intents"
            intents_dir.mkdir()
            mock_session = object()

            def _fake_chase(_s: object, _sym: str) -> float:
                return 240.0

            def _fake_place(_s: object, resolved: object) -> dict[str, object]:
                return {"is_success": True, "order_no": "T123", "message": "ok"}

            def _fake_poll(_session: object, **kwargs: object) -> dict[str, object]:
                return {
                    "lifecycle_status": "working",
                    "filled_qty": 0,
                    "quantity_shares": 83,
                    "notional_twd": 19920.0,
                    "order_no": "T123",
                    "broker_status": 10,
                }

            with patch.dict(sys.modules, module_patches):
                with patch.dict(
                    os.environ,
                    {
                        "ABC_V3_F1_ORDER_ENABLED": "1",
                        "ABC_V3_F1_AUTO_SUBMIT": "1",
                        "ABC_V3_F1_ORDER_FORCE_LEGACY": "1",
                    },
                    clear=False,
                ):
                    with patch("order.fubon_subprocess.host_needs_fubon_venv", return_value=False):
                        with patch(
                            "order.live_submit_guard.live_submit_block_reason",
                            return_value=None,
                        ):
                            with patch("order.abc_v3_f1_reentry.LEDGER_PATH", ledger_path):
                                with patch("abc_v3_f1_intent_bridge.INTENTS_DIR", intents_dir):
                                    with patch("order.abc_v3_f1_reentry.load_tw_trading_dates", return_value=TD):
                                        with patch("order.fubon_session.connect_fubon", return_value=mock_session):
                                            with patch(
                                                "order.fubon_account.bank_remain",
                                                return_value={"available_balance": 100000},
                                            ):
                                                with patch(
                                                    "order.chase_runner.chase_ask_price",
                                                    side_effect=_fake_chase,
                                                ):
                                                    with patch(
                                                        "order.fubon_orders.cancel_open_buys_for_symbols",
                                                        return_value=[],
                                                    ):
                                                        with patch(
                                                            "order.fubon_orders.place_resolved_order",
                                                            side_effect=_fake_place,
                                                        ):
                                                            with patch(
                                                                "order.abc_v3_f1_order.reconcile_before_submit",
                                                                return_value=None,
                                                            ):
                                                                with patch(
                                                                    "order.abc_v3_f1_order.poll_order_lifecycle",
                                                                    side_effect=_fake_poll,
                                                                ):
                                                                    results = process_abc_v3_f1_orders(
                                                                        session_date="2026-07-09",
                                                                        poll_minute="13:00",
                                                                        signals=[
                                                                            _Sig(stock_id="6257", price=241.5)
                                                                        ],
                                                                        dry_run=False,
                                                                    )
            self.assertEqual(results[0]["status"], "working")
            self.assertTrue(results[0].get("notify_submit"))
            self.assertEqual(results[0]["lifecycle_status"], "working")
            self.assertIn("client_intent_id", results[0])


if __name__ == "__main__":
    unittest.main()
