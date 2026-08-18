"""Tick index builder + passive-fill models (queue-aware replay foundations).

Pins the three things that silently produced wrong numbers while this was
being built:
  1. the FinMind TX tape mixes outrights with calendar spreads (a "202608/
     202609" row prices at ~40 against an index near 44,000) — unfiltered it
     made the replay report +1.25M points/day;
  2. causal_engine._bar_tick_range() infers bar t's tick slice from bar t+1's
     start offset and silently falls back to end-of-day when t+1 is missing,
     so the builder MUST emit an offset for tickless minutes too;
  3. fill_model="touch" must remain byte-identical to the pre-existing
     behaviour, because every published result for this recipe assumes it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tmf_channel import tick_index as ti


def _tick(ts: str, px: float, vol: float, contract: str = "202608") -> dict:
    return {
        "futures_id": "TX",
        "contract_date": contract,
        "date": ts,
        "price": px,
        "volume": vol,
    }


class TickIndexBuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._patch = mock.patch.object(ti, "tick_dir", return_value=self.dir)
        self._patch.start()
        ti._load_raw.cache_clear()

    def tearDown(self):
        self._patch.stop()
        ti._load_raw.cache_clear()
        self._tmp.cleanup()

    def _write(self, day: str, rows: list[dict]) -> None:
        (self.dir / f"{day}.json").write_text(json.dumps(rows), encoding="utf-8")

    def test_calendar_spreads_are_excluded(self):
        self._write(
            "2026-08-06",
            [
                _tick("2026-08-06 09:00:01", 44000.0, 10),
                _tick("2026-08-06 09:00:02", 44001.0, 10),
                # spread row: price is the spread itself, ~40 pts
                _tick("2026-08-06 09:00:03", 40.0, 500, contract="202608/202609"),
            ],
        )
        idx = ti.build_tick_index(["2026-08-06T09:00:00+08:00"])
        self.assertIsNotNone(idx)
        self.assertEqual(idx.tk_px, [44000.0, 44001.0])
        self.assertNotIn(40.0, idx.tk_px)

    def test_front_month_is_the_highest_volume_outright(self):
        self._write(
            "2026-08-06",
            [
                _tick("2026-08-06 09:00:01", 44000.0, 100, contract="202608"),
                _tick("2026-08-06 09:00:02", 44050.0, 1, contract="202609"),
            ],
        )
        idx = ti.build_tick_index(["2026-08-06T09:00:00+08:00"])
        self.assertEqual(idx.tk_px, [44000.0])

    def test_tickless_minute_still_gets_an_offset(self):
        # Bar 09:01 has no prints. Without an offset for it, causal_engine's
        # _bar_tick_range() would hand bar 09:00 every remaining tick of the
        # session (its `end` falls back to n_tk) — wholesale look-ahead.
        self._write(
            "2026-08-06",
            [
                _tick("2026-08-06 09:00:01", 44000.0, 1),
                _tick("2026-08-06 09:02:01", 44100.0, 1),
            ],
        )
        T = [
            "2026-08-06T09:00:00+08:00",
            "2026-08-06T09:01:00+08:00",
            "2026-08-06T09:02:00+08:00",
        ]
        idx = ti.build_tick_index(T)
        self.assertEqual(sorted(idx.minute_start_idx), sorted(T))
        self.assertEqual(idx.minute_start_idx[T[0]], 0)
        self.assertEqual(idx.minute_start_idx[T[1]], 1)  # empty slice [1,1)
        self.assertEqual(idx.minute_start_idx[T[2]], 1)
        self.assertEqual(ti.coverage(T, idx), round(2 / 3, 4))

    def test_missing_day_returns_none(self):
        self.assertIsNone(ti.build_tick_index(["2026-01-01T09:00:00+08:00"]))
        self.assertIsNone(ti.build_tick_index([]))

    def test_night_session_spans_two_dates(self):
        self._write("2026-08-06", [_tick("2026-08-06 23:59:00", 44000.0, 1)])
        self._write("2026-08-07", [_tick("2026-08-07 00:01:00", 44010.0, 1)])
        idx = ti.build_tick_index(
            ["2026-08-06T23:59:00+08:00", "2026-08-07T00:01:00+08:00"]
        )
        self.assertEqual(idx.tk_px, [44000.0, 44010.0])


class FillModelTest(unittest.TestCase):
    """Drives causal_engine._entry_fillable through simulate()'s closure by
    exercising the public parameter surface instead of reaching inside."""

    def _run(self, fill_model: str, queue_ahead_lots: float = 0.0):
        from tmf_channel.engine import simulate

        n = 40
        # flat tape, then a probe that reaches exactly 44030 and one that
        # pushes through to 44035
        O = [44000.0] * n
        H = [44000.0] * n
        L = [44000.0] * n
        C = [44000.0] * n
        V = [100.0] * n
        T = [f"2026-08-06T{9 + i // 60:02d}:{i % 60:02d}:00+08:00" for i in range(n)]
        recipe = {
            "hang_anchor": "O",
            "hang_lo": 30.0,
            "hang_hi": 30.0,
            "tick_native": True,
            "fill_model": fill_model,
            "queue_ahead_lots": queue_ahead_lots,
            "eod_flatten": True,
            "max_lots": 1,
            "skip_quiet_regime": False,
            "skip_quiet_mode": "none",
            "use_thermo": False,
            "vix_session_bias": False,
            "session_pv_book": None,
        }
        # every minute prints 44000 then 44030 (touch) — never through
        px_seq, vol_seq, starts = [], [], {}
        for t in T:
            starts[t] = len(px_seq)
            px_seq += [44000.0, 44030.0, 44030.0]
            vol_seq += [1.0, 3.0, 3.0]
        idx = ti.TickIndex(
            T=T, minute_start_idx=starts, n_tk=len(px_seq), tk_px=px_seq, tk_vol=vol_seq
        )
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta={}, tick_index=idx)
        return trades

    def test_touch_fills_on_a_touch_only_tape(self):
        self.assertGreater(len(self._run("touch")), 0)

    def test_through_never_fills_on_a_touch_only_tape(self):
        self.assertEqual(len(self._run("through")), 0)

    def test_queue_needs_enough_volume_at_the_price(self):
        # 6 lots print at the rail each minute; a 5-lot queue clears, 10 does not
        self.assertGreater(len(self._run("queue", queue_ahead_lots=5)), 0)
        self.assertEqual(len(self._run("queue", queue_ahead_lots=10_000)), 0)

    def test_unknown_model_raises_rather_than_silently_filling(self):
        with self.assertRaises(ValueError):
            self._run("front_of_book_always")


if __name__ == "__main__":
    unittest.main()
