#!/usr/bin/env python3
"""Continuous (per-bar) NQ gate vs the current frozen-at-session-open gate
(2026-08-10), now that causal_engine.py's session_side_gate lookup supports
a per-bar-timestamp key (falls back to the original per-day key -- see the
docstring on that change and SessionSideGatePerBarKeyTest in
tests/test_tmf_channel_package.py for the backward-compatibility proof).

Motivation: the currently-live gate (cell.bias=True, nq_side_for_day())
anchors ONCE per session (08:45 for day, 15:00 for night) and never
updates again for the rest of that session, even though NQ/ES futures
keep trading (near-24h) throughout the TX day session. User's question:
"should be real-time, no?" This tests that directly instead of assuming
either direction -- recompute the NQ/ES overnight-pct fresh at EVERY 1-min
bar's own timestamp (continuous) and compare against the frozen-at-open
version already validated (bias_ablation: beats bias=False on both IS and
OOS, OOS p=0.086) across the same 22-day in-sample + 66-day OOS windows.

Does NOT touch src/tmf_channel/causal_engine.py further (already changed,
tested, backward compatible), src/order/, config/order.yaml, .env,
launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy
from datetime import datetime

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_channel.nq_gate import nq_side_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})


def assert_real_timestamps(day: str, T: list[str], source: str | None = None) -> None:
    """Guard against `T = f"{day}T{t}..."` being passed in for a session-convention
    source.

    Such a T is 24h early for every 00:00-04:59 bar, so the NQ/ES snapshot below
    is taken a full day before the bar it gates (2026-08-11 audit). Build T with
    ``tmf_channel.cache_store.bar_timestamps(day, rows, source=source)`` instead.
    Raises when the source is known to be session-convention; otherwise warns
    once, since for a calendar-convention source (e.g. tx_1m_tick_built_fullnight_aug)
    a post-midnight bar dated `day` is legitimately correct.
    """
    stale = [t for t in T if t[:10] == day and t[11:16] < "05:00"]
    if not stale:
        return

    hint = (
        f"{len(stale)} post-midnight bars in day={day} are timestamped with the "
        f"session date (first: {stale[0]}). For a session-convention source "
        "(tx_1m_fullnight_cache*) those bars are calendar day+1, so the NQ/ES "
        "lookup would be 24h stale. Build T with "
        "cache_store.bar_timestamps(day, rows, source=source) and pass source= here."
    )
    if source is not None:
        from tmf_channel.cache_store import post_midnight_convention

        if post_midnight_convention(source) == "session":
            raise ValueError(hint)
        return  # calendar-convention source: session-dated tail bars are correct

    # Fail closed. Before 2026-08-11 this path silently produced 24h-misaligned
    # gate values for ~26% of bars in every fullnight-cache backtest; a warning
    # is not enough, because the output still looks like a valid number.
    raise ValueError(
        hint + " (no source= was passed, so this cannot be verified -- if these "
        "bars really are calendar-convention, pass source= explicitly.)"
    )


def continuous_gate_for_day(
    day: str, T: list[str], *, source: str | None = None, allow_stale: bool = False
) -> dict[str, str]:
    """One session_side_gate value per BAR (not per day) -- freshly
    recomputed at each bar's own timestamp, using the same underlying
    nq_signal.futures_overnight_at()/bias_side() the live (frozen-anchor)
    gate uses, just with a moving `dt` instead of a fixed session-open dt.

    `T` must hold REAL instants (see assert_real_timestamps). `allow_stale=True`
    deliberately reproduces the pre-2026-08-11 misaligned behaviour and exists
    only for audit_bar_order_and_timestamp_impact.py -- never use it to produce
    a research result."""
    if not allow_stale:
        assert_real_timestamps(day, T, source)
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Taipei")
    out = {}
    for t in T:
        dt_tw = datetime.fromisoformat(t).astimezone(tz)
        snap = nq_signal.futures_overnight_at(
            dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d, us_dates=us_dates
        )
        nq = None if snap is None else snap.get("nq_overnight_pct")
        side = nq_signal.bias_side(nq)
        out[t] = {"up": "L", "down": "S"}.get(side, "none")
    return out


def run_day(day: str, recipe_frozen: dict, recipe_continuous_base: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    frozen_side = nq_side_for_day(day, hm="08:45")
    r_frozen = dict(recipe_frozen)
    r_frozen["session_side_gate"] = {day: frozen_side} if frozen_side is not None else {}

    cont_gate = continuous_gate_for_day(day, T, source=source)
    r_cont = dict(recipe_continuous_base)
    r_cont["session_side_gate"] = cont_gate

    trades_frozen, *_ = simulate(O, H, L, C, V, T, r_frozen, vix_delta=vix)
    trades_cont, *_ = simulate(O, H, L, C, V, T, r_cont, vix_delta=vix)
    net_frozen = round(sum(t["pnl"] for t in trades_frozen), 1)
    net_cont = round(sum(t["pnl"] for t in trades_cont), 1)

    return dict(
        day=day, n_frozen=len(trades_frozen), net_frozen=net_frozen,
        n_continuous=len(trades_cont), net_continuous=net_cont,
        diff=round(net_cont - net_frozen, 1),
    )


def run_window(days, recipe_frozen, recipe_cont, vix, label):
    rows = []
    for day in days:
        r = run_day(day, recipe_frozen, recipe_cont, vix)
        if r.get("skipped"):
            continue
        rows.append(r)
        print(json.dumps(r), flush=True)

    diffs = [r["diff"] for r in rows]
    n = len(rows)
    mean_d = st.mean(diffs)
    std_d = st.stdev(diffs) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 else 0.0
    try:
        from scipy import stats as sp

        p_val = float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
    except Exception:
        p_val = None

    print(f"\n=== {label} summary ===")
    print(f"n={n} frozen_sum={sum(r['net_frozen'] for r in rows):.1f} "
          f"continuous_sum={sum(r['net_continuous'] for r in rows):.1f}")
    print(f"diff(continuous-frozen) mean={mean_d:.2f} std={std_d:.2f} t={t_stat:.3f} p={p_val}")
    return dict(n=n, mean_diff=mean_d, std_diff=std_d, t=t_stat, p=p_val, rows=rows)


def main():
    # 2026-08-10: lookback_days=60 silently returned "none" (spurious --
    # no data, not a real signal) for 82% of the 66-day OOS window (54/66
    # days). Yahoo actually serves 1h data back to ~2025-05 when asked for
    # a wide enough window (confirmed by direct fetch) -- the original 60
    # was just too narrow. Widened so this re-validation is on real data.
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}

    recipe_frozen = deepcopy(PAPER_RECIPE)
    recipe_frozen.setdefault("hang_anchor", "O")
    recipe_cont = deepcopy(PAPER_RECIPE)
    recipe_cont.setdefault("hang_anchor", "O")

    is_days = JULY_DAYS + AUG_DAYS
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    is_result = run_window(is_days, recipe_frozen, recipe_cont, vix, "IN-SAMPLE (22 days)")
    oos_result = run_window(oos_days, recipe_frozen, recipe_cont, vix, "OUT-OF-SAMPLE (66 days)")

    out_path = "reports/research/channel_lab/tmf_continuous_gate_vs_frozen_anchor_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(in_sample=is_result, out_of_sample=oos_result), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
