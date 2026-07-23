"""Unit tests for expert_pool_pattern_risk annotation."""

from __future__ import annotations

import unittest

from research.expert_pool_pattern_risk import format_pattern_risk_lines


class TestFormatPatternRisk(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(format_pattern_risk_lines(None), [])
        self.assertEqual(format_pattern_risk_lines({"flags": []}), [])

    def test_block(self) -> None:
        lines = format_pattern_risk_lines(
            {
                "flags": [
                    {
                        "id": "T_REJECT",
                        "level": "sid_strong",
                        "text": "T_REJECT：demo",
                    }
                ]
            }
        )
        text = "\n".join(lines)
        self.assertIn("專家有共識但圖形有風險", text)
        self.assertIn("T_REJECT：demo", text)
        self.assertIn("非自動擋單", text)


if __name__ == "__main__":
    unittest.main()
