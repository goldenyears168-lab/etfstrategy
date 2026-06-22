"""Tests for lens UI copy · headline · TW prose."""

from __future__ import annotations

import unittest

from lens_ui_copy import SECTION_TITLE_ZH, format_headline_zh


class LensUiCopyTests(unittest.TestCase):
    def test_section_title(self) -> None:
        self.assertEqual(SECTION_TITLE_ZH, "今日亮點")

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


if __name__ == "__main__":
    unittest.main()
