#!/usr/bin/env python3
"""Prototype (2026-08-09): flip which timeframe drives PV8 cell selection.

Current architecture: PV8 regime (climax_up/dn, expand_up/dn, contract,
dry, normal, div_hh_weak_vol) is classified from 1-minute bars and picked
a NEW cell (and thus new hang/block/max_hold params) up to every 1-min bar
-- this is what made day|normal (the most common, noisiest 1-min state)
both the highest-volume and highest-loss cell in the 22-day breakdown, and
is the same flicker mechanism investigated earlier tonight (PV8
dwell/flicker).

User's request: make 30-minute bars the PRIMARY axis for PV8 classification
(cell selection updates only every 30 min, using a coarser/more stable
regime read), while 1-minute bars remain the "calibration" layer -- all
actual hang/fill/exit/struct_break/trail/stop mechanics stay exactly as
they are today, unchanged, on 1-min granularity, so trade FREQUENCY should
be largely preserved (entries/exits still trigger off 1-min price action;
only "which of the 16 cells' parameters currently apply" changes source).

Mechanism: causal_engine.classify_pv(C, O, rvol, t) is called from exactly
ONE call site inside simulate()'s main loop (`reg, _ = classify_pv(...);
regime[t] = reg`), and `regime[t]` is the ONLY thing used later to index
into session_pv_book[sess][reg] for cell-parameter lookup. So this
monkeypatches causal_engine.classify_pv (module-global lookup, same
pattern as tonight's other monkeypatches -- desired_cache._disk_path,
nq_gate._load_futures_bundle) with a wrapper that ignores its 1-min
C/O/rvol args and instead returns a precomputed 30-min-bar PV8 state for
bar index t. NEVER modifies causal_engine.py itself (frozen production
file).

30-min bars are built by wall-clock-aligned resampling of the SAME 1-min
array already in memory (no new data source needed). PIT-safe: bar t can
only see the last FULLY CLOSED 30-min bucket strictly before its own
bucket -- the in-progress 30-min bucket containing t is never used.

This script is Phase 1 (prototype + sanity check), NOT the parameter-tuning
pass. It runs the unmodified current 16-cell PARAMETERS (session_pv_book)
against the 30-min-driven regime feed, on the same 22-day window, purely to
check the mechanism works and see whether trade count/PnL land in a
plausible range before any tuning. Parameter tuning (the 16-agent pass) is
a separate follow-up once this prototype is confirmed sane.

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

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

_ORIG_CLASSIFY_PV = ce.classify_pv
_ORIG_RVOL_SERIES = ce.rvol_series

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

BIN_MIN = 30


def _bucket_key(hm: str) -> str:
    h, m = int(hm[:2]), int(hm[3:5])
    m30 = (m // BIN_MIN) * BIN_MIN
    return f"{h:02d}:{m30:02d}"


# Recalibrated for 30-min rvol (2026-08-09): the first prototype run reused
# causal_engine's 1-min-calibrated thresholds (DRY=0.45/CONTRACT=0.70/
# EXPAND=1.50/CLIMAX=2.50) verbatim and produced an implausible cell mix
# (climax_up/dn -- meant to be RARE -- became one of the most common
# states). 30-min bar-to-bar volume ratios have a genuinely wider spread
# than 1-min (fewer, chunkier bars -> less smoothing), so the same absolute
# cutoffs land at very different percentiles. Refit by finding what
# percentile each 1-min threshold actually sits at across 88 days'
# (22-day window + 66-day OOS window) worth of 1-min rvol
# (DRY->p12.3, CONTRACT->p30.0, EXPAND->p75.2, CLIMAX->p89.7), then reading
# off the 30-min rvol distribution at those SAME percentiles over the same
# 88 days -- preserves each state's intended rarity/frequency instead of
# guessing new absolute numbers.
DRY_30M = 0.357
CONTRACT_30M = 0.586
EXPAND_30M = 2.118
CLIMAX_30M = 4.510


def classify_pv_30m(C, O, rvol, t, look=5):
    """Mirrors causal_engine.classify_pv()'s exact logic, with the 30-min-
    recalibrated thresholds above instead of the 1-min module constants."""
    if rvol[t] is None or t < 1:
        return "na", 0.0
    rv = rvol[t]
    a = max(0, t - look)
    impulse = C[t] - C[a]
    up = impulse > 0
    if rv >= CLIMAX_30M:
        return ("climax_up" if up else "climax_dn"), impulse
    if rv >= EXPAND_30M:
        return ("expand_up" if up else "expand_dn"), impulse
    if rv <= DRY_30M:
        return "dry", impulse
    if rv <= CONTRACT_30M:
        return "contract", impulse
    hh = C[t] >= max(C[a : t + 1]) - 1e-9
    if hh and rv < 1.0 and impulse > 0:
        return "div_hh_weak_vol", impulse
    return "normal", impulse


def build_pv30_series(T: list[str], O, H, L, C, V) -> list[str]:
    """Return a PV8 label per 1-min index, using the last FULLY CLOSED
    30-min bucket strictly before that index's own bucket (PIT-safe)."""
    n = len(T)
    hm = [t[11:16] for t in T]
    bucket_of = [_bucket_key(h) for h in hm]

    # group 1-min indices into 30-min buckets, in array order (a new
    # bucket key starting anywhere -- including after a session gap --
    # is still just "the next bucket in sequence").
    buckets: list[list[int]] = []
    cur_key = None
    for i in range(n):
        if bucket_of[i] != cur_key:
            buckets.append([])
            cur_key = bucket_of[i]
        buckets[-1].append(i)

    # 30-min OHLCV per bucket
    O30 = [O[idxs[0]] for idxs in buckets]
    H30 = [max(H[i] for i in idxs) for idxs in buckets]
    L30 = [min(L[i] for i in idxs) for idxs in buckets]
    C30 = [C[idxs[-1]] for idxs in buckets]
    V30 = [sum(V[i] for i in idxs) for idxs in buckets]

    rv30 = _ORIG_RVOL_SERIES(V30)
    pv30 = []
    for bi in range(len(buckets)):
        reg, _ = classify_pv_30m(C30, O30, rv30, bi)
        pv30.append(reg)

    # map each 1-min index -> its bucket index -> the PRIOR bucket's PV
    # (bucket 0 has no prior bucket -> "na", matches classify_pv's own
    # cold-start return value)
    out = ["na"] * n
    for b_idx, idxs in enumerate(buckets):
        prior_pv = pv30[b_idx - 1] if b_idx > 0 else "na"
        for i in idxs:
            out[i] = prior_pv
    return out


def patched_classify_pv_factory(pv_series: list[str]):
    def _patched(C, O, rvol, t, look=5):
        return pv_series[t], 0.0
    return _patched


def run_day(day: str, recipe: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, n_trades=0, sum_pnl=0.0, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, events, ws, wl, rvol, regime, open_pos = ce.simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    cell_n = defaultdict(int)
    cell_net = defaultdict(float)
    for tr in trades:
        cell_n[tr.get("regime_e")] += 1
        cell_net[tr.get("regime_e")] += float(tr["pnl"])

    return dict(
        day=day, n_trades=len(trades), sum_pnl=round(sum(t["pnl"] for t in trades), 1),
        cell_n=dict(cell_n), cell_net={k: round(v, 1) for k, v in cell_net.items()},
    )


def main():
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    results = []
    for day in JULY_DAYS + AUG_DAYS:
        r = run_day(day, recipe, vix)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)

    vals = [r["sum_pnl"] for r in results if not r.get("skipped")]
    ns = [r["n_trades"] for r in results if not r.get("skipped")]
    print("\n=== 30m-primary/1m-calib PROTOTYPE summary ===")
    print(f"n_days={len(vals)} total_trades={sum(ns)} mean_trades/day={st.mean(ns):.1f}")
    print(f"sum_pnl={sum(vals):.1f} mean/day={st.mean(vals):.2f} std={st.stdev(vals):.2f}")

    out_path = "reports/research/channel_lab/tmf_30m_primary_1m_calib_prototype_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
