#!/usr/bin/env python3
"""Tune the night|contract cell for the 30m-primary/1m-calib PV8 architecture.

Assigned cell: night|contract. All other 15 cells stay at the current-live
-equivalent default (order.tmf_channel_pv16_book.specialized_cell_book()).

Methodology: see scripts/research/tmf_30m_primary_1m_calib_prototype.py for
the mechanism (30-min-bar-driven PV8 classification feeding a monkeypatched
causal_engine.classify_pv, 1-min execution mechanics unchanged). This script
reuses build_pv30_series / patched_classify_pv_factory verbatim and only
varies the night|contract cell's params: hang_lo, hang_hi, max_hold_bars,
early_fill_gamma, and whether it should be fully blocked (block=["L","S"]).

In-sample: 22 days (17 July + 5 Aug, two cache sources -- see SOURCE_FOR_DAY
below, copied from the prototype). Out-of-sample: 66 days < 2026-07-08 from
tx_1m_fullnight_cache_full.json, validated ONLY for the single best in-sample
candidate.

Day-clustered paired comparison: for each day, net-pnl of trades in
session=night & regime_e=contract, candidate cell vs baseline cell (all
other 15 cells identical, held at current-live-equivalent default). Paired
t-test over the deltas.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/*.py (read-only
import), config/order.yaml, .env, launchd/, scripts/order/.
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

# --- import the 30m-primary mechanism verbatim from the confirmed prototype ---
sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

CELL_SESS = "night"
CELL_PV = "contract"

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS

OOS_DAYS = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
OOS_SOURCE = "tx_1m_fullnight_cache_full.json"

BASELINE_CELL = dict(specialized_cell_book()["night"]["contract"])
print("baseline night|contract cell:", BASELINE_CELL, file=sys.stderr)


def day_session(hhmm: str) -> str:
    return "day" if "08:45" <= hhmm < "13:45" else "night"


def build_book(candidate: dict) -> dict:
    book = specialized_cell_book()
    book[CELL_SESS][CELL_PV] = dict(candidate)
    return book


def run_day(day: str, source: str, recipe_book: dict, vix: dict) -> tuple[float, int]:
    """Return (net_pnl, n_trades) for this cell (night|contract) on this day,
    using recipe_book as the FULL 16-cell session_pv_book (only our cell
    varies vs baseline)."""
    rows = load_day(day, source=source)
    if not rows:
        return 0.0, 0
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_pv_book"] = recipe_book

    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, events, ws, wl, rvol, regime, open_pos = ce.simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    net = 0.0
    n = 0
    for tr in trades:
        if tr.get("regime_e") != CELL_PV:
            continue
        hhmm = str(tr.get("et") or "")
        hhmm = hhmm.split("T", 1)[1][:5] if "T" in hhmm else hhmm[:5]
        sess = day_session(hhmm)
        if sess != CELL_SESS:
            continue
        net += float(tr["pnl"])
        n += 1
    return round(net, 1), n


def eval_candidate(candidate: dict, days: list[str], source_for_day) -> dict:
    vix = load_vixtwn_delta() or {}
    base_book = build_book(BASELINE_CELL)
    cand_book = build_book(candidate)

    per_day = []
    for day in days:
        source = source_for_day(day)
        base_net, base_n = run_day(day, source, base_book, vix)
        cand_net, cand_n = run_day(day, source, cand_book, vix)
        delta = round(cand_net - base_net, 1)
        per_day.append(dict(day=day, base_net=base_net, base_n=base_n,
                             cand_net=cand_net, cand_n=cand_n, delta=delta))

    deltas = [r["delta"] for r in per_day]
    n_appear = sum(1 for r in per_day if r["base_n"] > 0 or r["cand_n"] > 0)
    total_trades = sum(r["cand_n"] for r in per_day)
    mean_d = st.mean(deltas)
    std_d = st.stdev(deltas) if len(deltas) > 1 else 0.0
    se = std_d / (len(deltas) ** 0.5) if len(deltas) > 1 and std_d > 0 else None
    t_stat = (mean_d / se) if se else None
    # crude two-sided p from t via normal approx (no scipy dependency assumed)
    p_val = None
    if t_stat is not None:
        import math
        p_val = 2 * (1 - _norm_cdf(abs(t_stat)))

    # sensitivity: exclude largest-|delta| day
    if len(deltas) > 2:
        idx_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        rest = deltas[:idx_max] + deltas[idx_max + 1:]
        mean_wo = st.mean(rest)
    else:
        mean_wo = None

    return dict(
        per_day=per_day, n_days=len(days), n_appear=n_appear,
        total_trades=total_trades, mean=round(mean_d, 2), std=round(std_d, 2),
        t=round(t_stat, 3) if t_stat is not None else None,
        p=round(p_val, 4) if p_val is not None else None,
        mean_excl_largest=round(mean_wo, 2) if mean_wo is not None else None,
    )


def _norm_cdf(x: float) -> float:
    import math
    return 0.5 * (1 + math.erf(x / (2 ** 0.5)))


def main():
    # Candidate sweep for night|contract.
    # Baseline: hang_lo=16.0, hang_hi=30.0, early_fill_gamma=5.0,
    # max_hold_bars=20, block=[].
    # 30-min primary regime persists ~30min vs current ~1-5min dwell -> try
    # noticeably wider hang bands and longer holds, plus a blocked variant.
    candidates = {
        "baseline": dict(BASELINE_CELL),
        "wide_band": dict(BASELINE_CELL, hang_lo=24.0, hang_hi=40.0),
        "wide_band_longhold": dict(BASELINE_CELL, hang_lo=24.0, hang_hi=40.0, max_hold_bars=36),
        "very_wide_band": dict(BASELINE_CELL, hang_lo=30.0, hang_hi=48.0),
        "very_wide_longhold": dict(BASELINE_CELL, hang_lo=30.0, hang_hi=48.0, max_hold_bars=48),
        "longhold_only": dict(BASELINE_CELL, max_hold_bars=40),
        "shorthold_tight": dict(BASELINE_CELL, hang_lo=12.0, hang_hi=24.0, max_hold_bars=14),
        "gamma_up": dict(BASELINE_CELL, hang_lo=24.0, hang_hi=40.0, early_fill_gamma=9.0),
        "blocked": dict(BASELINE_CELL, block=["L", "S"]),
        "mild_wide": dict(BASELINE_CELL, hang_lo=20.0, hang_hi=34.0),
        "gamma_down": dict(BASELINE_CELL, early_fill_gamma=3.0),
        "block_short_only": dict(BASELINE_CELL, block=["S"]),
        "block_long_only": dict(BASELINE_CELL, block=["L"]),
    }

    print("\n=== IN-SAMPLE (22 days) ===")
    is_results = {}
    for name, cand in candidates.items():
        r = eval_candidate(cand, IN_SAMPLE_DAYS, lambda d: SOURCE_FOR_DAY[d])
        is_results[name] = r
        print(f"{name:22s} n_appear={r['n_appear']:2d} trades={r['total_trades']:4d} "
              f"mean={r['mean']:+8.2f} std={r['std']:8.2f} t={r['t']} p={r['p']} "
              f"excl_largest={r['mean_excl_largest']}")

    with open("reports/research/channel_lab/tmf_30m_tune_night_contract_insample.json", "w") as f:
        json.dump(is_results, f, indent=2, ensure_ascii=False)

    print("\nDone in-sample sweep. Inspect and pick best; then run OOS separately.")


if __name__ == "__main__":
    main()
