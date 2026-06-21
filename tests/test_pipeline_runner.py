"""Pipeline YAML loader."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipelines.run_nodes import _env_enabled, list_phase_nodes

PIPELINE = ROOT / "config" / "pipelines" / "daily_close.yaml"


class TestPipelineRunner(unittest.TestCase):
    def test_ingest_holdings_includes_etf_and_regime_reports(self) -> None:
        nodes = list_phase_nodes(PIPELINE, "ingest_holdings")
        ids = [n.node_id for n in nodes]
        self.assertIn("etf_daily_report", ids)
        self.assertIn("regime_daily_brief", ids)

    def test_env_flag_defaults_to_on(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_env_enabled("RUN_VCP_FUNNEL_SPECS", "1"))

    def test_env_flag_respects_zero(self) -> None:
        with mock.patch.dict(os.environ, {"ENABLE_FINMIND_SIGNAL": "0"}, clear=True):
            self.assertFalse(_env_enabled("ENABLE_FINMIND_SIGNAL", "1"))


if __name__ == "__main__":
    unittest.main()
