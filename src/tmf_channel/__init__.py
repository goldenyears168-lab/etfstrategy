"""TMF micro-channel package · engine + Order helpers + research harness.

SSOT for live simulate: ``tmf_channel.engine`` (causal O-anchor).
Recipe / 16-cell book remain in ``order.tmf_channel_config`` /
``order.tmf_channel_pv16_book``.
"""

from __future__ import annotations

__all__ = [
    "RECIPE_VERSION",
    "simulate",
    "load_vixtwn_1m",
    "load_vixtwn_delta",
]

# Lazy re-exports keep ``import tmf_channel`` light for non-engine callers.
def __getattr__(name: str):  # noqa: D401
    if name in ("simulate", "load_vixtwn_1m", "load_vixtwn_delta", "classify_pv", "rvol_series"):
        from tmf_channel import engine as _eng

        return getattr(_eng, name)
    if name == "RECIPE_VERSION":
        from order.tmf_channel_pv16_book import RECIPE_VERSION

        return RECIPE_VERSION
    raise AttributeError(name)
