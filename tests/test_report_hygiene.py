"""report_hygiene：移除 legacy 產物。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from report_hygiene import legacy_report_paths, prune_legacy_reports


class ReportHygieneTests(unittest.TestCase):
    def test_prune_legacy_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ensemble_digest.md").write_text("old", encoding="utf-8")
            (root / "20260616_ensemble_digest.md").write_text("old", encoding="utf-8")
            (root / "20260616_order_intents.md").write_text("old", encoding="utf-8")
            (root / "research_digest.md").write_text("keep", encoding="utf-8")

            removed = prune_legacy_reports(root)
            self.assertIn("ensemble_digest.md", removed)
            self.assertIn("20260616_ensemble_digest.md", removed)
            self.assertIn("20260616_order_intents.md", removed)
            self.assertTrue((root / "research_digest.md").is_file())
            self.assertEqual(legacy_report_paths(root), [])


if __name__ == "__main__":
    unittest.main()
