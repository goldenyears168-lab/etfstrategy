"""H-CAUSAL-ANCHOR follow-up: causal rolling VWAP anchor vs live O-anchor,
re-run on the CURRENT live v1.4.0 recipe (post PV16 cell-tune, eod_flatten=
False, far_cover=65/105) across all 4 sanctioned windows.

Motivation (audit gap): the one prior anchor bake-off
(reports/research/channel_lab/hang_anchor_causal_compare.py, 2026-08-05) only
covered O / EMA(close) family anchors, on a SUPERSEDED recipe, and never
reached significance. VWAP was flagged as a genuine unexplored gap: no file,
no engine support anywhere in the archive. Rationale for trying it now: hang
placement should track "volume-confirmed" fair value, not raw price -- a
volume-weighted anchor should sit still through thin, noisy stretches (many
of TX's whipsaw/struct_break losses cluster in low-volume chop) while still
tracking real, volume-backed directional moves. Hypothesis: more STABLE
anchor -> fewer premature/false hang placements -> lower std, and possibly
better mean if it avoids some of the struct_break tail losses.

Mechanism (strictly causal, PIT through bar t-1, matching the existing "ema"
family's contract -- bar t's own O/H/L/C/V never enter its own anchor value):
  TP[i] = (H[i] + L[i] + C[i]) / 3          (typical price, bar i)
  window = bars [max(0, t-n) .. t-1]         (n=15, same n as the prior
                                               bake-off's best-looking
                                               trend_ema15 candidate, for a
                                               fair like-for-like n)
  anchor[t] = sum(TP[i]*V[i] for i in window) / sum(V[i] for i in window)
  Fallback if sum(V)==0 in the window (illiquid night stretch): carry
  forward the previous anchor value (or O[t] if t==0).

Implementation: monkeypatches `tmf_channel.causal_engine.build_anchor_series`
in-process only (this script's own Python session) to add a "vwap" mode.
Does NOT edit src/tmf_channel/causal_engine.py on disk. The live simulate()
call site only passes (O, C, mode, n) to build_anchor_series -- it has no H/
L/V arguments -- so H/L/V for the day currently being simulated are stashed
in a module-level context dict immediately before each simulate() call (this
script's own day loop is strictly sequential/single-threaded, so this is
safe and PIT-correct: the stashed arrays are always exactly the arrays being
fed to the simulate() call that will read them).

Uses the shared tmf_channel.harness (whole-day net P&L, no cell filtering)
so the pooled/per-window numbers are directly comparable to the operator-
supplied baseline (pooled mean=+38.3, std=270.3, n=265).

Observe-only research. No order submit. No changes to src/order/,
src/tmf_channel/causal_engine.py, config/order.yaml, .env, launchd/,
scripts/order/.
"""
from __future__ import annotations

import json
import math
import statistics as st
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days
from tmf_channel.harness import run_days

import tmf_channel.causal_engine as ce

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

VWAP_N = 15

_ORIG_BUILD_ANCHOR = ce.build_anchor_series
_CTX: dict = {"H": None, "L": None, "V": None}


def _vwap_anchor(O, C, n=VWAP_N):
    H, L, V = _CTX["H"], _CTX["L"], _CTX["V"]
    assert H is not None and len(H) == len(O), "VWAP context not stashed for this day"
    n_bars = len(C)
    tp = [(H[i] + L[i] + C[i]) / 3.0 for i in range(n_bars)]
    out = [0.0] * n_bars
    prev = None
    for t in range(n_bars):
        lo = max(0, t - n)
        hi = t  # exclusive: window is [lo, t-1]
        num = 0.0
        den = 0.0
        for i in range(lo, hi):
            v = float(V[i] or 0.0)
            num += tp[i] * v
            den += v
        if den > 0:
            val = num / den
        elif prev is not None:
            val = prev
        else:
            val = float(O[t])
        out[t] = val
        prev = val
    return out


def _patched_build_anchor_series(O, C, mode, n=15):
    if str(mode or "") == "vwap":
        return _vwap_anchor(O, C, n=VWAP_N)
    return _ORIG_BUILD_ANCHOR(O, C, mode, n=n)


def _patched_simulate(O, H, L, C, V, T, params=None, **kw):
    _CTX["H"], _CTX["L"], _CTX["V"] = H, L, V
    return _ORIG_SIMULATE(O, H, L, C, V, T, params, **kw)


_ORIG_SIMULATE = ce.simulate
ce.build_anchor_series = _patched_build_anchor_series
ce.simulate = _patched_simulate

# tmf_channel.engine AND tmf_channel.harness both re-export/import `simulate`
# by reference at import time (`from tmf_channel.engine import ... simulate`)
# -- repoint every one of those bound names so tmf_channel.harness.run_days
# (the actual call path this script uses) calls the patched version too.
import tmf_channel.engine as te  # noqa: E402
import tmf_channel.harness as th  # noqa: E402

te.simulate = _patched_simulate
th.simulate = _patched_simulate


def t_test_1samp(vals: list[float]):
    n = len(vals)
    if n < 2:
        return float("nan"), float("nan")
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se == 0:
        return (float("inf") if mean != 0 else 0.0), (0.0 if mean != 0 else 1.0)
    t = mean / se
    try:
        from scipy import stats

        p = 2 * stats.t.sf(abs(t), df=n - 1)
    except Exception:
        p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return t, p


def paired_t_test(a: list[float], b: list[float]):
    """a - b, paired by index (same days, same order)."""
    diffs = [x - y for x, y in zip(a, b)]
    return t_test_1samp(diffs), diffs


def regression_check():
    """Prove patched engine with hang_anchor='O' (unpatched path) reproduces
    the exact baseline pooled numbers before trusting the vwap run."""
    all_nets = []
    for _w, cache in WINDOWS.items():
        days = list_days(cache)
        rows = run_days(days, recipe=PAPER_RECIPE, cache_name=cache)
        all_nets.extend(r["net"] for r in rows if r.get("ok"))
    mean = sum(all_nets) / len(all_nets)
    sd = st.pstdev(all_nets)
    print(
        f"[regression] patched-engine O-baseline: n={len(all_nets)} "
        f"mean={mean:.1f} std={sd:.1f} (expect ~38.3 / ~269.8)"
    )
    assert abs(mean - 38.3) < 2.0, f"regression check failed: mean={mean}"
    assert abs(sd - 269.8) < 5.0, f"regression check failed: std={sd}"


def main():
    regression_check()

    vwap_recipe = deepcopy(PAPER_RECIPE)
    vwap_recipe["hang_anchor"] = "vwap"

    baseline_by_window: dict[str, list[float]] = {}
    vwap_by_window: dict[str, list[float]] = {}
    baseline_days: dict[str, list[str]] = {}

    for w, cache in WINDOWS.items():
        days = list_days(cache)
        base_rows = run_days(days, recipe=PAPER_RECIPE, cache_name=cache)
        vwap_rows = run_days(days, recipe=vwap_recipe, cache_name=cache)
        base_ok = [r for r in base_rows if r.get("ok")]
        vwap_ok = {r["day"]: r for r in vwap_rows if r.get("ok")}
        # align strictly by day (both should have identical day sets)
        common = [d["day"] for d in base_ok if d["day"] in vwap_ok]
        assert len(common) == len(days), f"{w}: day mismatch {len(common)} vs {len(days)}"
        base_nets = [next(r["net"] for r in base_ok if r["day"] == d) for d in common]
        vwap_nets = [vwap_ok[d]["net"] for d in common]
        baseline_by_window[w] = base_nets
        vwap_by_window[w] = vwap_nets
        baseline_days[w] = common

    out = {"vwap_n": VWAP_N, "windows": {}}

    all_base, all_vwap = [], []
    for w in WINDOWS:
        b, v = baseline_by_window[w], vwap_by_window[w]
        all_base.extend(b)
        all_vwap.extend(v)
        (t, p), diffs = paired_t_test(v, b)
        out["windows"][w] = dict(
            n_days=len(b),
            base_mean=round(sum(b) / len(b), 1),
            base_std=round(st.pstdev(b), 1),
            vwap_mean=round(sum(v) / len(v), 1),
            vwap_std=round(st.pstdev(v), 1),
            diff_mean=round(sum(diffs) / len(diffs), 1),
            paired_t=round(t, 3),
            paired_p=round(p, 4),
        )
        print(
            f"{w:9s} n={len(b):3d} base_mean={out['windows'][w]['base_mean']:>7.1f} "
            f"vwap_mean={out['windows'][w]['vwap_mean']:>7.1f} "
            f"base_std={out['windows'][w]['base_std']:>7.1f} "
            f"vwap_std={out['windows'][w]['vwap_std']:>7.1f} "
            f"paired_t={t:.3f} p={p:.4f}"
        )

    (pt, pp), pdiffs = paired_t_test(all_vwap, all_base)
    pooled = dict(
        n_days=len(all_base),
        base_mean=round(sum(all_base) / len(all_base), 1),
        base_std=round(st.pstdev(all_base), 1),
        vwap_mean=round(sum(all_vwap) / len(all_vwap), 1),
        vwap_std=round(st.pstdev(all_vwap), 1),
        base_sharpe=round((sum(all_base) / len(all_base)) / st.pstdev(all_base), 4),
        vwap_sharpe=round((sum(all_vwap) / len(all_vwap)) / st.pstdev(all_vwap), 4),
        diff_mean=round(sum(pdiffs) / len(pdiffs), 1),
        paired_t=round(pt, 3),
        paired_p=round(pp, 4),
    )
    out["pooled"] = pooled
    print(f"\nPOOLED n={pooled['n_days']} base_mean={pooled['base_mean']} "
          f"vwap_mean={pooled['vwap_mean']} base_std={pooled['base_std']} "
          f"vwap_std={pooled['vwap_std']} base_sharpe={pooled['base_sharpe']} "
          f"vwap_sharpe={pooled['vwap_sharpe']} paired_t={pooled['paired_t']} "
          f"p={pooled['paired_p']}")

    # single best/worst diff-day removal check on pooled diffs
    idx_sorted = sorted(range(len(pdiffs)), key=lambda i: pdiffs[i])
    worst_i, best_i = idx_sorted[0], idx_sorted[-1]
    wo_best = [d for i, d in enumerate(pdiffs) if i != best_i]
    wo_worst = [d for i, d in enumerate(pdiffs) if i != worst_i]
    out["excl_check"] = dict(
        best_diff_day=pdiffs[best_i],
        mean_diff_excl_best=round(sum(wo_best) / len(wo_best), 2),
        worst_diff_day=pdiffs[worst_i],
        mean_diff_excl_worst=round(sum(wo_worst) / len(wo_worst), 2),
        mean_diff_all=round(sum(pdiffs) / len(pdiffs), 2),
    )
    print(f"excl_check: {out['excl_check']}")

    out_path = "reports/research/channel_lab/hang_anchor_vwap_causal_compare_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
