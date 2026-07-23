"""C18acc position monitor config · c18acc_pyramid_add from order.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config

STRATEGY_ID = "rrg-mono-swap-accel"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class C18accPyramidAddMonitorConfig:
    enabled: bool
    observe_only: bool
    rebound_min: float
    sizing_mode: str
    budget_fraction: float
    exit_mode: str
    strategy_id: str
    max_hold_days: int
    poll_interval_minutes: int


def load_c18acc_pyramid_add_monitor_config(
    cfg: dict[str, Any] | None = None,
) -> C18accPyramidAddMonitorConfig:
    raw = load_order_config() if cfg is None else cfg
    block = raw.get("c18acc_pyramid_add") if isinstance(raw.get("c18acc_pyramid_add"), dict) else {}
    if not isinstance(block, dict):
        block = {}
    yaml_on = bool(block.get("enabled", False))
    enabled = _env_flag("C18ACC_PYRAMID_ADD_ENABLED", "1" if yaml_on else "0")
    trigger = block.get("trigger") if isinstance(block.get("trigger"), dict) else {}
    sizing = block.get("sizing") if isinstance(block.get("sizing"), dict) else {}
    exit_block = block.get("exit") if isinstance(block.get("exit"), dict) else {}
    return C18accPyramidAddMonitorConfig(
        enabled=enabled,
        observe_only=not yaml_on,
        rebound_min=float(trigger.get("w3_rv_rebound_from_in_hold_trough_min") or 0.3),
        sizing_mode=str(sizing.get("mode") or "equal_weight_second_unit"),
        budget_fraction=float(sizing.get("budget_fraction_of_initial") or 1.0),
        exit_mode=str(exit_block.get("mode") or "sync_exit"),
        strategy_id=STRATEGY_ID,
        max_hold_days=10,
        poll_interval_minutes=int(trigger.get("poll_interval_minutes") or 30),
    )
