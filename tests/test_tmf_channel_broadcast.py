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
