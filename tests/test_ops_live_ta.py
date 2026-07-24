"""Unit tests · Live TA universe + disposition / continuous clocks (no network)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ops_live_ta import (
    build_live_ta_state,
    classify_auction_phase,
    classify_continuous_phase,
    load_holdings_from_db,
    next_auction,
    parse_disposition_ids,
    parse_stock_list,
    resolve_live_ta_universe,
)

_TPE = ZoneInfo("Asia/Taipei")


class TestOpsLiveTa(unittest.TestCase):
    def test_parse_default(self) -> None:
        rows = parse_stock_list("")
        self.assertEqual(rows[0][0], "2492")

    def test_parse_empty_no_default(self) -> None:
        self.assertEqual(parse_stock_list("", default_if_empty=False), [])

    def test_parse_named(self) -> None:
        rows = parse_stock_list("2492:華新科,2330")
        self.assertEqual(rows[0], ("2492", "華新科"))
        self.assertEqual(rows[1][0], "2330")

    def test_disposition_ids_default(self) -> None:
        self.assertEqual(parse_disposition_ids(""), {"2492"})

    def test_between_session(self) -> None:
        now = datetime(2026, 7, 23, 10, 5, tzinfo=_TPE)  # Thu
        phase, action, note, nxt = classify_auction_phase(now)
        self.assertEqual(phase, "between")
        self.assertEqual(action, "觀望")
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertEqual(nxt.strftime("%H:%M"), "10:20")
        self.assertIn("10:20", note)

    def test_pre_match(self) -> None:
        now = datetime(2026, 7, 23, 10, 19, 30, tzinfo=_TPE)
        phase, action, _, nxt = classify_auction_phase(now)
        self.assertEqual(phase, "pre_match")
        self.assertEqual(action, "注意撮合")
        assert nxt is not None
        self.assertEqual(nxt.strftime("%H:%M"), "10:20")

    def test_weekend(self) -> None:
        now = datetime(2026, 7, 25, 10, 0, tzinfo=_TPE)  # Sat
        phase, action, _, nxt = classify_auction_phase(now)
        self.assertEqual(phase, "weekend")
        self.assertEqual(action, "盤後")
        self.assertIsNone(nxt)
        self.assertIsNone(next_auction(now))

    def test_continuous_mid_session(self) -> None:
        now = datetime(2026, 7, 23, 10, 5, tzinfo=_TPE)
        phase, action, note, nxt = classify_continuous_phase(now)
        self.assertEqual(phase, "continuous")
        self.assertEqual(action, "觀望")
        self.assertIn("短動能", note)
        self.assertIsNotNone(nxt)

    def test_load_holdings_and_universe(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE order_holdings_snapshot (
                snapshot_date TEXT, stock_id TEXT, stock_name TEXT, shares INTEGER
            );
            INSERT INTO order_holdings_snapshot VALUES
              ('2026-07-23', '2330', '台積電', 100),
              ('2026-07-23', '2303', '聯電', 0),
              ('2026-07-22', '1101', '台泥', 50);
            """
        )
        rows = load_holdings_from_db(conn)
        self.assertEqual(rows, [("2330", "台積電")])
        with patch("ops_live_ta.load_holdings_from_ops", return_value=[("2454", "聯發科")]):
            uni = resolve_live_ta_universe(conn, extras_raw="2492:華新科")
        self.assertEqual([r[0] for r in uni], ["2330", "2454", "2492"])
        conn.close()

    def test_build_continuous_vs_disposition(self) -> None:
        now = datetime(2026, 7, 23, 10, 19, 30, tzinfo=_TPE)
        cont = build_live_ta_state(
            "2330",
            now=now,
            last_print=100.0,
            quote_meta={"mom_2bar_pct": 0.5, "quote_source": "test"},
            disposition_ids={"2492"},
        )
        self.assertEqual(cont.anchors["mode"], "continuous")
        self.assertEqual(cont.phase, "continuous")
        self.assertIsNone(cont.next_auction_at)
        self.assertEqual(cont.action, "偏強")

        disp = build_live_ta_state(
            "2492",
            now=now,
            last_print=100.0,
            quote_meta={"quote_source": "test"},
            disposition_ids={"2492"},
        )
        self.assertEqual(disp.anchors["mode"], "disposition")
        self.assertEqual(disp.phase, "pre_match")
        self.assertIsNotNone(disp.next_auction_at)


if __name__ == "__main__":
    unittest.main()
