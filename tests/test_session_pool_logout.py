"""Session pool 換手時必須 logout 舊 session（釋放券商連線額度）。

2026-09-03：三個 pool（tmf/dayflip/momentum）換手只丟 GC，換手瞬間帳號同時
持有兩條連線；多 worker 撞在一起打滿 ACCOUNT CONNECTION LIMIT（2026-09-01
collector 被迫退讓 600s×18 次）。本測試釘住「新 session 就位後舊 session 被
logout、logout 失敗不外洩」兩件事。
"""
from __future__ import annotations

import unittest
from unittest import mock


class FakeSDK:
    def __init__(self):
        self.logged_out = False

    def logout(self):
        self.logged_out = True


class FakeSession:
    def __init__(self):
        self.sdk = FakeSDK()


class SafeLogoutTest(unittest.TestCase):
    def test_logs_out(self):
        from order.fubon_session import safe_logout
        s = FakeSession()
        self.assertTrue(safe_logout(s))
        self.assertTrue(s.sdk.logged_out)

    def test_none_is_noop(self):
        from order.fubon_session import safe_logout
        self.assertFalse(safe_logout(None))

    def test_swallow_errors(self):
        from order.fubon_session import safe_logout
        s = FakeSession()
        s.sdk.logout = mock.Mock(side_effect=RuntimeError("broker down"))
        self.assertFalse(safe_logout(s))          # 不 raise


class PoolHandoverTest(unittest.TestCase):
    def _run(self, modname):
        import importlib
        mod = importlib.import_module(modname)
        mod.reset_session_pool()
        first, second = FakeSession(), FakeSession()
        with mock.patch.object(mod, "connect_fubon", side_effect=[first, second]):
            a = mod.get_fubon_session(max_age_sec=3500.0)
            self.assertIs(a, first)
            self.assertFalse(first.sdk.logged_out)
            b = mod.get_fubon_session(force_new=True)   # 換手
            self.assertIs(b, second)
            self.assertTrue(first.sdk.logged_out, f"{modname}: 舊 session 未 logout")
            self.assertFalse(second.sdk.logged_out)
        mod.reset_session_pool()

    def test_dayflip_pool(self):
        self._run("order.dayflip_short_session_pool")

    def test_momentum_pool(self):
        self._run("order.momentum_rotation_session_pool")

    def test_tmf_pool(self):
        import importlib
        mod = importlib.import_module("tmf_channel.session_pool")
        mod.reset_session_pool()
        first, second = FakeSession(), FakeSession()
        with mock.patch.object(mod, "connect_fubon", side_effect=[first, second]):
            a = mod.get_fubon_session(realtime=False, max_age_sec=3500.0)
            self.assertIs(a, first)
            b = mod.get_fubon_session(realtime=False, force_new=True)
            self.assertIs(b, second)
            self.assertTrue(first.sdk.logged_out, "tmf: 舊 session 未 logout")
            self.assertFalse(second.sdk.logged_out)
        mod.reset_session_pool()


if __name__ == "__main__":
    unittest.main()
