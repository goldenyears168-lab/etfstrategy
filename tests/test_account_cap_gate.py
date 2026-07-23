"""Tests for account_cap_gate · sell-first action ordering."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from order.account_cap_gate import (
    can_afford_buy,
    load_account_risk_config,
    sort_actions_sell_first,
    spendable_cash,
)


@dataclass
class _Act:
    kind: str
    side: str


class TestAccountCapGate(unittest.TestCase):
    def test_spendable_cash_reserved(self) -> None:
        cfg = load_account_risk_config()
        self.assertGreaterEqual(cfg.reserved_cash_twd, 0)
        self.assertEqual(spendable_cash(100_000, cfg=cfg), 100_000 - cfg.reserved_cash_twd)

    def test_can_afford_buy(self) -> None:
        cfg = load_account_risk_config({"account_risk": {"reserved_cash_twd": 10_000}})
        ok, _ = can_afford_buy(notional_twd=80_000, available_balance=100_000, cfg=cfg)
        self.assertTrue(ok)
        ok2, reason = can_afford_buy(notional_twd=95_000, available_balance=100_000, cfg=cfg)
        self.assertFalse(ok2)
        self.assertIn("insufficient_cash", reason)

    def test_sort_actions_sell_first(self) -> None:
        actions = [
            _Act("entry", "buy"),
            _Act("swap", "buy"),
            _Act("max_hold_exit", "sell"),
            _Act("swap", "sell"),
            _Act("pyramid_add", "buy"),
        ]
        ordered = sort_actions_sell_first(actions)
        kinds_sides = [(a.kind, a.side) for a in ordered]
        self.assertEqual(kinds_sides[0], ("max_hold_exit", "sell"))
        self.assertEqual(kinds_sides[1], ("swap", "sell"))
        self.assertLess(
            kinds_sides.index(("swap", "sell")),
            kinds_sides.index(("swap", "buy")),
        )
        self.assertLess(
            kinds_sides.index(("swap", "buy")),
            kinds_sides.index(("pyramid_add", "buy")),
        )


if __name__ == "__main__":
    unittest.main()
