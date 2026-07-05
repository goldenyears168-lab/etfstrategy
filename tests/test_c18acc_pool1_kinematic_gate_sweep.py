"""Tests for C18acc POOL1 kinematic gate · pause / gate / intraday confirm helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.c18acc_pool1_kinematic_gate_sweep import (
    build_hybrid_top_gate_lookup,
    build_intraday_extension_lookup,
    build_omega_kinematic_gate_lookup,
    build_rank_bonus_lookup,
    intraday_gate_coverage,
    pool1_kinematic_gate_configs,
    pool1_kinematic_gate_phase2_configs,
)
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _pick_best_challenger,
    apply_gate_by_date,
    filter_scan_rows_by_gate,
    intraday_swap_confirm_ok,
    quad_force_exit_eligible,
    quad_force_exit_paused_by_positive_spread,
    spread_rs_positive_at,
    spread_rs_positive_streak_days,
)
from rrg_mono_daily_brief import ScanRow


def _scan_row(stock_id: str, *, seg_last: float = 1.0) -> ScanRow:
    return ScanRow(
        stock_id=stock_id,
        stock_name=stock_id,
        fresh=True,
        mono=True,
        seg_last=seg_last,
        disp=1.0,
        segs=[1.0, 1.0, 1.0, 1.0],
        quadrants=["leading"] * 4,
        rs_ratio=100.0,
        rs_momentum=100.0,
        daily_pct=1.0,
    )


class TestSpreadRsPositive(unittest.TestCase):
    def test_positive_spread(self) -> None:
        panel = pd.DataFrame(
            {"2330": [0.5, -0.1]},
            index=["2024-06-01", "2024-06-02"],
        )
        self.assertTrue(
            spread_rs_positive_at(panel, as_of="2024-06-01", stock_id="2330")
        )
        self.assertFalse(
            spread_rs_positive_at(panel, as_of="2024-06-02", stock_id="2330")
        )

    def test_missing_panel(self) -> None:
        self.assertFalse(
            spread_rs_positive_at(None, as_of="2024-06-01", stock_id="2330")
        )


class TestQuadForceExitPause(unittest.TestCase):
    def _cfg(self, pause: bool) -> ScoreSwapCConfig:
        return ScoreSwapCConfig(
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_requires_min_hold=False,
            quad_force_exit_pause_spread_rs_positive=pause,
        )

    def test_pause_blocks_when_spread_positive(self) -> None:
        panel = pd.DataFrame({"2330": [0.3]}, index=["2024-06-01"])
        cfg = self._cfg(True)
        self.assertTrue(
            quad_force_exit_paused_by_positive_spread(
                cfg, spread_rs_panel=panel, as_of="2024-06-01", stock_id="2330"
            )
        )
        self.assertTrue(
            quad_force_exit_eligible(
                cfg,
                hold_days=5,
                effective_min_hold=2,
                streak_days=2,
                unrealized_ret=-0.06,
            )
        )

    def test_no_pause_when_flag_off(self) -> None:
        panel = pd.DataFrame({"2330": [0.3]}, index=["2024-06-01"])
        cfg = self._cfg(False)
        self.assertFalse(
            quad_force_exit_paused_by_positive_spread(
                cfg, spread_rs_panel=panel, as_of="2024-06-01", stock_id="2330"
            )
        )

    def test_streak_min_two_days(self) -> None:
        panel = pd.DataFrame(
            {"2330": [0.2, 0.3, -0.1]},
            index=["2024-06-01", "2024-06-02", "2024-06-03"],
        )
        dates = list(panel.index.astype(str))
        cfg = ScoreSwapCConfig(quad_force_exit_pause_spread_rs_streak_min=2)
        self.assertEqual(
            spread_rs_positive_streak_days(
                panel, as_of="2024-06-02", stock_id="2330", full_dates=dates
            ),
            2,
        )
        self.assertTrue(
            quad_force_exit_paused_by_positive_spread(
                cfg,
                spread_rs_panel=panel,
                as_of="2024-06-02",
                stock_id="2330",
                full_dates=dates,
            )
        )
        self.assertFalse(
            quad_force_exit_paused_by_positive_spread(
                cfg,
                spread_rs_panel=panel,
                as_of="2024-06-01",
                stock_id="2330",
                full_dates=dates,
            )
        )
        self.assertFalse(
            quad_force_exit_paused_by_positive_spread(
                cfg,
                spread_rs_panel=panel,
                as_of="2024-06-03",
                stock_id="2330",
                full_dates=dates,
            )
        )


class TestIntradayTiebreak(unittest.TestCase):
    def test_prefers_extension_when_tiebreak_on(self) -> None:
        cfg = ScoreSwapCConfig(intraday_swap_tiebreak=True, seg_margin=0.05)
        beats = [_scan_row("2330", seg_last=1.1), _scan_row("2317", seg_last=1.2)]
        lookup = {"2024-06-01": {"2330"}}
        picked = _pick_best_challenger(
            beats,
            cfg,
            buy_key=None,
            row_score=lambda r: float(r.seg_last),
            challenger_va_dot={},
            challenger_avg_accel={},
            rank_bonus_by_sid=None,
            intraday_extension_by_date=lookup,
            as_of="2024-06-01",
        )
        self.assertEqual(picked.stock_id, "2330")

    def test_falls_back_without_extension(self) -> None:
        cfg = ScoreSwapCConfig(intraday_swap_tiebreak=True)
        beats = [_scan_row("2330", seg_last=1.1), _scan_row("2317", seg_last=1.2)]
        picked = _pick_best_challenger(
            beats,
            cfg,
            buy_key=None,
            row_score=lambda r: float(r.seg_last),
            challenger_va_dot={},
            challenger_avg_accel={},
            rank_bonus_by_sid=None,
            intraday_extension_by_date={"2024-06-01": set()},
            as_of="2024-06-01",
        )
        self.assertEqual(picked.stock_id, "2317")


class TestRankBonusLookup(unittest.TestCase):
    def test_build_rank_bonus_lookup(self) -> None:
        panel = pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-02"],
                "sid": ["2330", "2317"],
                "drs5d_score_kin": [0.9, 0.1],
            }
        )
        lookup = build_rank_bonus_lookup(panel, score_col="drs5d_score_kin")
        self.assertEqual(lookup["2024-01-02"]["2330"], 0.9)
        self.assertEqual(lookup["2024-01-02"]["2317"], 0.1)


class TestGateFilters(unittest.TestCase):
    def test_filter_scan_rows_by_gate(self) -> None:
        rows = [_scan_row("2330"), _scan_row("2317")]
        out = filter_scan_rows_by_gate(rows, {"2330"})
        self.assertEqual([r.stock_id for r in out], ["2330"])

    def test_empty_gate_returns_empty(self) -> None:
        rows = [_scan_row("2330")]
        self.assertEqual(filter_scan_rows_by_gate(rows, set()), [])

    def test_apply_gate_by_date(self) -> None:
        rows = [_scan_row("2330"), _scan_row("2317")]
        gate = {"2024-06-01": {"2317"}}
        out = apply_gate_by_date(rows, "2024-06-01", gate)
        self.assertEqual([r.stock_id for r in out], ["2317"])
        self.assertEqual(len(apply_gate_by_date(rows, "2024-06-02", gate)), 0)


class TestIntradaySwapConfirm(unittest.TestCase):
    def test_confirm_ok_when_in_set(self) -> None:
        lookup = {"2024-06-01": {"2330"}}
        self.assertTrue(
            intraday_swap_confirm_ok(lookup, as_of="2024-06-01", stock_id="2330")
        )

    def test_confirm_fails_when_missing(self) -> None:
        lookup = {"2024-06-01": {"2330"}}
        self.assertFalse(
            intraday_swap_confirm_ok(lookup, as_of="2024-06-01", stock_id="2317")
        )

    def test_none_lookup_passes(self) -> None:
        self.assertTrue(
            intraday_swap_confirm_ok(None, as_of="2024-06-01", stock_id="2317")
        )


class TestGateLookups(unittest.TestCase):
    def _panel(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-02", "2024-01-03"],
                "sid": ["2330", "2317", "2330"],
                "in_pool_omega_kinematic": [True, False, True],
                "score_hybrid": [0.9, 0.1, 0.5],
                "intraday_extension": [True, False, False],
            }
        )

    def test_omega_kinematic_lookup(self) -> None:
        lookup = build_omega_kinematic_gate_lookup(self._panel())
        self.assertEqual(lookup["2024-01-02"], {"2330"})
        self.assertEqual(lookup["2024-01-03"], {"2330"})

    def test_hybrid_top_k_lookup(self) -> None:
        lookup = build_hybrid_top_gate_lookup(
            self._panel(), mode="hybrid_top_k", top_k=1
        )
        self.assertEqual(lookup["2024-01-02"], {"2330"})

    def test_hybrid_top_decile_lookup(self) -> None:
        panel = pd.DataFrame(
            {
                "date": ["2024-01-02"] * 12,
                "sid": [str(i) for i in range(12)],
                "in_pool_omega_kinematic": [True] * 12,
                "score_hybrid": list(range(12)),
            }
        )
        lookup = build_hybrid_top_gate_lookup(panel, mode="hybrid_top_decile")
        self.assertEqual(len(lookup["2024-01-02"]), 1)

    def test_intraday_extension_lookup(self) -> None:
        lookup = build_intraday_extension_lookup(self._panel())
        self.assertEqual(lookup.get("2024-01-02"), {"2330"})

    def test_intraday_coverage(self) -> None:
        lookup = {"2024-01-02": {"2330"}, "2023-12-29": set()}
        cov = intraday_gate_coverage(lookup, ["2023-12-29", "2024-01-02", "2024-06-01"])
        self.assertEqual(cov["n_intraday_dates"], 2)
        self.assertEqual(cov["n_dates_with_gate"], 1)


class TestPool1KinematicGateConfigs(unittest.TestCase):
    def test_variant_ids(self) -> None:
        ids = [c.variant_id for c in pool1_kinematic_gate_configs()]
        self.assertIn("S2", ids)
        self.assertIn("POOL1", ids)
        self.assertIn("P1-OMEGA", ids)
        self.assertIn("P1-SP", ids)
        self.assertIn("S2-IC", ids)

    def test_phase2_variant_ids(self) -> None:
        ids = [c.variant_id for c in pool1_kinematic_gate_phase2_configs()]
        self.assertIn("P1-KIN", ids)
        self.assertIn("P1-PAUSE-N2", ids)
        self.assertIn("P1-TB", ids)
        self.assertIn("S2-TB", ids)

    def test_spread_pause_flag(self) -> None:
        sp = next(c for c in pool1_kinematic_gate_configs() if c.variant_id == "S2-SP")
        self.assertTrue(sp.quad_force_exit_pause_spread_rs_positive)

    def test_streak_pause_config(self) -> None:
        n2 = next(c for c in pool1_kinematic_gate_phase2_configs() if c.variant_id == "P1-PAUSE-N2")
        self.assertEqual(n2.quad_force_exit_pause_spread_rs_streak_min, 2)

    def test_intraday_confirm_flag(self) -> None:
        ic = next(c for c in pool1_kinematic_gate_configs() if c.variant_id == "P1-IC")
        self.assertTrue(ic.intraday_swap_confirm)

    def test_intraday_tiebreak_flag(self) -> None:
        tb = next(c for c in pool1_kinematic_gate_phase2_configs() if c.variant_id == "P1-TB")
        self.assertTrue(tb.intraday_swap_tiebreak)
        self.assertFalse(tb.intraday_swap_confirm)


if __name__ == "__main__":
    unittest.main()
