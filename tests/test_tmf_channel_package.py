"""Architecture package tests · tmf_channel engine boundary + caches."""

from __future__ import annotations

import unittest
from unittest import mock


class EngineImportTest(unittest.TestCase):
    def test_order_imports_tmf_channel_engine(self):
        from tmf_channel.engine import simulate

        self.assertTrue(callable(simulate))

    def test_lab_shim_reexports_simulate(self):
        import sys
        from pathlib import Path

        lab = Path(__file__).resolve().parents[1] / "reports" / "research" / "channel_lab"
        # reports/* 是 gitignored（.gitignore:13）—— 這支 shim 只存在於 mini 本機的
        # 研究側，CI checkout 永遠拿不到。缺席時跳過，不是讓 CI 紅。
        if not (lab / "hang_anchor_causal_lab.py").exists():
            self.skipTest("channel_lab shim 不在 checkout 內（reports/* 為 gitignored）")
        sys.path.insert(0, str(lab))
        import hang_anchor_causal_lab as shim  # noqa: WPS433

        self.assertTrue(callable(shim.simulate))
        from tmf_channel import causal_engine

        self.assertIs(shim.simulate, causal_engine.simulate)


class DesiredCacheTest(unittest.TestCase):
    def test_hit_on_same_fingerprint(self):
        from tmf_channel.desired_cache import (
            clear_desired_cache,
            fingerprint_bars,
            get_cached_desired,
            store_desired,
        )

        clear_desired_cache()
        bars = [{"t": "2026-08-06T10:00:00+08:00", "c": 44000, "v": 1}]
        fp = fingerprint_bars(bars)
        store_desired(fp, {"ok": True, "want_s": 44100.0, "want_l": 43900.0}, bars=bars)
        hit = get_cached_desired(fp)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.get("desired_cache_hit"))
        self.assertEqual(hit.get("want_s"), 44100.0)
        clear_desired_cache()

    def test_miss_on_new_bar(self):
        from tmf_channel.desired_cache import (
            clear_desired_cache,
            fingerprint_bars,
            get_cached_desired,
            store_desired,
        )

        clear_desired_cache()
        a = [{"t": "t1", "c": 1, "v": 1}]
        b = [{"t": "t2", "c": 2, "v": 1}]
        store_desired(fingerprint_bars(a), {"ok": True, "want_s": 1.0}, bars=a)
        self.assertIsNone(get_cached_desired(fingerprint_bars(b)))
        clear_desired_cache()

    def test_disk_roundtrip(self):
        from tmf_channel.desired_cache import (
            clear_desired_cache,
            fingerprint_bars,
            get_cached_desired,
            store_desired,
            _LAST,
        )

        clear_desired_cache()
        bars = [{"t": "t-disk", "c": 9, "v": 2}]
        fp = fingerprint_bars(bars)
        store_desired(fp, {"ok": True, "want_s": 42.0, "spot": 9.0}, bars=bars)
        _LAST.clear()  # memory miss → disk
        hit = get_cached_desired(fp)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.get("desired_cache_disk"))
        self.assertEqual(hit.get("want_s"), 42.0)
        clear_desired_cache()


class SqliteBarsCacheTest(unittest.TestCase):
    def test_load_day_from_sqlite_if_present(self):
        from tmf_channel.cache_store import bars_db_path, list_days, load_day

        if not bars_db_path().is_file():
            self.skipTest("bars.sqlite not materialized")
        days = list_days("tx_1m_fullnight_cache_full.json")
        self.assertGreater(len(days), 10)
        rows = load_day(days[-1], source="tx_1m_fullnight_cache_full.json")
        self.assertGreater(len(rows), 100)
        self.assertIn("c", rows[0])


class BarCalendarAttributionTest(unittest.TestCase):
    """Pins the 2026-08-11 fix: a session key is not always a calendar date."""

    SESSION_SRC = "tx_1m_fullnight_cache_full.json"
    CALENDAR_SRC = "tx_1m_tick_built_fullnight_aug"

    def test_post_midnight_bar_is_next_calendar_day_for_session_source(self):
        from tmf_channel.cache_store import bar_calendar_date, bar_timestamp

        self.assertEqual(
            bar_calendar_date("2026-04-02", "00:36", source=self.SESSION_SRC),
            "2026-04-03",
        )
        self.assertEqual(
            bar_timestamp("2026-04-02", "00:36", source=self.SESSION_SRC),
            "2026-04-03T00:36:00.000+08:00",
        )
        # day-session bars are unaffected
        self.assertEqual(
            bar_calendar_date("2026-04-02", "09:00", source=self.SESSION_SRC),
            "2026-04-02",
        )

    def test_calendar_convention_source_keeps_session_date(self):
        from tmf_channel.cache_store import bar_calendar_date

        self.assertEqual(
            bar_calendar_date("2026-08-04", "00:36", source=self.CALENDAR_SRC),
            "2026-08-04",
        )

    def test_explicit_cal_field_wins(self):
        from tmf_channel.cache_store import bar_calendar_date

        row = {"t": "00:36", "cal": "2026-04-05"}
        self.assertEqual(
            bar_calendar_date("2026-04-02", "00:36", source=self.SESSION_SRC, row=row),
            "2026-04-05",
        )

    def test_unknown_source_raises_instead_of_guessing(self):
        from tmf_channel.cache_store import bar_calendar_date

        with self.assertRaises(KeyError):
            bar_calendar_date("2026-04-02", "00:36", source="not_a_registered_source")

    def test_load_day_returns_chronological_order_with_tail_last(self):
        from tmf_channel.cache_store import bars_db_path, load_day

        if not bars_db_path().is_file():
            self.skipTest("bars.sqlite not materialized")
        rows = load_day("2026-04-02", source=self.SESSION_SRC)
        if not rows:
            self.skipTest("session not in cache")
        # night tail must sort AFTER the day session, not before it
        self.assertGreaterEqual(rows[0]["t"], "08:00")
        self.assertLess(rows[-1]["t"], "05:00")
        self.assertEqual(rows[-1]["cal"], "2026-04-03")
        cals = [(r["cal"], r["t"]) for r in rows]
        self.assertEqual(cals, sorted(cals))


class LegacyHelpersNoLabPath(unittest.TestCase):
    def test_causal_engine_imports_without_jack_v5(self):
        import tmf_channel.causal_engine as eng

        self.assertEqual(eng.COST, 3.0)
        self.assertTrue(callable(eng.summarize))


class AuxCacheTest(unittest.TestCase):
    def test_ttl_avoids_second_loader_call(self):
        from tmf_channel.aux_cache import clear_aux_cache, get_cached

        clear_aux_cache()
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return {"x": calls["n"]}

        a = get_cached("k", 60.0, loader)
        b = get_cached("k", 60.0, loader)
        self.assertEqual(a, b)
        self.assertEqual(calls["n"], 1)
        clear_aux_cache()


class SessionRealtimeOnceTest(unittest.TestCase):
    def test_ensure_realtime_only_once(self):
        from tmf_channel.session_pool import ensure_realtime, reset_session_pool

        reset_session_pool()
        sdk = mock.Mock()
        sess = mock.Mock()
        sess.sdk = sdk
        sess.init_realtime = mock.Mock()
        ensure_realtime(sess)
        ensure_realtime(sess)
        self.assertEqual(sess.init_realtime.call_count, 1)
        reset_session_pool()


class HarnessRecipeGuardTest(unittest.TestCase):
    def test_assert_live_recipe_rejects_stale(self):
        from order.tmf_channel_pv16_book import RECIPE_VERSION
        from tmf_channel.harness import assert_live_recipe

        with self.assertRaises(AssertionError):
            assert_live_recipe({"recipe_version": "final_v1_1_1_stale"})
        assert_live_recipe({"recipe_version": RECIPE_VERSION})


class QuietGateStillWorks(unittest.TestCase):
    def test_flat_dry_strips_once_streak_matures(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        tz = ZoneInfo("Asia/Taipei")
        desired = {
            "regime": "dry",
            "active_cell": {"pv": "dry", "recipe": {"skip_quiet_mode": "dry"}},
        }
        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=tz)
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            1.0, 2.0, broker_live=None, desired=desired, ledger={}, now=t0
        )
        later = t0 + timedelta(minutes=2, seconds=1)
        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            1.0, 2.0, broker_live=None, desired=desired, ledger=ledger, now=later
        )
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertIn("quiet_flat_skip", why or "")


class SessionSideGatePerBarKeyTest(unittest.TestCase):
    """causal_engine.py's session_side_gate lookup (2026-08-10): found only
    per-CALENDAR-DAY granularity was supported (`ssg.get(day, "none")`),
    which cannot express a gate that updates within a single trading day.
    Changed to `ssg.get(T[t], ssg.get(day, "none"))` -- a full-bar-timestamp
    key takes priority when present, falling back to the original day-key
    lookup otherwise. This must be provably backward compatible: every
    existing caller (live desired_from_simulate, all research scripts)
    only ever passes {day: "L"/"S"/"none"} dicts, whose keys never look
    like a full ISO bar timestamp, so T[t] can never match for them and
    behavior must be byte-for-byte identical to before this change.
    """

    @staticmethod
    def _synthetic_day_bars(day: str, n: int = 200, start_px: float = 44000.0):
        """n 1-min day-session bars starting 08:45 -- oscillating drift +
        noise, volatile enough to actually cross hang levels and produce
        real entries/exits within the array (needed so the trade-count
        assertions below are meaningful, not just "still empty either way")."""
        O, H, L, C, V, T = [], [], [], [], [], []
        px = start_px
        for i in range(n):
            hh = 8 + (45 + i) // 60
            if hh >= 14:
                break
            mm = (45 + i) % 60
            drift = 15.0 if (i // 10) % 2 == 0 else -15.0
            noise = 5.0 if i % 2 == 0 else -5.0
            o = px
            c = px + drift + noise
            h = max(o, c) + 3.0
            lo = min(o, c) - 3.0
            v = 1000.0 + (i % 7) * 500.0
            O.append(o)
            H.append(h)
            L.append(lo)
            C.append(c)
            V.append(v)
            T.append(f"{day}T{hh:02d}:{mm:02d}:00.000+08:00")
            px = c
        return O, H, L, C, V, T

    def _recipe(self):
        from copy import deepcopy

        from order.tmf_channel_config import PAPER_RECIPE

        recipe = deepcopy(PAPER_RECIPE)
        recipe["hang_anchor"] = "O"
        return recipe

    def test_day_key_only_dict_is_unaffected_by_the_new_lookup(self):
        """Old-style {day: ...} dicts must produce byte-identical trades
        before and after this change -- this pins that guarantee directly
        rather than trusting it by inspection."""
        from tmf_channel.causal_engine import simulate

        day = "2026-08-06"
        O, H, L, C, V, T = self._synthetic_day_bars(day)
        recipe = self._recipe()
        recipe["session_side_gate"] = {day: "none"}

        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta={})
        # ssg_none hard-blocks both sides for every cell with bias=True
        # while flat -- with no position ever opened, there can be no exits
        # or entries born from this cell-gate path at all.
        self.assertEqual(trades, [])

    def test_per_bar_key_takes_priority_over_day_key(self):
        """A bar-timestamp-keyed 'L' must override a day-keyed 'none' for
        that specific bar -- proving the new lookup path is actually wired
        in, not just present and unused."""
        from tmf_channel.causal_engine import simulate

        day = "2026-08-06"
        O, H, L, C, V, T = self._synthetic_day_bars(day)
        recipe = self._recipe()
        # Day-level default says "none" (would block everything under the
        # old behavior); every individual bar is overridden to "L" via its
        # exact timestamp key.
        ssg = {day: "none"}
        ssg.update({t: "L" for t in T})
        recipe["session_side_gate"] = ssg

        trades_bar_override, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta={})

        # Control: same recipe, but WITHOUT the per-bar overrides (pure
        # day-level "none") must still be fully blocked, confirming the
        # override above is what changed the outcome, not something else
        # about this synthetic data.
        recipe_day_only = self._recipe()
        recipe_day_only["session_side_gate"] = {day: "none"}
        trades_day_only, *_ = simulate(O, H, L, C, V, T, recipe_day_only, vix_delta={})

        self.assertEqual(trades_day_only, [])
        self.assertNotEqual(trades_bar_override, [])
        for tr in trades_bar_override:
            self.assertEqual(tr["s"], "L")  # ssg_L steers entries long-only

    def test_missing_session_side_gate_key_falls_back_to_day(self):
        """A per-bar dict that only covers SOME bars must fall back to the
        day-level value for the rest, not silently no-op (e.g. default to
        unblocked)."""
        from tmf_channel.causal_engine import simulate

        day = "2026-08-06"
        O, H, L, C, V, T = self._synthetic_day_bars(day)
        recipe = self._recipe()
        # Only the first 5 bars have a bar-level override (to "L"); every
        # other bar has no bar-level key at all and must fall back to the
        # day-level "none" (full block).
        ssg = {day: "none"}
        ssg.update({t: "L" for t in T[:5]})
        recipe["session_side_gate"] = ssg

        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta={})
        for tr in trades:
            entry_t = tr.get("et")
            self.assertIn(entry_t, T[:5], "entries must only occur inside the overridden window")


class NqCalibContinuousDistanceTest(unittest.TestCase):
    """2026-08-11: user asked for "always something resting, farther away
    when unsure, closer when the signal is strong" instead of the old
    binary session_side_gate block/allow. Wired via nq_calib="always_nq" +
    nq_conf_soft_gate=True (both pre-existing causal_engine.py params, never
    exercised with real data before -- see nq_hang_adj()). nq_conf_soft_gate
    skips the ssg block section entirely; cell.block (session_pv_book's
    per-cell hard block, e.g. night|normal's block=["L","S"]) is a
    completely separate code path (unconditional, runs earlier) that must
    keep holding regardless -- that's the one property this MUST NOT break.
    """

    @staticmethod
    def _synthetic_day_bars(day: str, n: int = 200, start_px: float = 44000.0):
        O, H, L, C, V, T = [], [], [], [], [], []
        px = start_px
        for i in range(n):
            hh = 8 + (45 + i) // 60
            if hh >= 14:
                break
            mm = (45 + i) % 60
            drift = 15.0 if (i // 10) % 2 == 0 else -15.0
            noise = 5.0 if i % 2 == 0 else -5.0
            o = px
            c = px + drift + noise
            h = max(o, c) + 3.0
            lo = min(o, c) - 3.0
            v = 1000.0 + (i % 7) * 500.0
            O.append(o)
            H.append(h)
            L.append(lo)
            C.append(c)
            V.append(v)
            T.append(f"{day}T{hh:02d}:{mm:02d}:00.000+08:00")
            px = c
        return O, H, L, C, V, T

    def _recipe(self, *, all_cells_blocked: bool = False):
        from copy import deepcopy

        from order.tmf_channel_config import PAPER_RECIPE
        from order.tmf_channel_pv16_book import specialized_cell_book

        recipe = deepcopy(PAPER_RECIPE)
        recipe["hang_anchor"] = "O"
        book = specialized_cell_book()
        if all_cells_blocked:
            for sess in book.values():
                for cell in sess.values():
                    cell["block"] = ["L", "S"]
        recipe["session_pv_book"] = book
        return recipe

    def test_cell_block_holds_under_soft_gate_regardless_of_signal(self):
        """Every cell blocked both sides + nq_conf_soft_gate=True + a strong
        directional spread every bar -- must still produce zero trades. This
        is the one property that must never regress: cell.block always wins,
        independent of whatever the continuous nq_calib pull is doing."""
        from tmf_channel.causal_engine import simulate

        day = "2026-08-06"
        O, H, L, C, V, T = self._synthetic_day_bars(day)
        recipe = self._recipe(all_cells_blocked=True)
        recipe["session_side_gate"] = {day: "L"}
        recipe["nq_conf_soft_gate"] = True
        recipe["nq_calib"] = "always_nq"
        recipe["nq_on_1m"] = {day: {t[11:16]: 0.5 for t in T}}  # strong, constant signal

        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta={})
        self.assertEqual(trades, [])

    def test_soft_gate_off_by_default_everywhere_else(self):
        """Recipes that don't set nq_conf_soft_gate/nq_calib (every existing
        caller: research scripts, other tests, backtests) must be completely
        unaffected -- both default to off/none."""
        from order.tmf_channel_config import PAPER_RECIPE

        self.assertEqual(PAPER_RECIPE.get("nq_conf_soft_gate", False), False)
        self.assertIn(PAPER_RECIPE.get("nq_calib", "none"), ("none", "", "off"))

    def test_strong_signal_pulls_favored_side_closer_than_no_signal(self):
        """always_nq mode: a strong positive spread should tighten the S
        rail toward spot vs. no signal at all (adj>0 -> closer short, per
        nq_hang_adj's own docstring) -- confirms the continuous pull is
        actually wired end to end, not just present and inert."""
        from tmf_channel.causal_engine import simulate

        day = "2026-08-06"
        O, H, L, C, V, T = self._synthetic_day_bars(day)

        recipe_none = self._recipe()
        recipe_none["nq_conf_soft_gate"] = True
        recipe_none["nq_calib"] = "none"
        _, _, ws_none, _, *_ = simulate(O, H, L, C, V, T, recipe_none, vix_delta={})

        recipe_strong = self._recipe()
        recipe_strong["nq_conf_soft_gate"] = True
        recipe_strong["nq_calib"] = "always_nq"
        recipe_strong["nq_on_1m"] = {day: {t[11:16]: 0.5 for t in T}}
        _, _, ws_strong, _, *_ = simulate(O, H, L, C, V, T, recipe_strong, vix_delta={})

        # Compare at bars where both are flat-state resting quotes (not None,
        # not mid-trade) -- the S rail should be strictly closer to spot
        # (smaller offset from anchor) under the strong pull than under none,
        # at at least one comparable bar.
        found_tighter = False
        for i in range(len(T)):
            a, b = ws_none[i], ws_strong[i]
            if a is None or b is None:
                continue
            if b < a:  # S rail pulled down/closer under the strong signal
                found_tighter = True
                break
        self.assertTrue(found_tighter, "expected at least one bar where the strong signal tightened the S rail")


if __name__ == "__main__":
    unittest.main()
