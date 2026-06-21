#!/usr/bin/env python3
"""產物 registry：載入 config/strategies.yaml（facts / regime / strategy 產物路徑）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from report_paths import REPORTS_DAILY, REPORTS_DIR
from stock_db import PROJECT_ROOT

DEFAULT_STRATEGIES_PATH = PROJECT_ROOT / "config" / "strategies.yaml"

StrategyKind = str  # trading | research | diagnostic | competition
StrategyLayer = str  # facts | regime | research | strategy
PortfolioRole = str  # core | satellite


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    title: str
    layer: StrategyLayer
    kind: StrategyKind
    enabled: bool
    description: str = ""
    env_flag: str | None = None
    score_version: str | None = None
    model_version: str | None = None
    etf_scope: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)
    portfolio_role: PortfolioRole | None = None
    workflow_ref: str | None = None
    e0: bool = False

    @property
    def reports_dir(self) -> Path:
        return REPORTS_DAILY / self.strategy_id

    def is_active(self, env: dict[str, str] | None = None) -> bool:
        if not self.enabled:
            return False
        if not self.env_flag:
            return True
        e = env if env is not None else os.environ
        return e.get(self.env_flag, "0") == "1"

    def skip_reason(self, env: dict[str, str] | None = None) -> str | None:
        if not self.enabled:
            return "registry enabled=false"
        if self.env_flag:
            e = env if env is not None else os.environ
            if e.get(self.env_flag, "0") != "1":
                return f"{self.env_flag}=0"
        return None


@dataclass(frozen=True)
class StrategyRegistry:
    version: str
    primary_strategy: str
    strategies: tuple[StrategySpec, ...]

    def get(self, strategy_id: str) -> StrategySpec | None:
        for s in self.strategies:
            if s.strategy_id == strategy_id:
                return s
        return None

    def active_strategies(self, env: dict[str, str] | None = None) -> list[StrategySpec]:
        return [s for s in self.strategies if s.is_active(env)]

    @property
    def primary(self) -> StrategySpec:
        spec = self.get(self.primary_strategy)
        if spec is None:
            raise KeyError(f"primary_strategy not found: {self.primary_strategy}")
        return spec


def _parse_strategy(strategy_id: str, raw: dict[str, Any]) -> StrategySpec:
    etf_scope = raw.get("etf_scope") or []
    kind = str(raw.get("kind", "research"))
    layer = str(raw.get("layer") or ("facts" if kind == "diagnostic" else "strategy"))
    return StrategySpec(
        strategy_id=strategy_id,
        title=str(raw.get("title", strategy_id)),
        layer=layer,
        kind=kind,
        enabled=bool(raw.get("enabled", True)),
        description=str(raw.get("description", "")).strip(),
        env_flag=raw.get("env_flag"),
        score_version=raw.get("score_version"),
        model_version=raw.get("model_version"),
        etf_scope=tuple(str(x) for x in etf_scope),
        sources=tuple(str(x) for x in (raw.get("sources") or [])),
        aliases={str(k): str(v) for k, v in (raw.get("aliases") or {}).items()},
        portfolio_role=raw.get("portfolio_role"),
        workflow_ref=raw.get("workflow_ref"),
        e0=bool(raw.get("e0", False)),
    )


def load_strategy_registry(path: Path | None = None) -> StrategyRegistry:
    cfg_path = path or DEFAULT_STRATEGIES_PATH
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid strategies yaml: {cfg_path}")
    strategies_raw = data.get("strategies") or {}
    strategies = tuple(
        _parse_strategy(sid, raw)
        for sid, raw in strategies_raw.items()
        if isinstance(raw, dict)
    )
    return StrategyRegistry(
        version=str(data.get("version", "strategies-v1")),
        primary_strategy=str(data.get("primary_strategy", "etf-daily")),
        strategies=strategies,
    )


def resolve_source_name(pattern: str, *, ref_date: str) -> str:
    """將 {date} 替換為 YYYYMMDD。"""
    stamp = ref_date.replace("-", "")
    return pattern.replace("{date}", stamp)


def resolve_strategy_sources(
    spec: StrategySpec,
    *,
    ref_date: str,
    reports_dir: Path = REPORTS_DIR,
) -> list[tuple[Path, Path]]:
    """回傳 (來源檔, 策略目錄內檔名) 清單。"""
    out: list[tuple[Path, Path]] = []
    dest_root = spec.reports_dir
    seen_dest: set[Path] = set()

    for pattern in spec.sources:
        name = resolve_source_name(pattern, ref_date=ref_date)
        src = reports_dir / name
        dest = dest_root / name
        if dest not in seen_dest:
            out.append((src, dest))
            seen_dest.add(dest)

    for alias, pattern in spec.aliases.items():
        dated = resolve_source_name(pattern, ref_date=ref_date)
        src = reports_dir / dated
        if not src.is_file():
            src = reports_dir / pattern.replace("{date}", ref_date.replace("-", ""))
        dest = dest_root / alias
        if dest not in seen_dest:
            out.append((src, dest))
            seen_dest.add(dest)

    return out
