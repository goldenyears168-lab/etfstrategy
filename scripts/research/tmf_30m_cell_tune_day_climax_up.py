#!/usr/bin/env python3
"""30m-primary/1m-calib PV8 cell tuning: ASSIGNED CELL = day|climax_up.

Uses the already-validated 30m-primary/1m-calib mechanism from
scripts/research/tmf_30m_primary_1m_calib_prototype.py (build_pv30_series +
patched_classify_pv_factory monkeypatch pattern) to re-run simulate() with
PV8 regime driven by 30-min bars while all execution mechanics stay 1-min.

For each candidate parameter set for day|climax_up, only that ONE cell in
the 16-cell book is varied; all other 15 cells stay at the
current-live-equivalent default (order.tmf_channel_pv16_book.specialized_cell_book()).

Day-clustered paired comparison (candidate net pnl for cell vs baseline net
pnl for cell, per day) across:
  - IN-SAMPLE: 22 days (17 July + 5 Aug), tune here only
  - OUT-OF-SAMPLE: 66 days (2026-04-01..2026-07-07), validate ONLY the final
    winning candidate, no peeking during search.

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml. Reads (never writes) reports/research/channel_lab/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "day"
PV = "climax_up"

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

_all_full = list_days(source="tx_1m_fullnight_cache_full.json")
OOS_DAYS = [d for d in _all_full if d < "2026-07-08"]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"


def base_book() -> dict:
    """current-live-equivalent 16-cell book (same construction specialized_cell_book() uses)."""
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


BASE_CELL = deepcopy(base_book()[SESS][PV])


def build_book(cell_override: dict | None) -> dict:
    book = base_book()
    if cell_override is not None:
        book[SESS][PV] = {**book[SESS][PV], **cell_override}
    return book


def load_day_arrays(day: str):
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


def run_day(day: str, book: dict, vix: dict) -> tuple[float, int]:
    """Return (net_pnl_for_assigned_cell, n_trades_for_assigned_cell) for one day."""
    arrs = load_day_arrays(day)
    if arrs is None:
        return 0.0, 0
    O, H, L, C, V, T = arrs
    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_pv_book"] = book
    try:
        trades, *_rest = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    net = 0.0
    n = 0
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        et = str(tr.get("et", ""))
        hm = et[11:16] if "T" in et else et[-8:-3]
        sess = "day" if "08:45" <= hm < "13:45" else "night"
        if sess != SESS:
            continue
        net += float(tr["pnl"])
        n += 1
    return net, n


def evaluate_candidate(days: list[str], cell_override: dict | None, vix: dict):
    book = build_book(cell_override)
    per_day = {}
    for day in days:
        net, n = run_day(day, book, vix)
        per_day[day] = (net, n)
    return per_day


def paired_stats(baseline_pd: dict, cand_pd: dict, days: list[str]):
    deltas = [cand_pd[d][0] - baseline_pd[d][0] for d in days]
    n_appear = sum(1 for d in days if baseline_pd[d][1] > 0 or cand_pd[d][1] > 0)
    total_trades = sum(baseline_pd[d][1] for d in days)
    mean = st.mean(deltas)
    std = st.stdev(deltas) if len(deltas) > 1 else 0.0
    t = (mean / (std / (len(deltas) ** 0.5))) if std > 0 else float("nan")
    # rough two-sided p via normal approx (no scipy dependency assumed)
    p = None
    try:
        from scipy import stats as sstats

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=len(deltas) - 1))) if std > 0 else None
    except Exception:
        p = None
    return dict(
        deltas=deltas, mean=mean, std=std, t=t, p=p, n=len(deltas),
        n_appear=n_appear, total_baseline_trades=total_trades,
    )


def largest_abs_delta_excl(baseline_pd: dict, cand_pd: dict, days: list[str]):
    deltas = {d: cand_pd[d][0] - baseline_pd[d][0] for d in days}
    if not deltas:
        return None, None
    worst_day = max(deltas, key=lambda d: abs(deltas[d]))
    rest = [deltas[d] for d in days if d != worst_day]
    mean_excl = st.mean(rest) if rest else None
    return worst_day, mean_excl


def main():
    vix = load_vixtwn_delta() or {}
    print(f"Baseline (current-live-equivalent) day|climax_up cell: {BASE_CELL}")

    print("\n--- computing IN-SAMPLE baseline (candidate=None override) ---")
    baseline_is = evaluate_candidate(IN_SAMPLE_DAYS, None, vix)
    n_trades_total = sum(v[1] for v in baseline_is.values())
    print(f"baseline in-sample trades for {SESS}|{PV}: {n_trades_total} "
          f"across {sum(1 for v in baseline_is.values() if v[1] > 0)} day-appearances")
    for d, (net, n) in baseline_is.items():
        print(f"  {d}: net={net:.1f} n={n}")

    if n_trades_total < 15:
        print(f"\n*** THIN SAMPLE: only {n_trades_total} trades across 22 in-sample days. "
              f"Will report INSUFFICIENT_DATA regardless of sweep results. ***")

    # Candidate sweep. BASE_CELL hang_lo=27.0, hang_hi=42.0 (unblocked), max_hold_bars=30,
    # early_fill_gamma=0. Sweep wider bands (regime now persists ~30min not ~1-5min) plus
    # a couple of max_hold variants, plus fully-blocked as one candidate.
    candidates = {
        "current_default": None,
        "wider_35_60": dict(hang_lo=35.0, hang_hi=60.0),
        "wider_40_75": dict(hang_lo=40.0, hang_hi=75.0),
        "wider_35_60_hold45": dict(hang_lo=35.0, hang_hi=60.0, max_hold_bars=45),
        "wider_35_60_hold20": dict(hang_lo=35.0, hang_hi=60.0, max_hold_bars=20),
        "narrower_20_35": dict(hang_lo=20.0, hang_hi=35.0),
        "wider_40_75_hold45": dict(hang_lo=40.0, hang_hi=75.0, max_hold_bars=45),
        "blocked": dict(block=["L", "S"]),
    }

    print("\n--- IN-SAMPLE sweep ---")
    results = {}
    for name, override in candidates.items():
        if name == "current_default":
            cand_pd = baseline_is
        else:
            cand_pd = evaluate_candidate(IN_SAMPLE_DAYS, override, vix)
        stats = paired_stats(baseline_is, cand_pd, IN_SAMPLE_DAYS)
        results[name] = dict(override=override, per_day=cand_pd, stats=stats)
        worst_day, mean_excl = largest_abs_delta_excl(baseline_is, cand_pd, IN_SAMPLE_DAYS)
        print(f"{name}: override={override}")
        print(f"  mean_delta={stats['mean']:.2f} std={stats['std']:.2f} t={stats['t']:.3f} "
              f"p={stats['p']} n={stats['n']}")
        print(f"  worst_day={worst_day} mean_excl_worst={mean_excl}")

    # NOTE: reports/research/channel_lab/ is read-only for this script (hard
    # safety rule) -- do not write sweep output there. Print-only.
    print("\n(Selection of final candidate + OOS validation happens in a follow-up step "
          "after inspecting these numbers.)")


if __name__ == "__main__":
    main()
