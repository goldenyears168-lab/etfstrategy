#!/usr/bin/env python3
"""v2 (2026-08-09): the v1 combo (30-min-primary PV8 + a 2-hour conflict
SUPPRESSION filter) tested clean but reliably WORSE than the current live
1-min recipe in both windows (IS mean=-302.8/day p=0.051, OOS mean=-184.9/day
p=0.022 -- same direction both times, not overfitting noise, just a real
negative effect). User's follow-up ask: also add the complementary,
offensive half -- when the two scales AGREE (not just "don't punish
conflict", actively reward alignment/confidence), strengthen that side.

Mechanism: reuse the SAME remap-via-classify_pv-monkeypatch trick as v1,
but now with FOUR cases instead of two, per session:
  - conflict (30m impulse sign vs 2h trend sign disagree): remap to the
    cell that's ALREADY block=["L","S"] in the live book (day: "expand_up",
    night: "expand_dn") -- unchanged from v1.
  - aligned bullish (both +1): remap to the strongest UNBLOCKED
    bull-favoring cell already tuned in the live book (day: "climax_up",
    widest band 27-42, the single best-performing day cell in the original
    1-min 22-day breakdown at +26.7pt/trade; night: "expand_up", 2nd-best
    night cell at +20.3pt/trade).
  - aligned bearish (both -1): remap to the strongest S-favoring cell
    (day: "expand_dn", block=["L"] only so S stays open; night:
    "climax_dn", block=["L"] only, the single BEST cell overall at
    +27.4pt/trade).
  - anything else (30m impulse flat, or 2h trend flat): pass the original
    30m-driven label through unchanged, same as v1.
No brand-new parameters are invented -- every reward target reuses an
EXISTING, already-tuned cell's hang/gamma/max_hold from the current live
book; "confidence" is expressed by routing aligned bars to cells that are
already wider-banded and historically the best performers for that
direction, not by hand-picking new numbers.

Window choice (120 bars = 2h) is fixed by reasoning, not searched. Tested
once on the 22-day in-sample set AND the 66-day OOS set in the same run,
both reported, no iteration between them.

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
DAY_SINK = "expand_up"    # block=["L","S"] in the live book (day session) -- conflict
NIGHT_SINK = "expand_dn"  # block=["L","S"] in the live book (night, via CELL_TUNE_V2) -- conflict
DAY_BULL_REWARD = "climax_up"    # unblocked, widest day band, best day-cell performer
DAY_BEAR_REWARD = "expand_dn"    # block=["L"] only -> S stays open
NIGHT_BULL_REWARD = "expand_up"  # unblocked, 2nd-best night-cell performer
NIGHT_BEAR_REWARD = "climax_dn"  # block=["L"] only -> S stays open, best cell overall


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
    n_reward = 0
    for t in range(n):
        reg = pv_at[t]
        if reg in ("na",):
            out[t] = reg
            continue
        isign, tsign = imp_sign_at[t], trend_sign_at[t]
        is_day = sess_of_hhmm(hm[t]) == "day"
        if isign != 0 and tsign != 0 and isign != tsign:
            out[t] = DAY_SINK if is_day else NIGHT_SINK
            n_conflict += 1
        elif isign == 1 and tsign == 1:
            out[t] = DAY_BULL_REWARD if is_day else NIGHT_BULL_REWARD
            n_reward += 1
        elif isign == -1 and tsign == -1:
            out[t] = DAY_BEAR_REWARD if is_day else NIGHT_BEAR_REWARD
            n_reward += 1
        else:
            out[t] = reg
    return out, n_conflict, n_reward


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

    # combo: 30-min PV base + 2h trend-conflict suppression + alignment reward
    pv_series, n_conflict, n_reward = build_pv30_plus_2h(T, O, H, L, C, V)
    ce.classify_pv = patched_factory(pv_series)
    try:
        trades_combo, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    net_combo = round(sum(t["pnl"] for t in trades_combo), 1)

    return dict(
        day=day, n_base=len(trades_base), net_base=net_base,
        n_combo=len(trades_combo), net_combo=net_combo,
        n_conflict_bars=n_conflict, n_reward_bars=n_reward,
        diff=round(net_combo - net_base, 1),
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

    out_path = "reports/research/channel_lab/tmf_30m_plus_2h_trend_combo_v2_reward_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(in_sample=is_result, out_of_sample=oos_result), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
