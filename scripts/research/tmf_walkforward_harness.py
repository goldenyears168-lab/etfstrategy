#!/usr/bin/env python3
"""2026-08-12: reusable fit->holdout->recent-day walk-forward harness.

Built after tonight's research chain hand-rolled the same three-stage
validation (chronologically-stratified fit sample -> holdout sample ->
most-recent-real-days adversarial check) FOUR separate times as throwaway
/tmp scripts, for stop_pts, max_hold_safety_min, adverse_pts_safety_net, and
an entry-regime-commitment filter -- every one of the first three "looked
good on fit/holdout, reversed hard on the most recent 3 real days" (the
fourth reversed on holdout too). That reversal pattern is the single most
important thing this session learned, and it very nearly got missed each
time by ad hoc scripts assembled under time pressure (one had a real bug:
importing a throwaway sibling script re-executed its whole module-level
sweep as a side effect, silently wasting ~16 minutes and polluting stdout
with a second, unwanted JSON blob). This module exists so future recipe/
parameter experiments reuse ONE tested harness instead of re-deriving the
same day lists, the same NQ-gate-neutralization mock, and the same partial-
completion-on-timeout logic from scratch each time.

What this does NOT do: decide what's worth testing, or run anything
against the live worker/ledger/broker. Pure historical replay against the
cached tx_1m_tick_built_582d bar dataset via an internal order-layer-aware
replay loop (_replay_day() below) -- NOT raw causal_engine.simulate(),
which diverges badly from real order-layer behavior (see scripts/research/
tmf_order_layer_aware_replay.py's docstring, the original source of this
pattern). Read-only against ${GOLDENSTOCKS_DATA_DIR}; touches no live
state, no src/order/ live path.

2026-08-12: _replay_day() is a from-scratch copy of tmf_order_layer_aware_
replay.replay_day() extended to ALSO exercise check_adverse_pts_safety_net
and check_trailing_stop_safety_net (that module only ever wired up
check_max_hold_safety_net) -- both new safety nets tonight, both testable
here via plain recipe_overrides keys (adverse_pts_safety_cap,
trail_stop_giveback_pts, trail_stop_min_hold_min) exactly like
max_hold_safety_min already was. This replaces THREE near-duplicate ad hoc
copies of this same loop written earlier tonight in /tmp scripts (one per
new safety net) -- the whole point of this module is to stop doing that.

Day-sample presets (FIT_SAMPLE / HOLDOUT_SAMPLE / RECENT_DAYS below) are the
exact lists used throughout tonight's session (2026-08-12), chronologically
stratified across the full 754-day 2023-07-03..2026-08-07 dataset with a
clean fit/holdout split at the midpoint, so runs made with this harness
stay directly comparable to tonight's already-reported baselines instead of
each drifting to its own ad hoc sample.

Known, deliberate caveat baked into every run (see neutralize_nq_gate()):
tmf_channel.nq_1m_spread_gate fetches Yahoo NQ data anchored to real wall-
clock now(), not the historical date being replayed -- so during ANY
historical replay it would otherwise hard-block every bias=True cell.
Every run through this harness neutralizes it (mocked to return None,
fail-open / gate inactive), matching this repo's own unit-test convention.
This isolates whatever recipe variable is actually under test.

CLI usage:
    PYTHONPATH=src .venv/bin/python scripts/research/tmf_walkforward_harness.py \\
        --sample fit --recipe-overrides '{"max_hold_safety_min": 60.0}'

    PYTHONPATH=src .venv/bin/python scripts/research/tmf_walkforward_harness.py \\
        --sample recent --days 2026-07-16,2026-07-28

Library usage (for a workflow/agent driving many variants in parallel):
    from tmf_walkforward_harness import FIT_SAMPLE, run_batch
    result = run_batch(FIT_SAMPLE, recipe_overrides={"stop_pts": 80.0})
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest import mock

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

# 2026-08-12 fit/holdout/recent day-sample presets (see module docstring).
FIT_SAMPLE: list[str] = [
    "2023-07-03", "2023-09-28", "2023-12-28", "2024-04-09", "2024-07-08", "2024-10-09",
]
HOLDOUT_SAMPLE: list[str] = [
    "2025-01-15", "2025-04-25", "2025-07-24", "2025-10-23", "2026-01-22", "2026-05-05",
]
RECENT_DAYS: list[str] = ["2026-07-16", "2026-07-28", "2026-08-07"]

DEFAULT_TIME_BUDGET_SEC = 480.0
DEFAULT_SOURCE = "tx_1m_tick_built_582d"


@contextmanager
def neutralize_nq_gate():
    """See module docstring -- required for every historical replay run."""
    with (
        mock.patch("tmf_channel.nq_1m_spread_gate.spread_side_for_day", return_value=None),
        mock.patch("tmf_channel.nq_1m_spread_gate.last_spread_load_error", return_value=None),
        mock.patch("tmf_channel.nq_1m_spread_gate.last_spread_debug", return_value=None),
    ):
        yield


def build_recipe(
    recipe_overrides: dict[str, Any] | None = None,
    *,
    session_pv_book: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Deepcopy of PAPER_RECIPE with optional flat-key overrides (e.g.
    stop_pts, max_hold_safety_min, adverse_pts_safety_cap) and/or a full
    session_pv_book replacement (e.g. an "ungated" uniform-per-session book,
    see uniform_session_book() below)."""
    from order.tmf_channel_config import PAPER_RECIPE

    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    if recipe_overrides:
        recipe.update(recipe_overrides)
    if session_pv_book is not None:
        recipe["session_pv_book"] = session_pv_book
    return recipe


def uniform_session_book(
    day_recipe: dict[str, Any], night_recipe: dict[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Collapse all 8 PV8 states within each session to ONE recipe -- keeps
    the day/night session split (structural) but removes PV8-based cell
    selection (the noisy part, see 2026-08-12 dwell-time finding). Use this
    to test whether PV8 regime-gating itself is earning its complexity vs a
    plain session-only baseline."""
    from order.tmf_channel_pv16_book import PV8

    return {
        "day": {pv: dict(day_recipe) for pv in PV8},
        "night": {pv: dict(night_recipe) for pv in PV8},
    }


RAIL_MATCH_PTS = 2.0


def _bars_from_rows(day: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """2026-08-13: row['t'] is normally 'HH:MM' (1-minute bars from
    cache_store.load_day -- minute resolution is correct/lossless there).
    Event-clock resamples (volume/tick bars) instead produce several real
    bars per wall-clock minute, so their row['t'] carries seconds ('HH:MM:SS')
    -- found live when a volume-bar prototype's 47% of bars collapsed onto
    the same nominal minute here, corrupting every wall-clock-elapsed safety
    net (max_hold_safety_min, trailing-stop min-hold, adverse-pts). Detect
    which precision a row actually carries instead of always assuming
    minute-only, so callers with real sub-minute timestamps aren't silently
    truncated."""
    out = []
    for r in rows:
        t = str(r.get("t") or "")
        suffix = "" if t.count(":") >= 2 else ":00"
        out.append(
            {
                "t": f"{day}T{t}{suffix}.000+08:00",
                "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"]),
                "v": float(r.get("v") or 0),
            }
        )
    return out


def _replay_day(day: str, rows: list[dict[str, Any]], recipe: dict[str, Any], *, min_bars: int = 20) -> list[dict[str, Any]]:
    """Order-layer-aware single-day replay -- see module docstring. Exercises
    the real production functions (desired_from_simulate, apply_quiet_flat_
    entry_gate, block_same_side_scale_wants, should_throttle_quiet_cancel,
    check_max_hold_safety_net, check_adverse_pts_safety_net,
    check_trailing_stop_safety_net) against a paper order book. Safety-net
    caps are read from the recipe dict (max_hold_safety_min, default 90.0
    -- always on, matches production; adverse_pts_safety_cap and
    trail_stop_giveback_pts/trail_stop_min_hold_min, default 0.0/5.0 --
    off unless explicitly overridden, matching production's disabled-by-
    default posture for both).

    2026-08-13: every trade dict also carries mae/mfe (Maximum Adverse/
    Favorable Excursion in pts, always >=0) -- tracked bar-by-bar, causally,
    from the bar AFTER entry through the bar the trade closes. This is
    research-infra-only (this module, not causal_engine.py/the live order
    path) added to support entry-MAE research (see config/research.yaml's
    detective-synthesis blindspot findings)."""
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
    ledger: dict[str, Any] = {
        "quiet_pv_since": None, "quiet_pv_value": None, "quiet_not_quiet_since": None,
        "cancel_throttle_last": {"S": None, "L": None},
        "position_open_ts": None, "position_open_sig": None,
        "trail_open_sig": None, "trail_peak_price": None, "trail_open_ts": None,
    }
    paper_pos: dict[str, Any] | None = None
    working: dict[str, float | None] = {"S": None, "L": None}
    trades: list[dict[str, Any]] = []

    from datetime import datetime as _dt

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
            elif abs(float(want) - cur) > RAIL_MATCH_PTS:
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
    return trades


def run_batch(
    days: list[str],
    *,
    recipe_overrides: dict[str, Any] | None = None,
    session_pv_book: dict[str, dict[str, dict[str, Any]]] | None = None,
    source: str = DEFAULT_SOURCE,
    time_budget_sec: float = DEFAULT_TIME_BUDGET_SEC,
    label: str = "",
) -> dict[str, Any]:
    """Run _replay_day() across `days`, aggregate, and ALWAYS return a real
    result for however many days completed inside time_budget_sec -- never
    raises/blocks forever on a slow day list. Mirrors the pattern used (and
    the bug fixed) in tonight's four ad hoc sweep scripts."""
    from tmf_channel.cache_store import load_day

    recipe = build_recipe(recipe_overrides, session_pv_book=session_pv_book)
    deadline = time.monotonic() + time_budget_sec

    all_trades: list[dict[str, Any]] = []
    days_attempted: list[str] = []
    n_days_processed = 0
    n_days_with_data = 0
    n_days_zero_rows = 0
    n_days_errored = 0
    hit_budget = False

    with neutralize_nq_gate():
        for day in days:
            if time.monotonic() > deadline:
                hit_budget = True
                print(f"[{label}] TIME_BUDGET_HIT before {day}", file=sys.stderr)
                break
            n_days_processed += 1
            days_attempted.append(day)
            try:
                rows = load_day(day, source=source)
            except Exception as exc:  # noqa: BLE001 -- best-effort batch, never crash
                n_days_errored += 1
                print(f"[{label}] LOAD_ERROR {day}: {exc}", file=sys.stderr)
                continue
            if not rows:
                n_days_zero_rows += 1
                continue
            n_days_with_data += 1
            try:
                day_trades = _replay_day(day, rows, recipe)
            except Exception as exc:  # noqa: BLE001
                n_days_errored += 1
                print(f"[{label}] REPLAY_ERROR {day}: {exc}", file=sys.stderr)
                continue
            for t in day_trades:
                t["day"] = day
                all_trades.append(t)
            print(f"[{label}] done {day} ntrades_so_far={len(all_trades)}", file=sys.stderr)

    n = len(all_trades)
    wins = [t["pnl"] for t in all_trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in all_trades if t["pnl"] < 0]
    per_day_pnl: dict[str, float] = {}
    for t in all_trades:
        per_day_pnl[t["day"]] = round(per_day_pnl.get(t["day"], 0.0) + t["pnl"], 1)
    # Days that were actually attempted (reached before hit_time_budget) but
    # produced zero trades are a real "0 pnl" observation for day-clustered
    # pairing (e.g. a paired t-test against another config's per-day
    # series) -- without this a quiet day silently drops out of the pairing
    # instead of contributing a true zero. Days NEVER reached because of
    # hit_time_budget must NOT be backfilled -- that would fabricate a zero
    # for a day that was never actually measured.
    for day in days_attempted:
        per_day_pnl.setdefault(day, 0.0)
    return {
        "label": label,
        "n_days_requested": len(days),
        "n_days_processed": n_days_processed,
        "n_days_with_data": n_days_with_data,
        "n_days_zero_rows": n_days_zero_rows,
        "n_days_errored": n_days_errored,
        "hit_time_budget": hit_budget,
        "ran_to_completion": n_days_processed == len(days) and not hit_budget,
        "n_trades": n,
        "sum_pnl": round(sum(t["pnl"] for t in all_trades), 1),
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "avg_win": round(sum(wins) / len(wins), 1) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 1) if losses else 0.0,
        "max_single_loss": round(min((t["pnl"] for t in all_trades), default=0.0), 1),
        "per_day_pnl": per_day_pnl,
        "trades": all_trades,
    }


def run_fit_holdout_recent(
    *,
    recipe_overrides: dict[str, Any] | None = None,
    session_pv_book: dict[str, dict[str, dict[str, Any]]] | None = None,
    label: str = "",
) -> dict[str, Any]:
    """The full three-stage check: fit -> holdout -> most-recent-real-days.
    A candidate is only worth trusting if the direction is consistent
    across all three -- tonight's session found this is NOT the common
    case, it's the exception (0/4 tested ideas survived intact)."""
    fit = run_batch(FIT_SAMPLE, recipe_overrides=recipe_overrides, session_pv_book=session_pv_book, label=f"{label}:fit")
    holdout = run_batch(HOLDOUT_SAMPLE, recipe_overrides=recipe_overrides, session_pv_book=session_pv_book, label=f"{label}:holdout")
    recent = run_batch(RECENT_DAYS, recipe_overrides=recipe_overrides, session_pv_book=session_pv_book, label=f"{label}:recent")
    return {"label": label, "fit": fit, "holdout": holdout, "recent": recent}


def _sample_for_name(name: str, explicit_days: list[str] | None) -> list[str]:
    if explicit_days:
        return explicit_days
    return {"fit": FIT_SAMPLE, "holdout": HOLDOUT_SAMPLE, "recent": RECENT_DAYS}[name]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", choices=["fit", "holdout", "recent", "all"], default="all")
    ap.add_argument("--days", default=None, help="comma-separated YYYY-MM-DD list, overrides --sample")
    ap.add_argument("--recipe-overrides", default=None, help='JSON dict, e.g. \'{"stop_pts": 80.0}\'')
    ap.add_argument("--time-budget-sec", type=float, default=DEFAULT_TIME_BUDGET_SEC)
    ap.add_argument("--label", default="cli-run")
    args = ap.parse_args()

    overrides = json.loads(args.recipe_overrides) if args.recipe_overrides else None
    explicit_days = args.days.split(",") if args.days else None

    if args.sample == "all" and not explicit_days:
        result = run_fit_holdout_recent(recipe_overrides=overrides, label=args.label)
    else:
        days = _sample_for_name(args.sample, explicit_days)
        result = run_batch(days, recipe_overrides=overrides, time_budget_sec=args.time_budget_sec, label=args.label)

    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
