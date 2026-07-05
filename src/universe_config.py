"""Load config/universe.yaml (universe SSOT · etf_watchlist vs tw100)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "universe.yaml"


def load_universe_config(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    if not p.is_file():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def universe_block(cfg: dict[str, Any], universe_id: str) -> dict[str, Any]:
    universes = cfg.get("universes") or {}
    block = universes.get(universe_id) or {}
    return block if isinstance(block, dict) else {}


def tw100_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    block = universe_block(cfg or load_universe_config(), "tw100")
    return {
        "rank_limit": int(block.get("rank_limit", 100)),
        "stock_id_pattern": str(block.get("stock_id_pattern", r"^\d{4}$")),
        "source": str(block.get("source", "finmind")),
        "extra_stock_ids": [str(s) for s in (block.get("extra_stock_ids") or [])],
        "exclude_stock_ids": [str(s) for s in (block.get("exclude_stock_ids") or [])],
        "min_constituent_count": int(block.get("min_constituent_count", 95)),
        "research_backfill_start": str(block.get("research_backfill_start", "2015-01-01")),
        "research_snapshot_date": str(block.get("research_snapshot_date", "2026-06-26")),
        "default_lookback_days": int(block.get("default_lookback_days", 400)),
    }
