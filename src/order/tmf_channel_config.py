"""TMF channel Order sleeve config · Final v1.3.0 PV16 cell-tune v2 (O-anchor).

Fail-closed: enabled/auto_submit off, dry_run on unless env overrides.
Also gated by ORDER_MASTER_ENABLED (see fubon_orders.order_master_enabled).

Engine SSOT: ``tmf_channel.causal_engine`` (hang_anchor=O) · session_pv_book 16 cells
+ CELL_TUNE_V2_PATCHES (see tmf_channel_pv16_book.RECIPE_VERSION for the live tag).
Prior: Final v1.1.3 day-hi38 / night 15–30 / far_cover 65–105 (exit skeleton kept).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config
from order.fubon_orders import order_master_enabled
from order.tmf_channel_pv16_book import RECIPE_VERSION, paper_recipe_overlay

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


# Exit / risk skeleton retained from v1.1.3; hang/quiet/EARLY/block → PV16 book.
_PAPER_BASE: dict[str, Any] = dict(
    eod_flatten=False,  # poll must not flatten mid-session
    stop_pts=150.0,
    min_hold_before_stop=12,
    open_bias_pts=0.0,
    night_entries=True,
    night_hang_scale=0.5,
    day_dir_filter=False,
    in_pos_hang="both",
    exit_mode="hybrid_trail",
    trail_arm_pts=50.0,
    trail_giveback_pts=40.0,
    far_cover_lo=65.0,
    far_cover_hi=105.0,
    struct_exit_look=12,
    min_hold_before_smart=3,
    gap_fill_improve=True,
    improv_struct_grace_bars=5,
    improv_struct_min_pts=5.0,
    improv_struct_until_trail=False,
    place_every=3,
    max_lots=1,
    allow_flip=False,
)

PAPER_RECIPE: dict[str, Any] = {**_PAPER_BASE, **paper_recipe_overlay()}


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
    max_hold_safety_min: float
    kill_consecutive_failures: int
    recipe: dict[str, Any]
    recipe_version: str


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
    if not (master and enabled and auto):
        dry = True

    place_every = _env_int(
        "ORDER_TMF_CHANNEL_PLACE_EVERY", int(block.get("place_every", 5))
    )
    recipe = dict(PAPER_RECIPE)
    # Deep-copy book so poll mutations cannot corrupt module singleton
    book = recipe.get("session_pv_book")
    if isinstance(book, dict):
        from copy import deepcopy

        recipe["session_pv_book"] = deepcopy(book)
    _ = _env_int("ORDER_TMF_CHANNEL_MAX_LOTS", int(block.get("max_lots", 1)))
    recipe["max_lots"] = 1
    recipe["place_every"] = max(1, place_every)
    recipe["hang_anchor"] = "O"
    recipe["recipe_version"] = RECIPE_VERSION

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
        max_hold_safety_min=_env_float(
            "ORDER_TMF_CHANNEL_MAX_HOLD_SAFETY_MIN",
            float(block.get("max_hold_safety_min", 90.0)),
        ),
        kill_consecutive_failures=_env_int(
            "ORDER_TMF_CHANNEL_KILL_CONSECUTIVE_FAILURES",
            int(block.get("kill_consecutive_failures", 5)),
        ),
        recipe=recipe,
        recipe_version=RECIPE_VERSION,
    )
