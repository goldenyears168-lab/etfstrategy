#!/usr/bin/env python3
"""30m-primary/1m-calib architecture: tune cell night|expand_dn.

Assigned cell: night|expand_dn. Baseline (current-live-equivalent, from
order.tmf_channel_pv16_book.specialized_cell_book()) has this cell
block=["L","S"] (fully blocked -- zero trades under the CURRENT 1-min-driven
architecture). This script asks: under the NEW 30-min-primary/1-min-calib
regime feed (classification from build_pv30_series() in
tmf_30m_primary_1m_calib_prototype.py, execution mechanics unchanged on
1-min bars), does unblocking this cell -- with parameters re-tuned for a
regime that now persists ~30min instead of ~1-5min -- produce a day-clustered
significant edge over the current default (still blocked)?

Methodology: per CLAUDE-provided task spec.
  - 22-day in-sample window (July + Aug days below), tune here only.
  - 66-day OOS window (2026-04-01..2026-07-07 from
    tx_1m_fullnight_cache_full.json), validate the ONE final candidate only.
  - Day-clustered: one net-pnl-delta number per day (candidate cell-net-pnl
    minus baseline cell-net-pnl, which is always 0 for this cell since the
    baseline blocks it), paired t-test across days.
  - All 15 other cells stay at the current-live-equivalent default
    (specialized_cell_book()) throughout -- only night|expand_dn varies.

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml. Pure read-only research script; only reads
reports/research/channel_lab/ (does not write there for this run's raw
cache; writes its OWN result json under the same dir, which is fine --
research scripts write research artifacts there routinely).
"""
from __future__ import annotations

import json
import statistics as st
from copy import deepcopy

import tmf_channel.causal_engine as ce
from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import specialized_cell_book
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta

from tmf_30m_primary_1m_calib_prototype import (
    build_pv30_series,
    patched_classify_pv_factory,
)

try:
    from scipy import stats as sstats
except Exception:  # pragma: no cover
    sstats = None

_ORIG_CLASSIFY_PV = ce.classify_pv

CELL_SESSION = "night"
CELL_REGIME = "expand_dn"

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

# Baseline current-live-equivalent night|expand_dn cell (from the 16-cell
# book) for reference -- hang_lo=16, hang_hi=30, max_hold_bars=16,
# early_fill_gamma=5.0 (night_base), block=["L","S"] (fully blocked).
_BASE_BOOK = specialized_cell_book()
BASELINE_CELL = dict(_BASE_BOOK[CELL_SESSION][CELL_REGIME])

# Candidates: (label, hang_lo, hang_hi, max_hold_bars, early_fill_gamma, block)
# 30-min regime persists longer than the 1-min-driven state this cell was
# never tuned for (it's hard-blocked live) -- explore noticeably wider bands
# and longer holds, plus keep the original 1-min band and the fully-blocked
# no-change option as references.
CANDIDATES = [
    ("blocked_default", 16.0, 30.0, 16, 5.0, ["L", "S"]),
    ("same_band_unblocked", 16.0, 30.0, 16, 5.0, []),
    ("wide1_mh20", 24.0, 42.0, 20, 5.0, []),
    ("wide2_mh30", 32.0, 55.0, 30, 5.0, []),
    ("wide3_mh30", 40.0, 70.0, 30, 5.0, []),
    ("wide1_mh45", 24.0, 42.0, 45, 5.0, []),
    ("wide2_mh16", 32.0, 55.0, 16, 5.0, []),
    ("same_band_mh30", 16.0, 30.0, 30, 5.0, []),
    ("wide2_gamma9", 32.0, 55.0, 30, 9.0, []),
]


def _arrays_from_rows(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def _hm(et: str) -> str:
    s = str(et or "")
    return s.split("T", 1)[1][:5] if "T" in s else s[:5]


def _is_night(hm: str) -> bool:
    return hm >= "15:00" or hm < "05:00"


def build_book(hang_lo, hang_hi, max_hold_bars, early_fill_gamma, block):
    book = deepcopy(_BASE_BOOK)
    cell = book[CELL_SESSION][CELL_REGIME]
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    cell["max_hold_bars"] = max_hold_bars
    cell["early_fill_gamma"] = early_fill_gamma
    cell["block"] = list(block)
    return book


def run_day_cell_pnl(day: str, source: str, book: dict, recipe_base: dict, vix: dict) -> tuple[float, int]:
    """Run one day with the 30m-primary regime feed + given 16-cell book,
    return (net pnl, n_trades) summed over trades in CELL_SESSION|CELL_REGIME only."""
    rows = load_day(day, source=source)
    if not rows:
        return 0.0, 0
    O, H, L, C, V, T = _arrays_from_rows(day, rows)
    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    net = 0.0
    n_trades = 0
    for tr in trades or []:
        if tr.get("regime_e") != CELL_REGIME:
            continue
        if not _is_night(_hm(tr.get("et") or "")):
            continue
        net += float(tr["pnl"])
        n_trades += 1
    return net, n_trades


def day_clustered_ttest(deltas: list[float]):
    n = len(deltas)
    if n < 2:
        return None, None, n
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    if sd == 0:
        return (0.0 if mean == 0 else None), (1.0 if mean == 0 else None), n
    t = mean / (sd / n**0.5)
    p = None
    if sstats is not None:
        p = 2 * (1 - sstats.t.cdf(abs(t), df=n - 1))
    return t, p, n


def evaluate_candidate(label, hang_lo, hang_hi, max_hold_bars, early_fill_gamma, block,
                        days, source_for_day, recipe_base, vix, cache_baseline=None):
    """Returns (deltas list, per-day dict, n_trades_total)."""
    book = build_book(hang_lo, hang_hi, max_hold_bars, early_fill_gamma, block)
    deltas = []
    per_day = {}
    n_trades_total = 0
    for day in days:
        source = source_for_day[day] if isinstance(source_for_day, dict) else source_for_day
        cand_net, n_trades = run_day_cell_pnl(day, source, book, recipe_base, vix)
        base_net = 0.0  # baseline blocks this cell -> always 0
        if cache_baseline is not None:
            base_net = cache_baseline.get(day, 0.0)
        delta = cand_net - base_net
        deltas.append(delta)
        per_day[day] = {"cand_net": cand_net, "base_net": base_net, "delta": delta, "n_trades": n_trades}
        n_trades_total += n_trades
    return deltas, per_day, n_trades_total


def main():
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    print(f"Baseline cell (current-live-equivalent) {CELL_SESSION}|{CELL_REGIME}: "
          f"{BASELINE_CELL}")
    print(f"IN-SAMPLE days: {len(IN_SAMPLE_DAYS)}  OOS days: {len(OOS_DAYS)} "
          f"({OOS_DAYS[0]}..{OOS_DAYS[-1]})\n")

    results = {}
    for label, lo, hi, mh, gamma, block in CANDIDATES:
        deltas, per_day, n_trades_total = evaluate_candidate(
            label, lo, hi, mh, gamma, block,
            IN_SAMPLE_DAYS, SOURCE_FOR_DAY, recipe_base, vix,
        )
        t, p, n = day_clustered_ttest(deltas)
        mean_d = st.mean(deltas) if deltas else None
        sd_d = st.stdev(deltas) if len(deltas) > 1 else None
        results[label] = dict(
            hang_lo=lo, hang_hi=hi, max_hold_bars=mh, early_fill_gamma=gamma, block=block,
            mean=mean_d, std=sd_d, t=t, p=p, n=n, n_trades_total=n_trades_total,
            sum_delta=sum(deltas),
        )
        print(f"[{label}] lo={lo} hi={hi} mh={mh} gamma={gamma} block={block}")
        print(f"  sum_delta={sum(deltas):.1f} mean={mean_d:.2f} std={sd_d if sd_d else 0:.2f} "
              f"t={t} p={p} n_days={n} n_trades_total={n_trades_total}")

    out_path = "reports/research/channel_lab/tmf_30m_cell_tune_night_expand_dn_insample.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nWrote {out_path}")

    # pick best candidate (excluding the blocked_default reference which is
    # by construction delta==0) by mean, but only "wins" if positive and not
    # obviously dominated by a single day
    non_default = {k: v for k, v in results.items() if k != "blocked_default"}
    best_label = max(non_default, key=lambda k: non_default[k]["mean"] or -1e18)
    best = results[best_label]
    print(f"\n=== BEST IN-SAMPLE CANDIDATE: {best_label} ===")
    print(json.dumps(best, indent=2, default=str))

    if best["mean"] is None or best["mean"] <= 0 or best["n_trades_total"] < 15:
        print("\nNo positive/robust in-sample edge found (or thin sample, "
              f"n_trades_total={best['n_trades_total']}) -> recommend NO CHANGE "
              "(keep blocked_default). Skipping OOS validation.")
        return

    # Robustness: exclude largest-|delta| day and recheck sign/significance
    label, lo, hi, mh, gamma, block = next(c for c in CANDIDATES if c[0] == best_label)
    deltas, per_day, n_trades_total = evaluate_candidate(
        label, lo, hi, mh, gamma, block, IN_SAMPLE_DAYS, SOURCE_FOR_DAY, recipe_base, vix,
    )
    max_abs_idx = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
    excl_day = IN_SAMPLE_DAYS[max_abs_idx]
    excl_deltas = [d for i, d in enumerate(deltas) if i != max_abs_idx]
    t2, p2, n2 = day_clustered_ttest(excl_deltas)
    print(f"\nExcluding largest-|delta| day ({excl_day}, delta={deltas[max_abs_idx]:.1f}): "
          f"mean={st.mean(excl_deltas):.2f} t={t2} p={p2} n={n2}")

    # OOS validation (single candidate only)
    print(f"\n=== OOS VALIDATION: {best_label} on {len(OOS_DAYS)} days ===")
    oos_deltas, oos_per_day, oos_n_trades_total = evaluate_candidate(
        label, lo, hi, mh, gamma, block, OOS_DAYS, OOS_SOURCE, recipe_base, vix,
    )
    t_oos, p_oos, n_oos = day_clustered_ttest(oos_deltas)
    mean_oos = st.mean(oos_deltas) if oos_deltas else None
    sd_oos = st.stdev(oos_deltas) if len(oos_deltas) > 1 else None
    print(f"  sum_delta={sum(oos_deltas):.1f} mean={mean_oos:.2f} "
          f"std={sd_oos if sd_oos else 0:.2f} t={t_oos} p={p_oos} n_days={n_oos} "
          f"n_trades_total={oos_n_trades_total}")

    out2 = dict(
        best_label=best_label, best_params=dict(
            hang_lo=lo, hang_hi=hi, max_hold_bars=mh, early_fill_gamma=gamma, block=block),
        in_sample=results[best_label],
        oos=dict(mean=mean_oos, std=sd_oos, t=t_oos, p=p_oos, n=n_oos,
                  n_trades_total=oos_n_trades_total, sum_delta=sum(oos_deltas)),
        robustness_excl_top_day=dict(day=excl_day, t=t2, p=p2,
                                       mean=st.mean(excl_deltas) if excl_deltas else None),
    )
    out_path2 = "reports/research/channel_lab/tmf_30m_cell_tune_night_expand_dn_final.json"
    with open(out_path2, "w") as f:
        json.dump(out2, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nWrote {out_path2}")


if __name__ == "__main__":
    main()
