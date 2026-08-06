"""Public simulate API for Order + research (frozen causal engine).

外部（Order 層、research harness、測試）一律 import 這個門面，
不要直接 import ``tmf_channel.causal_engine`` —— 保持單一入口，
引擎內部重排時不破外部呼叫者。詳見同目錄 ``README.md``。
"""

from __future__ import annotations

from tmf_channel.causal_engine import (  # noqa: F401
    COST,
    classify_pv,
    load_gap_bias_map,
    load_vixtwn_1m,
    load_vixtwn_delta,
    rvol_series,
    simulate,
    summarize,
)

__all__ = [
    "COST",
    "classify_pv",
    "load_gap_bias_map",
    "load_vixtwn_1m",
    "load_vixtwn_delta",
    "rvol_series",
    "simulate",
    "summarize",
]
