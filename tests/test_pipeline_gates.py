"""pipeline_gates：daily_sync registry + RUN_* gate。"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace

from pipeline_gates import (
    daily_sync_skip_reason,
    registry_env_mismatches,
    should_run_daily_sync_step,
)
from strategy_registry import load_strategy_registry


def _registry_with_disabled(*strategy_ids: str):
    reg = load_strategy_registry()
    disabled = set(strategy_ids)
    strategies = tuple(
        replace(s, enabled=False) if s.strategy_id in disabled else s
        for s in reg.strategies
    )
    return replace(reg, strategies=strategies)


class PipelineGatesTests(unittest.TestCase):
    def test_rrg_mono_skip_when_registry_disabled(self) -> None:
        env = {**os.environ, "RUN_RRG_MONO_DAILY": "1"}
        reg = _registry_with_disabled("rrg-mono-hold7")
        reason = daily_sync_skip_reason("rrg_mono_daily", env, registry=reg)
        self.assertEqual(reason, "registry enabled=false")

    def test_rrg_mono_runs_when_registry_and_run_env_on(self) -> None:
        env = {**os.environ, "RUN_RRG_MONO_DAILY": "1"}
        reason = daily_sync_skip_reason("rrg_mono_daily", env)
        self.assertIsNone(reason)

    def test_rrg_mono_skip_when_run_env_off(self) -> None:
        env = {**os.environ, "RUN_RRG_MONO_DAILY": "0"}
        reason = daily_sync_skip_reason("rrg_mono_daily", env)
        self.assertEqual(reason, "RUN_RRG_MONO_DAILY=0")

    def test_rrg_universe_any_strategy_gate(self) -> None:
        reg = _registry_with_disabled("rrg-mono-hold7", "rrg-mono-swap-accel")
        env = {**os.environ, "RUN_RRG_UNIVERSE_CLOSE": "1"}
        reason = daily_sync_skip_reason("rrg_universe_close", env, registry=reg)
        self.assertIn("registry enabled=false", reason or "")

    def test_copytrade_l1h9_respects_registry(self) -> None:
        reg = _registry_with_disabled("00981a-l1h9")
        self.assertFalse(
            should_run_daily_sync_step("copytrade_l1h9_daily", registry=reg)
        )

    def test_vcp_close_run_env_off(self) -> None:
        env = {**os.environ, "RUN_VCP_FUNNEL_CLOSE": "0", "RUN_VCP_FUNNEL_SPECS": "0"}
        reason = daily_sync_skip_reason("vcp_funnel_close", env)
        self.assertEqual(reason, "RUN_VCP_FUNNEL_CLOSE=0")

    def test_vcp_close_not_blocked_by_funnel_specs_env(self) -> None:
        env = {
            **os.environ,
            "RUN_VCP_FUNNEL_CLOSE": "1",
            "RUN_VCP_FUNNEL_SPECS": "0",
        }
        reason = daily_sync_skip_reason("vcp_funnel_close", env)
        self.assertIsNone(reason)

    def test_registry_env_mismatch_detects_run_on_registry_off(self) -> None:
        reg = _registry_with_disabled("rrg-mono-hold7")
        env = {**os.environ, "RUN_RRG_MONO_DAILY": "1"}
        mismatches = registry_env_mismatches(env, registry=reg)
        self.assertTrue(any("rrg_mono_daily" in m for m in mismatches))


if __name__ == "__main__":
    unittest.main()
