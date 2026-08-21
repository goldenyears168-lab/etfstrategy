"""order.tmf_channel_marketdata · front-month session param regression tests.

Covers the 2026-08-08 live incident: resolve_front_symbol() called Fubon's
tickers() endpoint without a ``session`` param (defaulting to REGULAR), which
returns an empty contract list outside day-session hours. The worker kept
resolving a symbol overnight only via the 300s cache carrying over from
day-session close; once that cache went stale past midnight, every poll
failed with "No front-month TMF from Fubon tickers" and the worker could
neither enter nor exit any position until the fix below (passing
session=AFTERHOURS at night) was deployed.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from order import tmf_channel_marketdata as mkt


class TickerApiSessionTest(unittest.TestCase):
    def test_day_session_hours_use_regular(self):
        for hm in ("08:45", "10:30", "13:45"):
            self.assertEqual(mkt._ticker_api_session(hm), "REGULAR", hm)

    def test_outside_day_session_uses_afterhours(self):
        for hm in ("00:00", "00:30", "04:59", "05:00", "08:44", "13:46", "15:00", "23:59"):
            self.assertEqual(mkt._ticker_api_session(hm), "AFTERHOURS", hm)

    def test_defaults_to_current_wall_clock(self):
        # No explicit hm: must resolve via session_hhmm_now(), not crash.
        result = mkt._ticker_api_session()
        self.assertIn(result, ("REGULAR", "AFTERHOURS"))


class Fetch1mBarsWsFallbackTest(unittest.TestCase):
    """2026-08-13: day-session REST candles() call now prefers a websocket
    feed (see order.tmf_channel_ws_feed) and only falls back to REST when
    that feed isn't fresh -- pins both branches so a regression in the
    wiring can't silently drop the REST fallback path (the only thing
    standing between a websocket outage and an entirely blind worker).

    2026-08-17: bar timestamps are now generated from the current wall-clock
    date. They were hardcoded to "2026-08-13T09:00" when written, and
    fetch_1m_bars' _in_day_window() only keeps rows whose calendar date IS
    today -- so both tests started returning 0 bars on 2026-08-14 and had
    been red on every run since (never noticed: this work was still
    uncommitted, so CI never saw it)."""

    @staticmethod
    def _bar_ts() -> str:
        # 09:00 today, Asia/Taipei -- inside _in_day_window for any weekday.
        return datetime.now(tz=mkt._TZ).replace(
            hour=9, minute=0, second=0, microsecond=0
        ).isoformat(timespec="milliseconds")

    def _fake_session(self, day_return, night_return=None):
        session = MagicMock()
        session._tmf_realtime_ok = True
        fut = session.sdk.marketdata.rest_client.futopt
        fut.intraday.candles.return_value = day_return
        return session, fut

    def test_ws_fresh_skips_rest_day_call(self):
        session, fut = self._fake_session({"data": []})
        ws_rows = [
            {"date": self._bar_ts(), "open": 100, "high": 101, "low": 99, "close": 100.5},
        ]
        with patch.object(mkt, "get_day_rows_via_ws", return_value=ws_rows) as mock_ws:
            bars = mkt.fetch_1m_bars(session, "TMFH6")
        mock_ws.assert_called_once_with(session, "TMFH6")
        # day candles() must NOT be called with the plain (day) signature --
        # only the explicit session="afterhours" call is allowed through.
        for call in fut.intraday.candles.call_args_list:
            self.assertEqual(call.kwargs.get("session"), "afterhours")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["o"], 100.0)

    def test_ws_none_falls_back_to_rest_day_call(self):
        session, fut = self._fake_session(
            {"data": [{"date": self._bar_ts(), "open": 200, "high": 201, "low": 199, "close": 200.5}]}
        )
        with patch.object(mkt, "get_day_rows_via_ws", return_value=None) as mock_ws:
            bars = mkt.fetch_1m_bars(session, "TMFH6")
        mock_ws.assert_called_once_with(session, "TMFH6")
        plain_calls = [c for c in fut.intraday.candles.call_args_list if "session" not in c.kwargs]
        self.assertEqual(len(plain_calls), 1)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["o"], 200.0)


class ResolveFrontSymbolSessionParamTest(unittest.TestCase):
    def setUp(self):
        mkt._CACHE["sym"] = None
        mkt._CACHE["ts"] = 0.0

    def tearDown(self):
        mkt._CACHE["sym"] = None
        mkt._CACHE["ts"] = 0.0

    def _fake_session(self, tickers_return):
        session = MagicMock()
        session._tmf_realtime_ok = True
        fut = session.sdk.marketdata.rest_client.futopt
        fut.intraday.tickers.return_value = tickers_return
        return session, fut

    def test_passes_afterhours_session_at_night_and_resolves(self):
        # ⚠️ 到期日不可寫死：resolve_front_symbol 會濾掉 ``end < today`` 的合約，
        # 寫死日期會讓本測試在該日之後永久失敗（實測 2026-08-19 的 fixture 到
        # 2026-08-21 就開始紅）。一律用相對今天的未來日。
        future_end = (datetime.now(tz=mkt._TZ).date() + timedelta(days=30)).isoformat()
        session, fut = self._fake_session(
            {
                "data": [
                    {"symbol": "TMFH6", "endDate": future_end, "name": "微型臺指期貨086"},
                ]
            }
        )
        with patch.object(mkt, "_ticker_api_session", return_value="AFTERHOURS"):
            sym, name, end = mkt.resolve_front_symbol(session, product="TMF")
        self.assertEqual(sym, "TMFH6")
        fut.intraday.tickers.assert_called_once_with(
            type="FUTURE", exchange="TAIFEX", product="TMF", session="AFTERHOURS"
        )

    def test_empty_regular_session_data_raises_not_silently_flat(self):
        session, fut = self._fake_session({"data": []})
        with self.assertRaises(RuntimeError):
            mkt.resolve_front_symbol(session, product="TMF")


if __name__ == "__main__":
    unittest.main()
