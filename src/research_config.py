"""Load config/research.yaml (Research 層 · 探索性主題 SSOT)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "research.yaml"
DEFAULT_SPLITS_CONFIG = PROJECT_ROOT / "config" / "research_splits.yaml"

_STANDARD_GATE_KEYS = frozenset(
    {
        "G1_preregistered_hypothesis",
        "G2_oos_holdout",
        "G3_regime_stratification",
        "G4_rejection_registry",
        "G5_frozen_spec",
        "G6_adoption_report",
    }
)


@dataclass(frozen=True)
class GraduationGates:
    G1_preregistered_hypothesis: str = "pending"
    G2_oos_holdout: str = "pending"
    G3_regime_stratification: str = "pending"
    G4_rejection_registry: str = "pending"
    G5_frozen_spec: str = "pending"
    G6_adoption_report: str = "pending"
    extra_gates: dict[str, str] | None = None


@dataclass(frozen=True)
class GraduationSpec:
    strategy_id: str | None = None
    strategy_ids: tuple[str, ...] = ()
    variant_id: str | None = None
    gates: GraduationGates | None = None
    extra_gates: dict[str, str] | None = None
    champion_artifact: str | None = None
    champion_artifacts: dict[str, str] | None = None
    pass_threshold: str | None = None


@dataclass(frozen=True)
class ResearchTopicSpec:
    topic_id: str
    title: str
    status: str
    phase: str = "hypothesis"
    description: str = ""
    run_scripts: tuple[str, ...] = ()
    methodology: str | None = None
    report_dir: str | None = None
    config_ref: str | None = None
    archive_path: str | None = None
    parent_strategy: str | None = None
    baseline_strategy: str | None = None
    graduated_strategy: str | None = None
    graduated_strategies: tuple[str, ...] = ()
    graduation: GraduationSpec | None = None
    hypotheses: tuple[dict[str, Any], ...] = ()
    sweep: dict[str, Any] | None = None
    split_spec: str | None = None
    artifacts: dict[str, str] | None = None


@dataclass(frozen=True)
class SplitSpec:
    split_id: str
    method: str
    ratio: float | None = None
    min_oos_n: int | None = None
    train_years: int | None = None
    test_years: int | None = None
    train_days: int | None = None
    test_days: int | None = None
    embargo_days: int | None = None
    train_months: int | None = None
    test_months: int | None = None
    embargo_months: int | None = None
    holdout_start: str | None = None
    holdout_end: str | None = None
    cost_model_rt_pct: float | None = None
    description: str | None = None


@dataclass(frozen=True)
class ResearchSplitsConfig:
    version: str
    split_specs: dict[str, SplitSpec]

    def get(self, split_id: str) -> SplitSpec | None:
        return self.split_specs.get(split_id)


@dataclass(frozen=True)
class ResearchConfig:
    version: str
    layer: str
    principles: tuple[str, ...]
    topics: tuple[ResearchTopicSpec, ...]
    graduation_gates: dict[str, dict[str, str]] | None = None
    splits_ref: str | None = None

    def get(self, topic_id: str) -> ResearchTopicSpec | None:
        for t in self.topics:
            if t.topic_id == topic_id:
                return t
        return None

    def topic_ids(self) -> tuple[str, ...]:
        return tuple(t.topic_id for t in self.topics)


def _parse_gates(raw: dict | None) -> GraduationGates | None:
    if not raw:
        return None
    extra: dict[str, str] = {}
    for key, val in raw.items():
        if key not in _STANDARD_GATE_KEYS:
            extra[key] = str(val)
    explicit_extra = raw.get("extra_gates")
    if isinstance(explicit_extra, dict):
        for key, val in explicit_extra.items():
            extra[str(key)] = str(val)
    return GraduationGates(
        G1_preregistered_hypothesis=str(raw.get("G1_preregistered_hypothesis", "pending")),
        G2_oos_holdout=str(raw.get("G2_oos_holdout", "pending")),
        G3_regime_stratification=str(raw.get("G3_regime_stratification", "pending")),
        G4_rejection_registry=str(raw.get("G4_rejection_registry", "pending")),
        G5_frozen_spec=str(raw.get("G5_frozen_spec", "pending")),
        G6_adoption_report=str(raw.get("G6_adoption_report", "pending")),
        extra_gates=extra or None,
    )


def _parse_graduation(raw: dict | None) -> GraduationSpec | None:
    if not raw:
        return None
    strategy_ids_raw = raw.get("strategy_ids") or []
    champion_artifacts = raw.get("champion_artifacts")
    gates = _parse_gates(raw.get("gates"))
    extra_gates = raw.get("extra_gates")
    merged_extra: dict[str, str] | None = None
    if isinstance(extra_gates, dict):
        merged_extra = {str(k): str(v) for k, v in extra_gates.items()}
    elif gates and gates.extra_gates:
        merged_extra = dict(gates.extra_gates)
    return GraduationSpec(
        strategy_id=raw.get("strategy_id"),
        strategy_ids=tuple(str(x) for x in strategy_ids_raw),
        variant_id=raw.get("variant_id"),
        gates=gates,
        extra_gates=merged_extra,
        champion_artifact=raw.get("champion_artifact"),
        champion_artifacts=champion_artifacts if isinstance(champion_artifacts, dict) else None,
        pass_threshold=raw.get("pass_threshold"),
    )


def _parse_topic(topic_id: str, raw: dict) -> ResearchTopicSpec:
    run_scripts = raw.get("run_scripts") or []
    graduated = raw.get("graduated_strategies") or []
    grad = _parse_graduation(raw.get("graduation"))
    hypotheses_raw = raw.get("hypotheses") or []
    hypotheses = tuple(h for h in hypotheses_raw if isinstance(h, dict))
    sweep = raw.get("sweep") if isinstance(raw.get("sweep"), dict) else None
    artifacts_raw = raw.get("artifacts")
    artifacts = (
        {str(k): str(v) for k, v in artifacts_raw.items()}
        if isinstance(artifacts_raw, dict)
        else None
    )

    graduated_strategy = raw.get("graduated_strategy")
    if not graduated_strategy and grad and grad.strategy_id:
        graduated_strategy = grad.strategy_id

    return ResearchTopicSpec(
        topic_id=topic_id,
        title=str(raw.get("title") or topic_id),
        status=str(raw.get("status") or "active"),
        phase=str(raw.get("phase") or "hypothesis"),
        description=str(raw.get("description", "")).strip(),
        run_scripts=tuple(str(x) for x in run_scripts),
        methodology=raw.get("methodology"),
        report_dir=raw.get("report_dir"),
        config_ref=raw.get("config_ref"),
        archive_path=raw.get("archive_path"),
        parent_strategy=raw.get("parent_strategy"),
        baseline_strategy=raw.get("baseline_strategy"),
        graduated_strategy=graduated_strategy,
        graduated_strategies=tuple(str(x) for x in graduated),
        graduation=grad,
        hypotheses=hypotheses,
        sweep=sweep,
        split_spec=raw.get("split_spec"),
        artifacts=artifacts,
    )


def _parse_split_spec(split_id: str, raw: dict) -> SplitSpec:
    return SplitSpec(
        split_id=split_id,
        method=str(raw.get("method") or ""),
        ratio=float(raw["ratio"]) if raw.get("ratio") is not None else None,
        min_oos_n=int(raw["min_oos_n"]) if raw.get("min_oos_n") is not None else None,
        train_years=int(raw["train_years"]) if raw.get("train_years") is not None else None,
        test_years=int(raw["test_years"]) if raw.get("test_years") is not None else None,
        train_days=int(raw["train_days"]) if raw.get("train_days") is not None else None,
        test_days=int(raw["test_days"]) if raw.get("test_days") is not None else None,
        embargo_days=int(raw["embargo_days"]) if raw.get("embargo_days") is not None else None,
        train_months=int(raw["train_months"]) if raw.get("train_months") is not None else None,
        test_months=int(raw["test_months"]) if raw.get("test_months") is not None else None,
        embargo_months=int(raw["embargo_months"]) if raw.get("embargo_months") is not None else None,
        holdout_start=raw.get("holdout_start"),
        holdout_end=raw.get("holdout_end"),
        cost_model_rt_pct=(
            float(raw["cost_model_rt_pct"]) if raw.get("cost_model_rt_pct") is not None else None
        ),
        description=raw.get("description"),
    )


def load_research_splits(path: Path | None = None) -> ResearchSplitsConfig:
    p = path or DEFAULT_SPLITS_CONFIG
    if not p.is_file():
        return ResearchSplitsConfig(version="research-splits-v0", split_specs={})
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    specs_raw = raw.get("split_specs") or {}
    specs: dict[str, SplitSpec] = {}
    if isinstance(specs_raw, dict):
        for sid, body in specs_raw.items():
            if isinstance(body, dict):
                specs[str(sid)] = _parse_split_spec(str(sid), body)
    return ResearchSplitsConfig(
        version=str(raw.get("version") or "research-splits-v1"),
        split_specs=specs,
    )


def load_research_config(path: Path | None = None) -> ResearchConfig:
    p = path or DEFAULT_CONFIG
    if not p.is_file():
        return ResearchConfig(
            version="research-v0",
            layer="research",
            principles=(),
            topics=(),
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return ResearchConfig(
            version="research-v0",
            layer="research",
            principles=(),
            topics=(),
        )
    principles_raw = raw.get("principles") or []
    topics_raw = raw.get("topics") or {}
    topics = tuple(
        _parse_topic(tid, body)
        for tid, body in topics_raw.items()
        if isinstance(body, dict)
    )
    grad_gates = raw.get("graduation_gates")
    if not isinstance(grad_gates, dict):
        grad_gates = None
    return ResearchConfig(
        version=str(raw.get("version") or "research-v1"),
        layer=str(raw.get("layer") or "research"),
        principles=tuple(str(x) for x in principles_raw),
        topics=topics,
        graduation_gates=grad_gates,
        splits_ref=raw.get("splits_ref"),
    )
