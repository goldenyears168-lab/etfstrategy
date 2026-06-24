"""Tests for lens UI copy · headline · TW prose."""

from __future__ import annotations

import unittest

from lens_ui_copy import (
    CONVERGENCE_TOOLTIP_ZH,
    LENS_SUBTITLE_ZH,
    PIT_FOOTNOTE_ZH,
    RRG_MIGRATION_LABELS_ZH,
    SECTION_TITLE_ZH,
    SORT_SCORE_TOOLTIP_ZH,
    format_headline_zh,
    format_rrg_rank_zh,
    format_watchlist_count_zh,
)


class LensUiCopyTests(unittest.TestCase):
    def test_section_title(self) -> None:
        self.assertEqual(SECTION_TITLE_ZH, "今日亮點")

    def test_watchlist_count_chip(self) -> None:
        self.assertEqual(format_watchlist_count_zh(153), "監控清單 153 檔")

    def test_headline_fire_and_new_observation(self) -> None:
        text = format_headline_zh(
            "2026-06-22",
            fire_count=3,
            delta_new_count=2,
        )
        self.assertEqual(
            text,
            "今日亮點：3 檔四框架收斂 · 2 檔新進觀察",
        )
        self.assertNotIn("Lens", text)
        self.assertNotIn("池", text)
        self.assertNotIn("收盤情報", text)

    def test_headline_new_observation_only(self) -> None:
        text = format_headline_zh("2026-06-22", delta_new_count=7)
        self.assertEqual(text, "今日亮點：7 檔新進觀察")

    def test_headline_no_change(self) -> None:
        text = format_headline_zh("2026-06-22")
        self.assertEqual(text, "今日亮點：今日無結構變化")
        self.assertNotIn("新訊號", text)

    def test_w1_copy_constants(self) -> None:
        self.assertEqual(PIT_FOOTNOTE_ZH, "只用當日及以前資料，事後不改寫過去紀錄。")
        self.assertEqual(
            CONVERGENCE_TOOLTIP_ZH,
            "ETF 加碼、大盤環境、類股輪動、VCP 四項符合幾項。",
        )
        self.assertEqual(
            LENS_SUBTITLE_ZH,
            "和昨天比：ETF 持股、大盤強度、類股輪動、VCP 篩選條件有哪些變化。",
        )
        self.assertEqual(SORT_SCORE_TOOLTIP_ZH, "系統內部排序分數，不代表買賣建議。")
        self.assertEqual(RRG_MIGRATION_LABELS_ZH["improving_to_leading"], "轉強→領先")

    def test_format_rrg_rank_zh(self) -> None:
        self.assertEqual(format_rrg_rank_zh(2, 135), "2/135")
        self.assertEqual(format_rrg_rank_zh(None, 135), "—/135")
        self.assertIsNone(format_rrg_rank_zh(2, None))


if __name__ == "__main__":
    unittest.main()
