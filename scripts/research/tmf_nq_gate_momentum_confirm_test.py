#!/usr/bin/env python3
"""2026-08-11: test a hybrid NQ gate -- require recent-momentum confirmation
on top of the existing vs-prior-close cumulative signal, per user's design
question ("why compare to prior close instead of a few minutes ago").

Finest free granularity available (Yahoo NQ/ES) is 1h bars -- "a few
minutes" isn't buildable from this data source, so "recent momentum" here
means "direction over the last N settled hourly bars", still using the
SAME forming-bar-safe price_at_or_before(min_age=1h) fix from tonight.

New gate logic (only CHANGES from the live continuous gate when base
signal is "up"/"down" -- "flat"/"missing" still map to "none" unchanged):
  base_side = bias_side(nq_overnight_pct)  # vs prior US RTH close, existing
  momentum_pct = (price_now - price_N_hours_ago) / price_N_hours_ago * 100
  if base_side == "up" and momentum_pct <= 0: suppress to "none"
  if base_side == "down" and momentum_pct >= 0: suppress to "none"
  else: keep base_side's L/S as before

Tests momentum windows N in {1, 2, 3} hours. Screens IS(22d) first (cheap),
confirms best on OOS(66d), 3-holdout only if both look promising.
"""
from __future__ import annotations

import statistics as st
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel import nq_gate as nq_gate_mod  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IS_DAYS = JULY_DAYS + AUG_DAYS


def momentum_confirmed_gate_for_day(day: str, T: list[str], momentum_hours: int) -> dict[str, str]:
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    out = {}
    for t in T:
        dt_tw = datetime.fromisoformat(t).astimezone(_TZ)
        dt_et = dt_tw.astimezone(nq_signal.TZ_ET)
        snap = nq_signal.futures_overnight_at(
            dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d, us_dates=us_dates
        )
        nq = None if snap is None else snap.get("nq_overnight_pct")
        base_side = nq_signal.bias_side(nq)
        side = {"up": "L", "down": "S"}.get(base_side, "none")
        if side != "none":
            from us_futures_overnight import price_at_or_before

            min_age = nq_signal.NQ_ES_1H_MIN_AGE
            now_px = price_at_or_before(nq_1h, dt_et, min_age=min_age)
            past_px = price_at_or_before(nq_1h, dt_et - timedelta(hours=momentum_hours), min_age=min_age)
            if now_px is None or past_px is None or past_px <= 0:
                side = "none"
            else:
                momentum_pct = (now_px - past_px) / past_px * 100.0
                if side == "L" and momentum_pct <= 0:
                    side = "none"
                elif side == "S" and momentum_pct >= 0:
                    side = "none"
        out[t] = side
    return out


def load_arrays(day, source_map):
    source = source_map.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


def run_day(arr, gate, recipe_base, vix):
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    O, H, L, C, V, T = arr
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    return round(sum(t["pnl"] for t in trades), 1), len(trades)


def paired_stats(deltas):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=deltas[0] if deltas else 0.0, std=0.0, t=0.0, p=1.0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    t = 0.0 if sd == 0 else mean / (sd / (n ** 0.5))
    try:
        from scipy import stats as sp
        p = float(2 * (1 - sp.t.cdf(abs(t), df=n - 1)))
    except Exception:
        p = None
    return dict(n=n, mean=mean, std=sd, t=t, p=p)


def run_window(label, days, source_map, recipe_base, vix, momentum_hours):
    from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day

    deltas = []
    total_n = 0
    for d in days:
        arr = load_arrays(d, source_map if isinstance(source_map, dict) else {d: source_map})
        if arr is None:
            continue
        base_gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        momentum_gate = momentum_confirmed_gate_for_day(d, arr[5], momentum_hours)
        base_net, base_n = run_day(arr, base_gate, recipe_base, vix)
        cand_net, cand_n = run_day(arr, momentum_gate, recipe_base, vix)
        deltas.append(cand_net - base_net)
        total_n += cand_n
    stats = paired_stats(deltas)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None
    print(f"{label} momentum={momentum_hours}h: n={stats['n']} sum_delta={sum(deltas):.1f} "
          f"mean={stats['mean']:.2f} t={stats['t']:.3f} p={stats['p']} excl_top_day_mean={excl_mean} "
          f"candidate_trades={total_n}")
    return stats


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    print("=== IS(22d) screen across momentum windows ===")
    is_results = {}
    for hours in (1, 2, 3):
        is_results[hours] = run_window("IS_22d", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix, hours)

    promising = [h for h, r in is_results.items() if r["p"] is not None and r["p"] < 0.20]
    print(f"\nmomentum windows with IS p<0.20: {promising}")
    if not promising:
        print("nothing promising enough to confirm on OOS -- stopping here.")
        return

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    print("\n=== OOS(66d) confirm ===")
    for hours in promising:
        run_window("OOS_66d", oos_days, "tx_1m_fullnight_cache_full.json", recipe_base, vix, hours)


if __name__ == "__main__":
    main()
