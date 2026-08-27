#!/usr/bin/env python3
"""Combine two timeframes (2026-08-09): the already-built 30-min-primary PV8
classifier (scripts/research/tmf_30m_primary_1m_calib_prototype.py) PLUS an
even bigger dimension -- a 2-hour (120 one-minute-bar) directional trend
read -- used TOGETHER, not one replacing the other.

Rule: keep the 30-min bucket's PV8 label (as already built, PIT-safe, using
the last fully-closed 30-min bucket) UNLESS that bucket's own 30-min price
impulse direction CONFLICTS with the bigger 2-hour trend direction, in
which case the bar's regime is remapped to a cell that is ALREADY
block=["L","S"] in the current live book (day session -> "expand_up",
night session -> "expand_dn" -- both confirmed block=["L","S"] in
order.tmf_channel_pv16_book.specialized_cell_book()), suppressing the entry
entirely for that bar. When the two scales agree (or the 30-min impulse is
itself ~flat), the original 30-min-driven label passes through unchanged.

This is layered on top of the SAME 30-min-primary/1-min-calibration engine
already prototyped (not a new architecture) -- the point is testing whether
adding the bigger 2-hour dimension as a conflict filter fixes the pure
30-min system's problems (elevated trade count, degraded PnL vs the current
1-min-driven live recipe), per the user's explicit request to combine
scales rather than test them in isolation.

Window choice (120 bars = 2h) is fixed by reasoning (clearly bigger than
the 30-min/30-bar primary scale, still fits within a single session) BEFORE
looking at any results -- not searched -- to avoid re-introducing the same
in-sample overfitting this session has already been burned by twice
tonight (the 30-min efficiency-ratio filter, and multiple v3/simplify
candidates). Tested once on the 22-day in-sample set AND the 66-day OOS
set in the same run, both reported, no iteration between them.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/, config/order.yaml,
.env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    _bucket_key,
    classify_pv_30m,
)

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

BIG_WINDOW = 120  # 2 hours, fixed by reasoning, not searched
DAY_SINK = "expand_up"    # block=["L","S"] in the live book (day session)
NIGHT_SINK = "expand_dn"  # block=["L","S"] in the live book (night session, via CELL_TUNE_V2)


def sess_of_hhmm(hm: str) -> str:
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def build_pv30_plus_2h(T, O, H, L, C, V):
    n = len(T)
    hm = [t[11:16] for t in T]
    bucket_of = [_bucket_key(h) for h in hm]

    buckets: list[list[int]] = []
    cur_key = None
    for i in range(n):
        if bucket_of[i] != cur_key:
            buckets.append([])
            cur_key = bucket_of[i]
        buckets[-1].append(i)

    O30 = [O[idxs[0]] for idxs in buckets]
    C30 = [C[idxs[-1]] for idxs in buckets]
    V30 = [sum(V[i] for i in idxs) for idxs in buckets]
    rv30 = _ORIG_RVOL_SERIES(V30)

    pv30, impulse30 = [], []
    for bi in range(len(buckets)):
        reg, imp = classify_pv_30m(C30, O30, rv30, bi)
        pv30.append(reg)
        impulse30.append(imp)

    # per-1min-index: last CLOSED bucket's pv + impulse sign
    pv_at = ["na"] * n
    imp_sign_at = [0] * n
    for b_idx, idxs in enumerate(buckets):
        prior_pv = pv30[b_idx - 1] if b_idx > 0 else "na"
        prior_imp = impulse30[b_idx - 1] if b_idx > 0 else 0.0
        sign = 1 if prior_imp > 1e-9 else (-1 if prior_imp < -1e-9 else 0)
        for i in idxs:
            pv_at[i] = prior_pv
            imp_sign_at[i] = sign

    # bigger 2h trend sign, causal (only past bars)
    trend_sign_at = [0] * n
    for t in range(n):
        a = t - BIG_WINDOW
        if a < 0:
            continue
        d = C[t] - C[a]
        trend_sign_at[t] = 1 if d > 1e-9 else (-1 if d < -1e-9 else 0)

    out = ["na"] * n
    n_conflict = 0
    for t in range(n):
        reg = pv_at[t]
        if reg in ("na",):
            out[t] = reg
            continue
        isign, tsign = imp_sign_at[t], trend_sign_at[t]
        if isign != 0 and tsign != 0 and isign != tsign:
            out[t] = DAY_SINK if sess_of_hhmm(hm[t]) == "day" else NIGHT_SINK
            n_conflict += 1
        else:
            out[t] = reg
    return out, n_conflict


def patched_factory(pv_series):
    def _patched(C, O, rvol, t, look=5):
        return pv_series[t], 0.0
    return _patched


def run_day(day: str, source: str, recipe: dict, vix: dict) -> dict:
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    # baseline: current live 1-min-driven system, totally unmodified
    trades_base, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    net_base = round(sum(t["pnl"] for t in trades_base), 1)

    # combo: 30-min PV base + 2h trend-conflict suppression
    pv_series, n_conflict = build_pv30_plus_2h(T, O, H, L, C, V)
    ce.classify_pv = patched_factory(pv_series)
    try:
        trades_combo, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    net_combo = round(sum(t["pnl"] for t in trades_combo), 1)

    return dict(
        day=day, n_base=len(trades_base), net_base=net_base,
        n_combo=len(trades_combo), net_combo=net_combo,
        n_conflict_bars=n_conflict, diff=round(net_combo - net_base, 1),
    )


def run_window(days, vix, recipe, label):
    rows = []
    for day in days:
        r = run_day(day, SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"), recipe, vix)
        if r.get("skipped"):
            continue
        rows.append(r)
        print(json.dumps(r), flush=True)

    diffs = [r["diff"] for r in rows]
    base_vals = [r["net_base"] for r in rows]
    combo_vals = [r["net_combo"] for r in rows]
    n = len(rows)
    mean_d = st.mean(diffs)
    std_d = st.stdev(diffs) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 else 0.0
    try:
        from scipy import stats as sp

        p_val = float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
    except Exception:
        p_val = None

    print(f"\n=== {label} summary ===")
    print(f"n={n} base_sum={sum(base_vals):.1f} combo_sum={sum(combo_vals):.1f} "
          f"base_trades/day={sum(r['n_base'] for r in rows)/n:.1f} "
          f"combo_trades/day={sum(r['n_combo'] for r in rows)/n:.1f}")
    print(f"diff mean={mean_d:.2f} std={std_d:.2f} t={t_stat:.3f} p={p_val}")
    return dict(n=n, base_sum=sum(base_vals), combo_sum=sum(combo_vals),
                mean_diff=mean_d, std_diff=std_d, t=t_stat, p=p_val, rows=rows)


def main():
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    is_days = JULY_DAYS + AUG_DAYS
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    is_result = run_window(is_days, vix, recipe, "IN-SAMPLE (22 days)")
    oos_result = run_window(oos_days, vix, recipe, "OUT-OF-SAMPLE (66 days)")

    out_path = "reports/research/channel_lab/tmf_30m_plus_2h_trend_combo_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(in_sample=is_result, out_of_sample=oos_result), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
