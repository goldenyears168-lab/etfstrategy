"""TW100 WFA analysis tests (no DB / qlib required)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research.backtest.archive.tw100_wfa_analysis import (
    build_ic_tear_sheet,
    build_regime_stratification,
    load_wfa_payload,
    render_wfa_analysis_markdown,
    Tw100WfaAnalysisConfig,
)


SAMPLE_WFA = {
    "schema": "tw100_alpha158_lgbm_wfa-v1",
    "model": "LightGBM",
    "features": "Alpha158",
    "aggregate_test_ic": {"n_days": 3, "mean": 0.01, "icir": 0.1},
    "folds": [
        {
            "fold_id": 0,
            "test_start": "2017-02-13",
            "test_end": "2017-07-07",
            "best_iteration": 34,
            "valid_ic": {"n_days": 99, "mean": 0.02, "icir": 0.17},
            "test_ic": {"n_days": 97, "mean": 0.01, "icir": 0.09},
            "test_ic_daily": [
                {"date": "2017-02-13", "ic": 0.05},
                {"date": "2017-02-14", "ic": -0.01},
            ],
        }
    ],
}


class Tw100WfaAnalysisTests(unittest.TestCase):
    def test_build_ic_tear_sheet(self) -> None:
        tear = build_ic_tear_sheet(SAMPLE_WFA)
        self.assertEqual(tear["n_folds"], 1)
        self.assertAlmostEqual(tear["valid_to_test_decay_mean"], 0.5, places=4)
        self.assertEqual(tear["best_iteration_mean"], 34.0)

    def test_regime_skipped_without_daily_ic(self) -> None:
        payload = {**SAMPLE_WFA, "folds": [{"fold_id": 0, "valid_ic": {}, "test_ic": {}}]}
        cfg = Tw100WfaAnalysisConfig(wfa_json=Path("dummy.json"))
        out = build_regime_stratification(payload, None, cfg)  # type: ignore[arg-type]
        self.assertEqual(out["status"], "skipped")

    def test_load_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wfa.json"
            path.write_text(json.dumps(SAMPLE_WFA), encoding="utf-8")
            payload = load_wfa_payload(path)
            md = render_wfa_analysis_markdown(
                {"ic_tear_sheet": build_ic_tear_sheet(payload), "regime_stratification": {"status": "skipped"}}
            )
            self.assertIn("IC tear sheet", md)
            self.assertIn("LightGBM", md)


if __name__ == "__main__":
    unittest.main()
