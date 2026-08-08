"""dayflip-short KeepAlive worker + session pool tests（全 mock，不連 Fubon）.

2026-08-08：跟 tmf_channel worker_loop 同一套手法，涵蓋：
- worker_loop --once 路徑（窗內跑一輪即返回、窗外直接退出、exit code 語意）
- 窗內／窗外 sleep 選擇（_sleep_sec 預設 20/60 與 env 覆寫）
- reconcile 拋例外 → reset_session_pool() 且 worker 續跑不死
- SIGTERM／SIGINT handler 註冊與 _STOP 旗標
- session_pool：max_age 過期 refresh、force_new、pool_stats
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import order.dayflip_short_session_pool as session_pool
import order.dayflip_short_worker_loop as worker_loop
from order.dayflip_short_session_pool import get_fubon_session, pool_stats, reset_session_pool

_WL = "order.dayflip_short_worker_loop"


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
            "window": mock.patch(f"{_WL}.in_dayflip_short_trade_window", return_value=True),
            # 2026-08-08：_maybe_alert 真的會 subprocess.run 寄信 script（見
            # notify_job_result.py）——測試一律 mock 掉，避免 CI/本機跑測試時真的
            # 觸發 email 或寫進真實的每日 flag file。alert 觸發邏輯有專屬測試類別
            # AlertWiringTest 單獨驗證。
            "alert": mock.patch(f"{_WL}._maybe_alert"),
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
        self.m["reconcile"].return_value = {"action": "noop_terminal"}
        rc = self._run(once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.m["reconcile"].call_count, 1)
        self.m["reconcile"].assert_called_once_with(use_session_pool=True)
        self.m["get_sess"].assert_called_once_with()
        self.m["reset"].assert_not_called()
        summary = self._last_summary()
        self.assertEqual(summary["action"], "noop_terminal")
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

    def test_once_real_business_outcomes_still_exit_zero(self):
        # entry_failed/entered are legitimate structured reconcile_once results,
        # not worker crashes — the worker's own exit code only reflects whether
        # the tick itself ran, not whether the trade attempt succeeded.
        for action in ("entered", "entry_failed", "force_closed", "waiting_cover"):
            with self.subTest(action=action):
                worker_loop._STOP = False
                self.m["reconcile"].return_value = {"action": action}
                self.assertEqual(self._run(once=True), 0)


class SleepSelectionTest(_WorkerTestBase):
    def test_sleep_sec_defaults_20_in_window_60_idle(self):
        env = dict(os.environ)
        env.pop("ORDER_DAYFLIP_SHORT_WORKER_INTERVAL", None)
        env.pop("ORDER_DAYFLIP_SHORT_WORKER_IDLE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(worker_loop._sleep_sec(in_window=True), 20.0)
            self.assertEqual(worker_loop._sleep_sec(in_window=False), 60.0)

    def test_sleep_sec_env_override(self):
        with mock.patch.dict(
            os.environ,
            {
                "ORDER_DAYFLIP_SHORT_WORKER_INTERVAL": "5",
                "ORDER_DAYFLIP_SHORT_WORKER_IDLE": "7.5",
            },
        ):
            self.assertEqual(worker_loop._sleep_sec(in_window=True), 5.0)
            self.assertEqual(worker_loop._sleep_sec(in_window=False), 7.5)

    def test_outside_window_loop_sleeps_idle_interval(self):
        self.m["window"].return_value = False

        def stop_after_sleep(sec):
            worker_loop._STOP = True

        with mock.patch.dict(os.environ, {"ORDER_DAYFLIP_SHORT_WORKER_IDLE": "7.5"}):
            with mock.patch(f"{_WL}.time.sleep", side_effect=stop_after_sleep) as fs:
                rc = self._run()
        self.assertEqual(rc, 0)
        fs.assert_called_once_with(7.5)
        self.m["reconcile"].assert_not_called()

    def test_in_window_loop_sleep_bounded_by_interval(self):
        self.m["reconcile"].return_value = {"action": "noop_terminal"}
        sleep_args = []

        def record_and_stop(sec):
            sleep_args.append(sec)
            worker_loop._STOP = True

        with mock.patch.dict(os.environ, {"ORDER_DAYFLIP_SHORT_WORKER_INTERVAL": "0.05"}):
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
        self.assertEqual(summary["action"], "worker_exception")
        self.assertIn("sdk exploded", summary["error"])
        self.assertIn("session_pool", summary)

    def test_loop_survives_exception_and_keeps_ticking(self):
        calls = {"n": 0}

        def flaky_reconcile(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            worker_loop._STOP = True
            return {"action": "noop_terminal"}

        self.m["reconcile"].side_effect = flaky_reconcile
        # interval=0 → tick 後 deadline 立即到期，loop 不需真睡
        with mock.patch.dict(os.environ, {"ORDER_DAYFLIP_SHORT_WORKER_INTERVAL": "0"}):
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
        return mock.Mock(name="fubon-session")

    def test_first_call_logs_in_then_cache_hit(self):
        s1 = self._mock_session()
        with mock.patch(
            "order.dayflip_short_session_pool.connect_fubon", return_value=s1
        ) as connect:
            a = get_fubon_session()
            b = get_fubon_session()
        self.assertIs(a, s1)
        self.assertIs(b, s1)
        connect.assert_called_once_with()
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 1)

    def test_max_age_expiry_triggers_relogin(self):
        s1, s2 = self._mock_session(), self._mock_session()
        with mock.patch(
            "order.dayflip_short_session_pool.connect_fubon", side_effect=[s1, s2]
        ) as connect:
            a = get_fubon_session()
            with session_pool._LOCK:
                session_pool._STATE["born_mono"] = time.monotonic() - 3600.0
            b = get_fubon_session()
        self.assertIs(a, s1)
        self.assertIs(b, s2)
        self.assertIsNot(a, b)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 2)

    def test_within_max_age_no_relogin(self):
        s1 = self._mock_session()
        with mock.patch(
            "order.dayflip_short_session_pool.connect_fubon", return_value=s1
        ) as connect:
            get_fubon_session(max_age_sec=3500.0)
            get_fubon_session(max_age_sec=3500.0)
        connect.assert_called_once()
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 1)

    def test_force_new_relogs_even_when_fresh(self):
        s1, s2 = self._mock_session(), self._mock_session()
        with mock.patch(
            "order.dayflip_short_session_pool.connect_fubon", side_effect=[s1, s2]
        ) as connect:
            a = get_fubon_session()
            b = get_fubon_session(force_new=True)
        self.assertIsNot(a, b)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(pool_stats()["login_count"], self.base_logins + 2)

    def test_pool_stats_shape_and_reset(self):
        s1 = self._mock_session()
        with mock.patch("order.dayflip_short_session_pool.connect_fubon", return_value=s1):
            get_fubon_session()
        st = pool_stats()
        self.assertTrue(st["has_session"])
        self.assertIsNotNone(st["age_sec"])
        self.assertGreaterEqual(st["age_sec"], 0.0)
        self.assertEqual(st["login_count"], self.base_logins + 1)
        reset_session_pool()
        st2 = pool_stats()
        self.assertFalse(st2["has_session"])
        self.assertIsNone(st2["age_sec"])
        # login_count 是累計器，reset 不歸零
        self.assertEqual(st2["login_count"], self.base_logins + 1)


class AlertWiringTest(unittest.TestCase):
    """2026-08-08：舊 StartInterval launcher 靠 shell grep 特定 action 字串觸發寄信；
    KeepAlive worker 常駐後這條責任搬進 _maybe_alert()，這裡直接測它的觸發條件、
    daily flag file dedup、RUN_DAYFLIP_SHORT_EMAIL 開關，不透過整個 run_forever。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs_dir = mock.patch(f"{_WL}.stock_db.LOGS_DIR", Path(self.tmp.name))
        self.logs_dir.start()
        self.addCleanup(self.logs_dir.stop)

    def test_benign_action_never_calls_subprocess(self) -> None:
        with mock.patch(f"{_WL}.subprocess.run") as fake_run:
            worker_loop._maybe_alert({"action": "waiting_cover"}, ok=True)
        fake_run.assert_not_called()

    def test_entered_action_triggers_subprocess_once(self) -> None:
        with mock.patch(f"{_WL}.subprocess.run") as fake_run:
            worker_loop._maybe_alert({"action": "entered"}, ok=True)
        fake_run.assert_called_once()
        args = fake_run.call_args[0][0]
        self.assertIn("--subject-prefix", args)
        self.assertIn("0", args)  # success → exit-code 0

    def test_entry_failed_maps_to_nonzero_exit_code(self) -> None:
        with mock.patch(f"{_WL}.subprocess.run") as fake_run:
            worker_loop._maybe_alert({"action": "entry_failed"}, ok=True)
        args = fake_run.call_args[0][0]
        idx = args.index("--exit-code")
        self.assertEqual(args[idx + 1], "1")

    def test_worker_exception_always_alerts_even_off_alert_action_list(self) -> None:
        with mock.patch(f"{_WL}.subprocess.run") as fake_run:
            worker_loop._maybe_alert({"action": "worker_exception"}, ok=False)
        fake_run.assert_called_once()
        args = fake_run.call_args[0][0]
        idx = args.index("--exit-code")
        self.assertEqual(args[idx + 1], "1")

    def test_same_day_second_qualifying_action_deduped_by_flag_file(self) -> None:
        with mock.patch(f"{_WL}.subprocess.run") as fake_run:
            worker_loop._maybe_alert({"action": "entered"}, ok=True)
            worker_loop._maybe_alert({"action": "force_closed"}, ok=True)
        self.assertEqual(fake_run.call_count, 1)

    def test_run_dayflip_short_email_0_suppresses_alert(self) -> None:
        with (
            mock.patch.dict(os.environ, {"RUN_DAYFLIP_SHORT_EMAIL": "0"}),
            mock.patch(f"{_WL}.subprocess.run") as fake_run,
        ):
            worker_loop._maybe_alert({"action": "entered"}, ok=True)
        fake_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
