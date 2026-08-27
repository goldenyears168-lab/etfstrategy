#!/usr/bin/env python3
"""30m-primary/1m-calib architecture: cell tune for night|climax_dn.

Assigned cell: night|climax_dn. All other 15 cells stay at the current
live-equivalent default (specialized_cell_book()) throughout. Only this
cell's hang_lo/hang_hi/max_hold_bars/early_fill_gamma/block are varied.

Methodology: day-clustered paired comparison (candidate net pnl for THIS
cell's trades minus baseline net pnl for THIS cell's trades, one number per
day), across the 22-day in-sample window, then OOS validation of the single
winning candidate against the 66-day out-of-sample window. Uses the
already-prototyped 30-min-primary/1m-calib regime feed from
scripts/research/tmf_30m_primary_1m_calib_prototype.py (imported, not
re-implemented).

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

SESSION = "night"
PV = "climax_dn"

_ORIG_CLASSIFY_PV = ce.classify_pv

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

OOS_SOURCE = "tx_1m_fullnight_cache_full.json"
OOS_DAYS = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]

BASELINE_CELL = dict(specialized_cell_book()["night"]["climax_dn"])

# Candidate parameter sets for night|climax_dn ONLY. Baseline = current
# live-equivalent default (freeze_cell_book night_base; no SPECIALIZED or
# CELL_TUNE_V2 patch touches this cell): hang_lo=18, hang_hi=32,
# early_fill_gamma=5.0, max_hold_bars=20, block=["L"] (long blocked, short
# only -- climax_dn = climax down-move).
CANDIDATES: dict[str, dict] = {
    "baseline": {},
    "wider_24_40": {"hang_lo": 24.0, "hang_hi": 40.0},
    "much_wider_30_48": {"hang_lo": 30.0, "hang_hi": 48.0},
    "longer_hold_30": {"max_hold_bars": 30},
    "longer_hold_40": {"max_hold_bars": 40},
    "wider_24_40_hold30": {"hang_lo": 24.0, "hang_hi": 40.0, "max_hold_bars": 30},
    "wider_24_40_hold40_g8": {"hang_lo": 24.0, "hang_hi": 40.0, "max_hold_bars": 40, "early_fill_gamma": 8.0},
    "gamma0": {"early_fill_gamma": 0.0},
    "full_block": {"block": ["L", "S"]},
}


def hhmm(ts: str) -> str:
    return ts[11:16] if "T" in ts else ts[:5]


def session_of(hm: str) -> str:
    return "day" if "08:45" <= hm < "13:45" else "night"


def build_arrays(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_day_all_candidates(day: str, source: str, base_recipe: dict, vix: dict) -> dict:
    arrays = build_arrays(day, source)
    if arrays is None:
        return {}
    O, H, L, C, V, T = arrays
    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    out = {}
    try:
        for name, patch in CANDIDATES.items():
            book = {
                "day": deepcopy(base_recipe["session_pv_book"]["day"]),
                "night": deepcopy(base_recipe["session_pv_book"]["night"]),
            }
            cell = dict(BASELINE_CELL)
            cell.update(patch)
            book[SESSION][PV] = cell
            recipe = dict(base_recipe)
            recipe["session_pv_book"] = book
            trades, events, ws, wl, rvol, regime, open_pos = ce.simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
            cell_trades = [
                tr for tr in trades
                if tr.get("regime_e") == PV and session_of(hhmm(tr.get("et", ""))) == SESSION
            ]
            out[name] = dict(
                n=len(cell_trades),
                net=round(sum(t["pnl"] for t in cell_trades), 1),
            )
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    return out


def day_clustered_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n == 0:
        return dict(n=0, mean=None, std=None, t=None, p=None)
    mean = st.mean(deltas)
    std = st.stdev(deltas) if n > 1 else 0.0
    if n > 1 and std > 0:
        se = std / (n ** 0.5)
        t = mean / se
        try:
            from scipy import stats as sps
            p = 2 * (1 - sps.t.cdf(abs(t), df=n - 1))
        except Exception:
            # normal approx if scipy unavailable
            import math
            p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / (2 ** 0.5))))
    else:
        t, p = None, None
    return dict(n=n, mean=round(mean, 2), std=round(std, 2),
                t=(round(t, 3) if t is not None else None),
                p=(round(p, 4) if p is not None else None))


def evaluate(days: list[str], sources_for_day: dict, base_recipe: dict, vix: dict, label: str):
    per_day = {}
    for day in days:
        source = sources_for_day[day] if isinstance(sources_for_day, dict) else sources_for_day
        r = run_day_all_candidates(day, source, base_recipe, vix)
        if r:
            per_day[day] = r
        print(f"[{label}] {day}: " + json.dumps(r), flush=True)

    n_appear = sum(1 for d in per_day if per_day[d].get("baseline", {}).get("n", 0) > 0)
    summary = {}
    for name in CANDIDATES:
        deltas = []
        for day, cells in per_day.items():
            base_net = cells.get("baseline", {}).get("net", 0.0)
            cand_net = cells.get(name, {}).get("net", 0.0)
            deltas.append(cand_net - base_net)
        summary[name] = dict(stats=day_clustered_stats(deltas), deltas=deltas)
    return per_day, summary, n_appear


def main():
    vix = load_vixtwn_delta() or {}
    base_recipe = deepcopy(PAPER_RECIPE)
    base_recipe.setdefault("hang_anchor", "O")
    base_recipe["session_pv_book"] = specialized_cell_book()

    print("=== IN-SAMPLE (22 days) ===")
    per_day_is, summary_is, n_appear_is = evaluate(
        IN_SAMPLE_DAYS, SOURCE_FOR_DAY, base_recipe, vix, "IS"
    )

    print("\n=== IN-SAMPLE candidate summary (delta vs baseline, day-clustered) ===")
    for name, s in summary_is.items():
        print(name, s["stats"])

    print(f"\ndays with >=1 baseline trade in this cell: {n_appear_is}/{len(IN_SAMPLE_DAYS)}")

    # pick best candidate by mean delta among non-baseline, require it to
    # beat baseline (mean>0) -- else recommend "no change".
    ranked = sorted(
        ((name, s["stats"]) for name, s in summary_is.items() if name != "baseline"),
        key=lambda kv: (kv[1]["mean"] if kv[1]["mean"] is not None else -1e18),
        reverse=True,
    )
    best_name, best_stats = ranked[0] if ranked else (None, None)

    out = dict(
        cell=f"{SESSION}|{PV}",
        baseline=BASELINE_CELL,
        in_sample=summary_is,
        n_appear_in_sample_days=n_appear_is,
        n_in_sample_days=len(IN_SAMPLE_DAYS),
    )

    if best_name is not None and best_stats["mean"] is not None and best_stats["mean"] > 0:
        # exclude-largest-|delta|-day check
        deltas = summary_is[best_name]["deltas"]
        if deltas:
            max_abs_idx = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
            excl = deltas[:max_abs_idx] + deltas[max_abs_idx + 1:]
            mean_excl = st.mean(excl) if excl else None
            print(f"\n[robustness] best={best_name} mean_incl={best_stats['mean']} "
                  f"mean_excl_largest_day={round(mean_excl, 2) if mean_excl is not None else None}")
            out["robustness_excl_largest_day_mean"] = round(mean_excl, 2) if mean_excl is not None else None

        print(f"\n=== Best in-sample candidate: {best_name} {CANDIDATES[best_name]} ===")
        print(best_stats)

        print("\n=== OOS validation (66 days) ===")
        per_day_oos, summary_oos, n_appear_oos = evaluate(
            OOS_DAYS, OOS_SOURCE, base_recipe, vix, "OOS"
        )
        oos_stats = summary_oos[best_name]["stats"]
        print(f"\nOOS stats for {best_name}: {oos_stats}")
        print(f"days with >=1 baseline trade in this cell (OOS): {n_appear_oos}/{len(OOS_DAYS)}")

        out["winning_candidate"] = best_name
        out["winning_params"] = CANDIDATES[best_name]
        out["oos"] = summary_oos
        out["n_appear_oos_days"] = n_appear_oos
        out["n_oos_days"] = len(OOS_DAYS)
    else:
        print("\n=== No candidate beats baseline in-sample -> recommend no change ===")
        out["winning_candidate"] = None

    out_path = "/tmp/tmf_30m_cell_tune_night_climax_dn_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
