"""Tests for C18acc extension overlay · Round3 I36 defaults."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from c18acc_extension_overlay import (
    DEFAULT_EXIT_MODE,
    DEFAULT_MIN_FADE_PCT,
    DEFAULT_MIN_SPIKE_PCT,
    DEFAULT_POLL_INTERVAL_MIN,
    overlay_scan_config,
    poll_interval_for_now,
)


class TestC18accExtensionOverlay(unittest.TestCase):
    def test_i36_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = overlay_scan_config()
            self.assertEqual(cfg.exit_mode, DEFAULT_EXIT_MODE)
            self.assertEqual(cfg.exit_mode, "combo_spike")
            self.assertEqual(cfg.min_ext_prev_pct, DEFAULT_MIN_SPIKE_PCT)
            self.assertEqual(cfg.min_ext_prev_pct, 4.0)
            self.assertEqual(cfg.min_fade_from_high_pct, DEFAULT_MIN_FADE_PCT)
            self.assertFalse(cfg.require_opening_spike)
            self.assertEqual(poll_interval_for_now(), DEFAULT_POLL_INTERVAL_MIN)

    def test_env_override(self) -> None:
        env = {
            "C18ACC_EXTENSION_MODE": "combo_b",
            "C18ACC_EXTENSION_MIN_SPIKE_PCT": "5",
            "C18ACC_EXTENSION_MIN_FADE_PCT": "1.5",
            "C18ACC_EXTENSION_POLL_MIN": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = overlay_scan_config()
            self.assertEqual(cfg.exit_mode, "combo_b")
            self.assertEqual(cfg.min_ext_prev_pct, 5.0)
            self.assertEqual(cfg.min_fade_from_high_pct, 1.5)


if __name__ == "__main__":
    unittest.main()
