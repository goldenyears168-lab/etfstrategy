#!/usr/bin/env python3
"""Order-layer-aware backtest replay (2026-08-08).

Motivation: tonight's true re-simulation work (this whole batch of
scripts/research/tx_channel_*.py) all call ``tmf_channel.causal_engine.
simulate()`` directly on a whole day's bars at once. That function models
the RAW STRATEGY SIGNAL under idealized guaranteed fills -- it does NOT
model the order layer's execution discipline that actually runs live
(apply_quiet_flat_entry_gate's hysteresis, should_throttle_quiet_cancel,
block_same_side_scale_wants, check_max_hold_safety_net, place_every
cadence). Direct same-day comparison on 2026-08-06 (the only day with both
a bar source and confirmed real fills available tonight) showed raw
simulate() = 43 trades / -1168pt vs real broker fills = 23 trades / -14.4pt
-- nearly 2x the trade count and a ~1150pt gap. This divergence is in fact
already documented in this file's own trip_day_pnl_kill() docstring
("2026-08-06: sim -1230 vs broker day ~= +12"), which is why the live-submit
kill switch never uses sim PnL -- but every *research* comparison tonight
(and every night before it, as far as this author could verify) still
implicitly treated simulate() output as if it were live-performance-
equivalent.

This script replays historical 1m bars tick-by-tick (one tick per newly
closed bar) through the REAL production functions from
src/order/tmf_channel_order.py -- desired_from_simulate, block_same_side_
scale_wants, apply_quiet_flat_entry_gate, check_max_hold_safety_net,
should_throttle_quiet_cancel -- against a PAPER (in-memory, not broker)
position/order-book state, instead of calling causal_engine.simulate()
once on the whole day. This is NOT a from-scratch reimplementation of the
decision logic -- it imports and calls the exact live functions. What IS
reimplemented, necessarily, is the broker interface itself (place/cancel/
fill), because reconcile_once() is hard-wired to a real Fubon session.

Known simplifications vs true live behavior (documented, not hidden):
  - One tick per closed 1m bar, not one tick per ~20s poll (the worker
    polls ~3x/min; most of those see no new bar and are no-ops for
    desired_from_simulate anyway since _drop_forming_last_bar excludes the
    forming bar -- but sub-minute throttle/hysteresis timing is
    approximated at 1-bar granularity here, not exact wall-clock).
  - Fill model: idealized touch-fill (want price inside [L,H] of the
    current bar fills at the want price) for entries/covers, matching
    simulate()'s own assumption -- this script isolates the ORDER-LAYER-
    DISCIPLINE difference (fewer, more deliberate orders), not fill-quality
    modeling (real slippage / partial fills / queue priority are still not
    modeled here, on either side of the comparison).
  - Safety-net / kill flattens execute at the current bar's close (proxy
    for a market/IOC exit), not a separately-modeled market order book.
  - desired_cache.py's disk cache is redirected to a scratch dir (never the
    real GOLDENSTOCKS_DATA_DIR) so repeated/parallel runs of this script
    can never read stale results from a different recipe, and can never
    write into the live worker's shared state file.

Validation target: reproduce something much closer to 23 trades / -14.4pt
for 2026-08-06 than raw simulate()'s 43 / -1168 -- not exact (real fills
depend on live market microstructure this script does not model), but
directionally and materially closer.

Does NOT touch src/order/, config/order.yaml, .env, launchd/,
scripts/order/, or any live ledger/session state.
"""
from __future__ import annotations

import argparse
import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from copy import deepcopy  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

sys.path.insert(0, "src")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_order import (  # noqa: E402
    apply_quiet_flat_entry_gate,
    block_same_side_scale_wants,
    check_max_hold_safety_net,
    desired_from_simulate,
    should_throttle_quiet_cancel,
)
from tmf_channel.cache_store import load_day  # noqa: E402
import tmf_channel.desired_cache as _desired_cache_mod  # noqa: E402
from tmf_channel.desired_cache import clear_desired_cache  # noqa: E402
import tmf_channel.nq_gate as _nq_gate_mod  # noqa: E402
import tmf_channel.nq_1m_spread_gate as _nq_1m_spread_gate_mod  # noqa: E402

# Isolate desired_from_simulate()'s disk cache from the live worker's shared
# state file (${GOLDENSTOCKS_DATA_DIR}/data/order/tmf_desired_cache.json) --
# a real-money-adjacent path this backtest replay must never read or write.
# Redirecting only this one function (not the whole GOLDENSTOCKS_DATA_DIR env
# var) keeps cache_store's real bars.sqlite / VIX / NQ-gate reads untouched.
# Per-PID path (not a single shared scratch file): parallel invocations of
# this script (e.g. a month-long batch, several days at once) would otherwise
# race on the same JSON file -- concurrent read-during-write can corrupt it,
# and a same-fingerprint collision across processes could hand one day's
# desired-state to a different day.
import os  # noqa: E402

_SCRATCH_DESIRED_CACHE = (
    Path(tempfile.gettempdir()) / f"tmf_replay_scratch_desired_cache_{os.getpid()}.json"
)


def patch_nq_gate_for_backfill(*, lookback_days: int = 60) -> None:
    """Widen nq_gate.py's NQ/ES fetch window for historical replay.

    Found 2026-08-09: ``_load_futures_bundle()`` (live-oriented, correctly
    so for its real purpose) only fetches a ROLLING ``today-10d..today+1d``
    hourly window (plus ``today-40d..today+1d`` daily). A month-long replay
    run in early August queried mid-July days that fall entirely outside
    that rolling window -- ``nq_1h`` had zero rows there, ``nq_side_for_day``
    silently computed "none" for every one of them (not a real flat-market
    signal, a data-coverage artifact), and causal_engine.py's ``ssg_none``
    branch hard-blocks every new entry while flat -- so 16 straight July
    trading days replayed as completely empty (0 places, 0 trades). This
    patches ``nq_gate._load_futures_bundle`` (module-global lookup, same
    mechanism as the desired_cache patch above) with a wider one-shot fetch
    covering the whole replay range, cached for this process's lifetime --
    never touches the live nq_gate.py file or its normal rolling-window
    behavior outside this script.
    """
    from datetime import timedelta as _timedelta
    from zoneinfo import ZoneInfo as _ZoneInfo

    def _wide_bundle():
        from us_futures_overnight import (
            ES_YAHOO,
            NQ_YAHOO,
            fetch_yahoo_daily_closes,
            fetch_yahoo_intraday_closes,
        )

        tz = _ZoneInfo("Asia/Taipei")
        end = datetime.now(tz=tz).date() + _timedelta(days=1)
        start = end - _timedelta(days=lookback_days)
        nq_1h = fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval="1h")
        es_1h = fetch_yahoo_intraday_closes(ES_YAHOO, start, end, interval="1h")
        nq_d = fetch_yahoo_daily_closes(NQ_YAHOO, start - _timedelta(days=30), end)
        es_d = fetch_yahoo_daily_closes(ES_YAHOO, start - _timedelta(days=30), end)
        us_dates = sorted(set(nq_d.index.astype(str)) | set(es_d.index.astype(str)))
        return nq_1h, es_1h, nq_d, es_d, us_dates

    _nq_gate_mod._load_futures_bundle = _wide_bundle


def patch_nq_1m_gate_for_day(day: str, *, pad_days: int = 3) -> None:
    """Widen nq_1m_spread_gate.py's NQ 1m fetch window to cover ``day`` (2026-08-13 fix).

    ``patch_nq_gate_for_backfill`` above only patches ``nq_gate._load_futures_
    bundle`` -- the LEGACY hourly-bar gate. Since 2026-08-11 the live recipe
    (``final_v1_6_0_pv16_1m_spread_gate``) actually gates entries via
    ``nq_1m_spread_gate.spread_side_for_day``, a completely separate module
    with its own loader (``_load_nq_1m_recent``) and its own cache key
    (``"nq_1m_live"``, not ``"nq_futures_1h"``) -- the old patch never touches
    it. That loader is hard-coded to ``datetime.now()-2d .. now+1d`` (correct
    for its live purpose: always want the freshest few days), so replaying
    ANY historical day gets a NQ 1m series with zero overlap with that day's
    timestamps -- ``_us_dev()`` always returns None, ``spread_side_for_day``
    always returns ``"none"``/``None``, and causal_engine's ssg_none branch
    hard-blocks every entry all day. Confirmed 2026-08-13: even
    tmf_replay_v3_block_vs_live.py (which only calls the OLD patch) now
    replays 2026-08-06 as 0 trades / 0pt for both variants -- a silent
    regression in the replay tooling itself, not a real "quiet day" finding.

    Fix: patch ``nq_1m_spread_gate._load_nq_1m_recent`` to fetch a window
    centered on the REPLAY day instead of "now". Yahoo's 1m-interval data is
    only retained for roughly the last 7-8 calendar days total, so this only
    works for replaying RECENT days (a wide multi-week window like
    ``patch_nq_gate_for_backfill``'s hourly one is not possible at 1m
    granularity) -- acceptable here since this replay tool is meant for
    recent-day order-layer-aware comparisons, not month-long backtests (those
    use raw ``causal_engine.simulate()`` on cached bars instead, unaffected
    by this bug). Call once per day being replayed (not once per script, since
    the window is day-specific) before ``replay_day()``.
    """
    from datetime import timedelta as _timedelta
    from datetime import date as _date

    day_date = _date.fromisoformat(day)
    start = day_date - _timedelta(days=pad_days)
    end = day_date + _timedelta(days=pad_days)

    def _wide_nq_1m():
        from us_futures_overnight import NQ_YAHOO, fetch_yahoo_intraday_closes

        return fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval="1m")

    _nq_1m_spread_gate_mod._load_nq_1m_recent = _wide_nq_1m


_desired_cache_mod._disk_path = lambda: _SCRATCH_DESIRED_CACHE  # noqa: SLF001

_TZ = ZoneInfo("Asia/Taipei")
TMF_POINT_VALUE = 10.0
RAIL_MATCH_PTS = 3.0  # matches ORDER_TMF_CHANNEL_RAIL_MATCH_PTS default


def _empty_ledger() -> dict:
    return {
        "quiet_pv_since": None,
        "quiet_pv_value": None,
        "quiet_not_quiet_since": None,
        "cancel_throttle_last": {"S": None, "L": None},
        "position_open_ts": None,
        "position_open_sig": None,
    }


def _bars_from_cache_rows(day: str, rows: list[dict]) -> list[dict]:
    return [
        {
            "t": f"{day}T{r.get('t')}:00.000+08:00",
            "o": float(r["o"]),
            "h": float(r["h"]),
            "l": float(r["l"]),
            "c": float(r["c"]),
            "v": float(r.get("v") or 0),
        }
        for r in rows
    ]


def replay_day(
    day: str, rows: list[dict], recipe: dict, *, min_bars: int = 20,
    pre_open_flatten_hhmm: str | None = None,
) -> dict:
    """``pre_open_flatten_hhmm`` (any non-None value enables it, e.g. "08:40"):
    hypothesis test (2026-08-09, triggered by the 2026-07-29 forensics --
    both the live and v1.2.0-baseline recipes rode an overnight position
    straight through the ~2400pt 08:45 day-session-open gap and only got
    flattened ~90min later by the wall-clock safety net, by which point the
    P&L was already locked in one direction or the other; stop_pts=150
    never fired because the move was a GAP, not something price traded
    through). NOT a live feature -- proactively flattens any open paper
    position on the LAST available bar before the night->day session
    transition (the market is genuinely closed 05:00-08:44, so the bar
    array jumps straight from e.g. "04:59" to "08:45" with nothing between
    -- a fixed HH:MM window would never match anything real). Off (None)
    reproduces prior behavior exactly.
    """
    bars = _bars_from_cache_rows(day, rows)
    ledger = _empty_ledger()
    paper_pos: dict | None = None  # {"s": "L"/"S", "n": 1, "ep": float}
    working: dict[str, float | None] = {"S": None, "L": None}
    trades: list[dict] = []
    n_place = 0
    n_cancel = 0
    n_throttled_cancel = 0
    n_pre_open_flatten = 0

    # i = index of the bar just closed (bars[:i] = what the signal has seen).
    # A want computed from bars[:i] can only be touched by bars[i] onward --
    # checking it against bars[:i]'s own last bar (the bar that produced the
    # want) would be look-ahead. So each tick: (a) compute want from
    # bars[:i], update the resting order-book state, THEN (b) check that
    # resting state against bars[i] (the NEXT, not-yet-seen bar)'s range.
    for i in range(min_bars, len(bars)):
        window = bars[:i]
        bar = window[-1]
        next_bar = bars[i]
        now = datetime.fromisoformat(bar["t"])

        clear_desired_cache()
        desired = desired_from_simulate(window, day=day, recipe=recipe)
        if not desired.get("ok"):
            continue

        want_s = desired.get("want_s")
        want_l = desired.get("want_l")

        # Independent max-hold safety net first (matches reconcile_once order).
        ledger, elapsed_hold_min, safety_why = check_max_hold_safety_net(
            ledger,
            broker_live=paper_pos,
            max_hold_safety_min=float(recipe.get("max_hold_safety_min", 90.0)),
            now=now,
        )
        if safety_why and paper_pos:
            xp = float(bar["c"])
            pnl = (
                (xp - float(paper_pos["ep"]))
                if paper_pos["s"] == "L"
                else (float(paper_pos["ep"]) - xp)
            )
            trades.append(
                dict(s=paper_pos["s"], ep=paper_pos["ep"], xp=xp, pnl=round(pnl, 1),
                     et=None, xt=bar["t"], why="max_hold_safety_net")
            )
            paper_pos = None
            working = {"S": None, "L": None}

        # Hypothesis test: proactively flatten before the day-session gap
        # instead of riding it and relying on the (much later) wall-clock
        # safety net. Found while wiring this up: the night session's last
        # bar and the day session's first bar are NOT ~1 minute apart --
        # the market is genuinely closed 05:00-08:44, so the bar ARRAY
        # jumps straight from e.g. "04:59" to "08:45" with nothing between.
        # A fixed HH:MM window (e.g. "08:40"<=hm<"08:45") can therefore
        # never match any actual bar. Detect the night->day transition
        # itself instead: flatten on the last available bar before the
        # jump, whatever its clock time happens to be that day.
        hm_now = bar["t"][11:16]
        hm_next = next_bar["t"][11:16]
        crossing_into_day_open = hm_now < "08:45" <= hm_next < "13:45"
        if (
            pre_open_flatten_hhmm is not None
            and paper_pos is not None
            and crossing_into_day_open
        ):
            xp = float(bar["c"])
            pnl = (
                (xp - float(paper_pos["ep"]))
                if paper_pos["s"] == "L"
                else (float(paper_pos["ep"]) - xp)
            )
            trades.append(
                dict(s=paper_pos["s"], ep=paper_pos["ep"], xp=xp, pnl=round(pnl, 1),
                     et=None, xt=bar["t"], why="pre_open_flatten")
            )
            paper_pos = None
            working = {"S": None, "L": None}
            n_pre_open_flatten += 1

        want_s, want_l, quiet_skip, ledger = apply_quiet_flat_entry_gate(
            want_s, want_l, broker_live=paper_pos, desired=desired,
            recipe=recipe, ledger=ledger, now=now,
        )
        want_s, want_l, _scale_block = block_same_side_scale_wants(
            want_s, want_l, open_pos=paper_pos, max_lots=1,
        )

        for side, want in (("S", want_s), ("L", want_l)):
            cur = working.get(side)
            if want is None:
                if cur is not None:
                    suppress, ledger = should_throttle_quiet_cancel(
                        side, quiet_skip_reason=quiet_skip, open_pos=paper_pos,
                        ledger=ledger, now=now,
                    )
                    if suppress:
                        n_throttled_cancel += 1
                        continue
                    working[side] = None
                    n_cancel += 1
                continue
            if cur is None:
                working[side] = float(want)
                n_place += 1
            elif abs(float(want) - cur) > RAIL_MATCH_PTS:
                working[side] = float(want)
                n_place += 1
                n_cancel += 1

        # Fill check against the NEXT bar's range (not yet seen by the
        # signal that produced `want` -- avoids look-ahead).
        lo, hi = float(next_bar["l"]), float(next_bar["h"])
        for side in ("S", "L"):
            px = working.get(side)
            if px is None or not (lo <= px <= hi):
                continue
            if paper_pos is None:
                paper_pos = {"s": side, "n": 1, "ep": px}
                working = {"S": None, "L": None}
                break
            if paper_pos["s"] != side:
                xp = px
                pnl = (
                    (xp - float(paper_pos["ep"]))
                    if paper_pos["s"] == "L"
                    else (float(paper_pos["ep"]) - xp)
                )
                trades.append(
                    dict(s=paper_pos["s"], ep=paper_pos["ep"], xp=xp, pnl=round(pnl, 1),
                         et=next_bar["t"], xt=next_bar["t"], why="order_layer_fill")
                )
                paper_pos = None
                working = {"S": None, "L": None}
                break

    return dict(
        day=day, n_trades=len(trades), sum_pnl=round(sum(t["pnl"] for t in trades), 1),
        n_place=n_place, n_cancel=n_cancel, n_throttled_cancel=n_throttled_cancel,
        n_pre_open_flatten=n_pre_open_flatten,
        trades=trades, end_open_pos=paper_pos,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-08-06")
    ap.add_argument("--source", default="tx_1m_tick_built_582d")
    ap.add_argument(
        "--nq-lookback-days", type=int, default=60,
        help="widen nq_gate's NQ/ES fetch window to cover this many days back "
             "from today, so historical --day values outside the live "
             "10-day rolling window don't spuriously read as ssg_none",
    )
    ap.add_argument(
        "--pre-open-flatten", default=None,
        help='e.g. "08:40" -- proactively flatten any open position at/after '
             "this HH:MM, strictly before day-session open, instead of "
             "riding the gap. Off by default.",
    )
    args = ap.parse_args()

    patch_nq_gate_for_backfill(lookback_days=args.nq_lookback_days)

    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    rows = load_day(args.day, source=args.source)
    if not rows:
        print(f"no rows for {args.day} in {args.source}")
        return
    result = replay_day(args.day, rows, recipe, pre_open_flatten_hhmm=args.pre_open_flatten)
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, ensure_ascii=False))
    print("\ntrades:")
    for t in result["trades"]:
        print(f"  {t['s']} ep={t['ep']} xp={t['xp']} pnl={t['pnl']:+.1f} xt={t['xt']} why={t['why']}")


if __name__ == "__main__":
    main()
