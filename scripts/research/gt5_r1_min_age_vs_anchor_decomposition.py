#!/usr/bin/env python3
"""gt5 Round 1 (2026-08-11): decompose fix B (continuous per-bar NQ gate
anchor, 2026-08-10) vs fix C (min_age=1h forming-bar exclusion in
``price_at_or_before``, added to the workspace tonight, not yet committed)
into separate contributions, on the SAME 88-day window (22 IS + 66 OOS)
used by ``scripts/research/tmf_continuous_gate_vs_frozen_anchor.py``
(fix B's original validation, which ran BEFORE fix C existed in the
codebase -- at that time ``nq_signal.futures_overnight_at`` called
``price_at_or_before`` with no ``min_age`` kwarg at all, i.e. exactly the
"no min_age" arm below).

This is a DIAGNOSTIC re-audit, not a proposal. It does not touch
config/strategy.yaml, config/strategies.yaml, src/tmf_channel/*.py,
src/order/*.py, src/us_futures_overnight.py, launchd, or .env. All four
combinations below are produced by IN-MEMORY monkeypatches of
``tmf_channel.nq_signal.NQ_ES_1H_MIN_AGE`` (module-global consulted by
``futures_overnight_at`` at call time) -- never a disk edit -- restored via
try/finally even on exception.

Four cells (2 anchor modes x 2 min_age modes):
  - frozen anchor    = nq_side_for_day(day, hm="08:45"), one value/day
                        (matches the "frozen" arm of the original B-only
                        validation script).
  - continuous anchor = nq_signal.futures_overnight_at() recomputed at each
                        bar's own timestamp (the "continuous" arm).
  - no min_age        = NQ_ES_1H_MIN_AGE temporarily set to None, i.e. the
                        code as it existed before tonight's fix C (a bar
                        still forming its own hour can be read as if it
                        were already the final close).
  - min_age=1h        = NQ_ES_1H_MIN_AGE=timedelta(hours=1), i.e. the
                        current workspace state (fix C applied) -- only
                        accept a hourly bar once its own close time
                        (index + 1h) is <= the query time.

Run:
  cd /Users/jackm4/goldenstocks && PYTHONPATH=src:reports/research/channel_lab \
    nice -n 15 .venv/bin/python scripts/research/gt5_r1_min_age_vs_anchor_decomposition.py
"""
from __future__ import annotations

import json
import statistics as st
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import numpy as np  # noqa: E402
import statsmodels.api as sm  # noqa: E402

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_channel.nq_gate import nq_side_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402
from us_futures_overnight import price_at_or_before  # noqa: E402

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

OUT_DIR = "reports/research/channel_lab"


@contextmanager
def min_age_mode(min_age: timedelta | None):
    """Toggle fix C in-memory only. futures_overnight_at() reads
    nq_signal.NQ_ES_1H_MIN_AGE as a module global at call time (late
    binding), so reassigning the attribute here changes behavior for every
    caller (nq_side_for_day via tmf_channel.nq_gate, and the direct
    continuous_gate_for_day() call below) without editing the file on
    disk. Restored in finally so a mid-run exception can never leave the
    process in the "no min_age" (pre-fix-C) state for subsequent code."""
    prev = nq_signal.NQ_ES_1H_MIN_AGE
    nq_signal.NQ_ES_1H_MIN_AGE = min_age
    try:
        yield
    finally:
        nq_signal.NQ_ES_1H_MIN_AGE = prev


# 2026-08-11: this file used to carry its OWN copy of continuous_gate_for_day()
# that predated the source-aware timestamp fix (no `source=` kwarg, no
# assert_real_timestamps guard). The mass fix updated the call site below but
# missed this stale local def, so the script died with TypeError. Use the
# canonical fixed implementation instead of re-duplicating it -- same body,
# plus the fail-closed guard.
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402


def run_day(day: str, recipe_base: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    out = dict(day=day)
    for min_age_label, min_age_val in (("no_min_age", None), ("min_age_1h", timedelta(hours=1))):
        with min_age_mode(min_age_val):
            frozen_side = nq_side_for_day(day, hm="08:45")
            r_frozen = dict(recipe_base)
            r_frozen["session_side_gate"] = {day: frozen_side} if frozen_side is not None else {}
            trades_frozen, *_ = simulate(O, H, L, C, V, T, r_frozen, vix_delta=vix)
            net_frozen = round(sum(t["pnl"] for t in trades_frozen), 1)

            cont_gate = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
            r_cont = dict(recipe_base)
            r_cont["session_side_gate"] = cont_gate
            trades_cont, *_ = simulate(O, H, L, C, V, T, r_cont, vix_delta=vix)
            net_cont = round(sum(t["pnl"] for t in trades_cont), 1)

        out[f"n_frozen_{min_age_label}"] = len(trades_frozen)
        out[f"net_frozen_{min_age_label}"] = net_frozen
        out[f"n_continuous_{min_age_label}"] = len(trades_cont)
        out[f"net_continuous_{min_age_label}"] = net_cont

    # Derived diff series (see module docstring for what each isolates).
    out["diff_BC_no_min_age"] = round(out["net_continuous_no_min_age"] - out["net_frozen_no_min_age"], 1)
    out["diff_BC_min_age_1h"] = round(out["net_continuous_min_age_1h"] - out["net_frozen_min_age_1h"], 1)
    out["fixC_effect_on_continuous"] = round(out["net_continuous_min_age_1h"] - out["net_continuous_no_min_age"], 1)
    out["fixC_effect_on_frozen"] = round(out["net_frozen_min_age_1h"] - out["net_frozen_no_min_age"], 1)
    return out


def hac_summary(diffs: list[float], label: str) -> dict:
    n = len(diffs)
    x = np.asarray(diffs, dtype=float)
    mean_d = float(np.mean(x))
    std_d = float(np.std(x, ddof=1)) if n > 1 else 0.0
    # lag-1 autocorrelation
    if n > 2:
        x_c = x - mean_d
        lag1_acf = float(np.sum(x_c[:-1] * x_c[1:]) / np.sum(x_c**2)) if np.sum(x_c**2) > 0 else 0.0
    else:
        lag1_acf = 0.0
    t_naive = mean_d / (std_d / (n**0.5)) if std_d > 0 and n > 1 else 0.0
    try:
        from scipy import stats as sp

        p_naive = float(2 * (1 - sp.t.cdf(abs(t_naive), df=n - 1))) if n > 1 else None
    except Exception:
        p_naive = None

    hac = {}
    if n > 3:
        Xc = np.ones((n, 1))
        for maxlags in (1, 5, 10, 20):
            if maxlags >= n:
                continue
            try:
                model = sm.OLS(x, Xc).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
                hac[f"maxlags_{maxlags}"] = dict(
                    p_value=float(model.pvalues[0]), coef=float(model.params[0])
                )
            except Exception as exc:  # noqa: BLE001
                hac[f"maxlags_{maxlags}"] = dict(error=str(exc)[:200])

    return dict(
        label=label, n=n, mean=round(mean_d, 2), std=round(std_d, 2),
        lag1_autocorr=round(lag1_acf, 3),
        naive_t=round(t_naive, 3), naive_p=p_naive,
        hac=hac,
    )


def run_window(days, recipe_base, vix, label):
    rows = []
    for day in days:
        r = run_day(day, recipe_base, vix)
        if r.get("skipped"):
            continue
        rows.append(r)
        print(json.dumps(r), flush=True)

    series = {
        "diff_BC_no_min_age": [r["diff_BC_no_min_age"] for r in rows],
        "diff_BC_min_age_1h": [r["diff_BC_min_age_1h"] for r in rows],
        "fixC_effect_on_continuous": [r["fixC_effect_on_continuous"] for r in rows],
        "fixC_effect_on_frozen": [r["fixC_effect_on_frozen"] for r in rows],
    }
    summaries = {k: hac_summary(v, f"{label}:{k}") for k, v in series.items()}

    print(f"\n=== {label} summary (n={len(rows)}) ===")
    for k, s in summaries.items():
        print(f"  {k}: mean={s['mean']} naive_t={s['naive_t']} naive_p={s['naive_p']} "
              f"lag1_acf={s['lag1_autocorr']} hac={s['hac']}")

    return dict(n=len(rows), rows=rows, summaries=summaries)


def bug2_perturbation_test(nq_1h) -> list[dict]:
    """Inject an anomalous close into a bar that is STILL FORMING at
    several query timestamps, then assert: (a) with min_age=1h, every
    query strictly before that bar's own settle time (index+1h) is
    completely blind to the anomaly -- it reads the same value the
    unperturbed baseline series would have returned; (b) with min_age=None
    (the pre-fix-C code path, reproduced here purely in-memory), the same
    forming-bar queries DO leak the anomalous, not-yet-final value; (c)
    once the bar has actually settled (query >= index+1h), BOTH min_age
    settings correctly see the (now legitimately settled) perturbed value
    -- proving fix C only excludes access DURING formation, not the bar
    itself once real."""
    idx = nq_1h.index
    if len(idx) < 20:
        return [{"skipped": True, "reason": "series too short"}]
    pick_pos = len(idx) // 2
    ts0 = idx[pick_pos]
    orig_val = float(nq_1h.iloc[pick_pos])
    perturbed_val = round(orig_val * 1.5 + 500.0, 2)
    s_pert = nq_1h.copy()
    s_pert.iloc[pick_pos] = perturbed_val

    settle = ts0 + timedelta(hours=1)
    rows = []
    for minutes in (1, 10, 30, 59):
        q = ts0 + timedelta(minutes=minutes)
        v_fixed = price_at_or_before(s_pert, q, min_age=timedelta(hours=1))
        v_old = price_at_or_before(s_pert, q, min_age=None)
        v_baseline_fixed = price_at_or_before(nq_1h, q, min_age=timedelta(hours=1))
        rows.append(dict(
            phase="forming", offset_min=minutes, query=str(q), bar_ts0=str(ts0),
            settle_ts=str(settle), perturbed_val=perturbed_val,
            fixed_min_age_1h=v_fixed, old_no_min_age=v_old,
            baseline_unperturbed_fixed=v_baseline_fixed,
            fixed_is_immune_to_forming_bar_anomaly=(v_fixed == v_baseline_fixed and v_fixed != perturbed_val),
            old_leaks_forming_bar_anomaly=(v_old == perturbed_val),
        ))
    for minutes in (0, 30, 120):
        q = settle + timedelta(minutes=minutes)
        v_fixed = price_at_or_before(s_pert, q, min_age=timedelta(hours=1))
        v_old = price_at_or_before(s_pert, q, min_age=None)
        rows.append(dict(
            phase="settled", offset_min=minutes, query=str(q), bar_ts0=str(ts0),
            settle_ts=str(settle), perturbed_val=perturbed_val,
            fixed_min_age_1h=v_fixed, old_no_min_age=v_old,
            both_correctly_see_settled_perturbed_value=(v_fixed == perturbed_val and v_old == perturbed_val),
        ))
    return rows


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}

    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    is_days = JULY_DAYS + AUG_DAYS
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    is_result = run_window(is_days, recipe_base, vix, "IN-SAMPLE (22 days)")
    oos_result = run_window(oos_days, recipe_base, vix, "OUT-OF-SAMPLE (66 days)")

    # BUG-2 same-root look-ahead perturbation test, using the wide-fetched
    # NQ bundle already cached by patch_nq_gate_for_backfill's monkeypatch.
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    bug2_rows = bug2_perturbation_test(nq_1h)
    print("\n=== BUG-2 forming-bar perturbation test ===")
    for r in bug2_rows:
        print(json.dumps(r, ensure_ascii=False))

    out = dict(
        title="gt5 R1: fix B (continuous anchor) vs fix C (min_age=1h) decomposition",
        original_fixB_validation_claim="OOS mean=+206.27pt/day t=3.679 p=0.00048 (continuous-no_min_age minus frozen-no_min_age, pre-fix-C code)",
        in_sample=is_result,
        out_of_sample=oos_result,
        bug2_perturbation_test=bug2_rows,
    )
    out_path = f"{OUT_DIR}/gt5_r1_min_age_vs_anchor_decomposition_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
