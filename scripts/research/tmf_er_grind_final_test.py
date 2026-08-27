#!/usr/bin/env python3
"""FINAL pre-registered test (2026-08-13) of the Kaufman ER "grind detector"
entry filter for TMF PV8 dry/contract cells.

This is round 3 of a chain: (1) VWAP+RSI+relvol grind detector -- non-
redundant vs PV8 but a coin flip 3/6 days; (2) an ER-based detector,
directionally supportive 5/6 days but with THREE known flaws fixed here:
  (a) per-day tercile cutoffs (look-ahead) -> fixed to a FIXED global
      threshold ER >= 0.25, no per-day quantiles at all.
  (b) no day-clustered significance test -> added below (n=6 days, not
      n=trades; bar-level ER/PV8 state is heavily autocorrelated within a
      day so trade/bar-level n vastly overstates independent sample size).
  (c) entry-filter found INERT on the one day tested because the block was
      applied at want-generation (nulling the resting rail) rather than at
      the fill-check moment, and because real fills mostly print pv_entry=
      "normal" one bar after the dry/contract bar that set the rail ->
      fixed here at the fill-check stage (matches /tmp prototype v2), AND
      the prototype's own remaining bug -- excluding blocked_fill_side
      unconditionally, which could also block a REVERSAL-exit fill, not
      just a fresh entry -- is fixed by scoping the block to paper_pos is
      None only (an existing open position must always be free to reverse/
      exit; only a *fresh* entry against a high-ER grind is blocked).

Pre-registered gate (must report BEFORE any P&L claim): total binding
fills across the 6 FIT_SAMPLE days, counted at the actual fill-check
moment (price genuinely inside [lo,hi] AND paper_pos is None AND
side == blocked_fill_side) -- NOT bar-level want-generation counts. If
binding fills < 30 total, STOP: report as mechanism-wall closure, do not
interpret P&L deltas. If >= 30, additionally run a day-clustered
(filtered_pnl - baseline_pnl) comparison across the 6 days (n=6).

Reuses _bars_from_rows/build_recipe/neutralize_nq_gate/FIT_SAMPLE from
tmf_walkforward_harness.py; _replay_day() below is a copy of that module's
_replay_day with only the ER-grind filter hook inserted (see "FILTER"
comments) -- everything else (order-layer gates, safety nets, mae/mfe
tracking) is untouched so trade-level fields stay comparable. Read-only
against cached tx_1m_tick_built_582d bars, no live state touched.
"""
from __future__ import annotations

import sys
from datetime import datetime as _dt
from statistics import mean, stdev

sys.path.insert(0, "/Users/jackm4/goldenstocks/src")
sys.path.insert(0, "/Users/jackm4/goldenstocks/scripts/research")

from tmf_walkforward_harness import (  # noqa: E402
    FIT_SAMPLE,
    _bars_from_rows,
    build_recipe,
    neutralize_nq_gate,
)

N_ER = 20
HIGH_ER_THRESH = 0.25  # FIXED global cut, pre-registered -- NOT a per-day quantile.


def kaufman_er_and_dir(C: list[float], t: int, n: int = N_ER) -> tuple[float | None, int]:
    """Causal trailing-N Kaufman Efficiency Ratio at bar t, using only
    C[t-n .. t] (no look-ahead). Returns (er, net_dir) where net_dir is the
    sign of the net move over the window (+1 up, -1 down, 0 flat/undefined)."""
    a = t - n
    if a < 0:
        return None, 0
    net = C[t] - C[a]
    denom = sum(abs(C[i] - C[i - 1]) for i in range(a + 1, t + 1))
    if denom <= 0:
        return None, 0
    er = abs(net) / denom
    direction = 1 if net > 0 else (-1 if net < 0 else 0)
    return er, direction


def _replay_day(
    day: str, rows: list[dict], recipe: dict, *, min_bars: int = 20, use_filter: bool
) -> tuple[list[dict], int, int]:
    """Returns (trades, n_bars_condition_bound, n_binding_fills). The former
    counts bars where the filter *condition* held (dry/contract + ER>=0.25 +
    net_dir!=0) -- a want-generation-time count, kept for diagnostics only.
    The latter (the pre-registered gate number) counts only bars where a
    fresh entry (paper_pos is None) at the blocked side's working price
    would GENUINELY have filled (price inside [lo,hi] of the next bar) had
    the filter not intervened."""
    from order.tmf_channel_order import (
        apply_quiet_flat_entry_gate,
        block_same_side_scale_wants,
        check_adverse_pts_safety_net,
        check_max_hold_safety_net,
        check_trailing_stop_safety_net,
        desired_from_simulate,
        should_throttle_quiet_cancel,
    )
    from tmf_channel.desired_cache import clear_desired_cache

    bars = _bars_from_rows(day, rows)
    ledger: dict = {
        "quiet_pv_since": None, "quiet_pv_value": None, "quiet_not_quiet_since": None,
        "cancel_throttle_last": {"S": None, "L": None},
        "position_open_ts": None, "position_open_sig": None,
        "trail_open_sig": None, "trail_peak_price": None, "trail_open_ts": None,
    }
    paper_pos: dict | None = None
    working: dict = {"S": None, "L": None}
    trades: list[dict] = []
    n_condition_bound = 0
    n_binding_fills = 0

    for i in range(min_bars, len(bars)):
        window = bars[:i]
        bar = window[-1]
        next_bar = bars[i]
        now = _dt.fromisoformat(bar["t"])
        spot = float(bar["c"])

        if paper_pos is not None:
            bar_h, bar_l = float(bar["h"]), float(bar["l"])
            if paper_pos["s"] == "L":
                adverse = float(paper_pos["ep"]) - bar_l
                favorable = bar_h - float(paper_pos["ep"])
            else:
                adverse = bar_h - float(paper_pos["ep"])
                favorable = float(paper_pos["ep"]) - bar_l
            paper_pos["_mae"] = max(paper_pos.get("_mae", 0.0), adverse, 0.0)
            paper_pos["_mfe"] = max(paper_pos.get("_mfe", 0.0), favorable, 0.0)

        clear_desired_cache()
        desired = desired_from_simulate(window, day=day, recipe=recipe)
        if not desired.get("ok"):
            continue
        want_s, want_l = desired.get("want_s"), desired.get("want_l")
        cur_pv = (desired.get("active_cell") or {}).get("pv")

        # FILTER (fill-check stage, fixed threshold, paper_pos-scoped): a
        # fresh entry (paper_pos is None) against a high-ER grind direction
        # inside dry/contract is blocked at the moment its fill would
        # otherwise print. An existing open position is NEVER blocked from
        # reversing/exiting via the opposite side -- that is what fixes the
        # prototype's over-broad exclusion bug.
        blocked_fill_side = None
        if use_filter and cur_pv in ("dry", "contract"):
            C = [float(b["c"]) for b in window]
            er, net_dir = kaufman_er_and_dir(C, len(C) - 1, N_ER)
            if er is not None and er >= HIGH_ER_THRESH and net_dir != 0:
                blocked_fill_side = "S" if net_dir > 0 else "L"
                n_condition_bound += 1

        if paper_pos is not None and cur_pv in ("climax_up", "climax_dn", "expand_up", "expand_dn"):
            if paper_pos.get("_pv_shift_to") is None and cur_pv != paper_pos.get("_pv_entry"):
                paper_pos["_pv_shift_to"] = cur_pv

        ledger, _elapsed, safety_why = check_max_hold_safety_net(
            ledger, broker_live=paper_pos,
            max_hold_safety_min=float(recipe.get("max_hold_safety_min", 90.0)), now=now,
        )
        adverse_pts_cap = float(recipe.get("adverse_pts_safety_cap", 0.0))
        _adverse, adverse_why = check_adverse_pts_safety_net(
            broker_live=paper_pos, spot=spot, adverse_pts_safety_cap=adverse_pts_cap,
        )
        trail_giveback_cap = float(recipe.get("trail_stop_giveback_pts", 0.0))
        trail_min_hold = float(recipe.get("trail_stop_min_hold_min", 5.0))
        ledger, _giveback, trail_why = check_trailing_stop_safety_net(
            ledger, broker_live=paper_pos, spot=spot,
            trail_giveback_pts=trail_giveback_cap, min_hold_before_trail_min=trail_min_hold, now=now,
        )
        flatten_why = safety_why or adverse_why or trail_why
        if flatten_why and paper_pos:
            xp = spot
            pnl = (xp - float(paper_pos["ep"])) if paper_pos["s"] == "L" else (float(paper_pos["ep"]) - xp)
            trades.append(dict(s=paper_pos["s"], ep=paper_pos["ep"], xp=xp, pnl=round(pnl, 1),
                                et=paper_pos.get("_et"), xt=bar["t"], why=flatten_why.split()[0],
                                mae=round(paper_pos.get("_mae", 0.0), 1), mfe=round(paper_pos.get("_mfe", 0.0), 1),
                                pv_entry=paper_pos.get("_pv_entry"), pv_shift_to=paper_pos.get("_pv_shift_to")))
            paper_pos = None
            working = {"S": None, "L": None}

        want_s, want_l, quiet_skip, ledger = apply_quiet_flat_entry_gate(
            want_s, want_l, broker_live=paper_pos, desired=desired, recipe=recipe, ledger=ledger, now=now,
        )
        want_s, want_l, _scale_block = block_same_side_scale_wants(want_s, want_l, open_pos=paper_pos, max_lots=1)

        for side, want in (("S", want_s), ("L", want_l)):
            cur = working.get(side)
            if want is None:
                if cur is not None:
                    suppress, ledger = should_throttle_quiet_cancel(
                        side, quiet_skip_reason=quiet_skip, open_pos=paper_pos, ledger=ledger, now=now)
                    if suppress:
                        continue
                    working[side] = None
                continue
            if cur is None:
                working[side] = float(want)
            elif abs(float(want) - cur) > 2.0:
                working[side] = float(want)

        lo, hi = float(next_bar["l"]), float(next_bar["h"])
        for side in ("S", "L"):
            px = working.get(side)
            if px is None or not (lo <= px <= hi):
                continue
            # FIX (c): only block a FRESH entry (paper_pos is None) on the
            # blocked side. An already-open position must remain free to
            # reverse/exit through the opposite side regardless of
            # blocked_fill_side.
            if side == blocked_fill_side and paper_pos is None:
                n_binding_fills += 1
                continue
            if paper_pos is None:
                paper_pos = {"s": side, "n": 1, "ep": px, "_et": next_bar["t"], "_mae": 0.0, "_mfe": 0.0,
                             "_pv_entry": cur_pv, "_pv_shift_to": None}
                working = {"S": None, "L": None}
                break
            if paper_pos["s"] != side:
                xp = px
                pnl = (xp - float(paper_pos["ep"])) if paper_pos["s"] == "L" else (float(paper_pos["ep"]) - xp)
                trades.append(dict(s=paper_pos["s"], ep=paper_pos["ep"], xp=xp, pnl=round(pnl, 1),
                                    et=paper_pos.get("_et"), xt=next_bar["t"], why="order_layer_fill",
                                    mae=round(paper_pos.get("_mae", 0.0), 1), mfe=round(paper_pos.get("_mfe", 0.0), 1),
                                    pv_entry=paper_pos.get("_pv_entry"), pv_shift_to=paper_pos.get("_pv_shift_to")))
                paper_pos = None
                working = {"S": None, "L": None}
                break
    return trades, n_condition_bound, n_binding_fills


def main() -> None:
    from tmf_channel.cache_store import load_day

    recipe = build_recipe()
    per_day: dict[str, dict] = {}
    total_binding = 0
    total_condition_bound = 0

    with neutralize_nq_gate():
        for day in FIT_SAMPLE:
            rows = load_day(day, source="tx_1m_tick_built_582d")
            if not rows:
                print(f"[{day}] NO ROWS")
                per_day[day] = {"base_pnl": 0.0, "filt_pnl": 0.0, "binding": 0, "cond_bound": 0}
                continue
            base_trades, base_cond, base_bind = _replay_day(day, rows, recipe, use_filter=False)
            filt_trades, filt_cond, filt_bind = _replay_day(day, rows, recipe, use_filter=True)
            base_pnl = round(sum(t["pnl"] for t in base_trades), 1)
            filt_pnl = round(sum(t["pnl"] for t in filt_trades), 1)
            per_day[day] = {
                "base_pnl": base_pnl, "filt_pnl": filt_pnl,
                "delta_pnl": round(filt_pnl - base_pnl, 1),
                "n_base_trades": len(base_trades), "n_filt_trades": len(filt_trades),
                "binding": filt_bind, "cond_bound": filt_cond,
            }
            total_binding += filt_bind
            total_condition_bound += filt_cond
            print(f"[{day}] base_pnl={base_pnl} filt_pnl={filt_pnl} "
                  f"delta={round(filt_pnl - base_pnl, 1)} n_binding_fills={filt_bind} "
                  f"n_condition_bound_bars={filt_cond}")

    print("\n=== PRE-REGISTERED GATE ===")
    print(f"total_binding_fills_across_6_days = {total_binding}")
    print(f"(diagnostic only, not the gate) total_condition_bound_bars = {total_condition_bound}")

    if total_binding < 30:
        print("\nGATE RESULT: binding fills < 30 -> MECHANISM WALL. STOPPING.")
        print("Not proceeding to day-clustered P&L interpretation -- near-zero binding")
        print("means near-zero statistical power regardless of any delta computed above.")
        return

    print("\nGATE PASSED (binding fills >= 30) -> proceeding to day-clustered test.")
    deltas = [per_day[d]["delta_pnl"] for d in FIT_SAMPLE]
    n = len(deltas)
    m = mean(deltas)
    sd = stdev(deltas) if n > 1 else 0.0
    t_stat = (m / (sd / (n ** 0.5))) if sd > 0 else float("nan")
    print(f"per_day_delta_pnl = {deltas}")
    print(f"day_clustered_mean_delta (n={n} days) = {round(m, 2)}")
    print(f"day_clustered_stdev = {round(sd, 2)}")
    print(f"day_clustered_t_stat (n={n}, df={n-1}) = {round(t_stat, 3) if sd > 0 else 'nan'}")


if __name__ == "__main__":
    main()
