"""migrate_market_labels 單元測試。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from market_labels import (
    CHIP_FOREIGN_BUY,
    ENTRY_BREAKOUT,
    ENTRY_TAG_VOLUME,
    PM_OBSERVE,
    WL_GENERAL,
    WL_PRIMARY,
)
from migrate_market_labels import (
    migrate_entry_tags_json,
    migrate_market_labels,
    migrate_metadata_json,
    migrate_note_text,
)
from stock_db import connect, upsert_pm_watchlist


class TestMigrateHelpers(unittest.TestCase):
    def test_entry_tags_json(self) -> None:
        raw = json.dumps(["STRONG_TREND"])
        new, changed = migrate_entry_tags_json(raw)
        self.assertTrue(changed)
        self.assertEqual(json.loads(new or "[]"), [ENTRY_TAG_VOLUME])

    def test_metadata_json(self) -> None:
        raw = json.dumps({"entry_signal": "BREAKOUT", "chip_tag": "外資確認"})
        new, changed = migrate_metadata_json(raw)
        self.assertTrue(changed)
        meta = json.loads(new or "{}")
        self.assertEqual(meta["entry_signal"], ENTRY_BREAKOUT)
        self.assertEqual(meta["chip_tag"], CHIP_FOREIGN_BUY)

    def test_note_text_no_double_neutral(self) -> None:
        new, changed = migrate_note_text("法人中性 · 乖離過大 · 不列入")
        self.assertFalse(changed)
        self.assertEqual(new, "法人中性 · 乖離過大 · 不列入")

    def test_note_text_fix_corrupted(self) -> None:
        new, changed = migrate_note_text("法人法人中性 · 突破 · 一般觀察")
        self.assertTrue(changed)
        self.assertEqual(new, "法人中性 · 突破 · 一般觀察")

    def test_note_text_legacy(self) -> None:
        new, changed = migrate_note_text("三方共振 · OVEREXTENDED · B")
        self.assertTrue(changed)
        self.assertIn("外資、投信同步買超", new or "")
        self.assertIn("乖離過大", new or "")
        self.assertIn("一般觀察", new or "")


class TestMigrateDb(unittest.TestCase):
    def test_migrate_pm_watchlist_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": "2026-06-04",
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "investment_score": 70.0,
                        "watchlist": "B",
                        "entry_signal": "OVEREXTENDED",
                        "entry_tags_json": json.dumps(["STRONG_TREND"]),
                        "chip_tag": "三方共振",
                        "pm_bucket": "RESEARCH",
                        "flow_score": 60.0,
                        "chip_score": 80.0,
                        "tech_score": 55.0,
                        "catalyst_score": 50.0,
                        "fundamental_score": 50.0,
                        "note": "三方共振 · OVEREXTENDED · B",
                    }
                ],
            )
            stats = migrate_market_labels(conn, dry_run=False)
            self.assertGreater(stats.total(), 0)
            row = conn.execute(
                "SELECT * FROM pm_watchlist WHERE stock_id='2330'"
            ).fetchone()
            self.assertEqual(row["watchlist"], WL_GENERAL)
            self.assertEqual(row["pm_bucket"], PM_OBSERVE)
            self.assertEqual(json.loads(row["entry_tags_json"]), [ENTRY_TAG_VOLUME])
            conn.close()


if __name__ == "__main__":
    unittest.main()
