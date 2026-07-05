"""sweep_runner · trial registry tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research.sweep_runner import (
    TrialRunRecord,
    config_hash,
    evaluate_gate_rules,
    evaluate_gates,
    run_sweep_grid,
    write_family_index,
    write_trial_run,
)
from research_config import ResearchTopicSpec


class SweepRunnerTests(unittest.TestCase):
    def test_config_hash_stable(self) -> None:
        h1 = config_hash({"alpha": 0.75, "max_slots": 3})
        h2 = config_hash({"max_slots": 3, "alpha": 0.75})
        self.assertEqual(h1, h2)

    def test_evaluate_gates_delta(self) -> None:
        gates = evaluate_gates(
            {"mean_excess_pct": 7.5},
            pass_threshold={"mean_excess_pct_baseline": 7.0, "delta_min_pp": 0.5},
        )
        self.assertEqual(gates["G2_oos_holdout"], "passed")

    def test_evaluate_gate_rules_min_oos_n(self) -> None:
        ok, failures = evaluate_gate_rules({"n_oos": 17, "oos_avg_net_pct": 0.3}, {"min_oos_n": 80})
        self.assertFalse(ok)
        self.assertTrue(any("n_oos" in f for f in failures))

        gates = evaluate_gates(
            {"n_oos": 17, "oos_avg_net_pct": 0.3},
            gate_rules={"min_oos_n": 80, "min_oos_avg_net_pct": 0.0},
            split_spec={"min_oos_n": 80},
        )
        self.assertEqual(gates["G2_oos_holdout"], "failed")

    def test_evaluate_gate_rules_pass(self) -> None:
        gates = evaluate_gates(
            {"n_oos": 127, "oos_avg_net_pct": 0.05},
            gate_rules={"min_oos_n": 80, "min_oos_avg_net_pct": 0.0},
        )
        self.assertEqual(gates["G2_oos_holdout"], "passed")

    def test_run_sweep_grid_writes_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            topic = ResearchTopicSpec(
                topic_id="test-topic",
                title="Test",
                status="active",
                report_dir=tmp,
                split_spec="default_is_oos",
                sweep={"preregistered": True, "fdr_alpha": 0.05},
            )

            def runner(params: dict) -> dict:
                return {"mean_excess_pct": 1.0 if params["x"] == 1 else -1.0}

            runs = run_sweep_grid(
                topic,
                family_id="fam1",
                param_grid=[{"x": 1}, {"x": 2}],
                runner=runner,
            )
            self.assertEqual(len(runs), 2)
            idx = Path(tmp) / "trials" / "fam1" / "_index.json"
            self.assertTrue(idx.is_file())
            payload = json.loads(idx.read_text())
            self.assertEqual(payload["run_count"], 2)
            self.assertEqual(payload["sweep_meta"]["n_comparisons"], 2)
            self.assertEqual(payload["sweep_meta"]["split_spec"], "default_is_oos")


if __name__ == "__main__":
    unittest.main()
