"""Load config/regime.yaml (Regime 層 · task GPS · thresholds)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "regime.yaml"
LEGACY_CONFIG = PROJECT_ROOT / "config" / "market_regime.yaml"


def load_regime_config(path: Path | None = None) -> dict:
    p = path or DEFAULT_CONFIG
    if not p.is_file() and LEGACY_CONFIG.is_file():
        p = LEGACY_CONFIG
    if not p.is_file():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def breadth_block(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalized breadth config (supports legacy `breadth_impulse` top-level)."""
    if isinstance(cfg.get("breadth"), dict):
        return cfg["breadth"]
    legacy = cfg.get("breadth_impulse")
    if isinstance(legacy, dict):
        return {
            "rhythm": {
                "zweig_ema_span": legacy.get("zweig_ema_span", 10),
                "tiers": {
                    "off_max": 0.45,
                    "low_max": 0.50,
                    "mid_max": legacy.get("zweig_high", 0.58),
                },
            },
            "impulse": legacy,
        }
    return {}


def rhythm_tiers_from_regime(cfg: dict[str, Any]) -> dict[str, float]:
    b = breadth_block(cfg)
    tiers = (b.get("rhythm") or {}).get("tiers") or {}
    return {
        "off_max": float(tiers.get("off_max", 0.45)),
        "low_max": float(tiers.get("low_max", 0.50)),
        "mid_max": float(tiers.get("mid_max", 0.58)),
    }


def impulse_params_from_regime(cfg: dict[str, Any]) -> "BreadthImpulseParams":
    from market_breadth_impulse import BreadthImpulseParams

    b = breadth_block(cfg)
    rhythm = b.get("rhythm") or {}
    impulse = b.get("impulse") or {}
    tiers = rhythm.get("tiers") or {}
    return BreadthImpulseParams(
        zweig_low=float(impulse.get("zweig_thrust_low", impulse.get("zweig_low", 0.35))),
        zweig_high=float(
            impulse.get("zweig_thrust_high", tiers.get("mid_max", impulse.get("zweig_high", 0.58)))
        ),
        zweig_ema_span=int(rhythm.get("zweig_ema_span", impulse.get("zweig_ema_span", 10))),
        deemer_10d_ratio=float(impulse.get("deemer_10d_ratio", 1.97)),
        thrust_hold_days=int(impulse.get("thrust_hold_days", 63)),
    )


def load_market_regime_config(path: Path | None = None) -> dict:
    """Backward-compatible alias."""
    return load_regime_config(path)
