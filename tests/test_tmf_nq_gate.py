"""NQ gate tests — in-package signal + parity vs channel_lab R5 research copy.

Tests may importlib-load the research file under reports/ for parity pinning;
production code (src/tmf_channel/nq_gate.py) must not.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import us_futures_overnight
from tmf_channel import nq_gate, nq_signal
from tmf_channel.aux_cache import clear_aux_cache

PROJECT_ROOT = Path(__file__).resolve().parents[1]
R5_PATH = PROJECT_ROOT / "reports/research/channel_lab/r5_synth_p0p1_vs_baseline.py"
_TZ_ET = ZoneInfo("America/New_York")


def _load_r5():
    """Importlib-load the research module (allowed in tests only)."""
    name = "tmf_nq_gate_test_r5"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, R5_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register before exec (dataclass pickling quirk)
    spec.loader.exec_module(mod)
    return mod


def _write_cache(path: Path, last_nq_close: float) -> None:
    """Synthetic NQ/ES 1h cache: prior US RTH close 100.0 on 2026-08-04."""

    def recs(last_close: float) -> list[dict]:
        return [
            {"Datetime": "2026-08-04T15:30:00", "Close": 100.0},
            {"Datetime": "2026-08-04T20:00:00", "Close": last_close},
        ]

    payload = {
        "nq_futures_1h": {"records": recs(last_nq_close)},
        "es_futures_1h": {"records": recs(100.5)},
    }
    path.write_text(json.dumps(payload))


def _fake_intraday_fetch(nq_last_close: float, es_last_close: float = 100.5):
    """Two 1h points on 2026-08-04: RTH close 100.0, then an afterhours print."""

    def fake(yahoo_symbol, start, end, *, interval="1h"):
        last = nq_last_close if yahoo_symbol == us_futures_overnight.NQ_YAHOO else es_last_close
        idx = pd.DatetimeIndex(
            [pd.Timestamp("2026-08-04T15:30:00"), pd.Timestamp("2026-08-04T20:00:00")]
        ).tz_localize(_TZ_ET)
        return pd.Series([100.0, last], index=idx, dtype=float)

    return fake


def _fake_daily_fetch():
    def fake(yahoo_symbol, start, end):
        return pd.Series({"2026-08-04": 100.0})

    return fake


class NqSideForDayTest(unittest.TestCase):
    def setUp(self):
        clear_aux_cache()

    def tearDown(self):
        clear_aux_cache()

    def _side(self, last_nq_close: float, hm: str = "16:00", *, day: str = "2026-08-05") -> str | None:
        clear_aux_cache()
        with mock.patch.object(
            us_futures_overnight,
            "fetch_yahoo_intraday_closes",
            side_effect=_fake_intraday_fetch(last_nq_close),
        ), mock.patch.object(
            us_futures_overnight, "fetch_yahoo_daily_closes", side_effect=_fake_daily_fetch()
        ):
            return nq_gate.nq_side_for_day(day, hm=hm)

    def test_up_drift_returns_long(self):
        self.assertEqual(self._side(101.0), "L")  # +1.0% ≥ 0.15

    def test_down_drift_returns_short(self):
        self.assertEqual(self._side(98.0), "S")  # −2.0% ≤ −0.15

    def test_flat_drift_returns_none_side(self):
        self.assertEqual(self._side(100.1), "none")  # +0.1% inside ±0.15

    def test_day_session_hm_branch(self):
        # anchor = day + hm directly (continuous, 2026-08-10); same
        # synthetic data still resolves regardless of the exact hm chosen.
        self.assertEqual(self._side(101.0, hm="09:00"), "L")

    def test_fetch_failure_fails_safe_none(self):
        clear_aux_cache()
        with mock.patch.object(
            us_futures_overnight,
            "fetch_yahoo_intraday_closes",
            side_effect=RuntimeError("yahoo unreachable"),
        ):
            self.assertIsNone(nq_gate.nq_side_for_day("2026-08-05", hm="16:00"))
        self.assertIsNotNone(nq_gate.last_nq_load_error())

    def test_empty_series_fails_safe_none(self):
        clear_aux_cache()
        with mock.patch.object(
            us_futures_overnight,
            "fetch_yahoo_intraday_closes",
            side_effect=lambda *a, **k: pd.Series(dtype=float),
        ), mock.patch.object(
            us_futures_overnight,
            "fetch_yahoo_daily_closes",
            side_effect=lambda *a, **k: pd.Series(dtype=float),
        ):
            self.assertIsNone(nq_gate.nq_side_for_day("2026-08-05", hm="16:00"))
        self.assertIsNotNone(nq_gate.last_nq_load_error())

    def test_night_tail_anchors_to_actual_current_moment(self):
        """2026-08-08: hm<05:00 used to anchor to *yesterday's* 15:00 open
        (the session-open-freeze design) instead of the night session's
        actual current moment -- fixed same night. 2026-08-10: the whole
        session-open-freeze design was replaced by a continuous anchor (day
        + hm directly, recomputed every call instead of frozen once per
        session -- true re-simulation showed this beats the frozen anchor
        on both in-sample and out-of-sample windows, OOS p=0.0037). Under
        the new design, day="2026-08-06" hm="03:00" simply anchors to
        2026-08-06T03:00 -- today's own date, not shifted to yesterday --
        because ``day`` and ``hm`` together already describe the real
        current moment; no special-casing is needed for the tail."""
        seen = {}
        real_fn = nq_signal.futures_overnight_at

        def spy(dt_tw, **kw):
            seen["dt"] = dt_tw
            return real_fn(dt_tw, **kw)

        with mock.patch.object(
            us_futures_overnight,
            "fetch_yahoo_intraday_closes",
            side_effect=_fake_intraday_fetch(101.0),
        ), mock.patch.object(
            us_futures_overnight, "fetch_yahoo_daily_closes", side_effect=_fake_daily_fetch()
        ), mock.patch.object(nq_signal, "futures_overnight_at", side_effect=spy):
            nq_gate.nq_side_for_day("2026-08-06", hm="03:00")
        self.assertEqual(seen["dt"].date().isoformat(), "2026-08-06")
        self.assertEqual(seen["dt"].hour, 3)

    def test_anchor_moves_with_hm_within_the_same_day(self):
        """The core 'continuous' property (2026-08-10): two calls for the
        SAME day but different hm must anchor to different moments -- under
        the old frozen-at-session-open design both of these (08:46 and
        13:44, both inside the day session) would have anchored to the
        identical 08:45 timestamp."""
        seen = []
        real_fn = nq_signal.futures_overnight_at

        def spy(dt_tw, **kw):
            seen.append(dt_tw)
            return real_fn(dt_tw, **kw)

        with mock.patch.object(
            us_futures_overnight,
            "fetch_yahoo_intraday_closes",
            side_effect=_fake_intraday_fetch(101.0),
        ), mock.patch.object(
            us_futures_overnight, "fetch_yahoo_daily_closes", side_effect=_fake_daily_fetch()
        ), mock.patch.object(nq_signal, "futures_overnight_at", side_effect=spy):
            nq_gate.nq_side_for_day("2026-08-06", hm="08:46")
            nq_gate.nq_side_for_day("2026-08-06", hm="13:44")
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual((seen[0].hour, seen[0].minute), (8, 46))
        self.assertEqual((seen[1].hour, seen[1].minute), (13, 44))


class MinAgeExcludesFormingBarTest(unittest.TestCase):
    """2026-08-10 live: TMF's NQ overnight gate read "none" (flat) for ~4h
    straight one night. Root cause: an hourly bar indexed at its START time
    keeps updating its "close" as trades print through the whole hour, so a
    query 10min into that hour sees a partial/unsettled value -- re-querying
    the SAME historical moments later (once those bars had actually closed)
    showed real, threshold-crossing moves the live gate never saw. Same
    class of bug as order/tmf_channel_order.py's _drop_forming_last_bar for
    1m TX bars, one level up at hourly NQ/ES granularity.
    price_at_or_before(min_age=1h) only accepts a bar once its own close
    time (index + min_age) is at or before the query moment."""

    def _series(self):
        idx = pd.DatetimeIndex(
            [
                pd.Timestamp("2026-08-10T07:00:00", tz=_TZ_ET),
                pd.Timestamp("2026-08-10T08:00:00", tz=_TZ_ET),
            ]
        )
        return pd.Series([100.0, 105.0], index=idx, dtype=float)

    def test_query_within_the_forming_hour_uses_prior_settled_bar(self):
        s = self._series()
        # 08:10 ET is 10min into the 08:00 bar's own hour -- still forming.
        dt = pd.Timestamp("2026-08-10T08:10:00", tz=_TZ_ET)
        from datetime import timedelta

        val = us_futures_overnight.price_at_or_before(s, dt, min_age=timedelta(hours=1))
        self.assertEqual(val, 100.0)  # the settled 07:00 bar, not the forming 08:00 one

    def test_query_after_the_bar_fully_closes_uses_it(self):
        s = self._series()
        # 09:05 ET: the 08:00 bar closed at 09:00, a full hour has elapsed.
        dt = pd.Timestamp("2026-08-10T09:05:00", tz=_TZ_ET)
        from datetime import timedelta

        val = us_futures_overnight.price_at_or_before(s, dt, min_age=timedelta(hours=1))
        self.assertEqual(val, 105.0)

    def test_no_min_age_keeps_old_behavior(self):
        s = self._series()
        dt = pd.Timestamp("2026-08-10T08:10:00", tz=_TZ_ET)
        val = us_futures_overnight.price_at_or_before(s, dt)
        self.assertEqual(val, 105.0)  # unchanged: forming bar accepted when min_age=None


@unittest.skipUnless(R5_PATH.is_file(), "r5 research file not present")
class R5ParityTest(unittest.TestCase):
    """Pin numeric parity between the in-package copy and the R5 original."""

    def setUp(self):
        clear_aux_cache()
        self.r5 = _load_r5()
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "nq_es_1h.json"

    def tearDown(self):
        clear_aux_cache()
        self._tmp.cleanup()

    def test_bias_side_parity_grid(self):
        grid = [
            None,
            float("nan"),
            0.0,
            0.05,
            0.1499,
            0.15,
            0.16,
            -0.05,
            -0.1499,
            -0.15,
            -0.16,
            1.0,
            -3.2,
        ]
        for v in grid:
            self.assertEqual(nq_signal.bias_side(v), self.r5.bias_side(v), msg=f"v={v}")
        self.assertEqual(nq_signal.FLAT_EPS, self.r5.FLAT_EPS)

    def test_bundle_and_overnight_parity_on_synthetic_cache(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        _write_cache(self.cache, 101.0)
        with mock.patch.object(nq_signal, "NQ_ES_1H_CACHE", self.cache), mock.patch.object(
            self.r5, "NQ_ES_1H", self.cache
        ):
            ours = nq_signal.load_futures_1h()
            theirs = self.r5.load_futures_1h()
        for a, b in zip(ours[:4], theirs[:4]):
            self.assertTrue(a.equals(b))
        self.assertEqual(ours[4], theirs[4])  # us_dates

        tz_tw = ZoneInfo("Asia/Taipei")
        for dt in (
            datetime(2026, 8, 5, 15, 0, tzinfo=tz_tw),
            # 2026-08-05 08:45 TW deliberately excluded (2026-08-10): it maps
            # to 2026-08-04 20:45 ET, less than 1h after the synthetic
            # 20:00 ET point -- our copy now correctly excludes that
            # still-forming bar (price_at_or_before min_age=1h) while the
            # frozen r5 reference does not, so parity is expected to break
            # right here. See MinAgeExcludesFormingBarTest for the fix itself.
            datetime(2026, 8, 1, 15, 0, tzinfo=tz_tw),  # before any data
        ):
            kw = dict(
                nq_1h=ours[0], es_1h=ours[1], nq_d=ours[2], es_d=ours[3], us_dates=ours[4]
            )
            self.assertEqual(
                nq_signal.futures_overnight_at(dt, **kw),
                self.r5.futures_overnight_at(dt, **kw),
                msg=f"dt={dt}",
            )

    def test_real_cache_parity_if_present(self):
        """On mini the real 1h cache exists — full bundle + today parity."""
        if not nq_signal.NQ_ES_1H_CACHE.is_file():
            self.skipTest("real NQ/ES cache not materialized")
        with mock.patch.object(self.r5, "NQ_ES_1H", nq_signal.NQ_ES_1H_CACHE):
            ours = nq_signal.load_futures_1h()
            theirs = self.r5.load_futures_1h()
        for a, b in zip(ours[:4], theirs[:4]):
            self.assertTrue(a.equals(b))
        self.assertEqual(ours[4], theirs[4])
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Snapshot-value parity with r5 deliberately NOT asserted below
        # (2026-08-10): our copy now excludes still-forming NQ/ES hourly
        # bars (price_at_or_before min_age=1h -- see
        # MinAgeExcludesFormingBarTest) while the frozen r5 reference does
        # not, so any query point close enough to the real cache's most
        # recent bar legitimately diverges. Bundle-loading parity above
        # (the actual data fetch) is unaffected and still asserted.
        dt = datetime(2026, 8, 5, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        kw = dict(nq_1h=ours[0], es_1h=ours[1], nq_d=ours[2], es_d=ours[3], us_dates=ours[4])
        snap_a = nq_signal.futures_overnight_at(dt, **kw)
        self.assertTrue(snap_a is None or "nq_overnight_pct" in snap_a)


class NoResearchImportTest(unittest.TestCase):
    def test_nq_gate_has_no_reports_or_importlib_dependency(self):
        src = inspect.getsource(nq_gate)
        self.assertNotIn("importlib", src)
        self.assertNotIn("reports/", src)
        self.assertNotIn("r5_synth", src)

    def test_math_isfinite_note(self):
        # bias_side treats nan as missing — sanity on the frozen copy.
        self.assertEqual(nq_signal.bias_side(float("nan")), "missing")
        self.assertTrue(math.isnan(float("nan")))


if __name__ == "__main__":
    unittest.main()
