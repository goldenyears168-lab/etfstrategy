"""Load config/research.yaml (Research 層 · 探索性主題 SSOT)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "research.yaml"


@dataclass(frozen=True)
class ResearchTopicSpec:
    topic_id: str
    title: str
    status: str
    description: str = ""
    run_scripts: tuple[str, ...] = ()
    methodology: str | None = None
    report_dir: str | None = None
    config_ref: str | None = None
    archive_path: str | None = None
    graduated_strategy: str | None = None
    graduated_strategies: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchConfig:
    version: str
    layer: str
    principles: tuple[str, ...]
    topics: tuple[ResearchTopicSpec, ...]

    def get(self, topic_id: str) -> ResearchTopicSpec | None:
        for t in self.topics:
            if t.topic_id == topic_id:
                return t
        return None

    def topic_ids(self) -> tuple[str, ...]:
        return tuple(t.topic_id for t in self.topics)


def _parse_topic(topic_id: str, raw: dict) -> ResearchTopicSpec:
    run_scripts = raw.get("run_scripts") or []
    graduated = raw.get("graduated_strategies") or []
    return ResearchTopicSpec(
        topic_id=topic_id,
        title=str(raw.get("title") or topic_id),
        status=str(raw.get("status") or "active"),
        description=str(raw.get("description", "")).strip(),
        run_scripts=tuple(str(x) for x in run_scripts),
        methodology=raw.get("methodology"),
        report_dir=raw.get("report_dir"),
        config_ref=raw.get("config_ref"),
        archive_path=raw.get("archive_path"),
        graduated_strategy=raw.get("graduated_strategy"),
        graduated_strategies=tuple(str(x) for x in graduated),
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
    return ResearchConfig(
        version=str(raw.get("version") or "research-v1"),
        layer=str(raw.get("layer") or "research"),
        principles=tuple(str(x) for x in principles_raw),
        topics=topics,
    )
