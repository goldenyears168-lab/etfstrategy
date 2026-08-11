"""TW-US 1-minute MA-deviation spread gate tests.

Mirrors tests/test_tmf_nq_gate.py's mocking pattern (mock.patch.object on
us_futures_overnight.fetch_yahoo_intraday_closes + clear_aux_cache) so no
real network call happens.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import us_futures_overnight
from tmf_channel import nq_1m_spread_gate as gate
from tmf_channel.aux_cache import clear_aux_cache

_TZ = ZoneInfo("Asia/Taipei")
_TZ_ET = ZoneInfo("America/New_York")
WINDOW_MIN = gate.WINDOW_MIN


def _tw_bars(last_close: float, prior_close: float = 22000.0) -> tuple[list[float], list[str]]:
    """WINDOW_MIN+1 bars: last WINDOW_MIN all == prior_close, final bar == last_close.
    now_px = last_close; tw_ma should equal prior_close (current bar excluded)."""
    C = [prior_close] * WINDOW_MIN + [last_close]
    end = datetime(2026, 8, 5, 10, 0, tzinfo=_TZ)
    times = [end - timedelta(minutes=WINDOW_MIN - i) for i in range(len(C))]
    T = [t.isoformat(timespec="milliseconds") for t in times]
    return C, T


def _fake_nq_fetch(flat_value: float = 100.0, *, span_min: int = WINDOW_MIN + 10, end_et: datetime | None = None):
    if end_et is None:
        end_et = datetime(2026, 8, 4, 22, 0, tzinfo=_TZ_ET)
    idx = pd.date_range(end=end_et, periods=span_min, freq="1min", tz=_TZ_ET)
    series = pd.Series([flat_value] * span_min, index=idx, dtype=float)

    def fake(yahoo_symbol, start, end, *, interval="1m"):
        return series

    return fake


class SpreadSideForDayTest(unittest.TestCase):
    def setUp(self):
        clear_aux_cache()

    def tearDown(self):
        clear_aux_cache()

    def _side(self, C, T, *, nq_fetch=None):
        clear_aux_cache()
        nq_fetch = nq_fetch or _fake_nq_fetch()
        with mock.patch.object(us_futures_overnight, "fetch_yahoo_intraday_closes", side_effect=nq_fetch):
            return gate.spread_side_for_day("2026-08-05", hm=T[-1][11:16], C=C, T=T)

    def test_spread_above_threshold_returns_short(self):
        C, T = _tw_bars(22000.0 * 1.005)  # tw_dev = +0.5%, us_dev = 0 -> spread=+0.5 >= 0.2
        self.assertEqual(self._side(C, T), "S")

    def test_debug_numbers_recorded_for_audit(self):
        C, T = _tw_bars(22000.0 * 1.005)
        self._side(C, T)
        dbg = gate.last_spread_debug()
        self.assertAlmostEqual(dbg["tw_dev"], 0.5, places=2)
        self.assertAlmostEqual(dbg["us_dev"], 0.0, places=2)
        self.assertAlmostEqual(dbg["spread"], 0.5, places=2)
        self.assertIsNotNone(dbg["nq_last_ts"])

    def test_debug_cleared_on_fail_open(self):
        C, T = _tw_bars(22000.0 * 1.005)

        def raising_fetch(*a, **k):
            raise RuntimeError("network down")

        self._side(C, T, nq_fetch=raising_fetch)
        self.assertIsNone(gate.last_spread_debug())

    def test_spread_below_negative_threshold_returns_long(self):
        C, T = _tw_bars(22000.0 * 0.995)  # tw_dev = -0.5% -> spread=-0.5 <= -0.2
        self.assertEqual(self._side(C, T), "L")

    def test_spread_within_band_returns_none_str(self):
        C, T = _tw_bars(22000.0 * 1.001)  # tw_dev = +0.1%, within [-0.2, 0.2]
        self.assertEqual(self._side(C, T), "none")

    def test_current_bar_excluded_from_own_reference_average(self):
        # If the current bar leaked into its own MA, tw_dev would be diluted
        # (~0.005% instead of 0.5%) and this would NOT clear the threshold.
        C, T = _tw_bars(22000.0 * 1.005)
        self.assertEqual(self._side(C, T), "S")

    def test_insufficient_tx_bars_hard_blocks(self):
        C, T = _tw_bars(22000.0 * 1.005)
        C, T = C[-10:], T[-10:]  # far fewer than WINDOW_MIN+1
        self.assertEqual(self._side(C, T), "none")

    def test_no_nq_coverage_at_instant_hard_blocks(self):
        C, T = _tw_bars(22000.0 * 1.005)
        # NQ series covers a completely different time window
        stale_fetch = _fake_nq_fetch(end_et=datetime(2020, 1, 1, tzinfo=_TZ_ET))
        self.assertEqual(self._side(C, T, nq_fetch=stale_fetch), "none")

    def test_feed_load_exception_fails_open(self):
        C, T = _tw_bars(22000.0 * 1.005)

        def raising_fetch(*a, **k):
            raise RuntimeError("network down")

        self.assertIsNone(self._side(C, T, nq_fetch=raising_fetch))
        self.assertIn("network down", gate.last_spread_load_error() or "")

    def test_empty_nq_series_fails_open(self):
        C, T = _tw_bars(22000.0 * 1.005)

        def empty_fetch(*a, **k):
            return pd.Series(dtype=float)

        self.assertIsNone(self._side(C, T, nq_fetch=empty_fetch))


if __name__ == "__main__":
    unittest.main()
