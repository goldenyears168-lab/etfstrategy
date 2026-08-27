#!/usr/bin/env python3
"""One-off blind-spot check (2026-08-12): does rvol-numerator smoothing
(the dwell-flicker fix direction) actually help P&L under the REAL
order-layer-aware replay, or does it just make the regime label prettier
while making trading worse?

Monkeypatches ``tmf_channel.causal_engine.rvol_series`` (and its re-export
``tmf_channel.engine.rvol_series``, since ``desired_from_simulate`` binds
that name fresh via a local import each call) so the numerator is a
short causal moving average of V instead of the raw single-bar V[t] --
same idea validated on dwell-time stats earlier tonight, num_smooth=3.
Compares against the totally unpatched baseline (num_smooth=1) using
scripts/research/tmf_order_layer_aware_replay.py's replay_day() (NOT
raw simulate()) -- the real desired_from_simulate/order-layer-discipline
path -- on the 3 most recent real days used in tonight's earlier
max_hold_safety_min adversarial re-check, for direct comparability.

Also neutralizes tmf_channel.nq_1m_spread_gate.spread_side_for_day /
last_spread_load_error / last_spread_debug (mocked to return None) --
wall-clock-anchored, not backtest-safe, unrelated to this rvol test.

Self-imposed wall-clock deadline: prints partial results and exits
cleanly if the whole run threatens to exceed ~450s (order-layer replay
is O(n^2) in bar count, ~60-90s/day).
"""
from __future__ import annotations

import json
import statistics as st
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEADLINE_S = 450.0
_start = time.monotonic()


def deadline_hit() -> bool:
    return (time.monotonic() - _start) > DEADLINE_S


def make_smoothed_rvol_series(num_smooth: int):
    """Causal moving-average numerator, same VOL_WIN median denominator."""
    from tmf_channel.causal_engine import VOL_WIN

    def rvol_series_smoothed(V, win=VOL_WIN):
        out = [None] * len(V)
        for t in range(len(V)):
            a = max(0, t - win + 1)
            med = st.median(V[a : t + 1]) or 1.0
            b = max(0, t - num_smooth + 1)
            num = sum(V[b : t + 1]) / (t - b + 1)
            out[t] = num / med
        return out

    return rvol_series_smoothed


def apply_rvol_patch(num_smooth: int) -> None:
    """Patch both bindings: causal_engine's own global (used inside
    simulate()'s internal call) AND engine.py's re-export (a SEPARATE
    name binding copied at import time -- desired_from_simulate() does
    ``from tmf_channel.engine import rvol_series`` itself, a second,
    independent call site used only for the active_cell/pv display
    value, so both must be patched for full coverage).
    """
    import tmf_channel.causal_engine as causal_engine
    import tmf_channel.engine as engine

    fn = make_smoothed_rvol_series(num_smooth)
    causal_engine.rvol_series = fn
    engine.rvol_series = fn


def neutralize_spread_gate() -> None:
    import tmf_channel.nq_1m_spread_gate as gate

    gate.spread_side_for_day = lambda *a, **k: None
    gate.last_spread_load_error = lambda *a, **k: None
    gate.last_spread_debug = lambda *a, **k: None


def run_one(day: str, num_smooth: int, recipe: dict) -> dict:
    from tmf_channel.cache_store import load_day
    from tmf_order_layer_aware_replay import replay_day

    if num_smooth == 1:
        # True unpatched baseline: re-import causal_engine fresh state is
        # not needed since we restore explicitly below; num_smooth=1 uses
        # the ORIGINAL rvol_series (not our wrapper at all) to avoid any
        # doubt about wrapper fidelity at num_smooth=1.
        import tmf_channel.causal_engine as causal_engine
        import tmf_channel.engine as engine

        causal_engine.rvol_series = _ORIG_CAUSAL_RVOL
        engine.rvol_series = _ORIG_ENGINE_RVOL
    else:
        apply_rvol_patch(num_smooth)

    rows = load_day(day, source="tx_1m_tick_built_582d")
    if not rows:
        return dict(day=day, error="no_rows")
    t0 = time.monotonic()
    result = replay_day(day, rows, recipe)
    dt = time.monotonic() - t0
    trades = result.get("trades") or []
    return dict(
        day=day,
        n_trades=result["n_trades"],
        sum_pnl=result["sum_pnl"],
        n_place=result["n_place"],
        n_cancel=result["n_cancel"],
        wins=sum(1 for t in trades if t["pnl"] > 0),
        losses=sum(1 for t in trades if t["pnl"] <= 0),
        elapsed_s=round(dt, 1),
        trade_pnls=[t["pnl"] for t in trades],
    )


def main():
    global _ORIG_CAUSAL_RVOL, _ORIG_ENGINE_RVOL

    import tmf_channel.causal_engine as causal_engine
    import tmf_channel.engine as engine
    from order.tmf_channel_config import PAPER_RECIPE
    from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill

    _ORIG_CAUSAL_RVOL = causal_engine.rvol_series
    _ORIG_ENGINE_RVOL = engine.rvol_series

    neutralize_spread_gate()
    patch_nq_gate_for_backfill(lookback_days=60)

    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    days = ["2026-07-16", "2026-07-28", "2026-08-07"]
    configs = [1, 3]

    results: dict[int, list[dict]] = {c: [] for c in configs}
    partial = False
    for num_smooth in configs:
        for day in days:
            if deadline_hit():
                print(f"!! wall-clock deadline ({DEADLINE_S}s) hit before "
                      f"num_smooth={num_smooth} day={day} -- printing partial results", file=sys.stderr)
                partial = True
                break
            r = run_one(day, num_smooth, recipe)
            results[num_smooth].append(r)
            print(f"num_smooth={num_smooth} day={day}: "
                  f"n_trades={r.get('n_trades')} sum_pnl={r.get('sum_pnl')} "
                  f"elapsed={r.get('elapsed_s')}s", file=sys.stderr)
        if partial:
            break

    # restore originals (courtesy, this process exits right after anyway)
    causal_engine.rvol_series = _ORIG_CAUSAL_RVOL
    engine.rvol_series = _ORIG_ENGINE_RVOL

    summary = {}
    for num_smooth, rs in results.items():
        n_trades = sum(r.get("n_trades", 0) for r in rs)
        sum_pnl = round(sum(r.get("sum_pnl", 0.0) for r in rs), 1)
        wins = sum(r.get("wins", 0) for r in rs)
        losses = sum(r.get("losses", 0) for r in rs)
        summary[num_smooth] = dict(
            n_days=len(rs), n_trades=n_trades, sum_pnl=sum_pnl, wins=wins, losses=losses,
        )

    print(json.dumps(dict(partial=partial, per_day=results, summary=summary), indent=2, default=str))


if __name__ == "__main__":
    main()
