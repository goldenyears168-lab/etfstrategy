"""籌碼簡報 · 同業相對排序的回歸測試。

2026-08-27 起 HS 分數由「全市場截面百分位」改為「同業內百分位」。
釘住小產業退回邏輯 —— 若沒有 MIN_IND_N 門檻，只有 1 檔的產業其組內
``rank(pct=True)`` 恆為 1.0，那檔股票會永遠佔據偏空榜首（分數最高），
而 2 檔產業則只能產生 0.5／1.0 兩個值，同樣會系統性污染兩端名單。
"""

from __future__ import annotations

import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
B = SourceFileLoader(
    "run_chip_daily_brief", str(ROOT / "scripts" / "research" / "run_chip_daily_brief.py")
).load_module()


class IndRankTest(unittest.TestCase):
    def test_large_industry_ranks_within_group(self):
        df = pd.DataFrame({"x": [1, 2, 3, 4], "ind": ["A"] * 4})
        self.assertEqual(B._ind_rank(df, "x").tolist(), [0.25, 0.5, 0.75, 1.0])

    def test_small_industry_falls_back_to_market(self):
        # B 只有 2 檔（< MIN_IND_N=3）→ 必須用全市場百分位，不是組內的 0.5/1.0
        df = pd.DataFrame({"x": [1, 2, 3, 4, 5, 6], "ind": ["A"] * 4 + ["B"] * 2})
        r = B._ind_rank(df, "x")
        self.assertAlmostEqual(r.iloc[4], 5 / 6)
        self.assertAlmostEqual(r.iloc[5], 6 / 6)

    def test_singleton_industry_is_not_pinned_to_one(self):
        # 沒有退回的話這檔會是 1.0（永遠榜首）
        df = pd.DataFrame({"x": [1, 2, 3, 4, 5], "ind": ["A"] * 4 + ["Z"]})
        self.assertLess(B._ind_rank(df, "x").iloc[4], 1.0 + 1e-9)
        self.assertAlmostEqual(B._ind_rank(df, "x").iloc[4], 1.0)  # 恰為全市場最大值，合理
        df2 = pd.DataFrame({"x": [5, 2, 3, 4, 1], "ind": ["A"] * 4 + ["Z"]})
        self.assertAlmostEqual(B._ind_rank(df2, "x").iloc[4], 0.2)  # 全市場最小 → 不再是 1.0

    def test_missing_industry_column_falls_back(self):
        df = pd.DataFrame({"x": [3, 1, 2]})
        self.assertEqual(B._ind_rank(df, "x").tolist(), [1.0, 1 / 3, 2 / 3])

    def test_single_industry_universe_falls_back(self):
        df = pd.DataFrame({"x": [3, 1, 2], "ind": ["A"] * 3})
        self.assertEqual(B._ind_rank(df, "x").tolist(), [1.0, 1 / 3, 2 / 3])


class WeightTest(unittest.TestCase):
    def test_weight_is_equal_split(self):
        # 換基準後重掃 w：0.5~0.8 是高原，取等權點 0.50（非曲線上挑的位置）
        self.assertAlmostEqual(B.HS_W_ZP, 0.50)
        self.assertEqual(B.MIN_IND_N, 3)


if __name__ == "__main__":
    unittest.main()
