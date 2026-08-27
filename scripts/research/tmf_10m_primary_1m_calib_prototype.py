#!/usr/bin/env python3
"""10-minute-primary variant (2026-08-09), same architecture as the 30-min
prototype (scripts/research/tmf_30m_primary_1m_calib_prototype.py) -- PV8
regime classified from 10-min buckets (PIT-safe, last fully-closed bucket)
instead of 1-min bars, all execution mechanics (hang/fill/exit/struct_break/
trail/stop/max_hold) stay on 1-min granularity unchanged. Thresholds
percentile-matched to the 1-min thresholds' rarity (same technique as the
30-min version) across 88 days: DRY=0.419, CONTRACT=0.631, EXPAND=1.533,
CLIMAX=4.089 (vs 1-min's 0.45/0.70/1.50/2.50 -- much closer to the 1-min
values than 30-min's were, except climax which is still notably higher).

Tested ONCE against the unmodified current-live 1-min recipe on both the
22-day in-sample window and the 66-day OOS window in the same run, no
iteration between them -- both the 30-min-alone and 30-min+2h-combo lines
of research already failed consistently in both windows tonight; this is
the natural remaining question (does a timeframe between 1-min noise and
30-min's over-coarse regime-persistence problem do any better) rather than
further tuning the already-rejected 30-min line.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/, config/order.yaml,
.env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
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

BIN_MIN = 10
DRY_10M = 0.419
CONTRACT_10M = 0.631
EXPAND_10M = 1.533
CLIMAX_10M = 4.089


def _bucket_key(hm: str) -> str:
    h, m = int(hm[:2]), int(hm[3:5])
    m10 = (m // BIN_MIN) * BIN_MIN
    return f"{h:02d}:{m10:02d}"


def classify_pv_10m(C, O, rvol, t, look=5):
    if rvol[t] is None or t < 1:
        return "na", 0.0
    rv = rvol[t]
    a = max(0, t - look)
    impulse = C[t] - C[a]
    up = impulse > 0
    if rv >= CLIMAX_10M:
        return ("climax_up" if up else "climax_dn"), impulse
    if rv >= EXPAND_10M:
        return ("expand_up" if up else "expand_dn"), impulse
    if rv <= DRY_10M:
        return "dry", impulse
    if rv <= CONTRACT_10M:
        return "contract", impulse
    hh = C[t] >= max(C[a : t + 1]) - 1e-9
    if hh and rv < 1.0 and impulse > 0:
        return "div_hh_weak_vol", impulse
    return "normal", impulse


def build_pv10_series(T, O, H, L, C, V) -> list[str]:
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

    O10 = [O[idxs[0]] for idxs in buckets]
    C10 = [C[idxs[-1]] for idxs in buckets]
    V10 = [sum(V[i] for i in idxs) for idxs in buckets]
    rv10 = _ORIG_RVOL_SERIES(V10)

    pv10 = []
    for bi in range(len(buckets)):
        reg, _ = classify_pv_10m(C10, O10, rv10, bi)
        pv10.append(reg)

    out = ["na"] * n
    for b_idx, idxs in enumerate(buckets):
        prior_pv = pv10[b_idx - 1] if b_idx > 0 else "na"
        for i in idxs:
            out[i] = prior_pv
    return out


def patched_factory(pv_series):
    def _patched(C, O, rvol, t, look=5):
        return pv_series[t], 0.0
    return _patched


def run_day(day: str, recipe: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    trades_base, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    net_base = round(sum(t["pnl"] for t in trades_base), 1)

    pv_series = build_pv10_series(T, O, H, L, C, V)
    ce.classify_pv = patched_factory(pv_series)
    try:
        trades_10m, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    net_10m = round(sum(t["pnl"] for t in trades_10m), 1)

    return dict(
        day=day, n_base=len(trades_base), net_base=net_base,
        n_10m=len(trades_10m), net_10m=net_10m,
        diff=round(net_10m - net_base, 1),
    )


def run_window(days, vix, recipe, label):
    rows = []
    for day in days:
        r = run_day(day, recipe, vix)
        if r.get("skipped"):
            continue
        rows.append(r)
        print(json.dumps(r), flush=True)

    diffs = [r["diff"] for r in rows]
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
    print(f"n={n} base_sum={sum(r['net_base'] for r in rows):.1f} "
          f"10m_sum={sum(r['net_10m'] for r in rows):.1f} "
          f"base_trades/day={sum(r['n_base'] for r in rows)/n:.1f} "
          f"10m_trades/day={sum(r['n_10m'] for r in rows)/n:.1f}")
    print(f"diff mean={mean_d:.2f} std={std_d:.2f} t={t_stat:.3f} p={p_val}")
    return dict(n=n, mean_diff=mean_d, std_diff=std_d, t=t_stat, p=p_val, rows=rows)


def main():
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    is_days = JULY_DAYS + AUG_DAYS
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    is_result = run_window(is_days, vix, recipe, "IN-SAMPLE (22 days)")
    oos_result = run_window(oos_days, vix, recipe, "OUT-OF-SAMPLE (66 days)")

    out_path = "reports/research/channel_lab/tmf_10m_primary_1m_calib_prototype_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(in_sample=is_result, out_of_sample=oos_result), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
