"""project_config 單一真相來源測試。"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from project_config import (
    BENCHMARK_CODES,
    DEFAULT_ETF_CODES,
    ETF_CODES_BY_SOURCE,
    ETF_CODES_HOLDINGS,
    ETF_CODES_LISTED,
    SCORE_VERSION,
    csv_codes,
    parse_etf_codes,
    shell_export,
)

SRC = Path(__file__).resolve().parent.parent / "src"


class ProjectConfigTests(unittest.TestCase):
    def test_listed_codes_match_holdings_minus_unlisted(self) -> None:
        extra = set(ETF_CODES_HOLDINGS) - set(ETF_CODES_LISTED)
        self.assertEqual(extra, {"00407A"})

    def test_sources_cover_all_holdings(self) -> None:
        from_sources = set()
        for codes in ETF_CODES_BY_SOURCE.values():
            from_sources.update(codes)
        self.assertEqual(from_sources, set(ETF_CODES_HOLDINGS))

    def test_default_etf_codes_alias(self) -> None:
        self.assertEqual(DEFAULT_ETF_CODES, ETF_CODES_LISTED)

    def test_parse_etf_codes_empty_uses_default(self) -> None:
        self.assertEqual(parse_etf_codes(None), ETF_CODES_LISTED)
        self.assertEqual(parse_etf_codes(""), ETF_CODES_LISTED)

    def test_parse_etf_codes_custom(self) -> None:
        self.assertEqual(parse_etf_codes("2330, 2454"), ("2330", "2454"))

    def test_shell_export_contains_required_vars(self) -> None:
        out = shell_export()
        for key in (
            "ETF_CODES=",
            "ETF_CODES_HOLDINGS=",
            "ETF_CODES_EZMONEY=",
            "BENCHMARK_CODES=",
        ):
            self.assertIn(key, out)

    def test_cli_etf_codes_listed(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SRC / "project_config.py"), "etf-codes-listed"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), csv_codes(ETF_CODES_LISTED))

    def test_score_version(self) -> None:
        self.assertEqual(SCORE_VERSION, "p4-v2")

    def test_benchmark_codes(self) -> None:
        self.assertEqual(BENCHMARK_CODES, ("IX0001", "IR0002"))


if __name__ == "__main__":
    unittest.main()
