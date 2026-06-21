"""Tests for research HTML path helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import report_paths as rp
from report_paths import (
    RESEARCH_RRG,
    classify_research_html_filename,
    organize_research_html,
    research_html_path,
    write_research_html_redirects,
)


def _patch_research_roots(tmp_path: Path):
    research = tmp_path / "research"
    breadth = research / "breadth"
    rrg = research / "rrg"
    copytrade = research / "00981a-copytrade"
    vcp = research / "vcp"
    breadth.mkdir(parents=True)
    return mock.patch.multiple(
        rp,
        REPORTS_ROOT=tmp_path,
        REPORTS_RESEARCH=research,
        RESEARCH_BREADTH=breadth,
        RESEARCH_RRG=rrg,
        RESEARCH_COPYTRADE_00981A=copytrade,
        RESEARCH_VCP=vcp,
        RESEARCH_HTML_DIRS={
            "breadth": breadth,
            "rrg": rrg,
            "00981a-copytrade": copytrade,
            "vcp": vcp,
        },
    )


class ReportPathsTests(unittest.TestCase):
    def test_classify_research_html_filename(self) -> None:
        self.assertEqual(
            classify_research_html_filename("20260620_market_breadth_ma_2024_2026.html"),
            "breadth",
        )
        self.assertEqual(
            classify_research_html_filename("20260620_rrg_mono_hold7_slots_rrg_timeline.html"),
            "rrg",
        )
        self.assertEqual(
            classify_research_html_filename("20260620_00981a_l1h9_slots_rrg_timeline.html"),
            "00981a-copytrade",
        )
        self.assertIsNone(classify_research_html_filename("strategy_hub.html"))

    def test_research_html_path_categories(self) -> None:
        p = research_html_path("rrg", "demo.html")
        self.assertEqual(p.parent, RESEARCH_RRG)
        self.assertEqual(p.name, "demo.html")

    def test_organize_moves_stray_html(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            with _patch_research_roots(tmp_path):
                research = tmp_path / "research"
                breadth = research / "breadth"
                breadth.mkdir(parents=True, exist_ok=True)
                stray = research / "20260620_market_breadth_ma_2024_2026.html"
                stray.write_text("<html></html>", encoding="utf-8")

                moves = organize_research_html()
                self.assertEqual(len(moves), 1)
                dest = breadth / stray.name
                self.assertTrue(dest.is_file())
                self.assertFalse(stray.exists())

    def test_write_research_html_redirects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            with _patch_research_roots(tmp_path):
                research = tmp_path / "research"
                breadth = research / "breadth"
                breadth.mkdir(parents=True, exist_ok=True)
                canonical = breadth / "demo_breadth.html"
                canonical.write_text("<html>ok</html>", encoding="utf-8")

                stubs = write_research_html_redirects()
                self.assertEqual(len(stubs), 1)
                alias = research / "demo_breadth.html"
                self.assertTrue(alias.is_symlink())
                self.assertEqual(alias.resolve(), canonical.resolve())


if __name__ == "__main__":
    unittest.main()
