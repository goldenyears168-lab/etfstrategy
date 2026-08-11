"""Unit tests · TMF live commentator broadcast (no broker)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from order.tmf_channel_broadcast import build_broadcast, load_broadcast, save_broadcast


class BroadcastBuilderTest(unittest.TestCase):
    def test_outside_session_headline(self):
        bc = build_broadcast(
            {
                "reason": "outside_session",
                "dry_run": False,
                "hhmm": "08:10",
                "order_enabled": True,
                "auto_submit": True,
            }
        )
        self.assertIn("盤外", bc["headline"])
        self.assertFalse(bc["dry_run"])
        self.assertTrue(bc["live"])
        self.assertTrue(any("券商" in s for s in bc["situation"]))

    def test_rail_near_and_actions(self):
        bc = build_broadcast(
            {
                "reason": "reconciled",
                "dry_run": True,
                "hhmm": "09:00",
                "spot": 44500.0,
                "want_s": 44520.0,
                "want_l": 44440.0,
                "open_pos": None,
                "broker_live": None,
                "actions": [
                    {
                        "kind": "place",
                        "side": "L",
                        "price": 44440.0,
                        "why": "reconcile_place",
                        "ok": True,
                    }
                ],
                "api_calls_day": 10,
            }
        )
        ids = {p["id"] for p in bc["progress"]}
        self.assertIn("rail_S", ids)
        self.assertIn("rail_L", ids)
        self.assertIn("api_day", ids)
        near_s = next(p for p in bc["progress"] if p["id"] == "rail_S")
        self.assertTrue(near_s["near"])  # 20pt away
        self.assertTrue(any("place" in x for x in bc["action_lines"]))

    def test_flatten_narrative(self):
        bc = build_broadcast(
            {
                "reason": "flatten_first",
                "flatten_why": "broker_over_max n=3>max=1",
                "dry_run": False,
                "broker_live": {"s": "S", "n": 3, "ep": 43790.0},
                "open_pos": None,
                "spot": 43344.0,
                "actions": [
                    {
                        "kind": "exit_market",
                        "side": "S",
                        "lot": 3,
                        "why": "broker_over_max n=3>max=1",
                        "ok": True,
                    }
                ],
            }
        )
        self.assertIn("平倉", bc["headline"])
        self.assertTrue(any("禁止先掛" in n or "flatten" in n.lower() or "平倉" in n for n in bc["narrative"]))

    def test_dry_run_day_loss_shows_cumulative_realized(self):
        """dry_run: trip_day_pnl_kill() DOES act on day_pnl_pts here, so the
        '日已實現／熔斷' cumulative framing is accurate -- must stay as-is."""
        bc = build_broadcast(
            {
                "reason": "reconciled",
                "dry_run": True,
                "hhmm": "10:00",
                "spot": 44500.0,
            },
            ledger={"day_pnl_pts": -120.0},
        )
        item = next(p for p in bc["progress"] if p["id"] == "day_loss")
        self.assertIn("日已實現", item["label"])
        self.assertEqual(item["value"], -120.0)

    def test_live_day_loss_shows_per_position_float_not_day_realized(self):
        """2026-08-11: LIVE mode never trips on cumulative day-realized PnL
        (trip_day_pnl_kill hard-returns False when not dry_run) -- the label
        must not claim a day-total breaker exists, and must show the
        genuinely-active per-position floating-loss figure computed from
        real broker entry price vs spot, not the (deliberately-nulled-live)
        day_pnl_pts."""
        bc = build_broadcast(
            {
                "reason": "reconciled",
                "dry_run": False,
                "hhmm": "10:00",
                "spot": 44700.0,
                "broker_live": {"s": "L", "n": 1, "ep": 44500.0},
                "open_pos": None,
            },
            ledger={"day_pnl_pts": None},
        )
        item = next(p for p in bc["progress"] if p["id"] == "day_loss")
        self.assertNotIn("日已實現", item["label"])
        self.assertIn("無日虧總量熔斷", item["label"])
        self.assertEqual(item["value"], 200.0)  # (44700-44500)*1, real broker float

    def test_live_day_loss_flat_shows_no_position_state(self):
        bc = build_broadcast(
            {
                "reason": "reconciled",
                "dry_run": False,
                "hhmm": "10:00",
                "spot": 44700.0,
                "broker_live": None,
                "open_pos": None,
            },
            ledger={"day_pnl_pts": None},
        )
        item = next(p for p in bc["progress"] if p["id"] == "day_loss")
        self.assertIsNone(item["value"])
        self.assertIn("空手", item["label"])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "bc.json"
            payload = build_broadcast({"reason": "outside_session", "dry_run": True, "hhmm": "08:00"})
            save_broadcast(payload, fp)
            loaded = load_broadcast(fp, max_age_sec=9999)
            self.assertTrue(loaded.get("ok"))
            self.assertEqual(loaded.get("schema"), "tmf-channel-broadcast-v1")
            self.assertFalse(loaded.get("stale"))


if __name__ == "__main__":
    unittest.main()
