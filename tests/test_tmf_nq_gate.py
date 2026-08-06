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

from tmf_channel import nq_gate, nq_signal
from tmf_channel.aux_cache import clear_aux_cache

PROJECT_ROOT = Path(__file__).resolve().parents[1]
R5_PATH = PROJECT_ROOT / "reports/research/channel_lab/r5_synth_p0p1_vs_baseline.py"


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


class NqSideForDayTest(unittest.TestCase):
    def setUp(self):
        clear_aux_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "nq_es_1h.json"

    def tearDown(self):
        clear_aux_cache()
        self._tmp.cleanup()

    def _side(self, last_nq_close: float, hm: str = "16:00") -> str | None:
        _write_cache(self.cache, last_nq_close)
        clear_aux_cache()
        with mock.patch.object(nq_signal, "NQ_ES_1H_CACHE", self.cache):
            return nq_gate.nq_side_for_day("2026-08-05", hm=hm)

    def test_up_drift_returns_long(self):
        self.assertEqual(self._side(101.0), "L")  # +1.0% ≥ 0.15

    def test_down_drift_returns_short(self):
        self.assertEqual(self._side(98.0), "S")  # −2.0% ≤ −0.15

    def test_flat_drift_returns_none_side(self):
        self.assertEqual(self._side(100.1), "none")  # +0.1% inside ±0.15

    def test_day_session_hm_branch(self):
        # hm < 15:00 → 08:45 TW anchor; same synthetic data still resolves.
        self.assertEqual(self._side(101.0, hm="09:00"), "L")

    def test_missing_cache_fails_safe_none(self):
        clear_aux_cache()
        missing = Path(self._tmp.name) / "nope.json"
        with mock.patch.object(nq_signal, "NQ_ES_1H_CACHE", missing):
            self.assertIsNone(nq_gate.nq_side_for_day("2026-08-05", hm="16:00"))
        self.assertIsNotNone(nq_gate.last_nq_load_error())

    def test_broken_cache_fails_safe_none(self):
        clear_aux_cache()
        self.cache.write_text("{not json")
        with mock.patch.object(nq_signal, "NQ_ES_1H_CACHE", self.cache):
            self.assertIsNone(nq_gate.nq_side_for_day("2026-08-05", hm="16:00"))


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
            datetime(2026, 8, 5, 8, 45, tzinfo=tz_tw),
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

        dt = datetime(2026, 8, 5, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        kw = dict(nq_1h=ours[0], es_1h=ours[1], nq_d=ours[2], es_d=ours[3], us_dates=ours[4])
        snap_a = nq_signal.futures_overnight_at(dt, **kw)
        snap_b = self.r5.futures_overnight_at(dt, **kw)
        self.assertEqual(snap_a, snap_b)
        if snap_a is not None:
            nq = snap_a.get("nq_overnight_pct")
            self.assertEqual(nq_signal.bias_side(nq), self.r5.bias_side(nq))


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
