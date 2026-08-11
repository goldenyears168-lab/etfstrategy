#!/usr/bin/env python3
"""2026-08-11: the 1m-granularity window sweep (tw_nq_1m_window_finegrain,
run interactively, not committed as a script) found avg|IC| peaks on a FLAT
plateau across 90-120min real-1m windows (peak at 105min, but 90/105/120 are
statistically indistinguishable -- picking a single point within the
plateau from that scan would be overfitting to 3 days of noise). Since a
genuine 1m NQ history long enough for IS(22d) doesn't exist yet (Yahoo 1m
caps at ~8 days), this locks in ONE representative value from that plateau
-- 2h -- chosen in advance (not selected post-hoc from this run's own
results) and runs it through the SAME rigor bar as every other candidate
tonight using the hourly-NQ data that has the history to support it:
IS(22d) threshold screen -> OOS(66d) confirm -> 3-holdout(2025) if still
promising. Same "replace the NQ gate entirely" trading rule as
tmf_spread_gate_backtest.py (5h version); only TW_WINDOW_MIN and the US
lookback change from 5h to 2h.
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
WINDOW_HOURS = 2
TW_WINDOW_MIN = WINDOW_HOURS * 60
US_BARS = WINDOW_HOURS  # settled hourly bars to average

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

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def us_ma_dev(bundle, dt_et, min_age):
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    points = []
    for k in range(US_BARS + 1):
        px = price_at_or_before(nq_1h, dt_et - timedelta(hours=k), min_age=min_age)
        if px is not None:
            points.append(px)
        if len(points) >= US_BARS + 1:
            break
    if len(points) < US_BARS + 1:
        return None
    now_px = points[0]
    ma = sum(points[1:US_BARS + 1]) / US_BARS
    if ma <= 0:
        return None
    return (now_px - ma) / ma * 100.0


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
            us_cache[cache_key] = us_ma_dev(bundle, dt_et, min_age)
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

    print(f"=== IS(22d) screen across thresholds (window={WINDOW_HOURS}h) ===")
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
    oos_promising = []
    for threshold in promising:
        r = run_window("OOS_66d", oos_days, "tx_1m_fullnight_cache_full.json",
                        recipe_base, vix, bundle, min_age, threshold)
        if r["p"] is not None and r["p"] < 0.20:
            oos_promising.append(threshold)

    print(f"\nthresholds with OOS p<0.20: {oos_promising}")
    if not oos_promising:
        print("nothing cleared OOS -- stopping here (no 3-holdout run).")
        return

    print("\n=== 3-holdout(2025) confirm ===")
    for threshold in oos_promising:
        for label, source in HOLDOUT_SOURCES.items():
            days = list_days(source=source)
            run_window(label, days, source, recipe_base, vix, bundle, min_age, threshold)


if __name__ == "__main__":
    main()
