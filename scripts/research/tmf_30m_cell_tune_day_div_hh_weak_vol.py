#!/usr/bin/env python3
"""TMF 30m-primary/1m-calib architecture — cell tune for day|div_hh_weak_vol.

Assigned cell: day|div_hh_weak_vol (16-cell PV16 book, session=day, PV8=
div_hh_weak_vol). Baseline = current-live-equivalent book (specialized_cell_
book()'s own logic: freeze_cell_book() -> SPECIALIZED_PATCHES -> CELL_TUNE_
V2_PATCHES). For this specific cell that book applies NEITHER patch layer,
so the current default is exactly freeze_cell_book()'s day base: hang_lo=15,
hang_hi=30, early_fill_gamma=8, max_hold_bars=30, block=[], skip_quiet_mode=
"dry", bias=True, vixtwn_calib="blend", vixtwn_calib_gamma=5.0.

Architecture under test (NOT built here — see scripts/research/
tmf_30m_primary_1m_calib_prototype.py, imported unmodified): PV8 regime
selection for the session_pv_book lookup is driven by the last fully-closed
30-minute bar (recalibrated thresholds), while all hang/fill/exit/
struct_break/trail/stop/max_hold mechanics stay on native 1-minute
granularity exactly as in production. This script holds all 15 OTHER cells
fixed at the baseline book and sweeps ONLY day|div_hh_weak_vol's hang_lo/
hang_hi/max_hold_bars/early_fill_gamma/block, using the SAME 30m-driven
regime feed for both baseline and every candidate (so any pnl delta is
attributable purely to this cell's parameters, not to the 30m/1m switch
itself, which is evaluated in the prototype already).

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy
from typing import Any

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

sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "day"
PV = "div_hh_weak_vol"

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


def baseline_book() -> dict[str, dict[str, dict[str, Any]]]:
    """Current-live-equivalent 16-cell book (specialized_cell_book() logic,
    reproduced here rather than imported so we can freely deepcopy+patch
    without touching order.tmf_channel_pv16_book)."""
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def in_day_session(hhmm: str) -> bool:
    return "08:45" <= hhmm < "13:45"


def load_arrays(day: str, source: str):
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


def run_day_cell_pnl(day: str, source: str, book: dict, recipe_base: dict, vix: dict) -> tuple[int, float]:
    """Run one day with the given 16-cell book (30m-driven regime), return
    (n_trades, net_pnl) for trades whose regime_e==PV and session==SESS
    (entry HH:MM in [08:45,13:45))."""
    arrs = load_arrays(day, source)
    if arrs is None:
        return 0, 0.0
    O, H, L, C, V, T = arrs
    pv_series = build_pv30_series(T, O, H, L, C, V)
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
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
        eb = tr.get("eb")
        hhmm = T[eb][11:16] if eb is not None and 0 <= eb < len(T) else None
        if hhmm is None or not in_day_session(hhmm):
            continue
        n += 1
        net += float(tr["pnl"])
    return n, round(net, 1)


def paired_stats(deltas: list[float]) -> dict[str, Any]:
    n = len(deltas)
    if n == 0:
        return dict(n=0, mean=0.0, std=0.0, t=0.0, p=None)
    mean = st.mean(deltas)
    std = st.stdev(deltas) if n > 1 else 0.0
    if std == 0 or n < 2:
        t_stat = float("inf") if mean != 0 and std == 0 else 0.0
        p = None
    else:
        se = std / (n ** 0.5)
        t_stat = mean / se
        p = approx_two_sided_p(t_stat, n - 1)
    return dict(n=n, mean=round(mean, 2), std=round(std, 2), t=round(t_stat, 3) if t_stat not in (float("inf"),) else t_stat, p=p)


def approx_two_sided_p(t_stat: float, df: int) -> float:
    """Two-sided p-value for Student's t without scipy (Hill's approximation
    via incomplete-beta-free series is overkill here; use a numerically
    stable normal approximation for df>=20, else a small lookup-adjusted
    normal approx — adequate for research triage, not a publication stat)."""
    import math

    x = abs(t_stat)
    # Welch-Satterthwaite-free normal approx with a small-df variance bump
    if df <= 0:
        return 1.0
    adj = x / math.sqrt(1.0 + (x * x) / (2.0 * df))
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(adj / math.sqrt(2.0))))
    return round(max(0.0, min(1.0, p)), 4)


def build_book_variant(patch: dict[str, Any]) -> dict:
    book = baseline_book()
    book[SESS][PV] = dict(book[SESS][PV])
    book[SESS][PV].update(patch)
    return book


def main():
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    default_book = baseline_book()
    default_cell = default_book[SESS][PV]
    print(f"=== baseline (current-live-equivalent) {SESS}|{PV} cell ===")
    print(json.dumps(default_cell, ensure_ascii=False))

    # Candidate sweep: wider hang bands (30-min regime persists ~6-60x
    # longer than 1-min flicker windows, so a cell keyed off it may tolerate
    # a wider/less-frequent band or a longer max_hold before mean-reverting
    # inside one 30-min window instead of being force-closed mid-regime),
    # plus max_hold_bars variants, plus a full-block candidate.
    candidates: list[dict[str, Any]] = [
        dict(name="baseline", patch={}),
        dict(name="wide_band_A", patch=dict(hang_lo=20.0, hang_hi=38.0)),
        dict(name="wide_band_B", patch=dict(hang_lo=25.0, hang_hi=45.0)),
        dict(name="wide_band_C", patch=dict(hang_lo=18.0, hang_hi=34.0, max_hold_bars=45)),
        dict(name="longer_hold_A", patch=dict(max_hold_bars=45)),
        dict(name="longer_hold_B", patch=dict(max_hold_bars=60)),
        dict(name="shorter_hold", patch=dict(max_hold_bars=18)),
        dict(name="wide_and_longhold", patch=dict(hang_lo=22.0, hang_hi=40.0, max_hold_bars=50)),
        dict(name="tighter_gamma", patch=dict(early_fill_gamma=14.0)),
        dict(name="no_gamma", patch=dict(early_fill_gamma=0.0)),
        dict(name="block_LS", patch=dict(block=["L", "S"])),
    ]

    # Per-day baseline (n, net) cache to avoid recomputing baseline sim
    # (day|div_hh_weak_vol params identical across candidates that don't
    # touch it -- but simplest correct approach is: re-simulate the WHOLE
    # day per candidate book (other 15 cells fixed at default), since the
    # cell recipe is looked up live inside simulate()'s main loop and can
    # affect flat-state transitions for THIS cell's own future bars).
    per_candidate_daily: dict[str, dict[str, tuple[int, float]]] = {}
    for cand in candidates:
        book = build_book_variant(cand["patch"])
        daily: dict[str, tuple[int, float]] = {}
        for day in IN_SAMPLE_DAYS:
            daily[day] = run_day_cell_pnl(day, SOURCE_FOR_DAY[day], book, recipe_base, vix)
        per_candidate_daily[cand["name"]] = daily
        n_total = sum(v[0] for v in daily.values())
        pnl_total = round(sum(v[1] for v in daily.values()), 1)
        print(f"[in-sample] {cand['name']:20s} patch={cand['patch']} "
              f"n_trades={n_total} sum_pnl={pnl_total}")

    baseline_daily = per_candidate_daily["baseline"]
    n_appearances_days = sum(1 for v in baseline_daily.values() if v[0] > 0)
    n_total_baseline = sum(v[0] for v in baseline_daily.values())
    print(f"\nbaseline cell trade count across {len(IN_SAMPLE_DAYS)} in-sample "
          f"days: n_trades={n_total_baseline}, days-with->=1-trade={n_appearances_days}")

    thin_sample = n_total_baseline < 15
    if thin_sample:
        print(f"THIN SAMPLE: only {n_total_baseline} trades across "
              f"{len(IN_SAMPLE_DAYS)} in-sample days (<15) -- insufficient "
              f"data to force a tuned recommendation.")

    best_name = "baseline"
    best_stats = None
    best_delta_dominance_flag = False
    summary_rows = []
    for cand in candidates:
        if cand["name"] == "baseline":
            continue
        daily = per_candidate_daily[cand["name"]]
        deltas = []
        for day in IN_SAMPLE_DAYS:
            n_c, pnl_c = daily[day]
            n_b, pnl_b = baseline_daily[day]
            deltas.append(pnl_c - pnl_b)
        stats = paired_stats(deltas)
        summary_rows.append(dict(name=cand["name"], patch=cand["patch"], **stats))
        print(f"[in-sample delta vs baseline] {cand['name']:20s} "
              f"mean={stats['mean']} std={stats['std']} t={stats['t']} "
              f"p={stats['p']} n={stats['n']}")

    if not thin_sample:
        ranked = sorted(summary_rows, key=lambda r: r["mean"], reverse=True)
        top = ranked[0] if ranked else None
        if top and top["mean"] > 0:
            best_name = top["name"]
            best_stats = top
            # dominance check: exclude largest-|delta| day
            daily = per_candidate_daily[best_name]
            deltas_full = []
            for day in IN_SAMPLE_DAYS:
                n_c, pnl_c = daily[day]
                n_b, pnl_b = baseline_daily[day]
                deltas_full.append((day, pnl_c - pnl_b))
            deltas_full.sort(key=lambda x: abs(x[1]), reverse=True)
            excl = [d for _, d in deltas_full[1:]]
            mean_excl = st.mean(excl) if excl else 0.0
            mean_full = st.mean([d for _, d in deltas_full])
            if mean_full != 0 and (mean_excl <= 0 or abs(mean_excl) < 0.25 * abs(mean_full)):
                best_delta_dominance_flag = True
                print(f"\nDOMINANCE FLAG: excluding largest-|delta| day "
                      f"({deltas_full[0][0]}, delta={deltas_full[0][1]:.1f}) "
                      f"drops mean from {mean_full:.2f} to {mean_excl:.2f}")

    print(f"\n=== in-sample winner: {best_name} ===")
    if best_stats:
        print(json.dumps(best_stats, ensure_ascii=False))

    # ---- OOS validation of the single chosen candidate ----
    winning_patch = {}
    for cand in candidates:
        if cand["name"] == best_name:
            winning_patch = cand["patch"]
            break

    oos_days = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]
    print(f"\nOOS day count: {len(oos_days)} (source={OOS_SOURCE}, < 2026-07-08)")

    oos_deltas = []
    oos_n_candidate = 0
    oos_n_baseline = 0
    if best_name != "baseline":
        book_win = build_book_variant(winning_patch)
        book_base = build_book_variant({})
        for day in oos_days:
            n_c, pnl_c = run_day_cell_pnl(day, OOS_SOURCE, book_win, recipe_base, vix)
            n_b, pnl_b = run_day_cell_pnl(day, OOS_SOURCE, book_base, recipe_base, vix)
            oos_n_candidate += n_c
            oos_n_baseline += n_b
            oos_deltas.append(pnl_c - pnl_b)
        oos_stats = paired_stats(oos_deltas)
        print(f"[OOS delta vs baseline] n_trades_candidate={oos_n_candidate} "
              f"n_trades_baseline={oos_n_baseline}")
        print(json.dumps(oos_stats, ensure_ascii=False))
    else:
        oos_stats = None
        print("Winner is baseline (no change) -- no OOS delta to validate.")

    result = dict(
        cell=f"{SESS}|{PV}",
        baseline_cell=default_cell,
        thin_sample=thin_sample,
        n_trades_baseline_in_sample=n_total_baseline,
        candidates=summary_rows,
        winner=best_name,
        winner_patch=winning_patch,
        dominance_flag=best_delta_dominance_flag,
        oos=dict(stats=oos_stats, n_candidate=oos_n_candidate, n_baseline=oos_n_baseline, n_days=len(oos_days)),
    )
    # NOTE: reports/research/channel_lab/ is read-only for this task -- write
    # scratch output to /tmp instead.
    out_path = "/tmp/tmf_30m_cell_tune_day_div_hh_weak_vol_result.json"
    try:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {out_path}")
    except OSError as e:
        print(f"\n(could not write {out_path}: {e})")


if __name__ == "__main__":
    main()
