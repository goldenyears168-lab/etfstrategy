"""C18acc order config · config/order.yaml + env overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config
from order.fubon_orders import order_master_enabled

STRATEGY_ID = "rrg-mono-swap-accel"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class C18accOrderConfig:
    order_enabled: bool
    auto_submit: bool
    dry_run: bool
    budget_twd_per_slot: int
    board_lot: bool
    price_type: str
    market_type: str
    time_in_force: str
    user_def: str
    entry_confirm_bars: int
    candidate_pool: str
    avoid_spread_mixed: bool
    allow_pool_override: bool
    prior_ret_days: int
    prior_ret_max: float | None
    spread_gate_no_trade_before: str
    poll_max_attempts: int
    poll_interval_sec: float
    submit_notify: bool


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if raw.lower() in ("off", "none", "null"):
        return None
    try:
        return float(raw)
    except ValueError:
        return default


def load_c18acc_order_config(cfg: dict[str, Any] | None = None) -> C18accOrderConfig:
    raw = load_order_config() if cfg is None else cfg
    strategies = raw.get("strategies") if isinstance(raw.get("strategies"), dict) else {}
    spec = strategies.get("rrg-mono-swap-accel") if isinstance(strategies, dict) else {}
    if not isinstance(spec, dict):
        spec = {}
    lifecycle = raw.get("lifecycle") if isinstance(raw.get("lifecycle"), dict) else {}

    yaml_enabled = bool(spec.get("enabled", True))
    order_enabled = _env_flag("ORDER_C18ACC_ORDER_ENABLED", "1" if yaml_enabled else "0")
    auto_submit = _env_flag("ORDER_C18ACC_AUTO_SUBMIT", "0") and order_master_enabled()
    dry_run = _env_flag("ORDER_C18ACC_DRY_RUN", "1")
    budget = _env_int("C18ACC_BUDGET_TWD_PER_SLOT", int(spec.get("budget_twd_per_slot") or 20_000))
    yaml_avoid = bool(spec.get("avoid_spread_mixed", True))
    avoid_spread = _env_flag("C18ACC_AVOID_SPREAD_MIXED", "1" if yaml_avoid else "0")
    spread_gate = spec.get("spread_gate") if isinstance(spec.get("spread_gate"), dict) else {}
    no_trade_before = str(
        os.environ.get("C18ACC_NO_TRADE_BEFORE", "").strip()
        or spread_gate.get("no_trade_before")
        or "13:00"
    )
    chase = spec.get("chase_gate") if isinstance(spec.get("chase_gate"), dict) else {}
    yaml_prior_days = int(chase.get("prior_ret_days") or 5)
    yaml_prior_max = chase.get("prior_ret_max")
    if yaml_prior_max is None:
        yaml_prior_max = 0.12
    allow_override = _env_flag(
        "C18ACC_ALLOW_POOL_OVERRIDE",
        "1" if bool(spec.get("allow_pool_override", False)) else "0",
    )

    return C18accOrderConfig(
        order_enabled=order_enabled,
        auto_submit=auto_submit,
        dry_run=dry_run,
        budget_twd_per_slot=budget,
        board_lot=_env_flag("C18ACC_BOARD_LOT", "0"),
        price_type=str(spec.get("price_type") or "chase_ask"),
        market_type=str(spec.get("market_type") or "intraday_odd"),
        time_in_force=str(spec.get("time_in_force") or "rod"),
        user_def=str(spec.get("user_def") or STRATEGY_ID),
        entry_confirm_bars=_env_int(
            "C18ACC_CONFIRM_BARS",
            int(spec.get("entry_confirm_bars") or 1),
        ),
        candidate_pool=str(spec.get("candidate_pool") or "fresh"),
        avoid_spread_mixed=avoid_spread,
        allow_pool_override=allow_override,
        prior_ret_days=_env_int("C18ACC_PRIOR_RET_DAYS", yaml_prior_days),
        prior_ret_max=_env_float("C18ACC_PRIOR_5D_RET_MAX", float(yaml_prior_max)),
        spread_gate_no_trade_before=no_trade_before,
        poll_max_attempts=_env_int(
            "ORDER_C18ACC_POLL_MAX_ATTEMPTS",
            int(lifecycle.get("poll_max_attempts") or 3),
        ),
        poll_interval_sec=float(lifecycle.get("poll_interval_sec") or 2.0),
        submit_notify=_env_flag(
            "RUN_C18ACC_SUBMIT_EMAIL",
            "1" if bool(lifecycle.get("submit_notify", True)) else "0",
        ),
    )


def assert_pool_matches_champion(champion_pool: str, cfg: dict[str, Any] | None = None) -> None:
    """Order layer candidate_pool must match champion SSOT."""
    order_pool = load_c18acc_order_config(cfg).candidate_pool
    if champion_pool != order_pool:
        raise ValueError(
            f"C18acc candidate_pool mismatch: champion={champion_pool!r} "
            f"order.yaml={order_pool!r} · sync config/order.yaml with champion_score_swap_c_config()"
        )
