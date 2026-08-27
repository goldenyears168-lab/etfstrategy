#!/usr/bin/env python3
"""ROUND 4 (2026-08-13): "suppress-and-requeue" design for the ER-grind entry
filter -- the one design explicitly not yet tried tonight.

Rounds 1-3 chain: (1) VWAP+RSI+relvol grind detector, coin-flip 3/6 days;
(2)/(3) causal Kaufman ER (trailing N=20, fixed threshold 0.25) inside PV8
dry/contract cells, directionally supportive but GATED AT FILL TIME (either
nulling the resting rail at want-generation with the rail able to refill next
bar, or excluding the blocked side from the [lo,hi] fill check at the fill
moment) -- both variants found the discriminator fires plenty at the bar
level (n_condition_bound ~19-64/day) but binding fills are near-zero (3 total
across 6 FIT_SAMPLE days for the fill-check variant), because by the time
price actually reaches a rail set earlier, the ER/PV8 window has usually
already rolled over.

This round tests the mechanically different remaining design: BLOCK THE WANT
PRICE ITSELF, in real time, the instant the condition is true -- not gating
an already-posted rail's fill. want_s/want_l are forced to None on the
blocked side for AS LONG AS the condition holds (so no rail is left resting
against the grind on that side at all), and the side is free to post a fresh
want again the moment a later bar's condition no longer holds (PV8 rolled out
of dry/contract, or ER dropped below threshold, or net_dir flipped/flattened).
This lets the *working rail tracking loop itself* cancel a resting order that
was already posted before the condition became true, and repost immediately
on the first qualifying (condition-false) bar -- not a fill-time filter.

Filter rule (unchanged threshold/window from rounds 2-3, only the injection
point changes):
  ER = |C[t]-C[t-20]| / sum(|C[i]-C[i-1]|) over the trailing 20 bars (causal)
  net_dir = sign(C[t]-C[t-20])
  if cur_pv in (dry, contract) and ER >= 0.25 and net_dir != 0:
      net_dir > 0 (grinding UP)   -> want_s forced None this bar
      net_dir < 0 (grinding DOWN) -> want_l forced None this bar
Applied immediately after want_s/want_l are read off `desired`, BEFORE
apply_quiet_flat_entry_gate / block_same_side_scale_wants / the working-rail
update loop -- so the suppression is indistinguishable, from the working-rail
loop's point of view, from desired_from_simulate() itself having returned
None on that side this bar.

Single FRESH day not used in any prior round tonight (all of FIT_SAMPLE +
HOLDOUT_SAMPLE + RECENT_DAYS from tmf_walkforward_harness.py are considered
burned; the 6 explicitly named burned days are a subset of FIT_SAMPLE).
2023-08-14 checked via tx_channel_tick_validation.load_front_month_ticks()
first (98,875 front-month ticks -- not thin; 2023-08-15 fallback, 78,217
ticks, was not needed). The actual replay bars still come from
tmf_channel.cache_store.load_day(day, source="tx_1m_tick_built_582d") like
every other round tonight (839 causal 1-min bars for 2023-08-14, day+night
session) -- the tick check above is only a data-availability sanity check on
the day choice, consistent with how the underlying 582d dataset was built
from these same tick caches.

Reuses _bars_from_rows/build_recipe/neutralize_nq_gate from
tmf_walkforward_harness.py; classify_pv/rvol_series are exercised indirectly
via desired_from_simulate() (the real order-layer function), not
reimplemented here. _replay_day() below is a copy of that module's
_replay_day with only the suppress-and-requeue filter block inserted (see
"FILTER" comment) -- everything else (order-layer gates, safety nets,
mae/mfe tracking) is untouched so trade-level fields stay comparable.
Read-only against cached tx_1m_tick_built_582d bars, no live state touched.
"""
from __future__ import annotations

import sys
from datetime import datetime as _dt

sys.path.insert(0, "/Users/jackm4/goldenstocks/src")
sys.path.insert(0, "/Users/jackm4/goldenstocks/scripts/research")

from tmf_walkforward_harness import (  # noqa: E402
    _bars_from_rows,
    build_recipe,
    neutralize_nq_gate,
)

DAY = "2023-08-14"
FALLBACK_DAY = "2023-08-15"
N_ER = 20
HIGH_ER_THRESH = 0.25


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
    """Returns (trades, n_bars_condition_true, n_wants_suppressed).
    n_bars_condition_true counts bars where the block condition (dry/contract
    + ER>=0.25 + net_dir!=0) held, regardless of what want_s/want_l were.
    n_wants_suppressed counts only the subset of those bars where the
    blocked side's want was actually non-None before being forced to None
    (i.e. a real want price that would otherwise have been posted/kept
    resting was suppressed this bar) -- this is the number that matters for
    "did the mechanism actually bind", the suppress-and-requeue analogue of
    the fill-check-stage binding-fill count used in prior rounds."""
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
    n_bars_condition_true = 0
    n_wants_suppressed = 0

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

        # FILTER (suppress-and-requeue, real-time want-generation stage): the
        # instant the ER-grind condition is true, the against-grind side's
        # want price is forced to None THIS BAR, before the working-rail
        # update loop even sees it -- so no rail is left resting/posted on
        # that side while the condition holds. The side is free to post a
        # fresh want again the very next bar the condition is no longer true
        # (PV8 rolled out of dry/contract, ER dropped below threshold, or
        # net_dir flipped/flattened). Mechanically distinct from rounds 2-3,
        # which gated an already-computed want/rail at the fill check.
        if use_filter and cur_pv in ("dry", "contract"):
            C = [float(b["c"]) for b in window]
            er, net_dir = kaufman_er_and_dir(C, len(C) - 1, N_ER)
            if er is not None and er >= HIGH_ER_THRESH and net_dir != 0:
                n_bars_condition_true += 1
                if net_dir > 0:
                    if want_s is not None:
                        n_wants_suppressed += 1
                    want_s = None
                else:
                    if want_l is not None:
                        n_wants_suppressed += 1
                    want_l = None

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
    return trades, n_bars_condition_true, n_wants_suppressed


def summarize(label: str, trades: list[dict]) -> tuple[float, float]:
    n = len(trades)
    pnl = round(sum(t["pnl"] for t in trades), 1)
    mae = round(sum(t["mae"] for t in trades) / n, 2) if n else 0.0
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    win_rate = round(len(wins) / n, 3) if n else 0.0
    print(f"[{label}] n_trades={n} sum_pnl={pnl} avg_mae={mae} win_rate={win_rate}")
    return pnl, mae


def main() -> None:
    from tx_channel_tick_validation import load_front_month_ticks
    from tmf_channel.cache_store import load_day

    day = DAY
    ticks = load_front_month_ticks(day)
    if ticks is None or len(ticks) < 5000:
        print(f"[{day}] tick data missing/thin (n={0 if ticks is None else len(ticks)}) -> falling back to {FALLBACK_DAY}")
        day = FALLBACK_DAY
        ticks = load_front_month_ticks(day)
    print(f"day_used={day} n_front_month_ticks={len(ticks) if ticks is not None else 0}")

    rows = load_day(day, source="tx_1m_tick_built_582d")
    if not rows:
        print("NO BARS")
        return
    print(f"n_bars={len(rows)}")
    recipe = build_recipe()

    with neutralize_nq_gate():
        base_trades, _base_cond, _base_supp = _replay_day(day, rows, recipe, use_filter=False)
        filt_trades, cond_true, wants_suppressed = _replay_day(day, rows, recipe, use_filter=True)

    base_pnl, base_mae = summarize("BASELINE", base_trades)
    filt_pnl, filt_mae = summarize("FILTERED (suppress-and-requeue)", filt_trades)

    print(f"\nn_bars_condition_true = {cond_true}")
    print(f"n_wants_suppressed (actual want->None flips) = {wants_suppressed}")
    print(f"n_baseline_trades = {len(base_trades)}  n_filtered_trades = {len(filt_trades)}")
    print(f"delta_pnl (filtered-baseline) = {round(filt_pnl - base_pnl, 1)}")
    print(f"delta_trade_count = {len(filt_trades) - len(base_trades)}")
    print(f"delta_avg_mae = {round(filt_mae - base_mae, 2)}")


if __name__ == "__main__":
    main()
