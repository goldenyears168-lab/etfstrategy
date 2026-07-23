"""Combo page DOM prefix · script getElementById sync."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "research" / "archive"))

from render_rrg_intro_champion_combo_html import prefix_script_for_body


class PrefixScriptForBodyTests(unittest.TestCase):
    def test_script_gets_matching_prefix(self) -> None:
        body = (
            '<svg><g id="dynamic-layer"></g></svg>'
            '<div id="quad-filters"></div><span id="frame-date"></span>'
        )
        script = (
            "const layer = document.getElementById('dynamic-layer');"
            "document.querySelectorAll('#quad-filters button')"
        )
        new_body, new_script = prefix_script_for_body(body, script, "u-")
        self.assertIn('id="u-dynamic-layer"', new_body)
        self.assertIn("getElementById('u-dynamic-layer')", new_script)
        self.assertNotIn("getElementById('dynamic-layer')", new_script)
        self.assertIn("#u-quad-filters", new_script)


if __name__ == "__main__":
    unittest.main()
