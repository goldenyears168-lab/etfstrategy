"""Unit tests · Fubon live-book session blotter (no broker)."""

from __future__ import annotations

import unittest

from tmf_channel.blotter import (
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


class CloseRecordFilterTest(unittest.TestCase):
    def test_includes_today_and_prev_night_a_prefix(self):
        from tmf_channel.blotter import filter_session_fills

        legs = [
            # prev day session s* — exclude when date-only
            {
                "t": "2026-08-05T00:00:00+08:00",
                "date": "2026-08-05",
                "s": "L",
                "px": 44505.0,
                "n": 1,
                "order_no": "s00bi",
                "source": "close_position_record",
            },
            # prev night a* — include
            {
                "t": "2026-08-05T00:00:00+08:00",
                "date": "2026-08-05",
                "s": "S",
                "px": 44340.0,
                "n": 1,
                "order_no": "a00to",
                "source": "close_position_record",
            },
            # today — include
            {
                "t": "2026-08-06T08:49:32+08:00",
                "date": "2026-08-06",
                "s": "L",
                "px": 44231.0,
                "n": 1,
                "order_no": "s00u5",
                "source": "close_position_record",
            },
        ]
        out = filter_session_fills(legs, day="2026-08-06", scope="trading")
        nos = {f["order_no"] for f in out}
        self.assertIn("a00to", nos)
        self.assertIn("s00u5", nos)
        self.assertNotIn("s00bi", nos)

    def test_calendar_scope_excludes_prev_night(self):
        from tmf_channel.blotter import filter_session_fills

        legs = [
            {
                "t": "2026-08-05T00:00:00+08:00",
                "date": "2026-08-05",
                "s": "S",
                "px": 44340.0,
                "n": 1,
                "order_no": "a00to",
                "source": "close_position_record",
            },
            {
                "t": "2026-08-06T08:49:32+08:00",
                "date": "2026-08-06",
                "s": "L",
                "px": 44231.0,
                "n": 1,
                "order_no": "s00u5",
                "source": "close_position_record",
            },
        ]
        out = filter_session_fills(legs, day="2026-08-06", scope="calendar")
        nos = {f["order_no"] for f in out}
        self.assertNotIn("a00to", nos)
        self.assertIn("s00u5", nos)

    def test_close_leg_gets_fifo_pnl(self):
        from tmf_channel.blotter import build_session_blotter

        legs = [
            {
                "t": "2026-08-06T09:00:00+08:00",
                "date": "2026-08-06",
                "s": "L",
                "px": 100.0,
                "n": 1,
                "order_no": "a",
                "order_type": "new",
                "user_def": "tmfch",
                "source": "close_position_record",
            },
            {
                "t": "2026-08-06T09:10:00+08:00",
                "date": "2026-08-06",
                "s": "S",
                "px": 110.0,
                "n": 1,
                "order_no": "b",
                "order_type": "close",
                "user_def": "tmfch",
                "source": "close_position_record",
            },
        ]
        blot = build_session_blotter(legs, None, day="2026-08-06")
        by_no = {f["order_no"]: f for f in blot["session_fills"]}
        self.assertIsNone(by_no["a"].get("pnl"))
        self.assertEqual(by_no["b"].get("pnl"), 10.0)
        self.assertEqual(blot["summary_all"]["net"], 10.0)

    def test_fifo_net_subtracts_fubon_fee_tax(self):
        from tmf_channel.blotter import build_session_blotter

        # TMF point value NT$10 → fee20+tax9 per leg = 2.9pt × 2 legs = 5.8pt cost
        legs = [
            {
                "t": "2026-08-06T09:00:00+08:00",
                "date": "2026-08-06",
                "s": "L",
                "px": 100.0,
                "n": 1,
                "order_no": "a",
                "fee": 20.0,
                "tax": 9.0,
                "order_type": "new",
                "source": "close_position_record",
            },
            {
                "t": "2026-08-06T09:10:00+08:00",
                "date": "2026-08-06",
                "s": "S",
                "px": 110.0,
                "n": 1,
                "order_no": "b",
                "fee": 20.0,
                "tax": 9.0,
                "order_type": "close",
                "source": "close_position_record",
            },
        ]
        blot = build_session_blotter(legs, None, day="2026-08-06")
        t0 = blot["realized_trades"][0]
        self.assertEqual(t0["pnl_gross"], 10.0)
        self.assertEqual(t0["cost_ntd"], 58.0)
        self.assertEqual(t0["cost_pts"], 5.8)
        self.assertEqual(t0["pnl"], 4.2)
        self.assertEqual(blot["summary_all"]["net"], 4.2)
        self.assertEqual(blot["summary_all"]["net_gross"], 10.0)
        self.assertEqual(blot["summary_all"]["cost_pts"], 5.8)
        self.assertGreaterEqual(blot["health"]["fee_coverage_pct"], 99.0)
        # No equity SSOT in unit test → may warn; cost path itself must be ok.
        self.assertIn(
            "cost_applied",
            {i["code"] for i in blot["health"]["issues"]},
        )


class OpenTradeAndTotalPnlTest(unittest.TestCase):
    def test_open_row_prepended(self):
        from tmf_channel.blotter import compose_blotter_trade_rows

        closed = [
            {
                "s": "L",
                "et": "2026-08-06T09:00:00+08:00",
                "xt": "2026-08-06T09:10:00+08:00",
                "ep": 100.0,
                "xp": 110.0,
                "pnl": 10.0,
                "why": "broker",
            }
        ]
        open_pos = {"s": "S", "n": 1, "ep": 44300.0, "u_pnl": 25.0, "u_pnl_ntd": 250.0}
        residual = [{"s": "S", "n": 1, "px": 44300.0, "t": "2026-08-06T12:00:00+08:00", "order_no": "sOpen"}]
        rows = compose_blotter_trade_rows(closed, open_pos=open_pos, residual=residual)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["open"])
        self.assertEqual(rows[0]["why"], "open")
        self.assertEqual(rows[0]["s"], "S")
        self.assertEqual(rows[0]["pnl"], 25.0)
        self.assertEqual(rows[0]["order_no_in"], "sOpen")
        self.assertEqual(rows[1]["why"], "broker")

    def test_total_pnl_realized_plus_unrealized(self):
        from tmf_channel.blotter import total_pnl_from_equity

        eq = {
            "fut_realized_net_ntd": -144.0,
            "fut_realized_net_pts": -14.4,
            "fut_unrealized_pnl_ntd": 0.0,
            "fut_unrealized_pnl_pts": 0.0,
        }
        flat = total_pnl_from_equity(eq, None)
        self.assertEqual(flat["total_ntd"], -144.0)
        open_pos = {"s": "L", "n": 1, "ep": 44000.0, "u_pnl": 12.0, "u_pnl_ntd": 120.0}
        live = total_pnl_from_equity(eq, open_pos)
        self.assertEqual(live["total_ntd"], -24.0)
        self.assertEqual(live["unrealized_ntd"], 120.0)


class BaseOrderNoTest(unittest.TestCase):
    def test_strip_split_suffix(self):
        from order.fubon_futopt_orders import base_order_no

        self.assertEqual(base_order_no("s013a-0001"), "s013a")
        self.assertEqual(base_order_no("s00u5"), "s00u5")


if __name__ == "__main__":
    unittest.main()
