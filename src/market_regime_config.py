"""Backward-compatible re-export — prefer `regime_config.load_regime_config`."""

from __future__ import annotations

from regime_config import load_market_regime_config, load_regime_config

__all__ = ["load_regime_config", "load_market_regime_config"]
