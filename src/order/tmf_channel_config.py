"""TMF channel Order sleeve config · Final v1.1.2 day-hi38 + night 15–30.

Fail-closed: enabled/auto_submit off, dry_run on unless env overrides.
Also gated by ORDER_MASTER_ENABLED (see fubon_orders.order_master_enabled).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config
from order.fubon_orders import order_master_enabled

STRATEGY_ID = "tmf-micro-channel"
USER_DEF = "tmfch"


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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Formal Order recipe (Final v1.1.2) — day hang_hi tighten; night absolute 15–30
# Research: tmf-hang-hi-day-tighten · D38_dayonly (tmf_hang_hi_day_vs_shared_lab.json)
PAPER_RECIPE: dict[str, Any] = dict(
    eod_flatten=False,  # poll must not flatten mid-session
    hang_lo=30.0,
    hang_hi=38.0,
    stop_pts=150.0,
    min_hold_before_stop=12,
    open_bias_pts=0.0,
    night_entries=True,
    night_hang_scale=0.5,  # unused when night_hang_lo/hi set
    night_hang_lo=15.0,
    night_hang_hi=30.0,
    skip_quiet_regime=True,
    day_dir_filter=False,
    in_pos_hang="both",
    exit_mode="hybrid_trail",
    trail_arm_pts=50.0,
    trail_giveback_pts=40.0,
    far_cover_lo=80.0,
    far_cover_hi=120.0,
    struct_exit_look=12,
    min_hold_before_smart=3,
    trend_hang_dampen="regime",
    gap_fill_improve=True,
    improv_struct_grace_bars=5,
    improv_struct_min_pts=5.0,
    improv_struct_until_trail=False,
    place_every=3,
    max_lots=2,
    allow_flip=False,
)


@dataclass(frozen=True)
class TmfChannelOrderConfig:
    strategy_id: str
    order_enabled: bool
    auto_submit: bool
    dry_run: bool
    max_lots: int
    place_every: int
    rail_match_pts: float
    max_api_per_poll: int
    max_api_per_day: int
    user_def: str
    ledger_path: str
    product: str
    kill_day_loss_pts: float
    recipe: dict[str, Any]


def load_tmf_channel_order_config(cfg: dict[str, Any] | None = None) -> TmfChannelOrderConfig:
    raw = load_order_config() if cfg is None else cfg
    block = raw.get("strategies", {}).get("tmf-micro-channel") or {}
    master = order_master_enabled()
    enabled = bool(block.get("enabled", False)) and _env_flag("ORDER_TMF_CHANNEL_ENABLED", "0")
    auto = bool(block.get("auto_submit", False)) and _env_flag(
        "ORDER_TMF_CHANNEL_AUTO_SUBMIT", "0"
    )
    dry = _env_flag("ORDER_TMF_CHANNEL_DRY_RUN", "1")
    if not master:
        auto = False
    # Live cannot escape dry unless master+enabled+auto_submit all on
    if not (master and enabled and auto):
        dry = True

    max_lots = _env_int("ORDER_TMF_CHANNEL_MAX_LOTS", int(block.get("max_lots", 1)))
    place_every = _env_int(
        "ORDER_TMF_CHANNEL_PLACE_EVERY", int(block.get("place_every", 5))
    )
    recipe = dict(PAPER_RECIPE)
    recipe["max_lots"] = max(1, min(2, max_lots))
    recipe["place_every"] = max(1, place_every)
    # Live default: 1 lot until proven
    if not dry:
        recipe["max_lots"] = min(recipe["max_lots"], 1)

    return TmfChannelOrderConfig(
        strategy_id=str(block.get("strategy_id") or STRATEGY_ID),
        order_enabled=enabled and master,
        auto_submit=auto and master and enabled,
        dry_run=dry,
        max_lots=int(recipe["max_lots"]),
        place_every=int(recipe["place_every"]),
        rail_match_pts=_env_float(
            "ORDER_TMF_CHANNEL_RAIL_MATCH_PTS", float(block.get("rail_match_pts", 2.0))
        ),
        max_api_per_poll=_env_int(
            "ORDER_TMF_CHANNEL_MAX_API_POLL", int(block.get("max_api_per_poll", 8))
        ),
        max_api_per_day=_env_int(
            "ORDER_TMF_CHANNEL_MAX_API_DAY", int(block.get("max_api_per_day", 120))
        ),
        user_def=str(block.get("user_def") or USER_DEF)[:10],
        ledger_path=str(
            block.get("own_ledger")
            or "data/order/tmf_channel_ledger.json"
        ),
        product=str(block.get("product") or "TMF"),
        kill_day_loss_pts=_env_float(
            "ORDER_TMF_CHANNEL_KILL_DAY_LOSS",
            float(block.get("kill_day_loss_pts", 400.0)),
        ),
        recipe=recipe,
    )
