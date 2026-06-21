"""Load config/strategy.yaml (Strategy 層 · 採納規格 SSOT)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"

_BACKTEST_META_KEYS = frozenset(
    {
        "spec_type",
        "metrics",
        "source_module",
        "source_report",
        "fallback_report",
        "notes",
        "benchmark",
        "breadth_zone_200",
        "config",
        "etf_code",
    }
)


@dataclass(frozen=True)
class StrategyBacktest:
    spec_type: str
    metrics: tuple[str, ...]
    source_module: str | None = None
    source_report: str | None = None
    fallback_report: str | None = None
    etf_code: str | None = None
    benchmark: str | None = None
    breadth_zone_200: str | None = None
    config: str | None = None
    notes: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdoptedStrategySpec:
    strategy_id: str
    title: str
    kind: str
    schedule: str
    enabled: bool
    description: str = ""
    etf_code: str | None = None
    strategy_code: str | None = None
    entry_row: str | None = None
    n_slots: int | None = None
    hold_days: int | None = None
    module: str | None = None
    backtest_module: str | None = None
    run_script: str | None = None
    methodology: str | None = None
    launchd_label: str | None = None
    schedule_time: str | None = None
    env_flag: str | None = None
    model_version: str | None = None
    role: str | None = None
    parent_strategy: str | None = None
    config_ref: str | None = None
    backtest: StrategyBacktest | None = None


@dataclass(frozen=True)
class StrategyConfig:
    version: str
    layer: str
    benchmark_default: str
    principles: tuple[str, ...]
    strategies: tuple[AdoptedStrategySpec, ...]

    def get(self, strategy_id: str) -> AdoptedStrategySpec | None:
        for s in self.strategies:
            if s.strategy_id == strategy_id:
                return s
        return None

    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(s.strategy_id for s in self.strategies)

    def hub_strategies(self) -> dict[str, dict[str, str]]:
        """Shape compatible with legacy strategy hub track dict."""
        return {s.strategy_id: {"title": s.title} for s in self.strategies}


def _parse_backtest(body: dict[str, Any]) -> StrategyBacktest | None:
    bt = body.get("backtest")
    if not isinstance(bt, dict):
        return None
    metrics = bt.get("metrics") or []
    params = {k: v for k, v in bt.items() if k not in _BACKTEST_META_KEYS}
    return StrategyBacktest(
        spec_type=str(bt.get("spec_type") or ""),
        metrics=tuple(str(m) for m in metrics),
        source_module=bt.get("source_module"),
        source_report=bt.get("source_report"),
        fallback_report=bt.get("fallback_report"),
        etf_code=bt.get("etf_code"),
        benchmark=bt.get("benchmark"),
        breadth_zone_200=bt.get("breadth_zone_200"),
        config=bt.get("config"),
        notes=str(bt.get("notes") or "").strip(),
        params=params,
    )


def _parse_strategy(strategy_id: str, raw: dict[str, Any]) -> AdoptedStrategySpec:
    return AdoptedStrategySpec(
        strategy_id=strategy_id,
        title=str(raw.get("title") or strategy_id),
        kind=str(raw.get("kind") or "competition"),
        schedule=str(raw.get("schedule") or "manual"),
        enabled=bool(raw.get("enabled", True)),
        description=str(raw.get("description", "")).strip(),
        etf_code=raw.get("etf_code"),
        strategy_code=raw.get("strategy_id"),
        entry_row=raw.get("entry_row"),
        n_slots=raw.get("n_slots"),
        hold_days=raw.get("hold_days"),
        module=raw.get("module"),
        backtest_module=raw.get("backtest_module"),
        run_script=raw.get("run_script"),
        methodology=raw.get("methodology"),
        launchd_label=raw.get("launchd_label"),
        schedule_time=raw.get("schedule_time"),
        env_flag=raw.get("env_flag"),
        model_version=raw.get("model_version"),
        role=raw.get("role"),
        parent_strategy=raw.get("parent_strategy"),
        config_ref=raw.get("config_ref"),
        backtest=_parse_backtest(raw),
    )


def load_strategy_config(path: Path | None = None) -> StrategyConfig:
    p = path or DEFAULT_CONFIG
    if not p.is_file():
        return StrategyConfig(
            version="strategy-v0",
            layer="strategy",
            benchmark_default="IX0001",
            principles=(),
            strategies=(),
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return StrategyConfig(
            version="strategy-v0",
            layer="strategy",
            benchmark_default="IX0001",
            principles=(),
            strategies=(),
        )
    principles_raw = raw.get("principles") or []
    strategies_raw = raw.get("strategies") or {}
    strategies = tuple(
        _parse_strategy(sid, body)
        for sid, body in strategies_raw.items()
        if isinstance(body, dict)
    )
    return StrategyConfig(
        version=str(raw.get("version") or "strategy-v1"),
        layer=str(raw.get("layer") or "strategy"),
        benchmark_default=str(raw.get("benchmark_default") or "IX0001"),
        principles=tuple(str(x) for x in principles_raw),
        strategies=strategies,
    )


def validate_strategies_alignment(strategies_path: Path | None = None) -> list[str]:
    """Return adopted strategy_ids missing from strategies.yaml registry."""
    from strategy_registry import DEFAULT_STRATEGIES_PATH, load_strategy_registry

    cfg = load_strategy_config()
    reg = load_strategy_registry(strategies_path or DEFAULT_STRATEGIES_PATH)
    strat_ids = {s.strategy_id for s in reg.strategies if s.layer == "strategy"}
    missing: list[str] = []
    for sid in cfg.strategy_ids():
        if sid not in strat_ids:
            missing.append(sid)
    return missing
