#!/usr/bin/env python3
"""30m-primary/1m-calib architecture: tune the day|contract cell ONLY.

Baseline = current-live-equivalent 16-cell book (specialized_cell_book()),
fed by the NEW 30-min-driven PV8 classification (build_pv30_series /
patched_classify_pv_factory from
scripts/research/tmf_30m_primary_1m_calib_prototype.py). All 15 OTHER cells
stay at the current-live default throughout -- only day|contract's
hang_lo/hang_hi/max_hold_bars/early_fill_gamma/block are swept.

Methodology: day-clustered paired comparison (candidate net pnl for
day|contract trades minus baseline net pnl for the SAME cell on the SAME
day), across the 22-day in-sample window, then OOS validation of the single
winning candidate on the 66-day out-of-sample window (2026-04-01..07-07).

Does NOT touch src/tmf_channel/causal_engine.py, src/order/, config/order.yaml,
.env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "day"
PV = "contract"

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

_OOS_ALL = list_days(source="tx_1m_fullnight_cache_full.json")
OOS_DAYS = [d for d in _OOS_ALL if d < "2026-07-08"]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"

DAY_WIN = ("08:45", "13:45")


def is_day_session(et: str) -> bool:
    hm = et[11:16]
    return DAY_WIN[0] <= hm < DAY_WIN[1]


def base_book() -> dict:
    return specialized_cell_book()


def make_recipe(cell_overrides: dict | None) -> dict:
    recipe = deepcopy(PAPER_RECIPE)
    book = base_book()
    if cell_overrides is not None:
        book[SESS][PV] = {**book[SESS][PV], **cell_overrides}
    recipe["session_pv_book"] = book
    recipe.setdefault("hang_anchor", "O")
    return recipe


def load_arrays(day: str):
    source = SOURCE_FOR_DAY[day]
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


def run_day_cell(day: str, recipe: dict, vix: dict, arrays=None) -> tuple[int, float]:
    """Return (n_trades, net_pnl) for THIS cell's (SESS, PV) trades only."""
    if arrays is None:
        arrays = load_arrays(day)
    if arrays is None:
        return 0, 0.0
    O, H, L, C, V, T = arrays
    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    n = 0
    net = 0.0
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        if is_day_session(tr["et"]) != (SESS == "day"):
            continue
        n += 1
        net += float(tr["pnl"])
    return n, net


def paired_stats(deltas: list[float]):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=deltas[0] if n else 0.0, std=0.0, t=None, p=None)
    mean = st.mean(deltas)
    std = st.stdev(deltas)
    if std == 0:
        t = float("inf") if mean != 0 else 0.0
    else:
        t = mean / (std / (n ** 0.5))
    # crude two-sided p-value via normal approx (no scipy dependency assumed)
    try:
        from scipy import stats as sstats  # type: ignore

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=n - 1))) if std else (0.0 if mean else 1.0)
    except Exception:
        import math

        # normal approximation fallback
        p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / (2 ** 0.5)))) if std else (0.0 if mean else 1.0)
    return dict(n=n, mean=mean, std=std, t=t, p=p)


def evaluate_candidate(days: list[str], candidate: dict | None, baseline_cache: dict, vix: dict, arrays_cache: dict):
    """Return list of per-day deltas (candidate net - baseline net) and n_trades totals."""
    cand_recipe = make_recipe(candidate)
    deltas = []
    cand_ns = []
    base_ns = []
    per_day = []
    for day in days:
        arrays = arrays_cache.get(day)
        if arrays is None:
            arrays = load_arrays(day)
            arrays_cache[day] = arrays
        if arrays is None:
            continue
        if day not in baseline_cache:
            baseline_cache[day] = run_day_cell(day, make_recipe(None), vix, arrays)
        base_n, base_net = baseline_cache[day]
        cand_n, cand_net = run_day_cell(day, cand_recipe, vix, arrays)
        deltas.append(cand_net - base_net)
        cand_ns.append(cand_n)
        base_ns.append(base_n)
        per_day.append(dict(day=day, base_n=base_n, base_net=base_net, cand_n=cand_n, cand_net=cand_net, delta=round(cand_net - base_net, 1)))
    return deltas, cand_ns, base_ns, per_day


def main():
    vix = load_vixtwn_delta() or {}
    baseline_cache: dict = {}
    arrays_cache: dict = {}

    print(f"=== Baseline default cell {SESS}|{PV} ===")
    print(json.dumps(base_book()[SESS][PV], default=str))

    # 1) confirm baseline trade volume for this cell across in-sample days
    total_base_n = 0
    for day in IN_SAMPLE_DAYS:
        n, net = run_day_cell(day, make_recipe(None), vix)
        baseline_cache[day] = (n, net)
        total_base_n += n
    print(f"\nbaseline in-sample n_trades total for {SESS}|{PV} = {total_base_n} across {len(IN_SAMPLE_DAYS)} days")

    if total_base_n < 15:
        print("THIN SAMPLE (<15 trades) -- reporting insufficient data, skipping sweep.")
        return

    candidates = [
        ("wider_15_35", dict(hang_lo=15.0, hang_hi=35.0)),
        ("wider_18_40", dict(hang_lo=18.0, hang_hi=40.0)),
        ("wider_20_45", dict(hang_lo=20.0, hang_hi=45.0)),
        ("hold_narrower_25", dict(max_hold_bars=25)),
        ("hold_wider_50", dict(max_hold_bars=50)),
        ("wider_hold_wider", dict(hang_lo=18.0, hang_hi=40.0, max_hold_bars=50)),
        ("gamma_lower", dict(early_fill_gamma=6.0)),
        ("gamma_higher", dict(early_fill_gamma=15.0)),
        ("blocked", dict(block=["L", "S"])),
    ]

    results = {}
    for name, patch in candidates:
        deltas, cand_ns, base_ns, per_day = evaluate_candidate(IN_SAMPLE_DAYS, patch, baseline_cache, vix, arrays_cache)
        stats = paired_stats(deltas)
        results[name] = dict(patch=patch, stats=stats, per_day=per_day, sum_cand_n=sum(cand_ns), sum_base_n=sum(base_ns))
        print(f"\n--- candidate {name} {patch} ---")
        print(f"n_days={stats['n']} mean_delta={stats['mean']:.1f} std={stats['std']:.1f} t={stats['t']:.2f} p={stats['p']:.3f}" if stats['n'] >= 2 else stats)
        print(f"sum_cand_n={sum(cand_ns)} sum_base_n={sum(base_ns)}")
        sorted_days = sorted(per_day, key=lambda r: abs(r['delta']), reverse=True)
        if sorted_days:
            top = sorted_days[0]
            rest_mean = (sum(r['delta'] for r in per_day) - top['delta']) / max(1, len(per_day) - 1)
            print(f"largest-|delta| day: {top['day']} delta={top['delta']} ; mean-excl-that-day={rest_mean:.1f} (full mean={stats['mean']:.1f})")

    out_path = "/tmp/tmf_30m_cell_tune_day_contract_insample.json"
    with open(out_path, "w") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "per_day"} for k, v in results.items()}, f, indent=2, default=str)
    print(f"\nWrote {out_path}")

    # 2) pick best candidate by mean delta with a plausible t (need mean>0 and reasonably consistent)
    ranked = sorted(results.items(), key=lambda kv: kv[1]["stats"]["mean"], reverse=True)
    best_name, best = ranked[0]
    print(f"\n=== Best in-sample candidate: {best_name} mean_delta={best['stats']['mean']:.2f} t={best['stats']['t']} p={best['stats']['p']} ===")

    if best["stats"]["mean"] <= 0:
        print("No candidate beats baseline in-sample. FINAL VERDICT: NO_IMPROVEMENT (keep current default).")
        return

    # 3) OOS validation of the single best candidate
    print(f"\n=== OOS validation ({len(OOS_DAYS)} days) of candidate {best_name} {best['patch']} ===")
    oos_baseline_cache: dict = {}
    oos_arrays_cache: dict = {}
    deltas, cand_ns, base_ns, per_day = evaluate_candidate(OOS_DAYS, best["patch"], oos_baseline_cache, vix, oos_arrays_cache)
    oos_stats = paired_stats(deltas)
    print(f"OOS n_days={oos_stats['n']} mean_delta={oos_stats['mean']:.2f} std={oos_stats['std']:.2f} t={oos_stats['t']} p={oos_stats['p']}")
    print(f"OOS sum_cand_n={sum(cand_ns)} sum_base_n={sum(base_ns)}")

    out_path2 = "/tmp/tmf_30m_cell_tune_day_contract_oos.json"
    with open(out_path2, "w") as f:
        json.dump(dict(candidate=best_name, patch=best["patch"], in_sample_stats=best["stats"], oos_stats=oos_stats, oos_per_day=per_day), f, indent=2, default=str)
    print(f"\nWrote {out_path2}")


if __name__ == "__main__":
    main()
