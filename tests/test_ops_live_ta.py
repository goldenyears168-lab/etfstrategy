"""Unit tests · Live TA universe + disposition / continuous clocks (no network)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ops_live_ta import (
    _round_px,
    build_live_ta_state,
    classify_auction_phase,
    classify_continuous_phase,
    clear_mom_1m_cache,
    clear_ta_30m_cache,
    compute_ta_30m_snapshot,
    fetch_last_print,
    fetch_yahoo_chart_quote,
    get_mom_1m_anchors,
    get_ta_30m_anchors,
    last_closed_30m_window,
    load_holdings_from_db,
    next_auction,
    parse_disposition_ids,
    parse_fubon_candles,
    parse_fubon_marketdata_quote,
    parse_stock_list,
    resolve_live_ta_universe,
    run_live_ta_poll,
    session_vwap_from_bars,
)

_TPE = ZoneInfo("Asia/Taipei")


def _fake_5m(
    *,
    day: datetime,
    n: int = 12,
    start_hm: tuple[int, int] = (9, 0),
    open0: float = 100.0,
    step: float = 0.2,
    volume: float = 1000.0,
) -> list[dict]:
    h, m = start_hm
    out: list[dict] = []
    px = open0
    for i in range(n):
        ts = day.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(minutes=5 * i)
        o = px
        px = px + step
        out.append(
            {
                "ts": ts,
                "open": o,
                "high": max(o, px) + 0.05,
                "low": min(o, px) - 0.05,
                "close": px,
                "volume": volume,
            }
        )
    return out


class TestOpsLiveTa(unittest.TestCase):
    def setUp(self) -> None:
        clear_ta_30m_cache()
        clear_mom_1m_cache()

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
            ta_30m={"ta_30m_ready": False, "ta_30m_bias": None},
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
            ta_30m={"ta_30m_ready": False},
        )
        self.assertEqual(disp.anchors["mode"], "disposition")
        self.assertEqual(disp.phase, "pre_match")
        self.assertIsNotNone(disp.next_auction_at)

    def test_prefer_yahoo_prev_close_over_stale_db(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE stock_daily_bars (stock_id TEXT, trade_date TEXT, close REAL);
            INSERT INTO stock_daily_bars VALUES ('6505', '2026-07-17', 81.3);
            CREATE TABLE rrg_universe_scores (
                stock_id TEXT, stock_name TEXT, session_date TEXT
            );
            """
        )
        now = datetime(2026, 7, 24, 10, 0, tzinfo=_TPE)
        st = build_live_ta_state(
            "6505",
            now=now,
            conn=conn,
            last_print=94.2,
            quote_meta={"yahoo_prev_close": 93.2, "quote_source": "yahoo_chart:6505.TW:1m"},
            disposition_ids={"2492"},
            ta_30m={"ta_30m_ready": False},
        )
        self.assertEqual(st.anchors["prev_close"], 93.2)
        self.assertEqual(st.anchors["prev_close_db"], 81.3)
        self.assertEqual(st.anchors["prev_close_source"], "yahoo")
        self.assertEqual(st.anchors["pct_from_prev"], round((94.2 / 93.2 - 1.0) * 100, 3))
        conn.close()

    def test_prefer_fubon_prev_close_over_yahoo(self) -> None:
        now = datetime(2026, 7, 24, 10, 0, tzinfo=_TPE)
        st = build_live_ta_state(
            "2330",
            now=now,
            last_print=1200.0,
            quote_meta={
                "quote_source": "fubon:marketdata:2330",
                "fubon_prev_close": 1180.0,
                "fubon_change_percent": 1.695,
                "yahoo_prev_close": 1170.0,
            },
            disposition_ids={"2492"},
            ta_30m={"ta_30m_ready": False},
        )
        self.assertEqual(st.anchors["prev_close"], 1180.0)
        self.assertEqual(st.anchors["prev_close_source"], "fubon")
        self.assertEqual(st.anchors["pct_from_prev"], 1.695)

    def test_parse_fubon_marketdata_quote_last_trade(self) -> None:
        raw = {
            "symbol": "2330",
            "name": "台積電",
            "previousClose": 1180.0,
            "changePercent": 0.85,
            "lastPrice": 1190.0,
            "lastTrade": {"price": 1190.0, "size": 10},
            "bids": [{"price": 1189.0, "size": 1}],
            "asks": [{"price": 1190.0, "size": 2}],
        }
        px, meta = parse_fubon_marketdata_quote(raw, stock_id="2330")
        self.assertEqual(px, 1190.0)
        self.assertEqual(meta["quote_source"], "fubon:marketdata:2330")
        self.assertEqual(meta["price_source"], "fubon_last_trade")
        self.assertEqual(meta["fubon_prev_close"], 1180.0)
        self.assertEqual(meta["fubon_change_percent"], 0.85)
        self.assertEqual(meta["fubon_bid"], 1189.0)

    def test_fetch_last_print_prefers_fubon_attaches_fubon_mom(self) -> None:
        fubon_q = (
            291.5,
            {
                "quote_source": "fubon:marketdata:2492",
                "fubon_prev_close": 300.0,
                "price_source": "fubon_last_trade",
            },
        )
        with (
            patch("ops_live_ta._yahoo_fallback_enabled", return_value=False),
            patch(
                "ops_live_ta.get_mom_1m_anchors",
                return_value={
                    "bar_source": "fubon:candles:2492:1",
                    "mom_1bar_pct": 0.2,
                    "mom_2bar_pct": 0.4,
                    "n_1m_bars": 12,
                    "momentum_horizon": "1m_bars_lookback_1_2",
                },
            ) as mom_mock,
            patch("ops_live_ta.fetch_last_print_yahoo") as yahoo_mock,
        ):
            px, meta = fetch_last_print("2492", fubon_quote=fubon_q, prefer_fubon=True)
        self.assertEqual(px, 291.5)
        self.assertEqual(meta["quote_source"], "fubon:marketdata:2492")
        self.assertEqual(meta["bar_source"], "fubon:candles:2492:1")
        self.assertEqual(meta["mom_2bar_pct"], 0.4)
        self.assertEqual(meta["fubon_prev_close"], 300.0)
        mom_mock.assert_called_once()
        yahoo_mock.assert_not_called()

    def test_parse_fubon_candles_1m(self) -> None:
        raw = {
            "symbol": "2330",
            "timeframe": 1,
            "data": [
                {
                    "date": "2026-07-24 10:00:00",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 10,
                },
                {
                    "date": "2026-07-24 10:01:00",
                    "open": 100.5,
                    "high": 101.2,
                    "low": 100.0,
                    "close": 101.0,
                    "volume": 12,
                },
            ],
        }
        bars, meta = parse_fubon_candles(raw, stock_id="2330", timeframe="1")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["close"], 101.0)
        self.assertEqual(meta["bar_source"], "fubon:candles:2330:1")

    def test_get_mom_1m_from_fubon_no_yahoo(self) -> None:
        bars = [
            {
                "ts": datetime(2026, 7, 24, 10, 0, tzinfo=_TPE),
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "ts": datetime(2026, 7, 24, 10, 1, tzinfo=_TPE),
                "open": 100.0,
                "high": 100.8,
                "low": 99.8,
                "close": 100.5,
                "volume": 1.0,
            },
            {
                "ts": datetime(2026, 7, 24, 10, 2, tzinfo=_TPE),
                "open": 100.5,
                "high": 101.0,
                "low": 100.2,
                "close": 100.8,
                "volume": 1.0,
            },
        ]
        with (
            patch("ops_live_ta._yahoo_fallback_enabled", return_value=False),
            patch(
                "ops_live_ta.fetch_fubon_candles",
                return_value=(bars, {"bar_source": "fubon:candles:2330:1"}),
            ),
            patch("ops_live_ta.fetch_yahoo_chart_quote") as yahoo_mock,
        ):
            meta = get_mom_1m_anchors("2330", last_print=100.8)
        self.assertEqual(meta["bar_source"], "fubon:candles:2330:1")
        self.assertIn("mom_1bar_pct", meta)
        self.assertIn("mom_2bar_pct", meta)
        yahoo_mock.assert_not_called()

    def test_get_ta_30m_uses_fubon_5m(self) -> None:
        day = datetime(2026, 7, 23, tzinfo=_TPE)
        bars = _fake_5m(day=day, n=12, step=0.3)
        with (
            patch("ops_live_ta._yahoo_fallback_enabled", return_value=False),
            patch(
                "ops_live_ta.fetch_fubon_candles",
                return_value=(bars, {"ta_30m_source": "fubon:candles:2330:5"}),
            ),
            patch("ops_live_ta.fetch_yahoo_5m_bars") as yahoo_mock,
        ):
            snap = get_ta_30m_anchors(
                "2330",
                now=datetime(2026, 7, 23, 10, 5, tzinfo=_TPE),
                last_print=103.0,
                force_refresh=True,
            )
        self.assertTrue(snap["ta_30m_ready"])
        self.assertEqual(snap["ta_30m_source"], "fubon:candles:2330:5")
        yahoo_mock.assert_not_called()

    def test_run_poll_fubon_batch_no_reconnect(self) -> None:
        """Batch miss must not open a second Fubon login per stock."""
        calls: list[list[str]] = []

        def _fake_batch(symbols, session=None, gap_sec=0.0):  # noqa: ANN001
            calls.append(list(symbols))
            return (
                {
                    "2330": (
                        100.0,
                        {
                            "quote_source": "fubon:marketdata:2330",
                            "fubon_prev_close": 99.0,
                        },
                    )
                },
                None,
            )

        with (
            patch("ops_live_ta._quote_backend", return_value="fubon"),
            patch("ops_live_ta._yahoo_fallback_enabled", return_value=False),
            patch(
                "ops_live_ta.open_fubon_rest_stock",
                return_value=(object(), object(), None),
            ),
            patch("ops_live_ta.fetch_fubon_marketdata_quotes", side_effect=_fake_batch),
            patch(
                "ops_live_ta.get_mom_1m_anchors",
                return_value={"bar_source": "fubon:candles:2330:1", "mom_2bar_pct": 0.1},
            ),
            patch(
                "ops_live_ta.get_ta_30m_anchors",
                return_value={"ta_30m_ready": False, "ta_30m_source": "fubon:candles:2330:5"},
            ),
            patch("ops_live_ta.fetch_last_print_yahoo") as yahoo_mock,
            patch("ops_live_ta.upsert_live_ta"),
        ):
            rows = run_live_ta_poll(
                [("2330", "台積電"), ("2303", "聯電")],
                dry_run=True,
                yahoo_gap_sec=0.0,
                disposition_ids={"2492"},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["2330", "2303"])
        self.assertEqual(rows[0]["anchors"]["quote_source"], "fubon:marketdata:2330")
        self.assertEqual(rows[0]["anchors"]["bar_source"], "fubon:candles:2330:1")
        self.assertIn("fubon_error", rows[1]["anchors"])
        yahoo_mock.assert_not_called()

    def test_round_px(self) -> None:
        self.assertEqual(_round_px(60.599998474121094), 60.6)
        self.assertEqual(_round_px(314.0), 314.0)

    def test_chart_quote_parses_meta(self) -> None:
        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "regularMarketPrice": 291.5,
                                    "chartPreviousClose": 300.0,
                                },
                                "indicators": {
                                    "quote": [{"close": [290.0, 290.5, 291.5]}]
                                },
                            }
                        ]
                    }
                }

        with patch("ops_live_ta.requests.get", return_value=_Resp()):
            px, meta = fetch_yahoo_chart_quote("2492")
        self.assertEqual(px, 291.5)
        self.assertTrue(str(meta.get("quote_source", "")).startswith("yahoo_chart:"))
        self.assertEqual(meta.get("mom_1bar_pct"), round((291.5 / 290.5 - 1) * 100, 3))
        self.assertEqual(meta.get("yahoo_prev_close"), 300.0)

    def test_ta_30m_not_ready_before_0930(self) -> None:
        day = datetime(2026, 7, 23, tzinfo=_TPE)
        bars = _fake_5m(day=day, n=5)
        snap = compute_ta_30m_snapshot(bars, now=datetime(2026, 7, 23, 9, 20, tzinfo=_TPE))
        self.assertFalse(snap["ta_30m_ready"])
        self.assertIn("09:30", snap["ta_30m_note_zh"])

    def test_ta_30m_ready_after_first_half_hour(self) -> None:
        day = datetime(2026, 7, 23, tzinfo=_TPE)
        bars = _fake_5m(day=day, n=12, step=0.3)  # up → 偏多
        now = datetime(2026, 7, 23, 10, 5, tzinfo=_TPE)
        snap = compute_ta_30m_snapshot(bars, now=now, last_print=103.0)
        self.assertTrue(snap["ta_30m_ready"])
        self.assertIsNotNone(snap["ta_30m_ret_pct"])
        self.assertGreater(snap["ta_30m_ret_pct"], 0)
        self.assertEqual(snap["ta_30m_bias"], "偏多")
        self.assertIn("非下單訊號", snap["ta_30m_note_zh"])
        self.assertIsNotNone(snap["ta_30m_vs_vwap_pct"])

    def test_ta_30m_window_excludes_forming_bar(self) -> None:
        day = datetime(2026, 7, 23, tzinfo=_TPE)
        bars = _fake_5m(day=day, n=18)
        now = datetime(2026, 7, 23, 10, 12, tzinfo=_TPE)
        window, end = last_closed_30m_window(bars, now=now)
        assert end is not None
        self.assertEqual(end.strftime("%H:%M"), "10:00")
        self.assertTrue(all(b["ts"] < end for b in window))
        self.assertGreaterEqual(len(window), 4)

    def test_session_vwap(self) -> None:
        day = datetime(2026, 7, 23, tzinfo=_TPE)
        bars = _fake_5m(day=day, n=6, open0=100.0, step=0.0, volume=100.0)
        vwap = session_vwap_from_bars(bars)
        self.assertIsNotNone(vwap)
        assert vwap is not None
        self.assertAlmostEqual(vwap, 100.0, places=2)

    def test_build_merges_ta_30m_override(self) -> None:
        now = datetime(2026, 7, 23, 10, 5, tzinfo=_TPE)
        st = build_live_ta_state(
            "2330",
            now=now,
            last_print=100.0,
            quote_meta={"quote_source": "test"},
            disposition_ids={"2492"},
            ta_30m={
                "ta_30m_ready": True,
                "ta_30m_ret_pct": 0.5,
                "ta_30m_bias": "偏多",
                "ta_30m_note_zh": "觀察用・非下單訊號",
                "fade30_observe": {
                    "temp": -1,
                    "label": "fade30m·偏弱",
                    "disclaimer_zh": "研究觀察",
                },
            },
        )
        self.assertTrue(st.anchors["ta_30m_ready"])
        self.assertEqual(st.anchors["ta_30m_bias"], "偏多")
        self.assertEqual(st.anchors["fade30_observe"]["temp"], -1)


if __name__ == "__main__":
    unittest.main()
