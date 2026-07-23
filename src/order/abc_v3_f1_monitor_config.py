"""ABC v3+F1 position monitor config · champion TP + pyramid_add from order.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config

CHAMPION_TP_VARIANT = "TP_or_g54_g31_rvw3poll_drop_d25_r5_c1"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ChampionTpMonitorConfig:
    enabled: bool
    observe_only: bool
    strategy_id: str
    hold_days_cap: int
    variant_id: str
    gw5_min: float
    gw3_min: float
    rv_leg: str
    rv_mode: str
    drv_min: float
    min_ret_pct: float


@dataclass(frozen=True)
class PyramidAddMonitorConfig:
    enabled: bool
    observe_only: bool
    rebound_min: float
    sizing_mode: str
    budget_fraction: float
    exit_mode: str
    applies_to: tuple[str, ...]


def load_champion_tp_monitor_config(cfg: dict[str, Any] | None = None) -> ChampionTpMonitorConfig:
    raw = load_order_config() if cfg is None else cfg
    strategies = raw.get("strategies") if isinstance(raw.get("strategies"), dict) else {}
    spec = strategies.get("abc-v3-f1-champion-tp") if isinstance(strategies, dict) else {}
    if not isinstance(spec, dict):
        spec = {}
    exit_block = spec.get("exit") if isinstance(spec.get("exit"), dict) else {}
    yaml_on = bool(spec.get("enabled", False))
    # Default on when yaml enabled (launchd / .env can still force 0).
    enabled = _env_flag("ABC_V3_F1_CHAMPION_TP_ENABLED", "1" if yaml_on else "0")
    return ChampionTpMonitorConfig(
        enabled=enabled,
        observe_only=bool(spec.get("observe_only", True)),
        strategy_id=str(spec.get("strategy_id") or spec.get("user_def") or "abc-v3-f1-champion-tp"),
        hold_days_cap=int(spec.get("hold_days") or 5),
        variant_id=str(exit_block.get("variant_id") or CHAMPION_TP_VARIANT),
        gw5_min=float(exit_block.get("gw5_min") or 4.0),
        gw3_min=float(exit_block.get("gw3_min") or 1.0),
        rv_leg=str(exit_block.get("rv_leg") or "w3"),
        rv_mode=str(exit_block.get("rv_mode") or "poll_drop"),
        drv_min=float(exit_block.get("drv_min") or 0.25),
        min_ret_pct=float(exit_block.get("min_ret_pct") or 5.0),
    )


def load_pyramid_add_monitor_config(cfg: dict[str, Any] | None = None) -> PyramidAddMonitorConfig:
    raw = load_order_config() if cfg is None else cfg
    block = raw.get("pyramid_add") if isinstance(raw.get("pyramid_add"), dict) else {}
    if not isinstance(block, dict):
        block = {}
    yaml_on = bool(block.get("enabled", False)) if block else False
    enabled = _env_flag("ABC_V3_F1_PYRAMID_ADD_ENABLED", "1" if yaml_on else "0")
    applies = block.get("applies_to")
    if isinstance(applies, list):
        ids = tuple(str(x) for x in applies)
    else:
        ids = ("abc-v3-f1-champion-tp", "abc-v3-f1-pullback") if yaml_on else ()
    trigger = block.get("trigger") if isinstance(block.get("trigger"), dict) else {}
    sizing = block.get("sizing") if isinstance(block.get("sizing"), dict) else {}
    exit_block = block.get("exit") if isinstance(block.get("exit"), dict) else {}
    return PyramidAddMonitorConfig(
        enabled=enabled,
        observe_only=not yaml_on,
        rebound_min=float(trigger.get("w3_rv_rebound_from_in_hold_trough_min") or 0.3),
        sizing_mode=str(sizing.get("mode") or "equal_weight_second_unit"),
        budget_fraction=float(sizing.get("budget_fraction_of_initial") or 1.0),
        exit_mode=str(exit_block.get("mode") or "sync_exit"),
        applies_to=ids,
    )
