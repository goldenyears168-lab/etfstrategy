"""Worker loop + session pool tests · tmf_channel 常駐 worker（全 mock，不連 Fubon）。

涵蓋：
- worker_loop --once 路徑（窗內跑一輪即返回、窗外直接退出、exit code 語意）
- 窗內／窗外 sleep 選擇（_sleep_sec 預設 20/60 與 env 覆寫、loop 分支實際吃到的值）
- reconcile 拋例外 → reset_session_pool() 且 worker 續跑不死
- SIGTERM／SIGINT handler 註冊與 _STOP 旗標
- session_pool：max_age 過期 refresh、force_new、pool_stats、ensure_realtime 冪等
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import time
import unittest
from unittest import mock

import tmf_channel.session_pool as session_pool
import tmf_channel.worker_loop as worker_loop
from tmf_channel.session_pool import (
    ensure_realtime,
    get_fubon_session,
    pool_stats,
    reset_session_pool,
)

_WL = "tmf_channel.worker_loop"


class _WorkerTestBase(unittest.TestCase):
    """共用 patch：不讀生產 .env、不真連 Fubon、_STOP 每測重置。"""

    def setUp(self):
        worker_loop._STOP = False
        self._patches = {
            "dotenv": mock.patch(f"{_WL}.load_project_dotenv"),
            "get_sess": mock.patch(f"{_WL}.get_fubon_session"),
            "reconcile": mock.patch(f"{_WL}.reconcile_once"),
            "reset": mock.patch(f"{_WL}.reset_session_pool"),
            "stats": mock.patch(f"{_WL}.pool_stats", return_value={"has_session": True}),
            "hhmm": mock.patch(f"{_WL}.session_hhmm_now", return_value="09:00"),
            "window": mock.patch(f"{_WL}.in_tmf_trade_window", return_value=True),
        }
        self.m = {k: p.start() for k, p in self._patches.items()}
        for p in self._patches.values():
            self.addCleanup(p.stop)
        self.addCleanup(lambda: setattr(worker_loop, "_STOP", False))

    def _run(self, **kwargs) -> int:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = worker_loop.run_forever(**kwargs)
        self.stdout = buf.getvalue()
        return rc

    def _last_summary(self) -> dict:
        lines = [ln for ln in self.stdout.splitlines() if ln.strip().startswith("{")]
        for ln in reversed(lines):
            obj = json.loads(ln)
            if "event" not in obj:
                return obj
        raise AssertionError(f"no summary line in stdout: {self.stdout!r}")


class WorkerOncePathTest(_WorkerTestBase):
    def test_once_in_window_runs_single_tick_then_returns(self):
        self.m["reconcile"].return_value = {"ok": True}
        rc = self._run(once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.m["reconcile"].call_count, 1)
        self.m["reconcile"].assert_called_once_with(force=False, use_session_pool=True)
        self.m["get_sess"].assert_called_once_with(realtime=True)
        self.m["reset"].assert_not_called()
        summary = self._last_summary()
        self.assertTrue(summary["ok"])
        self.assertIn("worker_elapsed_sec", summary)
        self.assertIn("session_pool", summary)

    def test_once_outside_window_exits_without_reconcile(self):
        self.m["window"].return_value = False
        with mock.patch(f"{_WL}.time.sleep") as fake_sleep:
            rc = self._run(once=True)
        self.assertEqual(rc, 0)
        self.m["reconcile"].assert_not_called()
        self.m["get_sess"].assert_not_called()
        fake_sleep.assert_not_called()

    def test_once_failure_reason_maps_to_exit_one(self):
        self.m["reconcile"].return_value = {"ok": False, "reason": "boom"}
        self.assertEqual(self._run(once=True), 1)

    def test_once_benign_reasons_map_to_exit_zero(self):
        for reason in ("outside_session", "ORDER_TMF_CHANNEL_ENABLED=0"):
            with self.subTest(reason=reason):
                worker_loop._STOP = False
                self.m["reconcile"].return_value = {"ok": False, "reason": reason}
                self.assertEqual(self._run(once=True), 0)


class SleepSelectionTest(_WorkerTestBase):
    def test_sleep_sec_defaults_20_in_window_60_idle(self):
        env = dict(os.environ)
        env.pop("ORDER_TMF_CHANNEL_WORKER_INTERVAL", None)
        env.pop("ORDER_TMF_CHANNEL_WORKER_IDLE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(worker_loop._sleep_sec(in_window=True), 20.0)
            self.assertEqual(worker_loop._sleep_sec(in_window=False), 60.0)

    def test_sleep_sec_env_override(self):
        with mock.patch.dict(
            os.environ,
            {
                "ORDER_TMF_CHANNEL_WORKER_INTERVAL": "5",
                "ORDER_TMF_CHANNEL_WORKER_IDLE": "7.5",
            },
        ):
            self.assertEqual(worker_loop._sleep_sec(in_window=True), 5.0)
            self.assertEqual(worker_loop._sleep_sec(in_window=False), 7.5)

    def test_outside_window_loop_sleeps_idle_interval(self):
        self.m["window"].return_value = False

        def stop_after_sleep(sec):
            worker_loop._STOP = True

        with mock.patch.dict(os.environ, {"ORDER_TMF_CHANNEL_WORKER_IDLE": "7.5"}):
            with mock.patch(f"{_WL}.time.sleep", side_effect=stop_after_sleep) as fs:
                rc = self._run()
        self.assertEqual(rc, 0)
        fs.assert_called_once_with(7.5)
        self.m["reconcile"].assert_not_called()

    def test_in_window_loop_sleep_bounded_by_interval(self):
        self.m["reconcile"].return_value = {"ok": True}
        sleep_args = []

        def record_and_stop(sec):
            sleep_args.append(sec)
            worker_loop._STOP = True

        with mock.patch.dict(os.environ, {"ORDER_TMF_CHANNEL_WORKER_INTERVAL": "0.05"}):
            with mock.patch(f"{_WL}.time.sleep", side_effect=record_and_stop):
                rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(sleep_args), 1)
        self.assertGreater(sleep_args[0], 0.0)
        self.assertLessEqual(sleep_args[0], 0.05)


class WorkerExceptionTest(_WorkerTestBase):
    def test_once_exception_resets_pool_and_exits_one(self):
        self.m["reconcile"].side_effect = RuntimeError("sdk exploded")
        rc = self._run(once=True)
        self.assertEqual(rc, 1)
        self.m["reset"].assert_called_once_with()
        summary = self._last_summary()
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["reason"], "worker_exception")
        self.assertIn("sdk exploded", summary["error"])
        self.assertIn("session_pool", summary)

    def test_loop_survives_exception_and_keeps_ticking(self):
        calls = {"n": 0}

        def flaky_reconcile(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            worker_loop._STOP = True
            return {"ok": True}

        self.m["reconcile"].side_effect = flaky_reconcile
        # interval=0 → tick 後 deadline 立即到期，loop 不需真睡
        with mock.patch.dict(os.environ, {"ORDER_TMF_CHANNEL_WORKER_INTERVAL": "0"}):
            rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 2)  # 例外後仍繼續下一輪
        self.m["reset"].assert_called_once_with()


class SignalHandlingTest(_WorkerTestBase):
    def test_run_forever_registers_sigterm_and_sigint(self):
        self.m["window"].return_value = False
        with mock.patch(f"{_WL}.signal.signal") as fake_signal:
            self._run(once=True)
        registered = {call.args[0]: call.args[1] for call in fake_signal.call_args_list}
        self.assertIs(registered.get(signal.SIGTERM), worker_loop._on_signal)
        self.assertIs(registered.get(signal.SIGINT), worker_loop._on_signal)

    def test_on_signal_sets_stop_flag(self):
        self.assertFalse(worker_loop._STOP)
        with contextlib.redirect_stdout(io.StringIO()):
            worker_loop._on_signal(signal.SIGTERM, None)
        self.assertTrue(worker_loop._STOP)

    def test_stop_flag_breaks_idle_loop(self):
        self.m["window"].return_value = False

        def sigterm_during_sleep(sec):
            with contextlib.redirect_stdout(io.StringIO()):
                worker_loop._on_signal(signal.SIGTERM, None)

        with mock.patch(f"{_WL}.time.sleep", side_effect=sigterm_during_sleep):
            rc = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("worker_stop", self.stdout)


class MainEntryTest(unittest.TestCase):
    def test_main_parses_once_flag(self):
        with mock.patch(f"{_WL}.run_forever", return_value=0) as rf:
            self.assertEqual(worker_loop.main(["--once"]), 0)
            rf.assert_called_once_with(once=True)
        with mock.patch(f"{_WL}.run_forever", return_value=0) as rf:
            worker_loop.main([])
            rf.assert_called_once_with(once=False)


class SessionPoolTest(unittest.TestCase):
    """session_pool：全 mock connect_fubon，不真登入。"""

    def setUp(self):
        reset_session_pool()
        self.base_logins = pool_stats()["login_count"]
        self.addCleanup(reset_session_pool)

    def _mock_session(self):
        sess = mock.Mock(name="fubon-session")
        sess.init_realtime = mock.Mock()
        return sess

    def test_first_call_logs_in_then_cache_hit(self):
        s1 = self._mock_session()
        with mock.patch(
            "tmf_channel.session_pool.connect_fubon", return_value=s1
        ) as connect:
            a = get_fubon_session(realtime=True)
            b = get_fubon_session(realtime=True)
        self.assertIs(a, s1)
        self.assertIs(b, s1)
        connect.assert_called_once_with(realtime=False)
        s1.init_realtime.assert_called_once_with()
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 1)

    def test_max_age_expiry_triggers_relogin(self):
        s1, s2 = self._mock_session(), self._mock_session()
        with mock.patch(
            "tmf_channel.session_pool.connect_fubon", side_effect=[s1, s2]
        ) as connect:
            a = get_fubon_session(realtime=True)
            # 把 born_mono 撥老到超過預設 max_age 3500s
            with session_pool._LOCK:
                session_pool._STATE["born_mono"] = time.monotonic() - 3600.0
            b = get_fubon_session(realtime=True)
        self.assertIs(a, s1)
        self.assertIs(b, s2)
        self.assertIsNot(a, b)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 2)

    def test_within_max_age_no_relogin(self):
        s1 = self._mock_session()
        with mock.patch(
            "tmf_channel.session_pool.connect_fubon", return_value=s1
        ) as connect:
            get_fubon_session(realtime=True, max_age_sec=3500.0)
            get_fubon_session(realtime=True, max_age_sec=3500.0)
        connect.assert_called_once()
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 1)

    def test_force_new_relogs_even_when_fresh(self):
        s1, s2 = self._mock_session(), self._mock_session()
        with mock.patch(
            "tmf_channel.session_pool.connect_fubon", side_effect=[s1, s2]
        ) as connect:
            a = get_fubon_session(realtime=True)
            b = get_fubon_session(realtime=True, force_new=True)
        self.assertIsNot(a, b)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 2)

    def test_pool_stats_shape_and_reset(self):
        s1 = self._mock_session()
        with mock.patch("tmf_channel.session_pool.connect_fubon", return_value=s1):
            get_fubon_session(realtime=True)
        st = pool_stats()
        self.assertTrue(st["has_session"])
        self.assertTrue(st["realtime_ok"])
        self.assertIsNotNone(st["age_sec"])
        self.assertGreaterEqual(st["age_sec"], 0.0)
        self.assertEqual(st["login_count"], self.base_logins + 1)
        reset_session_pool()
        st2 = pool_stats()
        self.assertFalse(st2["has_session"])
        self.assertFalse(st2["realtime_ok"])
        self.assertIsNone(st2["age_sec"])
        # login_count 是累計器，reset 不歸零
        self.assertEqual(st2["login_count"], self.base_logins + 1)

    def test_realtime_false_skips_init_then_ensure_realtime_upgrades(self):
        s1 = self._mock_session()
        with mock.patch("tmf_channel.session_pool.connect_fubon", return_value=s1):
            sess = get_fubon_session(realtime=False)
        sess.init_realtime.assert_not_called()
        self.assertFalse(pool_stats()["realtime_ok"])
        ensure_realtime(sess)
        sess.init_realtime.assert_called_once_with()
        self.assertTrue(pool_stats()["realtime_ok"])

    def test_ensure_realtime_idempotent_after_pool_init(self):
        s1 = self._mock_session()
        with mock.patch("tmf_channel.session_pool.connect_fubon", return_value=s1):
            sess = get_fubon_session(realtime=True)
        sess.init_realtime.assert_called_once_with()
        ensure_realtime(sess)  # pool 已 init → 不重複呼叫
        sess.init_realtime.assert_called_once_with()


class WatchdogAndHeartbeatTest(unittest.TestCase):
    """2026-08-17 incident regression: a reconcile that never returns must not
    be able to wedge the worker silently again (see worker_loop module docstring
    for the 3d16h / 100%-CPU / zero-log-lines postmortem)."""

    def test_arm_watchdog_disarms_without_firing(self):
        from tmf_channel import worker_loop as wl

        wl._arm_watchdog(300.0, phase="unit")
        wl._disarm_watchdog()  # must not kill the test process
        self.assertTrue(wl.incident_path().exists())

    def test_watchdog_disabled_when_env_zero(self):
        from tmf_channel import worker_loop as wl

        with mock.patch.dict(os.environ, {wl._WATCHDOG_ENV: "0"}):
            self.assertEqual(wl._watchdog_sec(), 0.0)
        with mock.patch.dict(os.environ, {wl._WATCHDOG_ENV: "not-a-number"}):
            self.assertEqual(wl._watchdog_sec(), wl._WATCHDOG_DEFAULT_SEC)

    def test_heartbeat_roundtrip_and_atomic_replace(self):
        from tmf_channel import worker_loop as wl

        wl.write_heartbeat({"ts": "2026-08-17T21:00:00+08:00", "phase": "reconcile_enter"})
        payload = json.loads(wl.heartbeat_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["phase"], "reconcile_enter")
        self.assertFalse(wl.heartbeat_path().with_suffix(".json.tmp").exists())

    def test_heartbeat_write_failure_never_raises(self):
        from tmf_channel import worker_loop as wl

        with mock.patch.object(wl, "heartbeat_path", side_effect=OSError("disk full")):
            wl.write_heartbeat({"ts": "x"})  # must swallow

    def test_run_forever_arms_and_disarms_around_reconcile(self):
        from tmf_channel import worker_loop as wl

        armed: list[str] = []
        with (
            # Must mock: run_forever() calls load_project_dotenv(), which
            # merges the production .env into os.environ for the whole pytest
            # process and silently flips later tests onto live flags
            # (ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE=1 rewrites the PV16
            # night book, breaking test_tmf_channel_pv16_book downstream).
            mock.patch.object(wl, "load_project_dotenv"),
            mock.patch.object(wl, "in_tmf_trade_window", return_value=True),
            mock.patch.object(wl, "session_hhmm_now", return_value="10:00"),
            mock.patch.object(wl, "get_fubon_session"),
            mock.patch.object(wl, "reconcile_once", return_value={"ok": True, "reason": "reconciled"}),
            mock.patch.object(wl, "pool_stats", return_value={}),
            mock.patch.object(wl, "_arm_watchdog", side_effect=lambda s, phase: armed.append("arm")),
            mock.patch.object(wl, "_disarm_watchdog", side_effect=lambda: armed.append("disarm")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            wl.run_forever(once=True)
        self.assertEqual(armed, ["arm", "disarm"])

    def test_watchdog_disarmed_even_when_reconcile_raises(self):
        from tmf_channel import worker_loop as wl

        armed: list[str] = []
        with (
            # Must mock: run_forever() calls load_project_dotenv(), which
            # merges the production .env into os.environ for the whole pytest
            # process and silently flips later tests onto live flags
            # (ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE=1 rewrites the PV16
            # night book, breaking test_tmf_channel_pv16_book downstream).
            mock.patch.object(wl, "load_project_dotenv"),
            mock.patch.object(wl, "in_tmf_trade_window", return_value=True),
            mock.patch.object(wl, "session_hhmm_now", return_value="10:00"),
            mock.patch.object(wl, "get_fubon_session"),
            mock.patch.object(wl, "reconcile_once", side_effect=RuntimeError("boom")),
            mock.patch.object(wl, "reset_session_pool"),
            mock.patch.object(wl, "pool_stats", return_value={}),
            mock.patch.object(wl, "_arm_watchdog", side_effect=lambda s, phase: armed.append("arm")),
            mock.patch.object(wl, "_disarm_watchdog", side_effect=lambda: armed.append("disarm")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            wl.run_forever(once=True)
        self.assertEqual(armed, ["arm", "disarm"])


if __name__ == "__main__":
    unittest.main()
