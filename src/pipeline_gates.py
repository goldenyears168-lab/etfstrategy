#!/usr/bin/env python3
"""Daily pipeline gates: strategies.yaml enabled + RUN_* env (daily_sync SSOT)."""

from __future__ import annotations

import os
import sys
from typing import Any

from strategy_registry import StrategyRegistry, load_strategy_registry

# daily_sync step → registry strategy_id(s) + optional RUN_* override.
# strategy_ids: run when ANY listed strategy is registry-active (enabled + env_flag).
_DAILY_SYNC_STEPS: dict[str, dict[str, Any]] = {
    "rrg_universe_close": {
        "strategy_ids": ("rrg-mono-hold7", "rrg-mono-swap-accel"),
        "match": "any",
        "run_env": "RUN_RRG_UNIVERSE_CLOSE",
        "run_default": "1",
    },
    "rrg_mono_daily": {
        "strategy_ids": ("rrg-mono-hold7",),
        "run_env": "RUN_RRG_MONO_DAILY",
        "run_default": "1",
    },
    "rrg_mono_swap_accel_daily": {
        "strategy_ids": ("rrg-mono-swap-accel",),
        "run_env": "RUN_RRG_MONO_SWAP_ACCEL_DAILY",
        "run_default": "1",
    },
    "copytrade_l1h9_daily": {
        "strategy_ids": ("00981a-l1h9",),
    },
    "vcp_funnel_close": {
        "strategy_ids": ("vcp-pivot-gate", "vcp-coil-close"),
        "match": "any",
        "run_env": "RUN_VCP_FUNNEL_CLOSE",
        "run_default": "1",
    },
}


def _registry_skip_reason(
    reg: StrategyRegistry,
    strategy_ids: tuple[str, ...],
    *,
    match: str,
    env: dict[str, str],
) -> str | None:
    """Registry gate: enabled only (launchd env_flag is checked separately via is_active)."""
    _ = env
    if match == "any":
        active: list[str] = []
        inactive: list[str] = []
        for sid in strategy_ids:
            spec = reg.get(sid)
            if spec is None:
                inactive.append(f"{sid}: missing")
                continue
            if spec.enabled:
                active.append(sid)
            else:
                inactive.append(f"{sid}: registry enabled=false")
        if active:
            return None
        return "registry enabled=false (" + "; ".join(inactive) + ")"

    sid = strategy_ids[0]
    spec = reg.get(sid)
    if spec is None:
        return f"unknown strategy_id={sid}"
    if not spec.enabled:
        return "registry enabled=false"
    return None


def daily_sync_skip_reason(
    step_id: str,
    env: dict[str, str] | None = None,
    *,
    registry: StrategyRegistry | None = None,
) -> str | None:
    """Return skip reason, or None when the step should run."""
    meta = _DAILY_SYNC_STEPS.get(step_id)
    if meta is None:
        return f"unknown pipeline step={step_id}"

    e = dict(os.environ if env is None else env)
    reg = registry or load_strategy_registry()

    strategy_ids: tuple[str, ...] = tuple(meta["strategy_ids"])
    match = str(meta.get("match", "all"))
    reason = _registry_skip_reason(reg, strategy_ids, match=match, env=e)
    if reason:
        return reason

    run_env = meta.get("run_env")
    if run_env:
        default = str(meta.get("run_default", "0"))
        if e.get(run_env, default) == "0":
            return f"{run_env}=0"
    return None


def should_run_daily_sync_step(
    step_id: str,
    env: dict[str, str] | None = None,
    *,
    registry: StrategyRegistry | None = None,
) -> bool:
    return daily_sync_skip_reason(step_id, env, registry=registry) is None


def registry_env_mismatches(
    env: dict[str, str] | None = None,
    *,
    registry: StrategyRegistry | None = None,
) -> list[str]:
    """WARN lines when RUN_*=1 but registry disabled, or registry on but RUN_*=0."""
    e = dict(os.environ if env is None else env)
    reg = registry or load_strategy_registry()
    out: list[str] = []

    for step_id, meta in _DAILY_SYNC_STEPS.items():
        run_env = meta.get("run_env")
        if not run_env:
            continue
        default = str(meta.get("run_default", "0"))
        run_on = e.get(run_env, default) != "0"
        reg_reason = _registry_skip_reason(
            reg,
            tuple(meta["strategy_ids"]),
            match=str(meta.get("match", "all")),
            env=e,
        )
        if run_on and reg_reason:
            out.append(f"{step_id}: {run_env}=1 but {reg_reason} → daily_sync will SKIP")
        elif (not run_on) and reg_reason is None:
            out.append(f"{step_id}: registry active but {run_env}=0 → daily_sync will SKIP")
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "Usage: pipeline_gates.py skip-reason <step_id>\n"
            "       pipeline_gates.py list-mismatches",
            file=sys.stderr,
        )
        return 2

    cmd = args[0]
    if cmd == "list-mismatches":
        for line in registry_env_mismatches():
            print(line)
        return 0
    if cmd == "skip-reason":
        if len(args) != 2:
            print("Usage: pipeline_gates.py skip-reason <step_id>", file=sys.stderr)
            return 2
        reason = daily_sync_skip_reason(args[1])
        if reason:
            print(reason)
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
