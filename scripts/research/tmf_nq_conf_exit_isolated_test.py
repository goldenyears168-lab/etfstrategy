#!/usr/bin/env python3
"""2026-08-12: ISOLATED test of ``nq_conf_exit_enable`` alone (causal_engine.py
~line 1106-1117 params, exit logic ~line 1884-1917).

Mechanism under test: when a position is open, ``nq_side_conf`` scores
whether the currently-live NQ-vs-TX direction agrees ("with") or disagrees
("against") with the held side, and MODULATES (not hard-closes) the
existing ``max_hold_bars`` / ``trail_arm_pts`` / ``trail_giveback_pts`` —
against tightens (exits sooner on a smaller reversal), with loosens. This
is the continuous-modulation cousin of "when NQ drops, exit TX early", not
a hard trigger. Two exit_mode variants are tested here:
  - symmetric:        with-NQ gets more room, against-NQ gets less (both
                       directions of the modulation active).
  - against_only:      only tightens when NQ is against the held side; does
                       nothing when NQ agrees or is flat. Closer match to
                       the "exit early when NQ turns against you" idea.
``with_same_tighten`` exists in the engine too but is NOT the shape the
user described (it distinguishes with-NQ-extended vs with-NQ-diverged, a
different axis), so it is left out of the primary isolated comparison.

Everything else is held at CURRENT LIVE values: ``order.tmf_channel_config.
PAPER_RECIPE`` + ``order.tmf_channel_pv16_book.specialized_cell_book()`` as
-is. In particular this does NOT enable ``nq_conf_enable`` (the early-entry
variant), does NOT force ``always_lo`` hang anchor, does NOT set
``struct_disabled``, and does NOT touch ``nq_conf_soft_gate`` -- entries are
still hard-gated by the SAME live continuous per-bar NQ/ES gate
(``continuous_gate_for_day``, RECIPE_VERSION's "逐分鐘即時閘門"; per the
2026-08-11 handoff this gate itself is live but its edge over a frozen
anchor was found to be ~0 post-defect-fix -- irrelevant here, we just reuse
it unmodified as the entry-side gate both arms share, so any delta we find
is attributable ONLY to the exit modulation).

Windows (all on HEAD post commits 7aeb393 / 6128480 -- defects 0/A/B fixed):
  - julsep25 / octdec25 / janmar26: the three defect-immune holdout caches
    (confirmed below: 0 bars <05:00 in any of the 65+62+55 days -- defects
    A/B are structurally no-ops here, see handoff §2.1b).
  - w83: tx_1m_fullnight_cache_full.json (OOS, 66 days, 2026-04-01..) +
    tx_1m_tick_built_fullnight_aug (IS, 5 Aug days) + 17 July days from the
    same fullnight_cache_full source -- the SAME 22-IS/66-OOS day lists used
    by gt5_r1_min_age_vs_anchor_decomposition.py and
    tmf_nq_width_calib_sweep.py, loaded via cache_store.load_day() /
    bar_timestamps() (fixed bar-order + per-source post-midnight
    convention), NQ gate widened via
    tmf_order_layer_aware_replay.patch_nq_gate_for_backfill(
    lookback_days=500) to avoid the lookback_days=60 truncation defect.

Reuses (imports, does not fork): tmf_channel.engine.simulate,
tmf_continuous_gate_vs_frozen_anchor.continuous_gate_for_day,
tmf_order_layer_aware_replay.patch_nq_gate_for_backfill,
slow_cell_significance_helper.classify_significance,
gt5_r1_min_age_vs_anchor_decomposition.bug2_perturbation_test.

Does NOT touch src/order/, src/tmf_channel/, config/order.yaml,
config/strategy.yaml, or any .env file. Read-only research.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")
sys.path.insert(0, "reports/research/channel_lab")

import numpy as np  # noqa: E402
import statsmodels.api as sm  # noqa: E402

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from gt5_r1_min_age_vs_anchor_decomposition import bug2_perturbation_test  # noqa: E402
from slow_cell_significance_helper import classify_significance  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = "reports/research/channel_lab"

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
W83_IS_SOURCE = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
W83_IS_SOURCE.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
W83_IS_DAYS = JULY_DAYS + AUG_DAYS

EXIT_MODES = ["symmetric", "against_only"]


# ------------------------------------------------------------------
# Data loading + causal NQ-overnight series (BUG-2 safe: only ever reads
# at-or-before the query timestamp; verified generically below).
# ------------------------------------------------------------------

def load_arrays(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


def nq_on_1m_causal(T: list[str]) -> dict[str, dict[str, float]]:
    """{real_calendar_date: {HH:MM: nq_overnight_pct}}, forward-fill-ready.

    Keyed by EACH bar's own real calendar date (``t[:10]`` of the fixed
    ``bar_timestamps()`` output), NOT by a single session-date string. This
    matters for full-night sessions: causal_engine.py's own nq_on[] builder
    (`_day(ts) = str(ts)[:10]`) looks up ``nq_on_1m[d]`` per-bar using that
    bar's OWN calendar date -- post-midnight bars in a full-night source
    carry ``t[:10] == day+1`` (cache_store's fixed post-midnight
    convention). An earlier sibling script (tmf_nq_width_calib_sweep.py)
    keyed its nq_on_1m dict under a single ``arr[5][0].split('T')[0]``
    (the session-open date only) -- harmless for its own IS/OOS split
    (which doesn't rely on the post-midnight tail for nq_calib) but would
    silently starve nq_on[] for post-midnight minutes on any source with a
    day+1 tail. This isolated test needs nq_on[] correct across the WHOLE
    session (nq_conf_exit modulates exits at any bar, not just the open),
    so we key it correctly here."""
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    out: dict[str, dict[str, float]] = {}
    for t in T:
        dt_tw = datetime.fromisoformat(t).astimezone(_TZ)
        d = t[:10]
        hm = dt_tw.strftime("%H:%M")
        snap = nq_signal.futures_overnight_at(
            dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d, us_dates=us_dates
        )
        if snap is not None and snap.get("nq_overnight_pct") is not None:
            out.setdefault(d, {})[hm] = float(snap["nq_overnight_pct"])
    return out


# ------------------------------------------------------------------
# Simulate one day under baseline / candidate; return trades + BUG-1 check
# ------------------------------------------------------------------

def run_day(arr, gate, nq1m, recipe_base, vix, overrides, *, eod_flatten: bool = True) -> dict:
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    recipe["nq_on_1m"] = nq1m
    recipe.update(overrides)
    # BUG-1 (session-end accounting gap): PAPER_RECIPE ships eod_flatten=
    # False -- correct for the LIVE poll (an in-progress day's bar array,
    # "last bar != session end", see tmf_channel_config.py's own comment)
    # but WRONG for this script, which feeds one COMPLETE single day's bars
    # per call -- here the last bar genuinely IS session end. Verified
    # empirically before writing this override: on a 10-day julsep25 slice,
    # eod_flatten=False silently dropped 1/10 still-open positions from
    # `trades` (net -235.0 vs honestly-closed -211.0, a 24pt/10-day gap) --
    # exactly BUG-1's failure mode, and a real risk for an EXIT-mechanism
    # test specifically (whichever variant more often leaves a position
    # open into the final bar gets a free non-accounting of that position's
    # real P&L, which is precisely the thing being modulated here). We
    # therefore force eod_flatten=True for every run_day call regardless of
    # what recipe_base carries; main() separately re-runs a small
    # eod_flatten=False comparison to size the leak this override closes.
    recipe["eod_flatten"] = eod_flatten
    O, H, L, C, V, T = arr
    trades, events, ws, wl, rvol, regime, open_pos = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    return dict(
        trades=trades,
        net=round(sum(t["pnl"] for t in trades), 1),
        n=len(trades),
        open_pos=open_pos,  # must be None whenever eod_flatten=True
    )


def naive_stats(diffs: list[float]) -> dict:
    n = len(diffs)
    x = np.asarray(diffs, dtype=float)
    mean_d = float(np.mean(x)) if n else 0.0
    std_d = float(np.std(x, ddof=1)) if n > 1 else 0.0
    t_naive = mean_d / (std_d / (n**0.5)) if std_d > 0 and n > 1 else 0.0
    try:
        from scipy import stats as sp

        p_naive = float(2 * (1 - sp.t.cdf(abs(t_naive), df=n - 1))) if n > 1 else 1.0
    except Exception:
        p_naive = None
    lag1_acf = 0.0
    if n > 2:
        xc = x - mean_d
        denom = float(np.sum(xc**2))
        if denom > 0:
            lag1_acf = float(np.sum(xc[:-1] * xc[1:]) / denom)
    return dict(n=n, mean=round(mean_d, 3), std=round(std_d, 3), t=round(t_naive, 3),
                p=p_naive, lag1_autocorr=round(lag1_acf, 3))


def hac_p_by_maxlags(diffs: list[float], maxlags=(1, 5, 10, 20)) -> dict:
    n = len(diffs)
    out = {}
    if n <= 3:
        return out
    x = np.asarray(diffs, dtype=float)
    Xc = np.ones((n, 1))
    for lag in maxlags:
        if lag >= n:
            continue
        try:
            model = sm.OLS(x, Xc).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
            out[str(lag)] = dict(p_value=float(model.pvalues[0]), coef=float(model.params[0]))
        except Exception as exc:  # noqa: BLE001
            out[str(lag)] = dict(error=str(exc)[:200])
    return out


def _hac_ps_only(hac: dict) -> dict:
    """classify_significance wants {maxlags: p_value}; hac_p_by_maxlags()
    stores {maxlags: {"p_value":..., "coef":...}} (or {"error":...} on
    failure) -- extract just the finite p-values."""
    out = {}
    for k, v in hac.items():
        pv = v.get("p_value") if isinstance(v, dict) else None
        if pv is not None and pv == pv:  # not NaN
            out[k] = pv
    return out


def exit_reason_decomposition(trades: list[dict]) -> dict:
    """BUG-7: bucket PnL/count by exit_reason prefix (before '|')."""
    buckets: dict[str, dict] = {}
    for t in trades:
        why = str(t.get("why") or "?")
        key = why.split("|")[0]
        b = buckets.setdefault(key, dict(n=0, pnl=0.0))
        b["n"] += 1
        b["pnl"] += float(t["pnl"])
    for b in buckets.values():
        b["pnl"] = round(b["pnl"], 1)
    total_n = sum(b["n"] for b in buckets.values())
    total_pnl = round(sum(b["pnl"] for b in buckets.values()), 1)
    return dict(buckets=buckets, total_n=total_n, total_pnl=total_pnl)


# ------------------------------------------------------------------
# Window runner
# ------------------------------------------------------------------

def run_window(label: str, days: list[str], source_map: dict, recipe_base: dict, vix: dict) -> dict:
    per_day = []
    for d in days:
        source = source_map.get(d, next(iter(source_map.values())) if source_map else None)
        arr = load_arrays(d, source)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=source)
        nq1m = nq_on_1m_causal(arr[5])

        base = run_day(arr, gate, nq1m, recipe_base, vix, {"nq_conf_exit_enable": False})
        cands = {}
        for mode in EXIT_MODES:
            cands[mode] = run_day(
                arr, gate, nq1m, recipe_base, vix,
                {"nq_conf_exit_enable": True, "nq_conf_exit_mode": mode},
            )
        per_day.append(dict(day=d, base=base, cands=cands))

    result: dict = dict(label=label, n_days=len(per_day))
    baseline_trades_all = [t for row in per_day for t in row["base"]["trades"]]
    result["baseline"] = dict(
        n_trades=len(baseline_trades_all),
        net_sum=round(sum(t["pnl"] for t in baseline_trades_all), 1),
        exit_decomp=exit_reason_decomposition(baseline_trades_all),
        open_pos_leaks=sum(1 for row in per_day if row["base"]["open_pos"] is not None),
    )

    for mode in EXIT_MODES:
        deltas = [row["cands"][mode]["net"] - row["base"]["net"] for row in per_day]
        cand_trades_all = [t for row in per_day for t in row["cands"][mode]["trades"]]
        naive = naive_stats(deltas)
        hac = hac_p_by_maxlags(deltas)
        hac_ps = _hac_ps_only(hac)
        sig = None
        if hac_ps:
            sig = classify_significance(naive["mean"], naive["p"] if naive["p"] is not None else 1.0, hac_ps)
        result[mode] = dict(
            deltas=[round(v, 1) for v in deltas],
            naive=naive,
            hac=hac,
            significance=sig,
            candidate_n_trades=len(cand_trades_all),
            candidate_net_sum=round(sum(t["pnl"] for t in cand_trades_all), 1),
            candidate_exit_decomp=exit_reason_decomposition(cand_trades_all),
            open_pos_leaks=sum(1 for row in per_day if row["cands"][mode]["open_pos"] is not None),
        )
    print(f"=== {label} (n={result['n_days']}) ===")
    print(f"  baseline: n_trades={result['baseline']['n_trades']} net_sum={result['baseline']['net_sum']}")
    for mode in EXIT_MODES:
        r = result[mode]
        print(f"  {mode:14s} mean_delta={r['naive']['mean']:8.2f} naive_t={r['naive']['t']:6.2f} "
              f"naive_p={r['naive']['p']} lag1_acf={r['naive']['lag1_autocorr']} "
              f"hac={ {k: v.get('p_value') for k, v in r['hac'].items()} } "
              f"label={(r['significance'] or {}).get('label')}")
    return result


def pool_windows(results: list[dict], label: str) -> dict:
    """Pool day-clustered delta series across independent windows (simple
    concatenation -- these windows share no calendar days, so pooling the
    per-day delta series directly, then re-running naive+HAC on the pooled
    series, is valid; NOT a weighted-mean-of-means shortcut)."""
    out = dict(label=label, n_days=sum(r["n_days"] for r in results))
    for mode in EXIT_MODES:
        pooled_deltas = [d for r in results for d in r[mode]["deltas"]]
        naive = naive_stats(pooled_deltas)
        hac = hac_p_by_maxlags(pooled_deltas)
        hac_ps = _hac_ps_only(hac)
        sig = classify_significance(naive["mean"], naive["p"] if naive["p"] is not None else 1.0, hac_ps) if hac_ps else None
        out[mode] = dict(naive=naive, hac=hac, significance=sig,
                          per_window_mean=[round(r[mode]["naive"]["mean"], 2) for r in results])
    print(f"\n=== POOLED: {label} (n={out['n_days']} days) ===")
    for mode in EXIT_MODES:
        r = out[mode]
        print(f"  {mode:14s} mean_delta={r['naive']['mean']:8.2f} naive_t={r['naive']['t']:6.2f} "
              f"naive_p={r['naive']['p']} label={(r['significance'] or {}).get('label')}")
    return out


def bug1_leak_diagnostic(days, source_map, recipe_base, vix) -> dict:
    """Size the BUG-1 session-end leak this script's eod_flatten=True
    override closes, on the baseline recipe (nq_conf_exit_enable=False):
    honest force-close net vs PAPER_RECIPE's raw eod_flatten=False net,
    over the SAME days/gates used in the real test."""
    net_honest = net_raw = 0.0
    leaks_raw = 0
    for d in days:
        source = source_map.get(d)
        arr = load_arrays(d, source)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=source)
        nq1m = nq_on_1m_causal(arr[5])
        r_honest = run_day(arr, gate, nq1m, recipe_base, vix, {"nq_conf_exit_enable": False}, eod_flatten=True)
        r_raw = run_day(arr, gate, nq1m, recipe_base, vix, {"nq_conf_exit_enable": False}, eod_flatten=False)
        net_honest += r_honest["net"]
        net_raw += r_raw["net"]
        if r_raw["open_pos"] is not None:
            leaks_raw += 1
    return dict(
        n_days=len(days), net_honest_eod_flatten_true=round(net_honest, 1),
        net_raw_eod_flatten_false=round(net_raw, 1),
        leak_pt=round(net_honest - net_raw, 1), leaks_raw=leaks_raw,
    )


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()
    # Explicitly confirm we are NOT enabling the sibling early-entry
    # mechanism or any BIAS/EARLY extras -- isolate nq_conf_exit_enable only.
    recipe_base["nq_conf_enable"] = False
    assert recipe_base.get("struct_disabled", False) is False
    assert recipe_base.get("nq_conf_soft_gate", False) is False

    holdout_results = {}
    for label, source in HOLDOUT_SOURCES.items():
        days = list_days(source=source)
        holdout_results[label] = run_window(label, days, {d: source for d in days}, recipe_base, vix)

    pooled_holdout = pool_windows(list(holdout_results.values()), "pooled 3 defect-immune holdout windows")

    # BUG-1 diagnostic: size the session-end leak on a 15-day sample of
    # julsep25 (fast, representative) that our eod_flatten=True override
    # (used everywhere else in this script) closes.
    bug1_sample_days = list_days(source="tx_1m_julsep_holdout_cache.json")[:15]
    bug1_diag = bug1_leak_diagnostic(
        bug1_sample_days, {d: "tx_1m_julsep_holdout_cache.json" for d in bug1_sample_days}, recipe_base, vix
    )
    print(f"\n=== BUG-1 session-end leak diagnostic (julsep25 first {bug1_diag['n_days']} days, "
          f"baseline recipe) ===")
    print(f"  eod_flatten=True (used in this script): net={bug1_diag['net_honest_eod_flatten_true']}")
    print(f"  eod_flatten=False (raw PAPER_RECIPE default): net={bug1_diag['net_raw_eod_flatten_false']} "
          f"leaks={bug1_diag['leaks_raw']}/{bug1_diag['n_days']}")
    print(f"  leak closed by this script's override: {bug1_diag['leak_pt']}pt")

    w83_is = run_window("w83_IS_22d", W83_IS_DAYS, W83_IS_SOURCE, recipe_base, vix)
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    w83_oos = run_window("w83_OOS_66d", oos_days, {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}, recipe_base, vix)
    w83_pooled = pool_windows([w83_is, w83_oos], "w83 pooled (IS+OOS, NOT independent -- PAPER_RECIPE cells were partly tuned on this window historically)")

    # BUG-2: perturbation test on the SAME nq_1h series feeding nq_side_conf
    # via nq_on_1m_causal() -- confirms no look-ahead in the underlying
    # continuous NQ gate infrastructure this mechanism depends on.
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    bug2_rows = bug2_perturbation_test(nq_1h)
    print("\n=== BUG-2 forming-bar perturbation test (shared NQ 1h infra) ===")
    for r in bug2_rows:
        print(json.dumps(r, ensure_ascii=False))
    bug2_pass = all(
        r.get("fixed_is_immune_to_forming_bar_anomaly", True)
        for r in bug2_rows if r.get("phase") == "forming"
    )

    out = dict(
        title="ISOLATED nq_conf_exit_enable test (symmetric / against_only) vs current live baseline",
        exit_modes_tested=EXIT_MODES,
        holdout_windows=holdout_results,
        pooled_independent_oos=pooled_holdout,
        w83_is=w83_is,
        w83_oos=w83_oos,
        w83_pooled=w83_pooled,
        bug2_perturbation_test=dict(rows=bug2_rows, all_forming_bar_queries_immune=bug2_pass),
        bug1_diagnostic=bug1_diag,
        bug1_note="PAPER_RECIPE ships eod_flatten=False (correct for live incremental polling, "
                  "wrong for this script's single-complete-day arrays); every run_day() call in "
                  "this script forces eod_flatten=True instead (see run_day docstring/comment) so "
                  "every day's still-open lots are honestly force-closed into `trades`, not "
                  "silently dropped. bug1_diagnostic sizes the leak this override closes. "
                  "open_pos_leaks fields elsewhere in this JSON should all be 0 as a result.",
    )
    out_path = f"{OUT_DIR}/tmf_nq_conf_exit_isolated_test_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")
    print(f"BUG-2 all forming-bar queries immune: {bug2_pass}")
    print("BUG-1 open_pos_leaks (should all be 0): " + ", ".join(
        f"{k}:base={v['baseline']['open_pos_leaks']}" for k, v in holdout_results.items()
    ))


if __name__ == "__main__":
    main()
