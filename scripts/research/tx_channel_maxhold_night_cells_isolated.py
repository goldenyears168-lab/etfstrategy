"""Isolated single-factor test: extend max_hold_bars for the three night PV8
cells that (a) never received any individual max_hold_bars validation (only
inherited the freeze-book default of 20) and (b) are the most frequently
cap-bound cells in the live recipe: night|contract (17.0% of trades hit cap),
night|dry (26.1%), night|climax_dn (16.8%). Diagnostic pass (see chat) showed
cap-hit trades in these cells have a materially HIGHER mean pnl than the
cell's overall average -- i.e. the cap is disproportionately truncating
winners, not cutting losers short. This script TRUE re-simulates baseline vs
a fork with only these three cells' max_hold_bars raised 20 -> 28 (matching
the magnitude of the existing validated night|expand_up bump), nothing else
touched, across all 4 sanctioned windows, day-clustered.

Read-only w.r.t. src/order/*, src/tmf_channel/causal_engine.py, config/order.yaml.
The "fork" here is a plain dict patch to PAPER_RECIPE's session_pv_book copy,
not an engine code fork -- session_pv_book is pure per-bar data read by the
frozen engine (see tmf_channel_pv16_book.py header + causal_engine.py usage).
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

SOURCES = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

PATCH_CELLS = [("night", "contract"), ("night", "dry"), ("night", "climax_dn")]
NEW_HOLD = 28


def _arrays(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def build_recipe(patched: bool) -> dict:
    recipe = deepcopy(dict(PAPER_RECIPE))
    recipe.setdefault("hang_anchor", "O")
    if patched:
        book = recipe["session_pv_book"]
        for sess, pv in PATCH_CELLS:
            book[sess][pv]["max_hold_bars"] = NEW_HOLD
    return recipe


def run(recipe, vix):
    per_window_day_pnl: dict[str, dict[str, float]] = defaultdict(dict)
    for label, cache in SOURCES.items():
        for day in list_days(source=cache):
            rows = load_day(day, source=cache)
            if not rows:
                continue
            O, H, L, C, V, T = _arrays(day, rows)
            trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
            per_window_day_pnl[label][day] = sum(t["pnl"] for t in trades)
    return per_window_day_pnl


def pooled_stats(per_window_day_pnl):
    all_days = [v for w in per_window_day_pnl.values() for v in w.values()]
    n = len(all_days)
    mean = statistics.mean(all_days)
    std = statistics.pstdev(all_days) if n > 1 else 0.0
    win = sum(1 for v in all_days if v > 0) / n
    return dict(n=n, mean=mean, std=std, win_rate=win, sum=sum(all_days))


def window_stats(day_pnl: dict[str, float]):
    vals = list(day_pnl.values())
    n = len(vals)
    mean = statistics.mean(vals) if n else 0.0
    std = statistics.pstdev(vals) if n > 1 else 0.0
    win = (sum(1 for v in vals if v > 0) / n) if n else 0.0
    return dict(n=n, mean=mean, std=std, win_rate=win)


def paired_ttest(a: list[float], b: list[float]):
    """b - a, paired t across days."""
    diffs = [y - x for x, y in zip(a, b)]
    n = len(diffs)
    if n < 2:
        return dict(n=n, mean_diff=diffs[0] if diffs else 0.0, t=None, p=None)
    m = statistics.mean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return dict(n=n, mean_diff=m, t=float("inf") if m != 0 else 0.0, p=None)
    t = m / (sd / (n**0.5))
    # two-sided p via normal approx (n is large enough, 265 days) -- keep it simple/manual (no scipy dep assumed)
    try:
        from scipy import stats as _st

        p = 2 * (1 - _st.t.cdf(abs(t), df=n - 1))
    except Exception:
        p = None
    return dict(n=n, mean_diff=m, t=t, p=p)


def main():
    vix = load_vixtwn_delta() or {}
    base_recipe = build_recipe(patched=False)
    patch_recipe = build_recipe(patched=True)

    base_days = run(base_recipe, vix)
    patch_days = run(patch_recipe, vix)

    print("=== Baseline (current live v1.4.0) ===")
    print(pooled_stats(base_days))
    for w in SOURCES:
        print(f"  {w}: {window_stats(base_days[w])}")

    print("\n=== Patched (night|contract,dry,climax_dn max_hold_bars 20->28) ===")
    print(pooled_stats(patch_days))
    for w in SOURCES:
        print(f"  {w}: {window_stats(patch_days[w])}")

    # aligned day lists per window for paired test
    all_diffs = []
    print("\n=== Per-window paired delta (patched - baseline), day-clustered t-test ===")
    for w in SOURCES:
        days = sorted(set(base_days[w]) & set(patch_days[w]))
        a = [base_days[w][d] for d in days]
        b = [patch_days[w][d] for d in days]
        res = paired_ttest(a, b)
        print(f"  {w}: n={res['n']} mean_diff={res['mean_diff']:.2f} t={res['t']} p={res['p']}")
        all_diffs.extend(y - x for x, y in zip(a, b))

    n = len(all_diffs)
    m = statistics.mean(all_diffs)
    sd = statistics.stdev(all_diffs) if n > 1 else 0.0
    t = m / (sd / (n**0.5)) if sd else float("inf") if m else 0.0
    try:
        from scipy import stats as _st

        p = 2 * (1 - _st.t.cdf(abs(t), df=n - 1))
    except Exception:
        p = None
    print(f"\nPooled paired delta: n={n} mean_diff={m:.3f} std_diff={sd:.2f} t={t:.3f} p={p}")

    # single-day-artifact check: drop the single best and single worst delta day
    idx_days = []
    for w in SOURCES:
        days = sorted(set(base_days[w]) & set(patch_days[w]))
        for d in days:
            idx_days.append((w, d, patch_days[w][d] - base_days[w][d]))
    idx_days.sort(key=lambda x: x[2])
    worst = idx_days[0]
    best = idx_days[-1]
    print(f"\nSingle-day check: worst delta day = {worst}, best delta day = {best}")
    trimmed = [d[2] for d in idx_days if d not in (worst, best)]
    print(
        f"Excluding both best+worst delta day: n={len(trimmed)} mean_diff={statistics.mean(trimmed):.3f} "
        f"std_diff={statistics.stdev(trimmed):.2f}"
    )

    out = {
        "baseline_pooled": pooled_stats(base_days),
        "patched_pooled": pooled_stats(patch_days),
        "baseline_per_window": {w: window_stats(base_days[w]) for w in SOURCES},
        "patched_per_window": {w: window_stats(patch_days[w]) for w in SOURCES},
        "pooled_paired_delta": dict(n=n, mean_diff=m, std_diff=sd, t=t, p=p),
        "worst_delta_day": worst,
        "best_delta_day": best,
    }
    out_path = "reports/research/channel_lab/maxhold_night_cells_isolated_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
