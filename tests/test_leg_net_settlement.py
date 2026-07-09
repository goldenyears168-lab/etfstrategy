"""Net leg settlement with Taiwan equity cost model."""

import unittest

from research.backtest.slot_portfolio_metrics import (
    format_cost_model_note,
    leg_net_equity_open,
    leg_net_settlement,
)

COST_6DISC = {
    "enabled": True,
    "buy_fee_pct": 0.1425 * 0.6,
    "sell_fee_pct": 0.1425 * 0.6,
    "sell_tax_pct": 0.3,
}


class LegNetSettlementTests(unittest.TestCase):
    def test_zero_cost_matches_gross(self) -> None:
        proceeds, pnl = leg_net_settlement(10_000, 100, 110, cost_model=None)
        self.assertAlmostEqual(proceeds, 11_000)
        self.assertAlmostEqual(pnl, 1_000)

    def test_discounted_fees_reduce_proceeds(self) -> None:
        proceeds, pnl = leg_net_settlement(10_000, 100, 110, cost_model=COST_6DISC)
        gross, _ = leg_net_settlement(10_000, 100, 110, cost_model=None)
        self.assertLess(proceeds, gross)
        self.assertLess(pnl, 1_000)

    def test_open_mark_no_sell_tax(self) -> None:
        eq = leg_net_equity_open(10_000, 100, 105, cost_model=COST_6DISC)
        entry_fee = 10_000 * COST_6DISC["buy_fee_pct"] / 100
        alloc = 10_000 - entry_fee
        self.assertAlmostEqual(eq, alloc * 1.05)

    def test_format_note_mentions_discount(self) -> None:
        note = format_cost_model_note({**COST_6DISC, "fee_discount_label": "手續費6折"})
        self.assertIn("手續費6折", note)
        self.assertIn("0.0855", note)


if __name__ == "__main__":
    unittest.main()
