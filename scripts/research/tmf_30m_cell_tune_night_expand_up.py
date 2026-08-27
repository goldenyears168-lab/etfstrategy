#!/usr/bin/env python3
"""Cell tune (2026-08-09): night|expand_up under the 30-min-primary PV8
regime architecture (see scripts/research/tmf_30m_primary_1m_calib_prototype.py
-- NOT touched here, only imported).

Assigned cell: night|expand_up. This script tunes ONLY this cell's params
(hang_lo, hang_hi, max_hold_bars, early_fill_gamma, block) while every
other one of the 16 session_pv_book cells stays at the current-live-
equivalent default (order.tmf_channel_pv16_book.specialized_cell_book()).

Baseline fact: in the CURRENT live book, night|expand_up is NOT blocked
(CELL_TUNE_V2_PATCHES sets hang_lo=15.0, hang_hi=29.0, early_fill_gamma=8.0,
max_hold_bars=28, block=[]), so the baseline fires real trades on both the
1-min and 30-min regime feeds -- the comparison here is candidate-vs-current-
tuned-default, not candidate-vs-zero.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/*.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml.
"""
from __future__ import annotations

import importlib.util
import statistics as st
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

# --- import the (unmodified) 30m-primary prototype as a module -------------
_PROTO_PATH = Path(__file__).parent / "tmf_30m_primary_1m_calib_prototype.py"
_spec = importlib.util.spec_from_file_location("tmf_30m_proto", _PROTO_PATH)
_proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_proto)
build_pv30_series = _proto.build_pv30_series
patched_classify_pv_factory = _proto.patched_classify_pv_factory
JULY_DAYS = _proto.JULY_DAYS
AUG_DAYS = _proto.AUG_DAYS
SOURCE_FOR_DAY = _proto.SOURCE_FOR_DAY

_ORIG_CLASSIFY_PV = ce.classify_pv

IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS

MY_SESS = "night"
MY_PV = "expand_up"


def day_in_window(hm: str) -> bool:
    return "08:45" <= hm < "13:45"


def load_arrays(day: str):
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
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


def run_book(day: str, arrays, book, recipe_base: dict, vix: dict):
    O, H, L, C, V, T = arrays
    pv_series = build_pv30_series(T, O, H, L, C, V)
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    net = 0.0
    n = 0
    for tr in trades:
        if tr.get("regime_e") != MY_PV:
            continue
        hm = str(tr.get("et") or "")
        if "T" in hm:
            hm = hm.split("T", 1)[1][:5]
        else:
            hm = hm[:5]
        sess = "day" if day_in_window(hm) else "night"
        if sess != MY_SESS:
            continue
        net += float(tr["pnl"])
        n += 1
    return net, n


def build_book(overrides: dict | None):
    book = deepcopy(specialized_cell_book())
    if overrides:
        book[MY_SESS][MY_PV].update(overrides)
    return book


# current default: hang_lo=15.0, hang_hi=29.0, early_fill_gamma=8.0,
# max_hold_bars=28, block=[] (1-min-tuned). Under 30-min-primary regime
# classification, night|expand_up now persists ~30min instead of ~1-5min,
# so widen the band / lengthen holds as the primary hypothesis, plus check
# whether blocking beats the tuned default entirely.
CANDIDATES = [
    # (label, overrides applied on top of the current default cell dict)
    ("baseline_current_default", None),  # hang_lo=15, hang_hi=29, gamma=8, hold=28
    ("wide_band_same_hold", dict(hang_lo=22.0, hang_hi=40.0)),
    ("wide_band_longer_hold", dict(hang_lo=22.0, hang_hi=40.0, max_hold_bars=45)),
    ("very_wide_band_long_hold", dict(hang_lo=28.0, hang_hi=50.0, max_hold_bars=60)),
    ("wide_band_lower_gamma", dict(hang_lo=22.0, hang_hi=40.0, early_fill_gamma=4.0)),
    ("wide_band_higher_gamma", dict(hang_lo=22.0, hang_hi=40.0, early_fill_gamma=12.0)),
    ("tight_band_short_hold", dict(hang_lo=12.0, hang_hi=24.0, max_hold_bars=18)),
    ("long_only_wide_band", dict(block=["S"], hang_lo=22.0, hang_hi=40.0, max_hold_bars=45)),
    ("short_only_wide_band", dict(block=["L"], hang_lo=22.0, hang_hi=40.0, max_hold_bars=45)),
    ("blocked", dict(block=["L", "S"])),
]


def evaluate(days: list[str], recipe_base: dict, vix: dict):
    baseline_book = build_book(None)
    arrays_cache = {}
    for d in days:
        arr = load_arrays(d)
        if arr is not None:
            arrays_cache[d] = arr

    per_cand_day_delta: dict[str, dict[str, float]] = {}
    per_cand_day_n: dict[str, dict[str, int]] = {}
    baseline_day_net: dict[str, float] = {}
    baseline_day_n: dict[str, int] = {}

    for d, arr in arrays_cache.items():
        bnet, bn = run_book(d, arr, baseline_book, recipe_base, vix)
        baseline_day_net[d] = bnet
        baseline_day_n[d] = bn

    for label, overrides in CANDIDATES:
        book = build_book(overrides)
        day_delta = {}
        day_n = {}
        for d, arr in arrays_cache.items():
            cnet, cn = run_book(d, arr, book, recipe_base, vix)
            day_delta[d] = cnet - baseline_day_net[d]
            day_n[d] = cn
        per_cand_day_delta[label] = day_delta
        per_cand_day_n[label] = day_n

    return per_cand_day_delta, per_cand_day_n, baseline_day_n, baseline_day_net


def paired_stats(deltas: list[float]):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=deltas[0] if deltas else 0.0, std=0.0, t=0.0, p=1.0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    if sd == 0:
        t = 0.0
    else:
        t = mean / (sd / (n ** 0.5))
    from math import erf, sqrt
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return dict(n=n, mean=mean, std=sd, t=t, p=p)


def main():
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    print(f"=== IN-SAMPLE ({len(IN_SAMPLE_DAYS)} days) ===")
    per_cand_delta, per_cand_n, baseline_n, baseline_net = evaluate(
        IN_SAMPLE_DAYS, recipe_base, vix
    )
    print(f"baseline night|expand_up trade counts/day: {sorted(baseline_n.values())}")
    print(f"baseline night|expand_up total trades: {sum(baseline_n.values())} "
          f"sum_net: {sum(baseline_net.values()):.1f}")

    summary = {}
    for label, _ in CANDIDATES:
        deltas = list(per_cand_delta[label].values())
        trade_ns = list(per_cand_n[label].values())
        total_trades = sum(trade_ns)
        stats = paired_stats(deltas)
        if len(deltas) >= 2:
            i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
            excl = deltas[:i_max] + deltas[i_max + 1:]
            excl_mean = st.mean(excl) if excl else 0.0
        else:
            excl_mean = None
        summary[label] = dict(stats=stats, total_trades=total_trades,
                               excl_top_day_mean=excl_mean,
                               sum_delta=sum(deltas))
        print(f"{label:28s} n_days={stats['n']:2d} total_trades={total_trades:4d} "
              f"sum_delta={sum(deltas):8.1f} mean={stats['mean']:7.2f} "
              f"std={stats['std']:7.2f} t={stats['t']:6.2f} p={stats['p']:.3f} "
              f"excl_top_day_mean={excl_mean}")

    print("\n=== picking best in-sample candidate (excluding baseline) ===")
    candidates_only = [(l, s) for l, s in summary.items() if l != "baseline_current_default"]
    # thin-sample check: use the baseline's own trade count as the activity
    # floor (candidates that block one/both sides will legitimately have
    # fewer trades than baseline -- that's a valid comparison, not thin data,
    # as long as baseline itself clears the bar)
    baseline_total = summary["baseline_current_default"]["total_trades"]
    print(f"baseline total trades across {len(IN_SAMPLE_DAYS)} days: {baseline_total}")
    if baseline_total < 15:
        print("baseline itself <15 trades across 22 days -> INSUFFICIENT_DATA")
        best_label = None
    else:
        eligible = [(l, s) for l, s in candidates_only if s["total_trades"] >= 15]
        if not eligible:
            print("no non-baseline candidate reaches >=15 trades -> INSUFFICIENT_DATA")
            best_label = None
        else:
            eligible.sort(key=lambda kv: kv[1]["stats"]["mean"], reverse=True)
            best_label, best_summary = eligible[0]
            print(f"best candidate: {best_label} -> {summary[best_label]}")

            if best_summary["stats"]["mean"] <= 0:
                print("best candidate's mean delta <= 0 -> current default wins")
                best_label = None

    if best_label is None:
        print("\nFINAL VERDICT: NO_IMPROVEMENT (or INSUFFICIENT_DATA, see above) "
              "-- keep current default for night|expand_up "
              "(hang_lo=15.0, hang_hi=29.0, early_fill_gamma=8.0, max_hold_bars=28, block=[])")
        return

    best_overrides = dict(CANDIDATES)[best_label]
    print(f"\n=== OOS validation of {best_label} ({best_overrides}) ===")
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json")
                if d < "2026-07-08"]
    print(f"OOS day count: {len(oos_days)}")

    oos_source_map = {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}
    global SOURCE_FOR_DAY
    SOURCE_FOR_DAY = {**SOURCE_FOR_DAY, **oos_source_map}

    baseline_book = build_book(None)
    cand_book = build_book(best_overrides)
    oos_deltas = []
    oos_ns = []
    oos_baseline_ns = []
    for d in oos_days:
        arr = load_arrays(d)
        if arr is None:
            continue
        bnet, bn = run_book(d, arr, baseline_book, recipe_base, vix)
        cnet, cn = run_book(d, arr, cand_book, recipe_base, vix)
        oos_deltas.append(cnet - bnet)
        oos_ns.append(cn)
        oos_baseline_ns.append(bn)

    oos_stats = paired_stats(oos_deltas)
    print(f"OOS: n_days={oos_stats['n']} total_trades(candidate)={sum(oos_ns)} "
          f"total_trades(baseline)={sum(oos_baseline_ns)} "
          f"sum_delta={sum(oos_deltas):.1f} mean={oos_stats['mean']:.2f} "
          f"std={oos_stats['std']:.2f} t={oos_stats['t']:.2f} p={oos_stats['p']:.3f}")

    print("\n=== FINAL ===")
    print(f"cell=night|expand_up candidate={best_label} overrides={best_overrides}")
    print(f"IN-SAMPLE: {summary[best_label]['stats']}")
    print(f"OOS: {oos_stats}")
    if oos_stats["mean"] <= 0 or sum(oos_ns) < 15:
        print("OOS FAILS to confirm in-sample edge -> OOS_FAILED, keep current default")
    else:
        print("OOS confirms in-sample edge -> ADOPT candidate")


if __name__ == "__main__":
    main()
