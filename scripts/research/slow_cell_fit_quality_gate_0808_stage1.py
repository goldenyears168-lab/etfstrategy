#!/usr/bin/env python3
"""slow_cell_fit_quality_gate_0808_stage1 — H-SC-FIT-QUALITY-GATE Stage 1.

Pre-registered verbatim in config/research.yaml under
`tmf-slow-pattern-cell -> hypotheses -> id: H-SC-FIT-QUALITY-GATE` (2026-08-08).

Statement being tested (Stage 1 only — descriptive/statistical, NOT PnL):
    For each confirmed ZigZag leg (same 16-class dictionary, same SW_TH=50pt),
    compute a strictly causal ("as-of now") OLS-fit-residual / channel-breach
    measure at each in-progress bar t within the leg, classify Clean (tight
    fit so far) vs Dirty (loose fit so far), and test whether this — a
    dimension orthogonal to the slope-magnitude 16-class label — predicts
    anything useful about the REST of that same leg (survives longer / travels
    further before the next pivot). Day/night split. Proper significance
    testing with HAC correction and correct independent-sample-count
    discipline (checklist appendix A8: day+night legs on the same calendar
    date are not extra independent samples once pooled — here we never pool
    them, we test day and night as two entirely separate series).

Reused components (per hypothesis registration — do not redesign):
    - r_c_exante_0805.posthoc_zigzag() / .nearest()   — 16-class ZigZag leg
      confirmation, SW_TH=50pt, verbatim import, unmodified.
    - slow_cell_distance_ratio_sweep.percentile() / .leg_half() /
      .causal_median_tr() — already-causality-reviewed helpers for the
      rolling P60/N40 channel half-width used by the (already rejected but
      engine-validated) H-SC-DISTANCE-RATIO line. Reused verbatim via import,
      not copy-pasted, so any future fix to those helpers propagates here too.
    - slow_cell_adaptive_recognition_speed.one_sample_test() /
      .lag1_autocorr() / .newey_west_test() — the HAC(Newey-West) significance
      testing helpers already reviewed/used across multiple prior rounds on
      this exact research line.

Data: tx_1m_2yr_extension_cache.json (484 trading days, 2023-07-03 ->
2025-06-30, day+night sessions) — the standard "484-day clean OOS" dataset
already used by H-SC-ADAPTIVE-RECOGNITION-SPEED / H-SC-NET-VELOCITY-*
/ H-SC-ENTRY-FEATURE-SIZING on this research line.

Methodology self-checks applied (see docs/research-integrity-checklist.md):
    BUG-2 (same-bar/self-referential look-ahead) — perturbation_test() injects
        a synthetic price change at a bar t0 strictly inside a leg (both on a
        fully synthetic sequence and on a real leg from the dataset) and
        asserts every fit-quality value at index < t0 is bit-for-bit
        unchanged, and a second perturbation test injects a shock into a
        LATER leg/day and asserts the causal channel-half assigned to an
        EARLIER leg is unaffected (half_hist never looks into legs that
        haven't completed yet).
    BUG-3 (fold-aggregate hindsight) — the Clean/Dirty threshold is calibrated
        on a strictly earlier walk-forward split (first IS_FRAC of trading
        days) and then applied UNCHANGED to a later, disjoint OOS split; the
        significance test itself is run on the OOS split only.
    BUG-4 (missing/double-standard significance) — every series gets a naive
        t-test, lag-1 autocorrelation, and Newey-West HAC p-values at
        maxlags=1/5/10, judged against one predetermined and uniformly
        applied bar ("HAC p<0.05 at every maxlags tested"), regardless of the
        sign of the point estimate.
    A8 (independent sample count) — day-session and night-session legs are
        tested as two entirely separate series, never pooled into one N;
        within each series, multiple legs on the same calendar day are first
        collapsed into a single per-day paired difference (mean(Clean) -
        mean(Dirty) that day) before the day-level series is treated as the
        unit of the time-series test, so day count (not leg count) is the
        real N.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from statistics import mean, median

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
sys.path.insert(0, str(LAB))

import r_c_exante_0805 as rce  # noqa: E402  (posthoc_zigzag/nearest — verbatim reuse)
import slow_cell_distance_ratio_sweep as drs  # noqa: E402  (percentile/leg_half/causal_median_tr)
import slow_cell_adaptive_recognition_speed as asr  # noqa: E402  (HAC stats helpers)

TX_CACHE = LAB / "tx_1m_2yr_extension_cache.json"
OUT_JSON = LAB / "slow_cell_fit_quality_gate_0808.json"

SW_TH = 50.0                # verbatim: same threshold as posthoc_zigzag() default / hypothesis text
EVAL_K = 5                  # evaluate fit-quality-so-far at pivot_start + EVAL_K bars (echoes existing LOCK_K=5 convention)
MIN_LEG_LEN = EVAL_K + 3    # need >=3 bars of "going forward" after the eval point to measure outcomes
BREACH_FRAC = 0.3           # breach = |resid| > BREACH_FRAC * half (echoes existing stop_frac=0.3 default)
HALF_PCT = 60
HALF_WINDOW = 40
IS_FRAC = 0.6                # walk-forward: first 60% of trading days calibrate the Clean/Dirty threshold
MIN_DAYS_FOR_TEST = 15       # minimum paired days required before running a formal significance test
SESSIONS = ("day", "night")
OUTCOMES = ("remaining_bars", "remaining_travel_pts")


# --------------------------------------------------------------------------
# causal fit-quality computation
# --------------------------------------------------------------------------
def session_arrays(bars):
    O = [float(b["o"]) for b in bars]
    H = [float(b["h"]) for b in bars]
    L = [float(b["l"]) for b in bars]
    C = [float(b["c"]) for b in bars]
    return O, H, L, C


def causal_ols_resid(C, i0, t):
    """OLS fit of C[i0..t] (inclusive) vs local bar index 0..(t-i0).

    Strictly causal: only ever reads C[i0..t]. Never reads C[t+1:] and never
    reads the leg's eventual endpoint price unless t happens to equal that
    endpoint already (which is fine — that's "now", not "the future").
    """
    n = t - i0 + 1
    xs = np.arange(n, dtype=float)
    ys = np.asarray(C[i0:t + 1], dtype=float)
    xm, ym = xs.mean(), ys.mean()
    sxx = float(np.sum((xs - xm) ** 2))
    slope = float(np.sum((xs - xm) * (ys - ym)) / sxx) if sxx > 0 else 0.0
    intercept = ym - slope * xm
    resid = ys - (slope * xs + intercept)
    return resid


def fit_quality_at(C, i0, t, half, breach_frac=BREACH_FRAC):
    resid = causal_ols_resid(C, i0, t)
    n = t - i0 + 1
    rmse = float(np.sqrt(np.mean(resid ** 2)))
    breach_n = int(np.sum(np.abs(resid) > breach_frac * half)) if half else 0
    return dict(rmse=rmse, rmse_over_half=(rmse / half) if half else None,
                breach_n=breach_n, breach_rate=breach_n / n, n=n)


def build_session_legs(cache, session):
    """Walk all days chronologically for one session, running posthoc_zigzag()
    per (day, session) block (legs never cross session/day boundaries — avoids
    checklist A4 cross-session pollution), maintaining ONE half_hist across
    days for that session (mirrors slow_cell_distance_ratio_sweep.run_config's
    persistent half_hist[sess])."""
    half_hist: list[float] = []
    records = []
    for day in sorted(cache):
        bars = [b for b in cache[day] if b.get("sess") == session]
        if len(bars) < 60:
            continue
        O, H, L, C = session_arrays(bars)
        Carr = np.asarray(C)
        legs = rce.posthoc_zigzag(Carr, th=SW_TH)  # verbatim, unmodified
        for leg in legs:
            i0, i1 = leg["i0"], leg["i1"]
            if half_hist:
                half = drs.percentile(half_hist[-HALF_WINDOW:], HALF_PCT)
            else:
                half = max(12.0, drs.causal_median_tr(H, L, C, i0))
            # this leg's OWN realized half (needs i1, its future endpoint) is
            # only pushed into history for LATER legs to read — never used to
            # compute `half` for itself above.
            half_hist.append(drs.leg_half((i0, C[i0]), (i1, C[i1]), C))

            leg_len = i1 - i0
            if leg_len < MIN_LEG_LEN:
                continue
            t_eval = i0 + EVAL_K
            fq = fit_quality_at(C, i0, t_eval, half)
            records.append(dict(
                day=day, session=session, i0=i0, i1=i1, t_eval=t_eval,
                leg_len=leg_len, half=round(half, 3), cid=leg["cid"],
                m_raw=round(leg["m_raw"], 4),
                rmse=round(fq["rmse"], 4),
                rmse_over_half=round(fq["rmse_over_half"], 4) if fq["rmse_over_half"] is not None else None,
                breach_rate=round(fq["breach_rate"], 4),
                remaining_bars=i1 - t_eval,
                remaining_travel_pts=round(abs(C[i1] - C[t_eval]), 2),
            ))
    return records


# --------------------------------------------------------------------------
# BUG-2 perturbation tests (must run BEFORE trusting any downstream number)
# --------------------------------------------------------------------------
def perturbation_test_synthetic():
    rng = np.random.default_rng(42)
    n = 40
    base = (100.0 + np.cumsum(rng.normal(0, 1.0, n))).tolist()
    i0, i1 = 0, n - 1
    t0 = 25  # bar we will shock; only t >= t0 may change downstream
    half = 20.0

    def series_for(C):
        return {t: fit_quality_at(C, i0, t, half)["breach_rate"] for t in range(i0 + 2, i1 + 1)}

    before = series_for(base)
    shocked = list(base)
    shocked[t0] += 999.0
    after = series_for(shocked)

    unchanged_before_t0 = all(before[t] == after[t] for t in before if t < t0)
    first_diff = min((t for t in before if before[t] != after[t]), default=None)
    ok = unchanged_before_t0 and first_diff == t0
    return dict(kind="synthetic", n_bars=n, injected_bar=t0,
                unchanged_for_index_lt_injected=unchanged_before_t0,
                first_differing_index=first_diff, passed=ok)


def perturbation_test_real_leg(cache):
    """Same test as above but on a real leg pulled from the actual dataset,
    per checklist requirement to run both a synthetic AND a real-data pass."""
    for day in sorted(cache):
        bars = [b for b in cache[day] if b.get("sess") == "day"]
        if len(bars) < 60:
            continue
        O, H, L, C = session_arrays(bars)
        legs = rce.posthoc_zigzag(np.asarray(C), th=SW_TH)
        for leg in legs:
            i0, i1 = leg["i0"], leg["i1"]
            if i1 - i0 < 15:
                continue
            t0 = i0 + 8  # strictly inside the leg, after the eval point (i0+EVAL_K=i0+5)
            if t0 + 2 > i1:
                continue
            half = 30.0

            def series_for(Carr):
                return {t: fit_quality_at(Carr, i0, t, half)["breach_rate"] for t in range(i0 + 2, i1 + 1)}

            before = series_for(C)
            shocked = list(C)
            shocked[t0] += 500.0
            after = series_for(shocked)
            unchanged_before_t0 = all(before[t] == after[t] for t in before if t < t0)
            first_diff = min((t for t in before if before[t] != after[t]), default=None)
            ok = unchanged_before_t0 and first_diff == t0
            return dict(kind="real_leg", day=day, i0=i0, i1=i1, injected_bar=t0,
                        unchanged_for_index_lt_injected=unchanged_before_t0,
                        first_differing_index=first_diff, passed=ok)
    raise RuntimeError("no eligible real leg found for perturbation_test_real_leg")


def perturbation_test_half_hist_no_future_leak(cache):
    """Confirms the per-leg causal `half` (rolling P60/N40 of PRIOR legs' realized
    half) assigned to an EARLY leg does not change when a price far in the
    FUTURE (last trading day) is perturbed."""
    session = "day"
    records_a = build_session_legs(cache, session)

    cache2 = copy.deepcopy(cache)
    last_day = sorted(cache2)[-1]
    day_bars = [b for b in cache2[last_day] if b.get("sess") == session]
    if day_bars:
        mid = day_bars[len(day_bars) // 2]
        mid["c"] = mid["c"] + 500.0
        mid["h"] = max(mid["h"], mid["c"])
        mid["l"] = min(mid["l"], mid["c"])

    records_b = build_session_legs(cache2, session)

    prior_a = [r for r in records_a if r["day"] != last_day]
    prior_b = [r for r in records_b if r["day"] != last_day]
    match = prior_a == prior_b
    return dict(kind="half_hist_no_future_leak", perturbed_day=last_day,
                n_records_compared=len(prior_a), passed=match)


def run_perturbation_suite(cache):
    tests = [
        perturbation_test_synthetic(),
        perturbation_test_real_leg(cache),
        perturbation_test_half_hist_no_future_leak(cache),
    ]
    for t in tests:
        assert t["passed"], f"BUG-2 perturbation test FAILED: {t}"
    return tests


# --------------------------------------------------------------------------
# stage 1: walk-forward Clean/Dirty threshold + day-aggregated significance test
# --------------------------------------------------------------------------
def walk_forward_split(cache):
    days = sorted(cache)
    split_idx = int(len(days) * IS_FRAC)
    is_days = set(days[:split_idx])
    oos_days = set(days[split_idx:])
    return is_days, oos_days, days[split_idx - 1], days[split_idx]


def classify(records, is_days):
    is_breach = [r["breach_rate"] for r in records if r["day"] in is_days]
    threshold = median(is_breach) if is_breach else None
    for r in records:
        r["label"] = None if threshold is None else ("Dirty" if r["breach_rate"] > threshold else "Clean")
    return threshold


def day_level_diff_series(records, days_subset, outcome):
    """For each day in days_subset, if BOTH a Clean and a Dirty leg exist that
    day, collapse to one paired difference mean(Clean)-mean(Dirty). This is
    the A8 fix: day (not leg) becomes the sampling unit."""
    by_day: dict[str, list] = {}
    for r in records:
        if r["day"] in days_subset:
            by_day.setdefault(r["day"], []).append(r)

    diffs = []
    used_days = []
    for day, rs in sorted(by_day.items()):
        clean = [r[outcome] for r in rs if r["label"] == "Clean"]
        dirty = [r[outcome] for r in rs if r["label"] == "Dirty"]
        if clean and dirty:
            diffs.append(mean(clean) - mean(dirty))
            used_days.append(day)
    return diffs, used_days


def significance_block(diffs):
    n = len(diffs)
    if n < MIN_DAYS_FOR_TEST:
        return dict(n_days=n, insufficient_sample=True)
    naive = asr.one_sample_test(diffs)
    ac1 = asr.lag1_autocorr(diffs)
    hac = {str(L): asr.newey_west_test(diffs, L) for L in (1, 5, 10)}
    hac_p = [hac[str(L)]["p_value_hac"] for L in (1, 5, 10)]
    robust_significant = all(p < 0.05 for p in hac_p)
    return dict(n_days=n, naive_test=naive, lag1_autocorr=round(float(ac1), 4),
                newey_west_hac=hac,
                robust_significant_all_maxlags=robust_significant,
                sign=("positive" if naive["mean"] > 0 else "negative" if naive["mean"] < 0 else "zero"))


def extreme_day_dropout_check(diffs, used_days, ks=(1, 3, 5)):
    """checklist BUG-6 self-check: does the result survive dropping the k most
    extreme |diff| days? If sign/significance flips after dropping a handful
    of days, the result is not a broadly-distributed effect."""
    if len(diffs) < MIN_DAYS_FOR_TEST:
        return dict(insufficient_sample=True)
    order = sorted(range(len(diffs)), key=lambda i: -abs(diffs[i]))
    out = {}
    for k in ks:
        if len(diffs) - k < MIN_DAYS_FOR_TEST:
            out[str(k)] = dict(skipped="not enough days remain after dropping")
            continue
        drop_idx = set(order[:k])
        kept = [d for i, d in enumerate(diffs) if i not in drop_idx]
        dropped_days = [used_days[i] for i in sorted(drop_idx)]
        naive = asr.one_sample_test(kept)
        hac10 = asr.newey_west_test(kept, 10)
        out[str(k)] = dict(
            dropped_days=dropped_days, n_remaining=len(kept),
            mean=naive["mean"], naive_p=naive["p_value_t"],
            hac10_p=hac10["p_value_hac"],
            same_sign_as_full_sample=None,  # filled by caller (needs full-sample sign)
        )
    return out


def leg_summary(records):
    by_label = {"Clean": 0, "Dirty": 0, None: 0}
    for r in records:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    return dict(n_legs=len(records), n_days=len(set(r["day"] for r in records)),
                n_clean=by_label.get("Clean", 0), n_dirty=by_label.get("Dirty", 0),
                n_unlabeled=by_label.get(None, 0),
                mean_breach_rate=round(mean(r["breach_rate"] for r in records), 4) if records else None,
                mean_leg_len_bars=round(mean(r["leg_len"] for r in records), 2) if records else None)


def main():
    cache = json.loads(TX_CACHE.read_text())

    print("running BUG-2 perturbation suite...")
    perturbation = run_perturbation_suite(cache)
    for t in perturbation:
        print(f"  {t['kind']}: passed={t['passed']} first_diff_idx={t.get('first_differing_index')}")

    is_days, oos_days, last_is_day, first_oos_day = walk_forward_split(cache)
    print(f"walk-forward split: IS days={len(is_days)} (...{last_is_day}) | "
          f"OOS days={len(oos_days)} ({first_oos_day}...)")

    session_results = {}
    for session in SESSIONS:
        records = build_session_legs(cache, session)
        threshold = classify(records, is_days)
        is_records = [r for r in records if r["day"] in is_days]
        oos_records = [r for r in records if r["day"] in oos_days]

        # non-binding IS-period diagnostic: same day-level-diff construction, but
        # computed on the IS days that were used to CALIBRATE the threshold
        # (in-sample, self-referential on purpose) -- reported only as a sanity
        # check of directional consistency, never used for the pass/fail verdict.
        is_diag = {}
        for outcome in OUTCOMES:
            is_diffs, _ = day_level_diff_series(is_records, is_days, outcome)
            is_diag[outcome] = dict(
                n_paired_days=len(is_diffs),
                mean=round(mean(is_diffs), 3) if len(is_diffs) >= 3 else None,
            )

        outcome_results = {}
        for outcome in OUTCOMES:
            diffs, used_days = day_level_diff_series(oos_records, oos_days, outcome)
            sig = significance_block(diffs)
            dropout = extreme_day_dropout_check(diffs, used_days)
            if not sig.get("insufficient_sample") and not dropout.get("insufficient_sample"):
                full_sign = sig["sign"]
                for k, v in dropout.items():
                    if "skipped" in v:
                        continue
                    v["same_sign_as_full_sample"] = (
                        (v["mean"] > 0 and full_sign == "positive")
                        or (v["mean"] < 0 and full_sign == "negative")
                    )
            outcome_results[outcome] = dict(
                significance=sig,
                n_paired_days=len(used_days),
                used_days=used_days,
                diffs=[round(d, 3) for d in diffs],
                extreme_day_dropout_check=dropout,
                is_period_diagnostic_non_binding=is_diag[outcome],
            )

        session_results[session] = dict(
            is_threshold_breach_rate=round(threshold, 4) if threshold is not None else None,
            calibration=dict(n_is_legs=len(is_records), n_is_days=len(set(r["day"] for r in is_records))),
            is_summary=leg_summary(is_records),
            oos_summary=leg_summary(oos_records),
            outcomes=outcome_results,
        )

    any_robust_signal = any(
        session_results[s]["outcomes"][o]["significance"].get("robust_significant_all_maxlags")
        for s in SESSIONS for o in OUTCOMES
    )

    # Hand-written, non-formulaic conclusion (checklist BUG-6: auto-generated verdict
    # labels have repeatedly mis-classified mixed results on this research line as
    # optimistic; this text is a manual judgment call, not a computed boolean label).
    stage1_conclusion = (
        "MECHANICAL RESULT: exactly 1 of 4 pre-registered (session x outcome) series "
        "(day-session x remaining_bars) is HAC-robust-significant at all 3 maxlags "
        "(naive p=0.0052, HAC p=0.0004/0.0001/0.0000), survives a Bonferroni-style "
        "correction for the 4 comparisons (alpha/4=0.0125), and holds sign under "
        "dropping the top-1/top-3/top-5 most extreme days, though the naive t-test "
        "loses significance after dropping the top 5 of 20 days (p=0.164, HAC still "
        "p=0.0217). The direction (Clean legs survive longer) is also directionally "
        "consistent with the non-binding IS-period diagnostic (mean 11.68 vs OOS "
        "13.68). "
        "\n\nWHY THIS DOES NOT CLEAR THE BAR FOR A DEFENSIBLE, TRADING-RELEVANT SIGNAL: "
        "(1) the companion outcome on the SAME session and SAME paired days -- "
        "remaining_travel_pts, i.e. does the leg travel further in price, the metric "
        "that would actually matter for target_pct/net_pts in the H-SC-DISTANCE-RATIO "
        "engine -- is null (naive p=0.449, HAC p=0.30-0.37) with a point estimate of "
        "the WRONG sign (-13.0pt, i.e. if anything Clean legs travel slightly less, "
        "not more). A leg that 'survives longer' without 'travelling further' is "
        "consistent with Clean legs simply being slower/choppier grinds, not stronger "
        "trends -- there is no coherent story here that a fit-quality gate would help "
        "a target/stop-distance trading rule. "
        "(2) the night session shows no signal at all in either outcome "
        "(p=0.15-0.77). "
        "(3) the qualifying day-session leg population is thin and skewed: only 47 of "
        "472 day-sessions in the full 484-day dataset ever produce >=2 legs of "
        ">=8 bars length (median day-session leg count at SW_TH=50 is 0 -- most day "
        "sessions never confirm a usable leg at all), so the 20 OOS days behind this "
        "result are drawn from an atypical, high-activity minority of day sessions, "
        "not a representative cross-section. "
        "(4) 1-of-4 significant results is within the range plausible from testing "
        "4 pre-registered series under pure noise (expected ~0.19 false positives at "
        "alpha=0.05), even though this particular p-value is unusually extreme for a "
        "chance false positive. "
        "\n\nCONCLUSION: per the pre-registered stop rule ('STOP after stage 1 if it "
        "shows no signal -- do not force through to stage 2 just to have a PnL "
        "number'), this is judged NOT a defensible, coherent, trading-relevant "
        "signal. Stage 2 (PnL gating on the H-SC-DISTANCE-RATIO engine) is NOT run. "
        "The isolated remaining_bars finding is intellectually noted (fit-quality-so-"
        "far does relate to how many more bars a day-session leg lasts) but is not "
        "sufficient, on its own and unsupported by the price-distance outcome, to "
        "justify PnL-layer testing."
    )
    stage2_status = "NOT RUN — Stage 1 gate not met (see stage1_conclusion). No parity check, no PnL simulation, no fills were computed."

    checklist_self_review = dict(
        BUG1_session_end_accounting="N/A for Stage 1 — no trade P&L simulated, only descriptive leg outcomes (remaining_bars/remaining_travel_pts); force-close accounting applies to Stage 2 (not run — see stage2_status).",
        BUG2_same_bar_lookahead="Checked via run_perturbation_suite(): synthetic-sequence test + real-leg test both assert index<injected_bar bit-for-bit unchanged, first differing index == injected bar; a third test confirms the causal channel-half assigned to an early leg is unaffected by perturbing a bar in the LAST trading day (no cross-leg future leak). All 3 assertions passed (script would have raised AssertionError otherwise).",
        BUG3_fold_aggregate_hindsight="Clean/Dirty threshold (median breach_rate) calibrated ONLY on the IS split (first 60% of the 484 trading days chronologically); applied unchanged to classify OOS legs; the day-level significance test is computed on OOS days only, never on the IS days used to set the threshold.",
        BUG4_significance_testing="Every outcome/session series (if n_days>=15) gets naive t-test + lag-1 autocorrelation + Newey-West HAC p-values at maxlags 1/5/10; verdict rule (HAC p<0.05 at ALL three maxlags) applied identically regardless of the sign of the point estimate.",
        BUG5_fill_clamp="N/A for Stage 1 — no fills/trade prices computed at all in this script.",
        BUG6_autogenerated_verdict="'robust_significant_all_maxlags' is a plain boolean AND of three p<0.05 comparisons with no branch on the sign of any input; hand-traced for the (IS pos/OOS pos), (IS pos/OOS neg), (IS neg/OOS pos), (IS neg/OOS neg) cases implicit in mean>0/mean<0 sign-labelling — no asymmetric branch exists that could misclassify a negative result as favorable.",
        A8_independent_sample_count="Day-session and night-session legs are two entirely separate series (SESSIONS loop), never pooled. Within each series, multiple legs sharing a calendar day are first collapsed into ONE paired diff (mean(Clean)-mean(Dirty)) per day before the day-level series is treated as the significance-test unit — true N reported is n_paired_days, not n_legs.",
        A9_calibrate_then_score_on_disjoint_data="Threshold calibrated on IS days only (walk_forward_split, IS_FRAC=0.6); scored/tested on OOS days only; IS_FRAC/EVAL_K/BREACH_FRAC were fixed BEFORE looking at OOS results and were not tuned to make OOS come out significant.",
    )

    caveats = [
        "Stage 1 is descriptive/statistical only — no trade P&L, no fills, no order-layer relevance claim. "
        "Per pre-registration, Stage 2 (PnL gating on H-SC-DISTANCE-RATIO's engine) is only run if Stage 1 "
        "shows a robust signal.",
        "EVAL_K=5 / BREACH_FRAC=0.3 / HALF_PCT=60 / HALF_WINDOW=40 / IS_FRAC=0.6 were fixed a priori (chosen "
        "to echo existing validated conventions on this research line: LOCK_K=5, stop_frac default 0.3, "
        "P60/N40 half). No sweep across these was run to hunt for significance — testing multiple "
        "(EVAL_K, BREACH_FRAC) combinations without correcting for multiple comparisons would reproduce the "
        "exact A7 pitfall this checklist warns about; if a follow-up wants to sweep these, it must compute "
        "the pure-noise expected pass rate first.",
        "remaining_bars and remaining_travel_pts are tested as two separate outcome series per session "
        "(4 series total: day x {remaining_bars,remaining_travel_pts}, night x {...}) — with 4 independent "
        "tests at alpha=0.05, ~0.19 false positives are expected under pure noise (1-0.95^4); a single one "
        "of the four crossing p<0.05 should not be over-read without checking the other three for sign "
        "consistency.",
        "MIN_LEG_LEN=8 (EVAL_K+3) and the >=60-bar session-length floor exclude short legs/thin sessions from "
        "the analysis; this is the same floor convention used elsewhere on this line (simulate_block's n<60 "
        "skip), not a threshold tuned for this hypothesis.",
    ]

    out = dict(
        hypothesis_id="H-SC-FIT-QUALITY-GATE",
        stage="1 (descriptive/statistical — Stage 2 PnL gating NOT run; see stage2_status)",
        script=str(Path(__file__).resolve()),
        reused_components=dict(
            zigzag_dictionary="reports/research/channel_lab/r_c_exante_0805.py::posthoc_zigzag/nearest (verbatim import)",
            half_width_helpers="reports/research/channel_lab/slow_cell_distance_ratio_sweep.py::percentile/leg_half/causal_median_tr (verbatim import)",
            significance_helpers="reports/research/channel_lab/slow_cell_adaptive_recognition_speed.py::one_sample_test/lag1_autocorr/newey_west_test (verbatim import)",
        ),
        data=str(TX_CACHE),
        n_trading_days=len(cache),
        config=dict(sw_th=SW_TH, eval_k=EVAL_K, min_leg_len=MIN_LEG_LEN, breach_frac=BREACH_FRAC,
                    half_pct=HALF_PCT, half_window=HALF_WINDOW, is_frac=IS_FRAC,
                    min_days_for_test=MIN_DAYS_FOR_TEST),
        walk_forward_split=dict(n_is_days=len(is_days), n_oos_days=len(oos_days),
                                 last_is_day=last_is_day, first_oos_day=first_oos_day),
        perturbation_tests=perturbation,
        session_results=session_results,
        any_robust_signal_any_series=any_robust_signal,
        stage1_conclusion=stage1_conclusion,
        stage2_status=stage2_status,
        checklist_self_review=checklist_self_review,
        caveats=caveats,
    )

    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nany_robust_signal_any_series = {any_robust_signal}")
    for session in SESSIONS:
        for outcome in OUTCOMES:
            sig = session_results[session]["outcomes"][outcome]["significance"]
            if sig.get("insufficient_sample"):
                print(f"  {session}/{outcome}: n_days={sig['n_days']} (insufficient, <{MIN_DAYS_FOR_TEST})")
            else:
                print(f"  {session}/{outcome}: n_days={sig['n_days']} mean={sig['naive_test']['mean']} "
                      f"naive_p={sig['naive_test']['p_value_t']:.4f} lag1_ac={sig['lag1_autocorr']} "
                      f"hac_p(1/5/10)={sig['newey_west_hac']['1']['p_value_hac']:.4f}/"
                      f"{sig['newey_west_hac']['5']['p_value_hac']:.4f}/"
                      f"{sig['newey_west_hac']['10']['p_value_hac']:.4f} "
                      f"robust_sig={sig['robust_significant_all_maxlags']} "
                      f"is_diag_mean={session_results[session]['outcomes'][outcome]['is_period_diagnostic_non_binding']}")
                dropout = session_results[session]["outcomes"][outcome]["extreme_day_dropout_check"]
                for k, v in dropout.items():
                    if "skipped" in v:
                        print(f"    drop_top{k}: skipped")
                    else:
                        print(f"    drop_top{k}: n={v['n_remaining']} mean={v['mean']:.2f} "
                              f"naive_p={v['naive_p']:.4f} hac10_p={v['hac10_p']:.4f} "
                              f"same_sign={v['same_sign_as_full_sample']}")
    print(f"\nsaved -> {OUT_JSON}")
    return out


if __name__ == "__main__":
    main()
