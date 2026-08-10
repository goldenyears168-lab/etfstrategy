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
        session, fut = self._fake_session(
            {
                "data": [
                    {"symbol": "TMFH6", "endDate": "2026-08-19", "name": "微型臺指期貨086"},
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
