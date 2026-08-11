#!/usr/bin/env python3
"""TMF PV16 book cell retune under the NEW continuous NQ/ES gate (2026-08-10).

Assigned cell: day|div_hh_weak_vol.

Context: causal_engine.py's session_side_gate lookup now supports a
per-bar-timestamp key (falls back to the original per-calendar-day key).
nq_gate.py's nq_side_for_day() was upgraded from a frozen-at-session-open
anchor to a CONTINUOUS anchor (recomputed at every bar). This continuous
gate is already validated (IS 22d + OOS 66d, OOS p=0.0037) and ALREADY
DEPLOYED LIVE -- see scripts/research/tmf_continuous_gate_vs_frozen_anchor.py
(imported here unmodified for continuous_gate_for_day(), not rebuilt).

Since the underlying gate signal changed, the 16-cell book's own per-cell
parameters (hang_lo/hang_hi/max_hold_bars/early_fill_gamma/block) -- tuned
under the OLD frozen-anchor gate -- may no longer be optimal. This script
holds all 15 OTHER cells fixed at the current-live book (specialized_cell_
book()'s own logic: freeze_cell_book() -> SPECIALIZED_PATCHES ->
CELL_TUNE_V2_PATCHES) and sweeps ONLY day|div_hh_weak_vol, with the
CONTINUOUS gate injected as session_side_gate for both baseline and every
candidate (so all comparisons are apples-to-apples on the new gate; the
gate-swap effect itself is evaluated in the script above, not here).

For day|div_hh_weak_vol specifically, current-live applies NEITHER
SPECIALIZED_PATCHES nor CELL_TUNE_V2_PATCHES, so baseline == freeze_cell_
book()'s plain day base: hang_lo=15, hang_hi=30, early_fill_gamma=8,
max_hold_bars=30, block=[], skip_quiet_mode="dry", bias=True,
vixtwn_calib="blend", vixtwn_calib_gamma=5.0.

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
src/tmf_channel/nq_gate.py, config/order.yaml, .env, launchd/,
scripts/order/, config/strategy.yaml, config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy
from typing import Any

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

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
    reproduced here so we can freely deepcopy+patch without touching
    order.tmf_channel_pv16_book)."""
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def build_book_variant(patch: dict[str, Any]) -> dict:
    book = baseline_book()
    book[SESS][PV] = dict(book[SESS][PV])
    book[SESS][PV].update(patch)
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
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


# Cache of (day -> (O,H,L,C,V,T)) and (day -> continuous gate dict), built
# ONCE and reused across every candidate (gate does not depend on
# session_pv_book, only on price/time data -- recomputing it per candidate
# would waste time for no reason, per task instructions).
_ARR_CACHE: dict[str, tuple] = {}
_GATE_CACHE: dict[str, dict[str, str]] = {}


def prepare_day(day: str, source: str) -> bool:
    if day in _ARR_CACHE:
        return True
    arrs = load_arrays(day, source)
    if arrs is None:
        return False
    O, H, L, C, V, T = arrs
    _ARR_CACHE[day] = arrs
    _GATE_CACHE[day] = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
    return True


def run_day_cell_pnl(day: str, book: dict, recipe_base: dict, vix: dict) -> tuple[int, float]:
    """Run one day with the given 16-cell book + continuous gate, return
    (n_trades, net_pnl) for trades whose regime_e==PV and session==SESS
    (entry HH:MM in [08:45,13:45))."""
    arrs = _ARR_CACHE.get(day)
    if arrs is None:
        return 0, 0.0
    O, H, L, C, V, T = arrs
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = _GATE_CACHE[day]

    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)

    n = 0
    net = 0.0
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        et = str(tr.get("et") or "")
        hhmm = et.split("T", 1)[1][:5] if "T" in et else et[:5]
        if not hhmm or not in_day_session(hhmm):
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
    return dict(
        n=n, mean=round(mean, 2), std=round(std, 2),
        t=round(t_stat, 3) if t_stat not in (float("inf"),) else t_stat, p=p,
    )


def approx_two_sided_p(t_stat: float, df: int) -> float:
    import math

    x = abs(t_stat)
    if df <= 0:
        return 1.0
    adj = x / math.sqrt(1.0 + (x * x) / (2.0 * df))
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(adj / math.sqrt(2.0))))
    return round(max(0.0, min(1.0, p)), 4)


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    default_book = baseline_book()
    default_cell = default_book[SESS][PV]
    print(f"=== baseline (current-live-equivalent) {SESS}|{PV} cell ===")
    print(json.dumps(default_cell, ensure_ascii=False))

    print("\nPreparing in-sample day arrays + continuous gates (built once, reused across candidates)...")
    for day in IN_SAMPLE_DAYS:
        ok = prepare_day(day, SOURCE_FOR_DAY[day])
        if not ok:
            print(f"  WARNING: no rows for {day}, skipping")

    candidates: list[dict[str, Any]] = [
        dict(name="baseline", patch={}),
        dict(name="wide_band_A", patch=dict(hang_lo=20.0, hang_hi=38.0)),
        dict(name="wide_band_B", patch=dict(hang_lo=25.0, hang_hi=45.0)),
        dict(name="narrow_band", patch=dict(hang_lo=10.0, hang_hi=22.0)),
        dict(name="longer_hold", patch=dict(max_hold_bars=45)),
        dict(name="shorter_hold", patch=dict(max_hold_bars=18)),
        dict(name="wide_and_longhold", patch=dict(hang_lo=20.0, hang_hi=38.0, max_hold_bars=45)),
        dict(name="tighter_gamma", patch=dict(early_fill_gamma=14.0)),
        dict(name="no_gamma", patch=dict(early_fill_gamma=0.0)),
        dict(name="block_LS", patch=dict(block=["L", "S"])),
    ]

    per_candidate_daily: dict[str, dict[str, tuple[int, float]]] = {}
    valid_days = [d for d in IN_SAMPLE_DAYS if d in _ARR_CACHE]
    for cand in candidates:
        book = build_book_variant(cand["patch"])
        daily: dict[str, tuple[int, float]] = {}
        for day in valid_days:
            daily[day] = run_day_cell_pnl(day, book, recipe_base, vix)
        per_candidate_daily[cand["name"]] = daily
        n_total = sum(v[0] for v in daily.values())
        pnl_total = round(sum(v[1] for v in daily.values()), 1)
        print(f"[in-sample] {cand['name']:20s} patch={cand['patch']} "
              f"n_trades={n_total} sum_pnl={pnl_total}")

    baseline_daily = per_candidate_daily["baseline"]
    n_appearances_days = sum(1 for v in baseline_daily.values() if v[0] > 0)
    n_total_baseline = sum(v[0] for v in baseline_daily.values())
    print(f"\nbaseline cell trade count across {len(valid_days)} in-sample "
          f"days: n_trades={n_total_baseline}, days-with->=1-trade={n_appearances_days}")

    thin_sample = n_total_baseline < 15
    if thin_sample:
        print(f"THIN SAMPLE: only {n_total_baseline} trades across "
              f"{len(valid_days)} in-sample days (<15) -- insufficient "
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
        for day in valid_days:
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
            daily = per_candidate_daily[best_name]
            deltas_full = []
            for day in valid_days:
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

    winning_patch = {}
    for cand in candidates:
        if cand["name"] == best_name:
            winning_patch = cand["patch"]
            break

    oos_days_all = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]
    print(f"\nOOS day count: {len(oos_days_all)} (source={OOS_SOURCE}, < 2026-07-08)")

    oos_stats = None
    oos_n_candidate = 0
    oos_n_baseline = 0
    oos_deltas = []
    if best_name != "baseline":
        book_win = build_book_variant(winning_patch)
        book_base = build_book_variant({})
        oos_days_valid = []
        for day in oos_days_all:
            ok = prepare_day(day, OOS_SOURCE)
            if not ok:
                continue
            oos_days_valid.append(day)
            n_c, pnl_c = run_day_cell_pnl(day, book_win, recipe_base, vix)
            n_b, pnl_b = run_day_cell_pnl(day, book_base, recipe_base, vix)
            oos_n_candidate += n_c
            oos_n_baseline += n_b
            oos_deltas.append(pnl_c - pnl_b)
        oos_stats = paired_stats(oos_deltas)
        print(f"[OOS delta vs baseline] n_trades_candidate={oos_n_candidate} "
              f"n_trades_baseline={oos_n_baseline} n_days={len(oos_days_valid)}")
        print(json.dumps(oos_stats, ensure_ascii=False))
    else:
        print("Winner is baseline (no change) -- no OOS delta to validate.")

    result = dict(
        cell=f"{SESS}|{PV}",
        baseline_cell=default_cell,
        thin_sample=thin_sample,
        n_trades_baseline_in_sample=n_total_baseline,
        n_days_in_sample=len(valid_days),
        candidates=summary_rows,
        winner=best_name,
        winner_patch=winning_patch,
        dominance_flag=best_delta_dominance_flag,
        oos=dict(stats=oos_stats, n_candidate=oos_n_candidate, n_baseline=oos_n_baseline, n_days=len(oos_days_all)),
    )
    out_path = "/tmp/tmf_continuous_gate_cell_tune_day_div_hh_weak_vol_result.json"
    try:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {out_path}")
    except OSError as e:
        print(f"\n(could not write {out_path}: {e})")


if __name__ == "__main__":
    main()
