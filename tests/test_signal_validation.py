"""signal_validation.py 純函式測試 —— 守住 PIT / 事件層 / permutation 三鐵律。

這支模組原本零測試，卻是「改壞 = 靜默作廢 alpha 且無警報」最危險的一塊
（2026-08-02 進階健檢標為唯一 🔴 裸奔核心）。這裡釘死其純函式的行為，
特別是 pit_beta 的「訊號日 T 只能用 date ≤ T」邊界——加一筆未來資料，
斷言 beta 不受影響。
"""
import unittest

from signal_validation import (
    fwd_pct,
    leave_one_event_out,
    permutation_diff,
    pit_beta,
    summarize,
    to_events,
)


def _make_panel(n=400, beta=2.0):
    """造 n 日：個股日報酬 = beta × 指數日報酬（完美線性 → 真 beta==beta）。"""
    cal = [f"d{i:04d}" for i in range(n)]
    cpos = {d: i for i, d in enumerate(cal)}
    # 指數報酬：非常數（否則 var=0），三段循環避免退化
    iret = {}
    px = {cal[0]: 100.0}
    for k in range(1, n):
        r = [0.01, -0.02, 0.015][k % 3]
        iret[cal[k]] = r
        px[cal[k]] = px[cal[k - 1]] * (1 + beta * r)
    return cal, cpos, iret, px


class PitBetaTests(unittest.TestCase):
    def test_recovers_known_beta(self):
        cal, cpos, iret, px = _make_panel(beta=2.0)
        b = pit_beta(px, iret, cal, cpos, "d0300", win=250, minobs=60)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b, 2.0, places=6)

    def test_no_lookahead_future_data_ignored(self):
        """核心 PIT 鐵律：把 date 之後的資料換成瘋狂值，pit_beta 必須不變。"""
        cal, cpos, iret, px = _make_panel(beta=2.0)
        at = "d0300"
        before = pit_beta(px, iret, cal, cpos, at, win=250, minobs=60)
        # 污染未來（k > 300）：個股報酬改成 -10× 指數，若洩漏 beta 會被拉走
        for k in range(301, len(cal)):
            iret[cal[k]] = 0.03
            px[cal[k]] = px[cal[k - 1]] * (1 - 10 * 0.03)
        after = pit_beta(px, iret, cal, cpos, at, win=250, minobs=60)
        self.assertEqual(before, after)
        self.assertAlmostEqual(after, 2.0, places=6)

    def test_insufficient_obs_returns_none(self):
        cal, cpos, iret, px = _make_panel()
        # date 太早（可用觀測 < minobs）→ None
        self.assertIsNone(pit_beta(px, iret, cal, cpos, "d0010", win=250, minobs=60))

    def test_unknown_date_returns_none(self):
        cal, cpos, iret, px = _make_panel()
        self.assertIsNone(pit_beta(px, iret, cal, cpos, "not-a-date"))

    def test_window_bounds_observations(self):
        """win 限制回看窗：短窗只吃 win 筆，仍應回出正確 beta。"""
        cal, cpos, iret, px = _make_panel(beta=1.5)
        b = pit_beta(px, iret, cal, cpos, "d0300", win=100, minobs=60)
        self.assertAlmostEqual(b, 1.5, places=6)


class ToEventsTests(unittest.TestCase):
    def test_clusters_adjacent_within_gap(self):
        cpos = {"a": 0, "b": 1, "c": 5, "d": 6}
        events = to_events(["a", "b", "c", "d"], cpos, max_gap=2)
        self.assertEqual(events, [["a", "b"], ["c", "d"]])

    def test_gap_boundary_splits(self):
        cpos = {"a": 0, "b": 3}  # 剛好差 3 > max_gap 2 → 兩事件
        self.assertEqual(to_events(["a", "b"], cpos, max_gap=2), [["a"], ["b"]])

    def test_ignores_dates_not_in_cpos(self):
        cpos = {"a": 0, "b": 1}
        self.assertEqual(to_events(["a", "zzz", "b"], cpos, max_gap=2), [["a", "b"]])


class FwdPctTests(unittest.TestCase):
    def test_forward_return(self):
        cal = ["d0", "d1", "d2"]
        cpos = {d: i for i, d in enumerate(cal)}
        close = {"d0": 100.0, "d1": 110.0, "d2": 121.0}
        self.assertAlmostEqual(fwd_pct(close, cpos, cal, "d0", 2), 21.0, places=6)

    def test_immature_returns_none(self):
        cal = ["d0", "d1"]
        cpos = {d: i for i, d in enumerate(cal)}
        close = {"d0": 100.0, "d1": 110.0}
        self.assertIsNone(fwd_pct(close, cpos, cal, "d1", 2))  # 前瞻不足


class PermutationDiffTests(unittest.TestCase):
    def test_reproducible_with_seed(self):
        a, b = [5.0, 6.0, 7.0], [-5.0, -6.0, -7.0]
        p1 = permutation_diff(a, b, nperm=2000, seed=42)
        p2 = permutation_diff(a, b, nperm=2000, seed=42)
        self.assertEqual(p1, p2)  # 種子固定 → 完全可重現

    def test_clear_separation_small_pvalue(self):
        a, b = [5.0, 6.0, 7.0], [-5.0, -6.0, -7.0]
        p = permutation_diff(a, b, nperm=5000, seed=42)
        self.assertLess(p, 0.1)  # 明顯分離 → 隨機超過觀察超額的機率低

    def test_empty_returns_none(self):
        self.assertIsNone(permutation_diff([], [1.0], nperm=10))


class LeaveOneEventOutTests(unittest.TestCase):
    def test_identifies_worst_event(self):
        base, worst_i, after = leave_one_event_out([10.0, 10.0, 10.0, -50.0])
        self.assertAlmostEqual(base, -5.0, places=6)
        self.assertEqual(worst_i, 3)          # 剔除 -50 最劇烈改變均值
        self.assertAlmostEqual(after, 10.0, places=6)

    def test_single_value(self):
        self.assertEqual(leave_one_event_out([3.0]), (3.0, None, None))


class SummarizeTests(unittest.TestCase):
    def test_basic_stats(self):
        s = summarize([1.0, -1.0, 2.0, None])
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["mean"], 2.0 / 3, places=6)
        self.assertAlmostEqual(s["win"], 100 * 2 / 3, places=6)

    def test_all_none(self):
        self.assertEqual(summarize([None, None])["n"], 0)


if __name__ == "__main__":
    unittest.main()
