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
    def test_flat_dry_strips(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        ws, wl, why = apply_quiet_flat_entry_gate(
            1.0,
            2.0,
            broker_live=None,
            desired={
                "regime": "dry",
                "active_cell": {"pv": "dry", "recipe": {"skip_quiet_mode": "dry"}},
            },
        )
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertIn("quiet_flat_skip", why or "")


if __name__ == "__main__":
    unittest.main()
