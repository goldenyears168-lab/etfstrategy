"""持有量（含今日新買、未交割）過濾與序列化 · 回歸測試。

複現：今日買進整筆的部位 tradable_qty=0，舊邏輯以可賣量判斷會漏掉整筆，
且加買的零股只計可賣量。改以 today_qty 反映實際持倉。
"""

from __future__ import annotations

import unittest

from order.fubon_account import _held_qty
from order.morning_holdings_brief import _shares_from_holding


class TestHeldQty(unittest.TestCase):
    def test_today_bought_not_dropped(self) -> None:
        # 今日買進 20 股零股，尚未交割 → tradable_qty=0 但 today_qty=20
        row = {"stock_no": "2327", "tradable_qty": 0, "today_qty": 0,
               "odd": {"lastday_qty": 0, "today_qty": 20, "tradable_qty": 0}}
        self.assertEqual(_held_qty(row), 20)
        self.assertEqual(_shares_from_holding(row), 20)

    def test_added_today_counts_full_position(self) -> None:
        # 昨 60 + 今買 200 = 260；tradable 只剩 60
        row = {"stock_no": "2303", "tradable_qty": 0, "today_qty": 0,
               "odd": {"lastday_qty": 60, "today_qty": 260, "tradable_qty": 60}}
        self.assertEqual(_held_qty(row), 260)
        self.assertEqual(_shares_from_holding(row), 260)

    def test_whole_lot_held(self) -> None:
        row = {"stock_no": "6505", "lastday_qty": 900, "today_qty": 900, "tradable_qty": 900}
        self.assertEqual(_held_qty(row), 900)
        self.assertEqual(_shares_from_holding(row), 900)

    def test_fallback_to_tradable_when_today_absent(self) -> None:
        # 舊式輸入（無 today_qty 欄）需向後相容
        row = {"stock_no": "2449", "tradable_qty": 0, "odd": {"tradable_qty": 88}}
        self.assertEqual(_held_qty(row), 88)
        self.assertEqual(_shares_from_holding(row), 88)

    def test_fully_sold_today_excluded(self) -> None:
        row = {"stock_no": "9999", "today_qty": 0, "tradable_qty": 0,
               "odd": {"today_qty": 0, "tradable_qty": 0}}
        self.assertEqual(_held_qty(row), 0)
        self.assertEqual(_shares_from_holding(row), 0)


if __name__ == "__main__":
    unittest.main()
