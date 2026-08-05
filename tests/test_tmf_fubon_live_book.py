"""Unit tests · Fubon live-book session blotter (no broker)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1] / "reports" / "research" / "channel_lab"
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

from live_v6_sim_server import (  # noqa: E402
    build_session_blotter,
    pair_fills_fifo,
    reconstruct_seed_lots,
)


class PairFillsFifoTest(unittest.TestCase):
    def test_long_then_cover(self):
        trades, residual = pair_fills_fifo(
            [
                {"t": "2026-08-05T09:00:00+08:00", "s": "L", "px": 100.0, "n": 1, "order_no": "a"},
                {"t": "2026-08-05T09:10:00+08:00", "s": "S", "px": 110.0, "n": 1, "order_no": "b"},
            ]
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["s"], "L")
        self.assertEqual(trades[0]["pnl"], 10.0)
        self.assertEqual(trades[0]["hold"], 10)
        self.assertEqual(residual, [])

    def test_short_then_cover_and_scale(self):
        trades, residual = pair_fills_fifo(
            [
                {"t": "2026-08-05T09:00:00+08:00", "s": "S", "px": 200.0, "n": 2, "order_no": "a"},
                {"t": "2026-08-05T09:05:00+08:00", "s": "L", "px": 190.0, "n": 1, "order_no": "b"},
                {"t": "2026-08-05T09:06:00+08:00", "s": "L", "px": 185.0, "n": 1, "order_no": "c"},
            ]
        )
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["pnl"], 10.0)
        self.assertEqual(trades[1]["pnl"], 15.0)
        self.assertEqual(sum(t["pnl"] for t in trades), 25.0)
        self.assertEqual(residual, [])


class SessionBlotterAlignTest(unittest.TestCase):
    def test_overnight_seed_keeps_open_short(self):
        """Aug 5 style: flatten overnight short×3, then day trades, end short×1."""
        legs = [
            {"t": "2026-08-05T08:31:42+08:00", "s": "L", "px": 44695.0, "n": 3, "order_no": "s008Y", "user_def": "tmfch"},
            {"t": "2026-08-05T08:45:26+08:00", "s": "L", "px": 44505.0, "n": 1, "order_no": "s00bi", "user_def": "tmfch"},
            {"t": "2026-08-05T08:45:38+08:00", "s": "L", "px": 44505.0, "n": 1, "order_no": "s00e7", "user_def": "tmfch"},
            {"t": "2026-08-05T08:47:45+08:00", "s": "L", "px": 44505.0, "n": 1, "order_no": "s00x1", "user_def": "tmfch"},
            {"t": "2026-08-05T08:48:33+08:00", "s": "S", "px": 44535.3, "n": 3, "order_no": "s013a", "user_def": "tmfch"},
            {"t": "2026-08-05T08:51:59+08:00", "s": "S", "px": 44573.0, "n": 1, "order_no": "s01Mv", "user_def": "tmfch"},
            {"t": "2026-08-05T08:52:23+08:00", "s": "L", "px": 44588.0, "n": 1, "order_no": "s01Ot", "user_def": "tmfch"},
            {"t": "2026-08-05T08:55:53+08:00", "s": "S", "px": 44608.0, "n": 1, "order_no": "s01g2", "user_def": "tmfch"},
        ]
        broker = {"s": "S", "n": 1, "ep": 44608.0}
        seed = reconstruct_seed_lots(legs, broker)
        self.assertEqual(seed[0]["s"], "S")
        self.assertEqual(seed[0]["n"], 3)

        blot = build_session_blotter(legs, broker, day="2026-08-05")
        self.assertEqual(blot["seed_lots"][0]["n"], 3)
        # overnight flat has no pnl
        self.assertTrue(any(t["why"] == "overnight_flat" for t in blot["trades"]))
        self.assertTrue(all(t.get("pnl") is not None for t in blot["realized_trades"]))
        # residual open short 1 @44608
        self.assertEqual(len(blot["residual"]), 1)
        self.assertEqual(blot["residual"][0]["s"], "S")
        self.assertEqual(blot["residual"][0]["n"], 1)
        self.assertEqual(blot["residual"][0]["px"], 44608.0)
        # last short is open — not wrongly closed as +20 long cover
        self.assertFalse(
            any(
                t.get("order_no_out") == "s01g2" and t.get("pnl") is not None
                for t in blot["trades"]
            )
        )
        # realized excludes overnight
        self.assertEqual(blot["summary_all"]["n_overnight_flat"], 3)


if __name__ == "__main__":
    unittest.main()
