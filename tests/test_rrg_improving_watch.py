"""Tests for RRG Improving watch scan."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pandas as pd

from buy_observation import BuyObservationPoolSpec, evaluate_pool_observation, load_buy_observation_config
from research.backtest.rrg_improving_lifecycle_backtest import ImprovingDayRow, _watch_setup
from research.backtest.rrg_improving_s3rv_executable import (
    compute_pre_release_conviction_score,
    conviction_from_improving_row,
    is_s3_executable,
)
from rrg_improving_watch import (
    classify_s2_archetype,
    collect_s2_archetype_hits,
    filter_improving_track,
    improving_hits_to_scan_rows,
    is_rule_b,
)
from rrg_mono_daily_brief import ScanRow


def _sample_row(**overrides: object) -> ImprovingDayRow:
    base = dict(
        date="2026-06-26",
        stock_id="2345",
        stock_name="智邦",
        sig_ret=-0.3,
        excess=-0.5,
        end_q="improving",
        improve_streak=7,
        rv=98.2,
        rs_momentum=101.0,
        disp=0.4,
        disp_d=-0.15,
        session_gap_days=1,
        base_setup=True,
        s2_setup_core=True,
        s3_burst=False,
        s2_rv_98_99=True,
        s3_rv_98_99=False,
        watch_setup=True,
        disp_contract=True,
        quadrants=("improving",) * 4,
        bench_today_ret=0.5,
        signal_close=100.0,
    )
    base.update(overrides)
    return ImprovingDayRow(**base)  # type: ignore[arg-type]


class TestImprovingWatchRules(unittest.TestCase):
    def test_watch_setup_rule(self) -> None:
        row = _sample_row(s2_setup_core=False, watch_setup=True)
        self.assertTrue(_watch_setup(row.base_setup, row.improve_streak, row.sig_ret))
        hits = filter_improving_track([row], "watch_setup")
        self.assertEqual(len(hits), 1)

    def test_rule_b(self) -> None:
        row = _sample_row()
        self.assertTrue(is_rule_b(row))
        hits = filter_improving_track([row], "rule_b")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].archetype, "A_coiled_spring")

    def test_archetype_b_leading(self) -> None:
        row = _sample_row(rv=99.6, sig_ret=-0.2, disp_d=-0.05)
        self.assertEqual(classify_s2_archetype(row), "B_leading_edge")
        hits = filter_improving_track([row], "arch_b")
        self.assertEqual(len(hits), 1)

    def test_archetype_c_spark(self) -> None:
        row = _sample_row(sig_ret=0.75, rv=99.1, disp_d=0.05, disp_contract=False)
        self.assertEqual(classify_s2_archetype(row), "C_pre_chase_spark")
        hits = filter_improving_track([row], "arch_c")
        self.assertEqual(len(hits), 1)

    def test_collect_s2_dedupes_rule_b_from_arch_a(self) -> None:
        row = _sample_row()
        buckets = collect_s2_archetype_hits([row])
        self.assertEqual(len(buckets["rule_b"]), 1)
        self.assertEqual(buckets["arch_a"], [])

    def test_s3_exec_filter(self) -> None:
        burst_row = _sample_row(
            s3_rv_98_99=True,
            s3_burst=True,
            sig_ret=6.0,
            bench_today_ret=1.0,
            sid_s3rv_hist_n=4,
            sid_s3rv_hist_mean=0.5,
            sid_s3rv_hist_ok=True,
        )
        self.assertTrue(
            is_s3_executable(
                s3_rv_98_99=burst_row.s3_rv_98_99,
                bench_today_ret=burst_row.bench_today_ret,
                sid_s3rv_hist_n=burst_row.sid_s3rv_hist_n,
                sid_s3rv_hist_ok=burst_row.sid_s3rv_hist_ok,
            )
        )
        hits = filter_improving_track([burst_row], "s3_exec")
        self.assertEqual(len(hits), 1)
        # burst-day row uses pre-release path → blocked; score may be 0
        pre_row = _sample_row(
            s3_rv_98_99=False,
            s3_burst=False,
            sig_ret=4.0,
            bench_today_ret=1.0,
            disp_contract=True,
            improve_streak=6,
            base_setup=True,
        )
        self.assertGreater(conviction_from_improving_row(pre_row).score, 32.0)

    def test_s3_conviction_tiers(self) -> None:
        weak = compute_pre_release_conviction_score(
            end_q="improving",
            bench_today_ret=-1.0,
            sig_ret=1.0,
            improve_streak=2,
            disp_contract=False,
            disp_d=0.2,
        )
        self.assertLess(weak.score, 32.0)

        strong = compute_pre_release_conviction_score(
            end_q="improving",
            bench_today_ret=2.5,
            sig_ret=4.5,
            improve_streak=6,
            disp_contract=True,
            disp_d=-0.2,
            base_setup=True,
            sid_burst_hist_ok=True,
        )
        self.assertGreaterEqual(strong.score, 52.0)
        self.assertGreater(strong.raw_mul, 1.0)

        self.assertGreater(
            compute_pre_release_conviction_score(
                end_q="improving",
                bench_today_ret=1.0,
                sig_ret=4.0,
                improve_streak=6,
                disp_contract=True,
            ).raw_mul,
            compute_pre_release_conviction_score(
                end_q="improving",
                bench_today_ret=1.0,
                sig_ret=1.0,
                improve_streak=2,
                disp_contract=False,
                disp_d=0.3,
            ).raw_mul,
        )

    def test_buy_observation_config_improving_pools_disabled_phase_c(self) -> None:
        _, specs = load_buy_observation_config()
        ids = {s.id for s in specs}
        for pid in (
            "rrg-improving-watch-setup",
            "rrg-improving-s3-rv",
            "rrg-improving-s3-exec",
            "rrg-improving-rule-b",
            "rrg-improving-arch-a",
            "rrg-improving-arch-b",
            "rrg-improving-arch-c",
        ):
            self.assertNotIn(pid, ids)
        self.assertNotIn("dual-wma-lead-pullback", ids)


class TestObserveOnlyPool(unittest.TestCase):
    def test_evaluate_observe_only_skips_c0(self) -> None:
        spec = BuyObservationPoolSpec(
            id="rrg-improving-rule-b",
            title="test",
            role="research",
            source="rrg_improving_rule_b",
            top_n=3,
            confirm_bars=1,
            notify=False,
            observe_only=True,
        )
        pool = improving_hits_to_scan_rows(
            filter_improving_track([_sample_row()], "rule_b")
        )
        close = pd.DataFrame({"2345": [100.0]}, index=["2026-06-26"])
        obs = evaluate_pool_observation(
            MagicMock(),
            spec,
            pool=pool,
            pool_as_of="2026-06-26",
            session="2026-06-27",
            poll_minute="09:15",
            close=close,
            kbar_cache={},
            confirm_state={},
            c0_cfg=MagicMock(),
        )
        self.assertEqual(obs.skip_note, "observe-only · 無 C0 進場")
        self.assertEqual(len(obs.top_rows), 1)
        self.assertFalse(obs.entry_stock_id)


if __name__ == "__main__":
    unittest.main()
