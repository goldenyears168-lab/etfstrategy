"""Load config/research.yaml (Research 層 · 探索性主題 SSOT)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "research.yaml"


@dataclass(frozen=True)
class GraduationGates:
    G1_preregistered_hypothesis: str = "pending"
    G2_oos_holdout: str = "pending"
    G3_regime_stratification: str = "pending"
    G4_rejection_registry: str = "pending"
    G5_frozen_spec: str = "pending"
    G6_adoption_report: str = "pending"


@dataclass(frozen=True)
class GraduationSpec:
    strategy_id: str | None = None
    strategy_ids: tuple[str, ...] = ()
    variant_id: str | None = None
    gates: GraduationGates | None = None
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


@dataclass(frozen=True)
class ResearchConfig:
    version: str
    layer: str
    principles: tuple[str, ...]
    topics: tuple[ResearchTopicSpec, ...]
    graduation_gates: dict[str, dict[str, str]] | None = None

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
    return GraduationGates(
        G1_preregistered_hypothesis=str(raw.get("G1_preregistered_hypothesis", "pending")),
        G2_oos_holdout=str(raw.get("G2_oos_holdout", "pending")),
        G3_regime_stratification=str(raw.get("G3_regime_stratification", "pending")),
        G4_rejection_registry=str(raw.get("G4_rejection_registry", "pending")),
        G5_frozen_spec=str(raw.get("G5_frozen_spec", "pending")),
        G6_adoption_report=str(raw.get("G6_adoption_report", "pending")),
    )


def _parse_graduation(raw: dict | None) -> GraduationSpec | None:
    if not raw:
        return None
    strategy_ids_raw = raw.get("strategy_ids") or []
    champion_artifacts = raw.get("champion_artifacts")
    return GraduationSpec(
        strategy_id=raw.get("strategy_id"),
        strategy_ids=tuple(str(x) for x in strategy_ids_raw),
        variant_id=raw.get("variant_id"),
        gates=_parse_gates(raw.get("gates")),
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
    )
