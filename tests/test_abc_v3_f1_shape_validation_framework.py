"""RP-6 shape validation framework 單元測試（純 stdlib unittest，不依賴 pytest）。

執行：cd 專案根目錄 && PYTHONPATH=src python3 -m unittest tests.test_abc_v3_f1_shape_validation_framework -v
（裝有 pytest 的環境亦可：PYTHONPATH=src python3 -m pytest tests/test_abc_v3_f1_shape_validation_framework.py -q）
"""

from __future__ import annotations

import math
import os
import unittest

from research.backtest.abc_v3_f1_shape_validation_framework import (
    REGISTRY_SCHEMA_ID,
    VALID_SHAPE_STATUSES,
    _split_dates_three_way,
    _t_quantile,
    achieved_power,
    apply_fdr_correction,
    evaluate_shape_candidate,
    min_n_for_power,
    one_sample_t_test,
    p_value_from_correlation,
    sigma_from_tail_fraction,
    t_two_sided_p,
    validate_shape_registry,
    walk_forward_recheck,
    welch_t_test,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False


# ---------------------------------------------------------------------------
# t 分布數學基礎
# ---------------------------------------------------------------------------


class TestTMath(unittest.TestCase):
    def test_two_sided_p_known_df10(self):
        # t_{0.975, 10} = 2.228 → 雙尾 p = 0.05
        self.assertAlmostEqual(t_two_sided_p(2.228, 10), 0.05, delta=2e-4)

    def test_two_sided_p_known_df1(self):
        # t_{0.975, 1} = 12.706
        self.assertAlmostEqual(t_two_sided_p(12.706, 1), 0.05, delta=2e-4)

    def test_two_sided_p_large_df_matches_normal(self):
        # df→∞ 收斂到常態：z=1.959964 → p=0.05
        self.assertAlmostEqual(t_two_sided_p(1.959964, 100000), 0.05, delta=5e-4)

    def test_two_sided_p_symmetry_and_bounds(self):
        self.assertAlmostEqual(t_two_sided_p(0.0, 5), 1.0, places=12)
        self.assertEqual(t_two_sided_p(-2.5, 8), t_two_sided_p(2.5, 8))
        self.assertLess(t_two_sided_p(1e9, 5), 1e-12)

    def test_quantile_roundtrip(self):
        for df in (1, 3, 30, 273):
            for prob in (0.6, 0.9, 0.975, 0.995):
                t = _t_quantile(prob, df)
                expected = 2 * (1 - prob)
                self.assertTrue(
                    math.isclose(t_two_sided_p(t, df), expected, rel_tol=1e-4),
                    f"df={df} prob={prob}",
                )

    def test_quantile_known_values(self):
        self.assertAlmostEqual(_t_quantile(0.975, 10), 2.2281, delta=1e-3)
        self.assertAlmostEqual(_t_quantile(0.95, 30), 1.6973, delta=1e-3)
        self.assertEqual(_t_quantile(0.5, 7), 0.0)
        self.assertAlmostEqual(_t_quantile(0.025, 10), -2.2281, delta=1e-3)

    def test_quantile_invalid_prob(self):
        with self.assertRaises(ValueError):
            _t_quantile(0.0, 10)
        with self.assertRaises(ValueError):
            _t_quantile(1.0, 10)


class TestTTests(unittest.TestCase):
    def test_one_sample_known(self):
        vals = [4.0, 5.0, 6.0, 5.0, 5.0]
        r = one_sample_t_test(vals, 3.0)
        self.assertEqual(r["n"], 5)
        self.assertAlmostEqual(r["mean"], 5.0, places=12)
        se = r["std"] / math.sqrt(5)
        self.assertAlmostEqual(r["t"], 2.0 / se, places=10)
        self.assertTrue(0.0 < r["p_two_sided"] < 0.01)

    def test_one_sample_degenerate(self):
        self.assertIsNone(one_sample_t_test([], 0.0)["p_two_sided"])
        self.assertIsNone(one_sample_t_test([1.0], 0.0)["p_two_sided"])
        self.assertIsNone(one_sample_t_test([2.0, 2.0, 2.0], 0.0)["p_two_sided"])  # 零變異

    def test_welch_symmetric_null(self):
        a = [1.0, 2.0, 3.0, 4.0]
        r = welch_t_test(a, list(a))
        self.assertAlmostEqual(r["t"], 0.0, places=12)
        self.assertAlmostEqual(r["p_two_sided"], 1.0, places=10)

    def test_welch_detects_difference(self):
        a = [10.0 + 0.1 * i for i in range(20)]
        b = [1.0 + 0.1 * i for i in range(20)]
        r = welch_t_test(a, b)
        self.assertLess(r["p_two_sided"], 1e-6)
        self.assertGreater(r["mean1"], r["mean2"])

    def test_welch_too_small(self):
        self.assertIsNone(welch_t_test([1.0], [1.0, 2.0])["p_two_sided"])

    def test_p_from_correlation_known(self):
        # H-ENTRY-NORM-1 實際數字：r=-0.1519, n=275 → p≈0.0116（雙尾）
        self.assertAlmostEqual(p_value_from_correlation(-0.1519, 275), 0.0116, delta=5e-4)

    def test_p_from_correlation_edges(self):
        self.assertAlmostEqual(p_value_from_correlation(0.0, 100), 1.0, places=10)
        self.assertEqual(p_value_from_correlation(1.0, 10), 0.0)
        self.assertIsNone(p_value_from_correlation(0.5, 2))  # n<3
        self.assertIsNone(p_value_from_correlation(1.5, 100))  # 非法 r


# ---------------------------------------------------------------------------
# 1) _split_dates_three_way
# ---------------------------------------------------------------------------


class TestSplitThreeWay(unittest.TestCase):
    @staticmethod
    def _dates(n):
        return [f"2026-01-{d:02d}" for d in range(1, n + 1)]

    def test_default_ratios_and_no_overlap(self):
        dates = self._dates(20)
        is_d, val_d, hold_d = _split_dates_three_way(dates)
        self.assertEqual((len(is_d), len(val_d), len(hold_d)), (10, 5, 5))
        self.assertEqual(is_d + val_d + hold_d, sorted(dates))
        self.assertFalse(set(is_d) & set(val_d))
        self.assertFalse(set(val_d) & set(hold_d))

    def test_no_lookahead_strict_time_order(self):
        is_d, val_d, hold_d = _split_dates_three_way(self._dates(17))
        self.assertLess(max(is_d), min(val_d))
        self.assertLess(max(val_d), min(hold_d))  # holdout 恆為最晚一段

    def test_unsorted_and_duplicate_input(self):
        raw = ["2026-01-03", "2026-01-01", "2026-01-02", "2026-01-01", "2026-01-04"]
        is_d, val_d, hold_d = _split_dates_three_way(raw)
        self.assertEqual(
            is_d + val_d + hold_d, ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
        )

    def test_edge_small_n(self):
        self.assertEqual(_split_dates_three_way([]), ([], [], []))
        self.assertEqual(_split_dates_three_way(["2026-01-01"]), (["2026-01-01"], [], []))
        # n=2：IS + Holdout（holdout 一定要留最晚一天）
        self.assertEqual(
            _split_dates_three_way(self._dates(2)), (["2026-01-01"], [], ["2026-01-02"])
        )
        # n>=3：三段各至少 1 天
        for n in range(3, 12):
            parts = _split_dates_three_way(self._dates(n))
            self.assertTrue(all(len(p) >= 1 for p in parts), f"n={n}: {parts}")

    def test_custom_ratios(self):
        is_d, val_d, hold_d = _split_dates_three_way(self._dates(10), is_ratio=0.6, val_ratio=0.2)
        self.assertEqual((len(is_d), len(val_d), len(hold_d)), (6, 2, 2))

    def test_invalid_ratios_raise(self):
        with self.assertRaises(ValueError):
            _split_dates_three_way(self._dates(10), is_ratio=0.0)
        with self.assertRaises(ValueError):
            _split_dates_three_way(self._dates(10), val_ratio=1.0)
        with self.assertRaises(ValueError):
            _split_dates_three_way(self._dates(10), is_ratio=0.8, val_ratio=0.2)  # holdout 為空


# ---------------------------------------------------------------------------
# 2) apply_fdr_correction
# ---------------------------------------------------------------------------


class TestFdrCorrection(unittest.TestCase):
    def test_bh_textbook_example(self):
        # p=[.01,.04,.03,.005], m=4 → BH 調整後 [.02,.04,.04,.02]
        out = apply_fdr_correction([0.01, 0.04, 0.03, 0.005], q=0.05, method="bh")
        adj = [r["p_adjusted"] for r in out["results"]]
        for got, want in zip(adj, [0.02, 0.04, 0.04, 0.02]):
            self.assertAlmostEqual(got, want, places=10)
        self.assertEqual(out["n_significant"], 4)
        self.assertEqual(out["m"], 4)

    def test_bh_matches_step_up_criterion(self):
        # 經典 step-up：最大 k 使 p_(k) <= q·k/m → 前 k 名顯著（此例 k=2）
        ps = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
        out = apply_fdr_correction(ps, q=0.05, method="bh")
        sig = [r["label"] for r in out["results"] if r["significant"]]
        self.assertEqual(sig, ["cand_0", "cand_1"])

    def test_bh_monotone_adjusted(self):
        out = apply_fdr_correction([0.5, 0.001, 0.02, 0.03], q=0.1, method="bh")
        ranked = sorted(
            (r for r in out["results"] if r["rank"] is not None), key=lambda r: r["rank"]
        )
        adj = [r["p_adjusted"] for r in ranked]
        self.assertEqual(adj, sorted(adj))  # 調整後 p 必須隨 rank 單調不減

    def test_bonferroni(self):
        out = apply_fdr_correction([0.004, 0.02, 0.5], q=0.05, method="bonferroni")
        adj = [r["p_adjusted"] for r in out["results"]]
        for got, want in zip(adj, [0.012, 0.06, 1.0]):
            self.assertAlmostEqual(got, want, places=10)
        self.assertEqual([r["significant"] for r in out["results"]], [True, False, False])

    def test_none_p_counts_toward_m(self):
        # n<2 算不出 p 的候選仍計入校正基準 m（申報紀律）
        out = apply_fdr_correction([0.001, None, None, None], q=0.05, method="bonferroni")
        self.assertEqual(out["m"], 4)
        self.assertAlmostEqual(out["results"][0]["p_adjusted"], 0.004, places=10)  # ×4 不是 ×1
        self.assertFalse(out["results"][1]["significant"])
        out_bh = apply_fdr_correction([0.001, None, None, None], q=0.05, method="bh")
        self.assertAlmostEqual(out_bh["results"][0]["p_adjusted"], 0.004, places=10)

    def test_preserves_input_order_and_labels(self):
        out = apply_fdr_correction([0.9, 0.001], labels=["worse", "better"], q=0.1)
        self.assertEqual([r["label"] for r in out["results"]], ["worse", "better"])
        self.assertEqual(out["results"][1]["rank"], 1)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            apply_fdr_correction([0.05], method="holm")
        with self.assertRaises(ValueError):
            apply_fdr_correction([0.05], q=0.0)
        with self.assertRaises(ValueError):
            apply_fdr_correction([1.5])
        with self.assertRaises(ValueError):
            apply_fdr_correction([0.05, 0.06], labels=["only_one"])

    def test_empty_input(self):
        out = apply_fdr_correction([])
        self.assertEqual((out["m"], out["n_significant"], out["results"]), (0, 0, []))


# ---------------------------------------------------------------------------
# 3) min_n_for_power / achieved_power
# ---------------------------------------------------------------------------


class TestPower(unittest.TestCase):
    def test_one_sample_benchmark_d_half(self):
        # 教科書基準：d=0.5（effect 5pp / σ 10pp）、α=.05 雙尾、power .8 → n=34
        self.assertEqual(min_n_for_power(5.0, 10.0), 34)

    def test_two_sample_benchmark_d_half(self):
        # 兩樣本設計每組 n=64（與 G*Power 同值）
        self.assertEqual(min_n_for_power(5.0, 10.0, design="two_sample"), 64)

    def test_returned_n_is_minimal(self):
        n = min_n_for_power(5.0, 10.0)
        self.assertGreaterEqual(achieved_power(n, 5.0, 10.0), 0.80)
        self.assertLess(achieved_power(n - 1, 5.0, 10.0), 0.80)

    def test_monotonicity(self):
        # 效果量越小 / σ 越大 / power 要求越高 → n 越大
        self.assertGreater(min_n_for_power(2.0, 10.0), min_n_for_power(5.0, 10.0))
        self.assertGreater(min_n_for_power(5.0, 12.0), min_n_for_power(5.0, 8.0))
        self.assertGreater(min_n_for_power(5.0, 10.0, power=0.9), min_n_for_power(5.0, 10.0))

    def test_one_sided_needs_fewer(self):
        self.assertLess(min_n_for_power(5.0, 10.0, two_sided=False), min_n_for_power(5.0, 10.0))

    def test_achieved_power_edges(self):
        self.assertEqual(achieved_power(1, 5.0, 10.0), 0.0)  # n<2 無法檢定
        self.assertGreater(achieved_power(1000, 5.0, 10.0), 0.999)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            min_n_for_power(0.0, 10.0)
        with self.assertRaises(ValueError):
            min_n_for_power(-1.0, 10.0)
        with self.assertRaises(ValueError):
            min_n_for_power(5.0, 0.0)
        with self.assertRaises(ValueError):
            min_n_for_power(5.0, 10.0, power=1.0)
        with self.assertRaises(ValueError):
            achieved_power(10, 5.0, 10.0, design="paired")

    def test_effect_too_small_raises(self):
        with self.assertRaises(ValueError):
            min_n_for_power(0.001, 10.0, max_n=1000)

    def test_sigma_from_tail_fraction_roundtrip(self):
        # 常態 N(0, 10)：P(X>=10) = 15.866% → 反推 σ=10
        got = sigma_from_tail_fraction(0.0, 10.0, 0.15866)
        self.assertTrue(math.isclose(got, 10.0, rel_tol=1e-3), got)

    def test_sigma_from_tail_fraction_degenerate(self):
        self.assertIsNone(sigma_from_tail_fraction(5.0, 8.0, 0.5))  # z=0 無法反推
        self.assertIsNone(sigma_from_tail_fraction(5.0, 8.0, 0.0))
        self.assertIsNone(sigma_from_tail_fraction(5.0, 8.0, 1.0))
        self.assertIsNone(sigma_from_tail_fraction(15.0, 8.0, 0.25))  # 反推出負 σ


# ---------------------------------------------------------------------------
# 4) walk_forward_recheck
# ---------------------------------------------------------------------------


def _chk(date, n, mean):
    return {"check_date": date, "n": n, "mean_ret_pct": mean}


class TestWalkForward(unittest.TestCase):
    def test_all_pass_active(self):
        out = walk_forward_recheck(
            [_chk("2026-03-31", 20, 9.0), _chk("2026-06-30", 25, 8.5)],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "active")
        self.assertEqual(out["fail_streak"], 0)
        self.assertIsNone(out["decayed_on"])
        self.assertEqual(out["n_checks"], 2)

    def test_single_fail_warning(self):
        out = walk_forward_recheck(
            [_chk("2026-03-31", 20, 9.0), _chk("2026-06-30", 20, 5.0)],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "warning")
        self.assertEqual(out["fail_streak"], 1)

    def test_two_consecutive_fails_decayed(self):
        out = walk_forward_recheck(
            [_chk("2026-03-31", 20, 5.0), _chk("2026-06-30", 20, 4.0)],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "decayed")
        self.assertEqual(out["decayed_on"], "2026-06-30")

    def test_nonconsecutive_fails_not_decayed(self):
        out = walk_forward_recheck(
            [
                _chk("2026-03-31", 20, 5.0),
                _chk("2026-06-30", 20, 9.0),
                _chk("2026-09-30", 20, 5.0),
            ],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "warning")
        self.assertEqual(out["fail_streak"], 1)

    def test_decayed_is_sticky(self):
        # 連兩次失敗後即使回升，仍維持 decayed（decayed_on 固定在首次觸發日）
        out = walk_forward_recheck(
            [
                _chk("2026-03-31", 20, 5.0),
                _chk("2026-06-30", 20, 4.0),
                _chk("2026-09-30", 20, 12.0),
            ],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "decayed")
        self.assertEqual(out["decayed_on"], "2026-06-30")

    def test_insufficient_n_counts_as_fail(self):
        out = walk_forward_recheck(
            [_chk("2026-03-31", 3, 20.0), _chk("2026-06-30", 2, 25.0)],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "decayed")  # 拿不出足量證據 = 失敗（保守）

    def test_unsorted_checks_sorted_by_date(self):
        out = walk_forward_recheck(
            [_chk("2026-06-30", 20, 4.0), _chk("2026-03-31", 20, 5.0)],
            threshold_pct=8.0, min_n=15,
        )
        self.assertEqual(out["status"], "decayed")
        self.assertEqual(out["last_check_date"], "2026-06-30")
        self.assertEqual(
            [c["check_date"] for c in out["checks"]], ["2026-03-31", "2026-06-30"]
        )

    def test_empty_unchecked(self):
        out = walk_forward_recheck([], threshold_pct=8.0)
        self.assertEqual(out["status"], "unchecked")
        self.assertIsNone(out["last_check_date"])

    def test_missing_mean_counts_as_fail(self):
        out = walk_forward_recheck(
            [{"check_date": "2026-03-31", "n": 20}], threshold_pct=8.0, min_n=15
        )
        self.assertEqual(out["status"], "warning")

    def test_custom_streak_threshold(self):
        checks = [_chk("2026-03-31", 20, 5.0)]
        out = walk_forward_recheck(checks, threshold_pct=8.0, fail_streak_to_decay=1)
        self.assertEqual(out["status"], "decayed")
        with self.assertRaises(ValueError):
            walk_forward_recheck(checks, threshold_pct=8.0, fail_streak_to_decay=0)


# ---------------------------------------------------------------------------
# evaluate_shape_candidate（組合把關）
# ---------------------------------------------------------------------------


class TestEvaluateShapeCandidate(unittest.TestCase):
    def test_strong_candidate_passes(self):
        out = evaluate_shape_candidate(
            shape_id="strong", n=120, mean_ret_pct=9.0, baseline_mean_pct=3.0,
            sigma_pct=10.0, n_candidates_tested=12, target_pct=8.0,
        )
        self.assertTrue(out["passed"])
        self.assertEqual(out["reasons"], [])
        self.assertLessEqual(out["min_n_observed_effect"], 120)

    def test_w3_mv_gt_100_like_flagged_not_passed(self):
        # H-ENTRY-MF-1 10m 窗口：n=8, mean 10.057 vs baseline 3.112，14 候選
        out = evaluate_shape_candidate(
            shape_id="w3_mv_gt_100", n=8, mean_ret_pct=10.057, baseline_mean_pct=3.112,
            sigma_pct=10.0, n_candidates_tested=14, target_pct=8.0,
        )
        self.assertFalse(out["passed"])
        self.assertIn("insufficient_n", out["reasons"])
        self.assertIn("not_significant_after_correction", out["reasons"])
        self.assertGreater(out["p_adjusted"], 0.05)

    def test_below_target_reason(self):
        out = evaluate_shape_candidate(
            shape_id="meh", n=500, mean_ret_pct=5.0, baseline_mean_pct=3.0,
            sigma_pct=6.0, target_pct=8.0,
        )
        self.assertFalse(out["passed"])
        self.assertIn("below_target", out["reasons"])

    def test_no_positive_effect(self):
        out = evaluate_shape_candidate(
            shape_id="neg", n=50, mean_ret_pct=2.0, baseline_mean_pct=3.0, sigma_pct=10.0
        )
        self.assertIn("no_positive_effect", out["reasons"])
        self.assertFalse(out["passed"])

    def test_tiny_effect_does_not_crash(self):
        # 觀測效果量極小 → 需求 n 爆表：不應 raise，應判 insufficient_n
        out = evaluate_shape_candidate(
            shape_id="tiny", n=100, mean_ret_pct=3.0001, baseline_mean_pct=3.0, sigma_pct=10.0
        )
        self.assertFalse(out["passed"])
        self.assertIn("insufficient_n", out["reasons"])
        self.assertIsNone(out["min_n_observed_effect"])

    def test_more_candidates_stricter(self):
        kw = dict(n=40, mean_ret_pct=9.0, baseline_mean_pct=3.0, sigma_pct=10.0, target_pct=8.0)
        p1 = evaluate_shape_candidate(shape_id="a", n_candidates_tested=1, **kw)["p_adjusted"]
        p60 = evaluate_shape_candidate(shape_id="a", n_candidates_tested=60, **kw)["p_adjusted"]
        self.assertAlmostEqual(p60, min(1.0, p1 * 60), places=10)

    def test_explicit_p_value_respected(self):
        out = evaluate_shape_candidate(
            shape_id="explicit", n=120, mean_ret_pct=9.0, baseline_mean_pct=3.0,
            sigma_pct=10.0, n_candidates_tested=2, p_value=0.001,
        )
        self.assertAlmostEqual(out["p_adjusted"], 0.002, places=10)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            evaluate_shape_candidate(
                shape_id="x", n=-1, mean_ret_pct=9.0, baseline_mean_pct=3.0, sigma_pct=10.0
            )
        with self.assertRaises(ValueError):
            evaluate_shape_candidate(
                shape_id="x", n=10, mean_ret_pct=9.0, baseline_mean_pct=3.0,
                sigma_pct=10.0, n_candidates_tested=0,
            )
        with self.assertRaises(ValueError):
            evaluate_shape_candidate(
                shape_id="x", n=10, mean_ret_pct=9.0, baseline_mean_pct=3.0, sigma_pct=0.0
            )


# ---------------------------------------------------------------------------
# Shape registry schema
# ---------------------------------------------------------------------------


def _valid_shape(sid="s1", status="discovery"):
    return {
        "id": sid,
        "definition": {"module": "m", "matcher": "f", "params": {}},
        "status": status,
        "stages": {"discovery": {"n": 10}, "validation": None, "holdout": None},
    }


class TestRegistrySchema(unittest.TestCase):
    def test_valid_payload(self):
        payload = {"schema": REGISTRY_SCHEMA_ID, "shapes": [_valid_shape()]}
        self.assertEqual(validate_shape_registry(payload), [])

    def test_bad_schema_id(self):
        errs = validate_shape_registry({"schema": "nope", "shapes": []})
        self.assertTrue(any("schema" in e for e in errs))

    def test_missing_required_keys(self):
        shape = _valid_shape()
        del shape["definition"]
        errs = validate_shape_registry({"schema": REGISTRY_SCHEMA_ID, "shapes": [shape]})
        self.assertTrue(any("definition" in e for e in errs))

    def test_invalid_status(self):
        errs = validate_shape_registry(
            {"schema": REGISTRY_SCHEMA_ID, "shapes": [_valid_shape(status="maybe")]}
        )
        self.assertTrue(any("invalid status" in e for e in errs))
        self.assertEqual(
            set(VALID_SHAPE_STATUSES),
            {"discovery", "validated", "holdout_passed", "decayed", "rejected"},
        )

    def test_duplicate_ids(self):
        errs = validate_shape_registry(
            {"schema": REGISTRY_SCHEMA_ID, "shapes": [_valid_shape("dup"), _valid_shape("dup")]}
        )
        self.assertTrue(any("duplicate id" in e for e in errs))

    def test_missing_stage_key(self):
        shape = _valid_shape()
        del shape["stages"]["holdout"]
        errs = validate_shape_registry({"schema": REGISTRY_SCHEMA_ID, "shapes": [shape]})
        self.assertTrue(any("missing stage 'holdout'" in e for e in errs))

    def test_holdout_passed_requires_run_once_done(self):
        shape = _valid_shape(status="holdout_passed")
        shape["stages"]["holdout"] = {"n": 30, "run_once_done": False}
        errs = validate_shape_registry({"schema": REGISTRY_SCHEMA_ID, "shapes": [shape]})
        self.assertTrue(any("run_once_done" in e for e in errs))
        shape["stages"]["holdout"]["run_once_done"] = True
        self.assertEqual(
            validate_shape_registry({"schema": REGISTRY_SCHEMA_ID, "shapes": [shape]}), []
        )

    def test_shapes_not_a_list(self):
        errs = validate_shape_registry({"schema": REGISTRY_SCHEMA_ID, "shapes": {}})
        self.assertEqual(errs, ["shapes must be a list"])

    @unittest.skipUnless(_HAS_YAML, "PyYAML not installed")
    def test_repo_registry_file_is_valid(self):
        path = os.path.join(_ROOT, "config", "abc_v3_f1_shape_registry.yaml")
        if not os.path.exists(path):
            self.skipTest("registry file not present")
        with open(path, encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
        self.assertEqual(validate_shape_registry(payload), [])


if __name__ == "__main__":
    unittest.main()
