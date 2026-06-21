"""Load config/execution.yaml (Execution layer · broker / account)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "execution.yaml"


def load_execution_config(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    if not p.is_file():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def broker_block(cfg: dict[str, Any]) -> dict[str, Any]:
    block = cfg.get("broker")
    return block if isinstance(block, dict) else {}


def account_block(cfg: dict[str, Any]) -> dict[str, Any]:
    block = cfg.get("account")
    return block if isinstance(block, dict) else {}
