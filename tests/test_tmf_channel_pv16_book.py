"""Unit tests for TMF PV16 specialized Order SSOT."""

from __future__ import annotations

import unittest

from order.tmf_channel_config import PAPER_RECIPE, load_tmf_channel_order_config
from order.tmf_channel_pv16_book import (
    PV8,
    RECIPE_VERSION,
    book_summary,
    cell_recipe,
    freeze_cell_book,
    session_from_hhmm,
    specialized_cell_book,
)


class Pv16BookTest(unittest.TestCase):
    def test_sixteen_cells(self):
        book = specialized_cell_book()
        self.assertEqual(set(book.keys()), {"day", "night"})
        for sess in ("day", "night"):
            self.assertEqual(set(book[sess].keys()), set(PV8))
            for pv, cell in book[sess].items():
                self.assertEqual(cell["skip_quiet_mode"], "dry")
                self.assertTrue(cell["bias"])
                self.assertIn("hang_lo", cell)
                self.assertIn("early_fill_gamma", cell)
                self.assertIsInstance(cell["block"], list)

    def test_specialized_patches(self):
        # Cell-tune v2（2026-08-06）疊加在 SPECIALIZED_PATCHES 之後、同 key 後者勝。
        book = specialized_cell_book()
        self.assertEqual(book["day"]["contract"]["hang_lo"], 10.0)
        self.assertEqual(book["day"]["contract"]["hang_hi"], 25.0)
        self.assertEqual(book["day"]["normal"]["early_fill_gamma"], 13.0)
        self.assertEqual(book["day"]["climax_up"]["block"], [])  # v2 解除 day climax_up 封鎖
        self.assertEqual(book["day"]["climax_up"]["hang_lo"], 27.0)
        self.assertEqual(book["night"]["expand_dn"]["hang_lo"], 16.0)
        self.assertEqual(book["night"]["expand_dn"]["max_hold_bars"], 16)
        self.assertEqual(book["night"]["expand_dn"]["block"], ["L", "S"])  # v2 封鎖
        self.assertEqual(book["night"]["normal"]["early_fill_gamma"], 9.0)
        self.assertEqual(book["night"]["contract"]["hang_lo"], 16.0)
        # hard rule
        self.assertEqual(book["night"]["climax_up"]["block"], ["L", "S"])

    def test_seed_differs_from_specialized(self):
        seed = freeze_cell_book()
        spec = specialized_cell_book()
        self.assertEqual(seed["day"]["contract"]["hang_lo"], 15.0)
        self.assertNotEqual(
            seed["day"]["contract"]["hang_lo"], spec["day"]["contract"]["hang_lo"]
        )

    def test_session_hhmm(self):
        self.assertEqual(session_from_hhmm("09:00"), "day")
        self.assertEqual(session_from_hhmm("15:01"), "night")
        self.assertEqual(session_from_hhmm("02:00"), "night")

    def test_hhmm_from_iso_bar_t(self):
        from order.tmf_channel_pv16_book import hhmm_from_bar_t

        self.assertEqual(hhmm_from_bar_t("2026-08-06T11:12:00.000+08:00"), "11:12")
        self.assertEqual(session_from_hhmm(hhmm_from_bar_t("2026-08-06T11:12:00.000+08:00")), "day")
        # regression: naive [:5] on ISO was "2026-" → always night
        self.assertEqual(session_from_hhmm("2026-"), "night")
        self.assertEqual(hhmm_from_bar_t("15:30"), "15:30")
        self.assertEqual(session_from_hhmm(hhmm_from_bar_t("2026-08-06T15:30:00+08:00")), "night")

    def test_book_summary_len(self):
        rows = book_summary(specialized_cell_book())
        self.assertEqual(len(rows), 16)

    def test_paper_recipe_has_book(self):
        self.assertEqual(PAPER_RECIPE.get("hang_anchor"), "O")
        self.assertEqual(PAPER_RECIPE.get("skip_quiet_mode"), "dry")
        self.assertEqual(PAPER_RECIPE.get("recipe_version"), RECIPE_VERSION)
        book = PAPER_RECIPE.get("session_pv_book")
        self.assertIsInstance(book, dict)
        self.assertEqual(
            cell_recipe(book, session="night", pv="climax_up").get("block"),
            ["L", "S"],
        )

    def test_load_config_deepcopy_book(self):
        cfg = load_tmf_channel_order_config(
            {
                "strategies": {
                    "tmf-micro-channel": {
                        "enabled": False,
                        "auto_submit": False,
                        "place_every": 5,
                    }
                }
            }
        )
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.recipe_version, RECIPE_VERSION)
        self.assertEqual(cfg.recipe.get("hang_anchor"), "O")
        # mutating loaded book must not touch module PAPER_RECIPE
        cfg.recipe["session_pv_book"]["day"]["normal"]["hang_lo"] = 999.0
        self.assertNotEqual(
            PAPER_RECIPE["session_pv_book"]["day"]["normal"]["hang_lo"], 999.0
        )


if __name__ == "__main__":
    unittest.main()
