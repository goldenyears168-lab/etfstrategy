"""Tests for ABC v3+f1 order lifecycle helpers."""

from __future__ import annotations

import unittest

from abc_v3_f1_intent_bridge import build_client_intent_id, build_intent_payload
from order.abc_v3_f1_lifecycle import (
    build_client_intent_id as order_cid,
    entry_blocks_retry,
    entry_notional,
    lifecycle_status_from_row,
)
from order.abc_v3_f1_reentry import AbcV3F1Ledger, evaluate_reentry


def _cfg(**overrides: object):
    from order.abc_v3_f1_config import AbcV3F1OrderConfig

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


class TestAbcV3F1Lifecycle(unittest.TestCase):
    def test_client_intent_id_stable(self) -> None:
        cid = build_client_intent_id(
            session_date="2026-07-09", poll_minute="13:00", symbol="6257"
        )
        self.assertEqual(cid, "abc-v3-f1-pullback_20260709_1300_6257")
        self.assertEqual(
            order_cid(
                strategy_id="abc-v3-f1-pullback",
                session_date="2026-07-09",
                poll_minute="13:00",
                symbol="6257",
            ),
            cid,
        )

    def test_intent_payload_has_guard_metadata(self) -> None:
        payload = build_intent_payload(
            symbol="6257",
            session_date="2026-07-09",
            poll_minute="13:00",
            signal_price=240.0,
        )
        meta = payload["metadata"]
        self.assertTrue(meta.get("abc_v3_f1_only"))
        self.assertTrue(meta.get("resolve_qty_from_budget"))
        self.assertIn("client_intent_id", meta)
        self.assertEqual(payload["intents"][0]["user_def"], meta["client_intent_id"])

    def test_lifecycle_status_filled(self) -> None:
        self.assertEqual(
            lifecycle_status_from_row({"status": 50, "quantity": 80, "filled_qty": 80}),
            "filled",
        )

    def test_lifecycle_status_working(self) -> None:
        self.assertEqual(
            lifecycle_status_from_row({"status": 10, "quantity": 80, "filled_qty": 0}),
            "working",
        )

    def test_failed_entry_allows_same_day_retry(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "symbol": "6257",
                    "session_date": "2026-07-09",
                    "lifecycle_status": "failed",
                    "ask_price": 240.0,
                }
            ]
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
        self.assertTrue(ok)
        self.assertEqual(reason, "first_entry")

    def test_entry_notional_filled_basis(self) -> None:
        e = {"filled_qty": 50, "ask_price": 200.0, "quantity_shares": 80}
        self.assertEqual(entry_notional(e, basis="filled"), 10000.0)
        self.assertEqual(entry_notional(e, basis="submitted"), 16000.0)

    def test_failed_entry_not_blocks_retry(self) -> None:
        self.assertFalse(
            entry_blocks_retry({"lifecycle_status": "failed"})
        )
        self.assertTrue(entry_blocks_retry({"lifecycle_status": "submitted"}))


if __name__ == "__main__":
    unittest.main()
