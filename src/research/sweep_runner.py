"""Research sweep runner · trial registry (ML4Trading-style taxonomy).

Taxonomy: topic → trial_family → trial → run
Artifacts: reports/research/{topic_id}/trials/{family_id}/{run_id}.json
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from research_config import ResearchTopicSpec, load_research_config, load_research_splits
from stock_db import PROJECT_ROOT

_TPE = ZoneInfo("Asia/Taipei")
DEFAULT_TRIALS_ROOT = PROJECT_ROOT / "reports" / "research"


@dataclass
class TrialRunRecord:
    run_id: str
    topic_id: str
    family_id: str
    trial_id: str
    status: str  # pending | pass | fail | error
    variant_params: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    gate_results: dict[str, str] = field(default_factory=dict)
    artifact_path: str | None = None
    config_hash: str | None = None
    hypothesis_ids: tuple[str, ...] = ()
    created_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hypothesis_ids"] = list(self.hypothesis_ids)
        return d


def config_hash(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def trials_dir(topic_id: str, family_id: str, report_dir: str | None = None) -> Path:
    base = Path(report_dir) if report_dir else DEFAULT_TRIALS_ROOT / topic_id
    return base / "trials" / family_id


def write_trial_run(record: TrialRunRecord, *, report_dir: str | None = None) -> Path:
    out_dir = trials_dir(record.topic_id, record.family_id, report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{record.run_id}.json"
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_family_index(
    topic_id: str,
    family_id: str,
    runs: list[TrialRunRecord],
    *,
    report_dir: str | None = None,
    sweep_meta: dict[str, Any] | None = None,
) -> Path:
    out_dir = trials_dir(topic_id, family_id, report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "topic_id": topic_id,
        "family_id": family_id,
        "run_count": len(runs),
        "sweep_meta": sweep_meta or {},
        "runs": [r.to_dict() for r in runs],
        "updated_at": datetime.now(_TPE).isoformat(timespec="seconds"),
    }
    path = out_dir / "_index.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _metric_float(metrics: dict[str, Any], key: str) -> float | None:
    val = metrics.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def evaluate_gate_rules(
    metrics: dict[str, Any],
    gate_rules: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Evaluate structured gate_rules from hypothesis or sweep config."""
    if not gate_rules:
        return True, []
    failures: list[str] = []

    n_oos = _metric_float(metrics, "n_oos")
    min_oos_n = gate_rules.get("min_oos_n")
    if min_oos_n is not None and n_oos is not None and n_oos < float(min_oos_n):
        failures.append(f"n_oos={n_oos} < min_oos_n={min_oos_n}")

    oos_avg = _metric_float(metrics, "oos_avg_net_pct")
    min_oos_avg = gate_rules.get("min_oos_avg_net_pct")
    if min_oos_avg is not None and oos_avg is not None and oos_avg < float(min_oos_avg):
        failures.append(f"oos_avg_net_pct={oos_avg} < min={min_oos_avg}")

    mean_ex = _metric_float(metrics, "mean_excess_pct")
    min_mean_ex = gate_rules.get("min_mean_excess_pct")
    if min_mean_ex is not None and mean_ex is not None and mean_ex < float(min_mean_ex):
        failures.append(f"mean_excess_pct={mean_ex} < min={min_mean_ex}")

    delta = _metric_float(metrics, "delta_vs_baseline_pp")
    min_delta = gate_rules.get("min_delta_vs_baseline_pp")
    if min_delta is not None and delta is not None and delta < float(min_delta):
        failures.append(f"delta_vs_baseline_pp={delta} < min={min_delta}")

    ci_lo = _metric_float(metrics, "bootstrap_ci95_lo")
    min_ci = gate_rules.get("min_bootstrap_ci95_lo")
    if min_ci is not None and ci_lo is not None and ci_lo <= float(min_ci):
        failures.append(f"bootstrap_ci95_lo={ci_lo} <= min={min_ci}")

    win_delta = _metric_float(metrics, "win_rate_delta_pp")
    min_win_delta = gate_rules.get("min_win_rate_delta_pp")
    if min_win_delta is not None and win_delta is not None and win_delta < float(min_win_delta):
        failures.append(f"win_rate_delta_pp={win_delta} < min={min_win_delta}")

    n_ratio = _metric_float(metrics, "n_reduction_ratio")
    max_n_ratio = gate_rules.get("max_n_reduction_ratio")
    if max_n_ratio is not None and n_ratio is not None and n_ratio >= float(max_n_ratio):
        failures.append(f"n_reduction_ratio={n_ratio} >= max={max_n_ratio}")

    return len(failures) == 0, failures


def evaluate_gates(
    metrics: dict[str, Any],
    *,
    pass_threshold: dict[str, Any] | None = None,
    gate_rules: dict[str, Any] | None = None,
    split_spec: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Evaluate G2_oos_holdout and optional structured gate_rules."""
    gates: dict[str, str] = {"G2_oos_holdout": "pending"}
    if not metrics:
        return gates
    if metrics.get("error"):
        gates["G2_oos_holdout"] = "error"
        return gates

    merged_rules: dict[str, Any] = dict(gate_rules or {})
    if split_spec and split_spec.get("min_oos_n") is not None:
        merged_rules.setdefault("min_oos_n", split_spec["min_oos_n"])

    rules_ok, rule_failures = evaluate_gate_rules(metrics, merged_rules or None)

    mean_ex = _metric_float(metrics, "mean_excess_pct")
    baseline_ex = (pass_threshold or {}).get("mean_excess_pct_baseline")
    delta_min = (pass_threshold or {}).get("delta_min_pp", 0.0)

    threshold_ok = False
    if mean_ex is not None and baseline_ex is not None:
        threshold_ok = mean_ex >= float(baseline_ex) + float(delta_min)
    elif mean_ex is not None and mean_ex > 0:
        threshold_ok = True
    elif _metric_float(metrics, "oos_avg_net_pct") is not None:
        oos_avg = _metric_float(metrics, "oos_avg_net_pct")
        threshold_ok = oos_avg is not None and oos_avg > 0

    if merged_rules:
        gates["G2_oos_holdout"] = "passed" if rules_ok else "failed"
    elif threshold_ok:
        gates["G2_oos_holdout"] = "passed"
    else:
        gates["G2_oos_holdout"] = "failed"

    if rule_failures:
        gates["gate_rule_notes"] = "; ".join(rule_failures)
    return gates


def resolve_split_spec(topic: ResearchTopicSpec) -> dict[str, Any] | None:
    """Resolve topic.split_spec id to split spec dict."""
    if not topic.split_spec:
        return None
    splits = load_research_splits()
    spec = splits.get(topic.split_spec)
    if spec is None:
        return None
    return {
        "split_id": spec.split_id,
        "method": spec.method,
        "ratio": spec.ratio,
        "min_oos_n": spec.min_oos_n,
        "train_years": spec.train_years,
        "test_years": spec.test_years,
        "train_days": spec.train_days,
        "test_days": spec.test_days,
        "embargo_days": spec.embargo_days,
        "holdout_start": spec.holdout_start,
        "holdout_end": spec.holdout_end,
        "cost_model_rt_pct": spec.cost_model_rt_pct,
    }


def run_sweep_grid(
    topic: ResearchTopicSpec,
    *,
    family_id: str,
    param_grid: list[dict[str, Any]],
    runner: Callable[[dict[str, Any]], dict[str, Any]],
    pass_threshold: dict[str, Any] | None = None,
    gate_rules: dict[str, Any] | None = None,
    hypothesis_ids: tuple[str, ...] = (),
) -> list[TrialRunRecord]:
    """Execute a preregistered parameter grid; one TrialRunRecord per cell."""
    split_spec = resolve_split_spec(topic)
    runs: list[TrialRunRecord] = []
    n_comparisons = len(param_grid)
    for i, params in enumerate(param_grid, start=1):
        run_id = f"{family_id}-{i:03d}-{config_hash(params)}"
        created = datetime.now(_TPE).isoformat(timespec="seconds")
        try:
            metrics = runner(params)
            gates = evaluate_gates(
                metrics,
                pass_threshold=pass_threshold,
                gate_rules=gate_rules,
                split_spec=split_spec,
            )
            status = "pass" if gates.get("G2_oos_holdout") == "passed" else "fail"
        except Exception as exc:  # noqa: BLE001 — record sweep cell errors
            metrics = {"error": str(exc)}
            gates = {"G2_oos_holdout": "error"}
            status = "error"

        record = TrialRunRecord(
            run_id=run_id,
            topic_id=topic.topic_id,
            family_id=family_id,
            trial_id=f"{family_id}:{config_hash(params)}",
            status=status,
            variant_params=params,
            metrics=metrics,
            gate_results=gates,
            config_hash=config_hash(params),
            hypothesis_ids=hypothesis_ids,
            created_at=created,
        )
        path = write_trial_run(record, report_dir=topic.report_dir)
        record.artifact_path = str(path)
        runs.append(record)

    sweep_meta: dict[str, Any] = {
        "preregistered": bool(topic.sweep and topic.sweep.get("preregistered")),
        "param_grid_size": len(param_grid),
        "n_comparisons": n_comparisons,
    }
    if topic.split_spec:
        sweep_meta["split_spec"] = topic.split_spec
    if topic.sweep and topic.sweep.get("fdr_alpha") is not None:
        sweep_meta["fdr_alpha"] = topic.sweep.get("fdr_alpha")

    write_family_index(
        topic.topic_id,
        family_id,
        runs,
        report_dir=topic.report_dir,
        sweep_meta=sweep_meta,
    )
    return runs


def load_topic(topic_id: str):
    cfg = load_research_config()
    topic = cfg.get(topic_id)
    if topic is None:
        raise KeyError(f"unknown research topic: {topic_id}")
    return topic
