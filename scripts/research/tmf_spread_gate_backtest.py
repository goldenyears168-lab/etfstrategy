#!/usr/bin/env python3
"""2026-08-11: convert the TW-US 5h MA-deviation spread IC finding (IS
p<0.001, OOS p<1e-6 across 15/30/60min horizons, mean-reversion direction)
into an actual trading rule and backtest it with full P&L, replacing the
existing NQ-direction session_side_gate entirely (same cell book, same
everything else -- only the direction-gate SOURCE changes).

spread(t) = tw_dev(t) - us_dev(t), tw_dev = TX deviation from its own
trailing-5h 1m-bar MA, us_dev = NQ deviation from its own trailing-5h
(5 settled hourly bars) MA -- same computation as
tmf_tw_us_ma5h_deviation_spread_ic.py. Distribution: mean~0.03, std~0.58.

Gate rule (mean-reversion, since IC is negative):
  spread(t) >= +threshold  -> expect reversion DOWN -> "S"
  spread(t) <= -threshold  -> expect reversion UP   -> "L"
  else                     -> "none" (no strong signal, block both)

Screens threshold in {0.2, 0.3, 0.4, 0.5} on IS(22d) first, confirms best on
OOS(66d), paired day-clustered significance vs the CURRENT LIVE baseline
(unchanged specialized_cell_book() + existing NQ gate).
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
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from us_futures_overnight import price_at_or_before  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
TW_WINDOW_MIN = 5 * 60

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


def us_ma5h_dev(bundle, dt_et, min_age):
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    points = []
    for k in range(6):
        px = price_at_or_before(nq_1h, dt_et - timedelta(hours=k), min_age=min_age)
        if px is not None:
            points.append(px)
        if len(points) >= 6:
            break
    if len(points) < 6:
        return None
    now_px = points[0]
    ma5h = sum(points[1:6]) / 5.0
    if ma5h <= 0:
        return None
    return (now_px - ma5h) / ma5h * 100.0


def spread_gate_for_day(day, T, C, bundle, min_age, threshold, us_cache):
    out = {}
    n = len(C)
    roll_sum = sum(C[:TW_WINDOW_MIN]) if n >= TW_WINDOW_MIN else 0.0
    for i, t in enumerate(T):
        if i < TW_WINDOW_MIN:
            out[t] = "none"
            continue
        if i > TW_WINDOW_MIN:
            roll_sum += C[i - 1] - C[i - 1 - TW_WINDOW_MIN]
        tw_ma = roll_sum / TW_WINDOW_MIN
        if tw_ma <= 0:
            out[t] = "none"
            continue
        tw_dev = (C[i] - tw_ma) / tw_ma * 100.0
        dt_et = datetime.fromisoformat(t).astimezone(_TZ).astimezone(nq_signal.TZ_ET)
        cache_key = dt_et.strftime("%Y-%m-%d %H")
        if cache_key not in us_cache:
            us_cache[cache_key] = us_ma5h_dev(bundle, dt_et, min_age)
        us_dev = us_cache[cache_key]
        if us_dev is None:
            out[t] = "none"
            continue
        spread = tw_dev - us_dev
        if spread >= threshold:
            out[t] = "S"
        elif spread <= -threshold:
            out[t] = "L"
        else:
            out[t] = "none"
    return out


def _source_for(day, source_map):
    if isinstance(source_map, dict):
        return source_map.get(day, "tx_1m_fullnight_cache_full.json")
    return source_map


def load_arrays(day, source_map):
    source = _source_for(day, source_map)
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    # Real instants: T feeds both the NQ 5h-MA lookup (converted to ET) and the
    # per-bar NQ/ES gate, so the naive f"{day}T{t}" form would read US futures
    # from 24h earlier for every 00:00-04:59 bar (fixed 2026-08-11).
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


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


def run_window(label, days, source_map, recipe_base, vix, bundle, min_age, threshold):
    deltas = []
    total_n = 0
    us_cache = {}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        O, H, L, C, V, T = arr
        baseline_gate = continuous_gate_for_day(d, T, source=_source_for(d, source_map))
        recipeB = deepcopy(recipe_base)
        recipeB["session_side_gate"] = baseline_gate
        tradesB, *_ = simulate(O, H, L, C, V, T, recipeB, vix_delta=vix)
        netB = sum(t["pnl"] for t in tradesB)

        spread_gate = spread_gate_for_day(d, T, C, bundle, min_age, threshold, us_cache)
        recipeC = deepcopy(recipe_base)
        recipeC["session_side_gate"] = spread_gate
        tradesC, *_ = simulate(O, H, L, C, V, T, recipeC, vix_delta=vix)
        netC = sum(t["pnl"] for t in tradesC)

        deltas.append(netC - netB)
        total_n += len(tradesC)

    stats = paired_stats(deltas)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None
    print(f"{label} threshold={threshold}: n={stats['n']} sum_delta={sum(deltas):.1f} "
          f"mean={stats['mean']:.2f} t={stats['t']:.3f} p={stats['p']} "
          f"excl_top_day_mean={excl_mean} candidate_trades={total_n}")
    return stats


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    min_age = nq_signal.NQ_ES_1H_MIN_AGE
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    print("=== IS(22d) screen across thresholds ===")
    is_results = {}
    for threshold in (0.2, 0.3, 0.4, 0.5):
        is_results[threshold] = run_window(
            "IS_22d", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix, bundle, min_age, threshold)

    promising = [th for th, r in is_results.items() if r["p"] is not None and r["p"] < 0.20]
    print(f"\nthresholds with IS p<0.20: {promising}")
    if not promising:
        print("nothing promising -- stopping here.")
        return

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    print("\n=== OOS(66d) confirm ===")
    for threshold in promising:
        run_window("OOS_66d", oos_days, "tx_1m_fullnight_cache_full.json",
                   recipe_base, vix, bundle, min_age, threshold)


if __name__ == "__main__":
    main()
