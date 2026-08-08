"""Tests for shared order lifecycle helpers (order/oms_lifecycle.py).

2026-08-08: module renamed from abc_v3_f1_lifecycle.py -- despite the old name,
these helpers were always used by the live c18acc/detach-gate/leading-dip
sleeves, not just the retired ABC v3+f1 strategy; the name now matches that.
"""

from __future__ import annotations

import unittest

from order.oms_lifecycle import (
    build_client_intent_id as order_cid,
    entry_blocks_retry,
    entry_notional,
    lifecycle_status_from_row,
)


class TestAbcV3F1Lifecycle(unittest.TestCase):
    def test_client_intent_id_stable(self) -> None:
        self.assertEqual(
            order_cid(
                strategy_id="rrg-c18acc",
                session_date="2026-07-09",
                poll_minute="13:00",
                symbol="6257",
            ),
            "rrg-c18acc_20260709_1300_6257",
        )

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
