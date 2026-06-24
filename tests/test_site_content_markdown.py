"""Tests for site_content sync · registry frontmatter (§7.4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class TestSiteContentSyncOptionalFields(unittest.TestCase):
    def test_frontmatter_registry_fields(self) -> None:
        from site_content_sync import _page_from_file

        text = """---
page_id: strategy_test
layer_id: strategy
strategy_id: test-slug
title: T
tab_label_zh: T
tab_label_en: T
sort_order: 1
research_page_id: research_case_test
brief_types:
  - copytrade_l1h9
icon: ri-test
description_short: short
---
body
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as tmp:
            tmp.write(text)
            path = Path(tmp.name)
        try:
            page = _page_from_file(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(page.strategy_id, "test-slug")
        self.assertEqual(page.research_page_id, "research_case_test")
        self.assertEqual(page.brief_types, ["copytrade_l1h9"])
        self.assertEqual(page.icon, "ri-test")

    def test_omitted_registry_fields_stay_none(self) -> None:
        from site_content_sync import _page_from_file

        text = """---
page_id: layer_facts
layer_id: facts
title: Facts
tab_label_zh: 事實
tab_label_en: Facts
sort_order: 1
---
body
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as tmp:
            tmp.write(text)
            path = Path(tmp.name)
        try:
            page = _page_from_file(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertIsNone(page.strategy_id)
        self.assertIsNone(page.research_page_id)


if __name__ == "__main__":
    unittest.main()
