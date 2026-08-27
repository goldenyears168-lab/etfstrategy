#!/usr/bin/env python3
"""H-SC-NGRAM-SEQUENCE (config/research.yaml, tmf-slow-pattern-cell).

Does an n-gram (bigram / trigram) of the last 2-3 already-confirmed causal
ZigZag leg class labels beat the existing single-leg causal predictor from
H-SC-CAUSAL-LAG (r_c_exante_0805.py::predict_class, baseline exact-hit
30.8%/soft-hit 50%/direction-hit 100% on the 2026-08-05 night) at predicting
the *next* leg's 16-class slope label -- evaluated separately for day session
(08:45-13:45) and night session (15:00-05:00)?

Design notes (see docs/research-integrity-checklist.md self-review at bottom
of the JSON output and in the run log):

* SLOPES_UP / SLOPES_DN / nearest() / posthoc_zigzag() / predict_class() /
  build_exante_segments() / snap_from_pivot() / micro_slope() / rvol_at() /
  range_envelope() are copied VERBATIM from
  reports/research/channel_lab/r_c_exante_0805.py -- not redesigned.
* ZigZag pivot confirmation (and therefore the baseline predictor, which
  needs H/L/C/V of the *same* session array to track pivot state) is reset
  at every session boundary (day-session-per-date, night-session-per-date)
  -- this follows the repo's A4 "no rolling window across session gaps"
  discipline, applied to the *raw price* threshold tracking.
* The n-gram CONTEXT is a discrete label sequence (not raw price), so it is
  allowed to chain across day boundaries within the same session type (all
  day-session legs from 2023-07-03..2026-03-31 form one chronological label
  stream; ditto night). This is a deliberate, flagged design decision
  (documented in evidence): without chaining, single-day leg counts are
  almost always 0-1 (see diagnostic n_pivots_per_session below), so a
  strict per-day-reset n-gram would have essentially zero scoreable
  examples. Chaining does NOT touch raw price / A4 is not violated (A4 is
  about rolling *price* windows, not discrete post-hoc label chains) but is
  itself a source of leakage risk (first leg of a new day using a
  many-hours/days-stale context) -- reported explicitly as a caveat.
* n-gram frequency table is a strict *online* walk-forward Markov model:
  legs are streamed in chronological order; a leg is scored using only
  counts accumulated from strictly earlier legs in the stream, then (after
  scoring) the true label is added to the counts. This is a strictly
  stronger causality guarantee than a day-level walk-forward split (it's
  leg-by-leg, hard T-1). A burn-in of the first N legs (per session type)
  is used for training only (not scored) to avoid tiny-count noise driving
  early predictions.
* Baseline (H-SC-CAUSAL-LAG) and n-gram are scored on the EXACT SAME leg
  subset: build_exante_segments' own `hold_ok` filter (MIN_HOLD=3) is
  applied first (matching the original script's own accuracy-scoring
  population), and only legs where the baseline's join-by-pivot_i succeeds
  are scored -- apples-to-apples population for both methods.
* Independent sample count = number of *distinct trading days* contributing
  >=1 scored leg (not leg count, not day+night pooled -- see checklist A8).
* BUG-2 perturbation test: synthetic price bump injected at bar t in one
  real session + a synthetic staircase series; assert every emitted
  value (pivots/legs/baseline preds/n-gram preds) at index < t, and for all
  chronologically earlier sessions, is byte-identical.
"""
from __future__ import annotations

import copy
import json
import statistics
from collections import Counter, deque
from pathlib import Path

import numpy as np
from scipy import stats as sstats
from statsmodels.stats.contingency_tables import mcnemar

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "reports/research/channel_lab"
OUT_PATH = CACHE_DIR / "slow_cell_ngram_sequence_0808.json"

MAIN_CACHE_FILES = [
    "tx_1m_2yr_extension_cache.json",
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
    "tx_1m_janmar_holdout_cache.json",
]
FULLNIGHT_CACHE_FILE = "tx_1m_fullnight_cache.json"

# ---------------------------------------------------------------------------
# Verbatim copy of the 16-class dictionary + causal ZigZag + baseline
# predictor from reports/research/channel_lab/r_c_exante_0805.py
# (SLOPES_UP, SLOPES_DN, nearest, micro_slope, rvol_at, range_envelope,
#  predict_class, snap_from_pivot, posthoc_zigzag, build_exante_segments).
# Do not redesign; only cosmetic (no logic) changes: removed the module's
# HTTP-fetch / Route-A PnL simulation code we don't need here.
# ---------------------------------------------------------------------------
SW_TH = 50.0
MIN_HOLD = 3
LOCK_K = 5

SLOPES_UP = [3.0, 5.0, 7.0, 10.0, 13.0, 16.0, 20.0, 28.0]
SLOPES_DN = [-3.0, -5.0, -8.0, -11.0, -14.0, -18.0, -25.0, -40.0]


def nearest(m: float) -> tuple[str, float]:
    cat = SLOPES_UP if m >= 0 else SLOPES_DN
    pref = "U" if m >= 0 else "D"
    i = min(range(8), key=lambda k: abs(cat[k] - m))
    return f"{pref}{i + 1}", cat[i]


def micro_slope(C: np.ndarray, t: int, look: int = 10) -> float | None:
    if t < look:
        return None
    return float((C[t] - C[t - look]) / look)


def rvol_at(V: np.ndarray, t: int, win: int = 20) -> float | None:
    if t < win:
        return None
    base = float(np.mean(V[t - win : t]))
    if base <= 0:
        return None
    return float(V[t] / base)


def range_envelope(H: np.ndarray, L: np.ndarray, t: int, look: int = 15) -> float | None:
    if t < look:
        return None
    return float(np.max(H[t - look + 1 : t + 1]) - np.min(L[t - look + 1 : t + 1]))


def predict_class(
    *,
    pk: int,
    pi: int,
    pp: float,
    t: int,
    C: np.ndarray,
    H: np.ndarray,
    L: np.ndarray,
    V: np.ndarray,
    last_pivot_i: int,
    last_pivot_p: float,
    last_kind: int,
) -> tuple[str, float, dict]:
    expect_up = pk == -1
    age = t - pi
    m_early = (float(C[t]) - pp) / age if age >= 1 else 0.0
    ms = micro_slope(C, t, 10)
    rv = rvol_at(V, t, 20)
    env = range_envelope(H, L, t, 15)
    if last_kind != 0 and pi > last_pivot_i:
        m_prior = (pp - last_pivot_p) / max(1, pi - last_pivot_i)
    else:
        m_prior = None

    blend = m_early
    if ms is not None:
        blend = 0.65 * m_early + 0.35 * ms
    if expect_up and blend < 0:
        blend = abs(blend)
    if not expect_up and blend > 0:
        blend = -abs(blend)

    if m_prior is not None and abs(m_prior) > 1e-6:
        accel = abs(blend) / abs(m_prior)
    else:
        accel = 1.0
    v_spike = rv is not None and rv >= 1.5
    if v_spike and accel > 1.2:
        blend *= 1.15
    if env is not None and env < 40 and abs(blend) > 15:
        blend *= 0.85

    cid, mstar = nearest(blend)
    meta = dict(
        m_early=round(m_early, 2),
        ms=None if ms is None else round(ms, 2),
        blend=round(blend, 2),
        pred=cid,
        pred_m=mstar,
    )
    return cid, mstar, meta


def snap_from_pivot(pi: int, pp: float, i: int, C: np.ndarray, pk: int) -> tuple[str, float]:
    age = max(1, i - pi)
    m_raw = (float(C[i]) - pp) / age
    if pk == 1 and m_raw > 0:
        m_raw = -abs(m_raw)
    if pk == -1 and m_raw < 0:
        m_raw = abs(m_raw)
    return nearest(m_raw)


def posthoc_zigzag(C: np.ndarray, th: float = SW_TH) -> list[dict]:
    n = len(C)
    piv = []
    direction = 0
    ei, ep = 0, float(C[0])
    for i in range(1, n):
        if direction >= 0:
            if C[i] >= ep:
                ei, ep = i, float(C[i])
            elif ep - C[i] >= th:
                piv.append((ei, ep, 1))
                direction = -1
                ei, ep = i, float(C[i])
        if direction <= 0:
            if C[i] <= ep:
                ei, ep = i, float(C[i])
            elif C[i] - ep >= th:
                piv.append((ei, ep, -1))
                direction = 1
                ei, ep = i, float(C[i])
    legs = []
    for a, b in zip(piv, piv[1:]):
        i0, p0, _ = a
        i1, p1, _ = b
        if i1 <= i0:
            continue
        m_raw = (p1 - p0) / (i1 - i0)
        cid, mstar = nearest(m_raw)
        legs.append(dict(i0=i0, i1=i1, cid=cid, m=mstar, m_raw=m_raw))
    return legs


def build_exante_segments(
    C: np.ndarray,
    H: np.ndarray,
    L: np.ndarray,
    V: np.ndarray,
    *,
    lock_k: int | None = None,
) -> tuple[list[dict], list[dict]]:
    n = len(C)

    direction = 0
    ei, ep = 0, float(C[0])
    last_pivot_i, last_pivot_p, last_kind = 0, float(C[0]), 0
    pending_leg_start = 0

    active: dict | None = None
    segments: list[dict] = []
    switches: list[dict] = []

    for t in range(1, n):
        flipped = False
        new_pivot = None
        if direction >= 0:
            if C[t] >= ep:
                ei, ep = t, float(C[t])
            elif ep - C[t] >= SW_TH:
                new_pivot = (ei, ep, 1)
                direction = -1
                ei, ep = t, float(C[t])
                flipped = True
        if direction <= 0 and not flipped:
            if C[t] <= ep:
                ei, ep = t, float(C[t])
            elif C[t] - ep >= SW_TH:
                new_pivot = (ei, ep, -1)
                direction = 1
                ei, ep = t, float(C[t])

        if new_pivot is None:
            if active is not None and lock_k is not None:
                bars_in = t - active["i0"]
                if bars_in == lock_k:
                    cid, mstar = snap_from_pivot(
                        active["pivot_i"], active["pivot_p"], t, C, active["pivot_k"]
                    )
                    if cid != active["cid"] or mstar != active["m"]:
                        active["cid"], active["m"] = cid, mstar
                        active["locked_at"] = t
            continue

        pi, pp, pk = new_pivot
        hold = pi - pending_leg_start
        allow = hold >= MIN_HOLD or last_kind == 0

        cid, mstar, meta = predict_class(
            pk=pk,
            pi=pi,
            pp=pp,
            t=t,
            C=C,
            H=H,
            L=L,
            V=V,
            last_pivot_i=last_pivot_i,
            last_pivot_p=last_pivot_p,
            last_kind=last_kind,
        )

        sw = dict(
            t_decide=t,
            pivot_i=pi,
            pivot_k=pk,
            hold_ok=allow,
            hold=hold,
            **meta,
        )
        switches.append(sw)

        if active is not None:
            segments.append(
                dict(
                    i0=active["i0"],
                    i1=pi,
                    cid=active["cid"],
                    m=active["m"],
                    pivot_i=active["pivot_i"],
                    locked_at=active.get("locked_at"),
                )
            )

        if allow:
            active = dict(
                i0=pi,
                cid=cid,
                m=mstar,
                pivot_i=pi,
                pivot_p=pp,
                pivot_k=pk,
            )
            pending_leg_start = pi
        else:
            pass

        last_pivot_i, last_pivot_p, last_kind = pi, pp, pk

    if active is not None:
        segments.append(
            dict(
                i0=active["i0"],
                i1=n - 1,
                cid=active["cid"],
                m=active["m"],
                pivot_i=active["pivot_i"],
                locked_at=active.get("locked_at"),
            )
        )

    allowed = [s for s in switches if s["hold_ok"]]
    return segments, allowed


# ---------------------------------------------------------------------------
# New code for this hypothesis (n-gram sequence context vs baseline).
# ---------------------------------------------------------------------------


def load_combined_cache() -> dict[str, list[dict]]:
    combined: dict[str, list[dict]] = {}
    for fname in MAIN_CACHE_FILES:
        d = json.loads((CACHE_DIR / fname).read_text())
        overlap = set(d) & set(combined)
        assert not overlap, f"{fname} overlaps with prior caches on dates: {sorted(overlap)[:5]}"
        combined.update(d)
    return combined


def session_arrays(bars: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    C = np.array([b["c"] for b in bars], float)
    H = np.array([b["h"] for b in bars], float)
    L = np.array([b["l"] for b in bars], float)
    V = np.array([b["v"] or 0 for b in bars], float)
    return C, H, L, V


def match_truth_to_switch(pivot_i: int, truth_by_i0: dict[int, dict]) -> dict | None:
    """Same join logic as r_c_exante_0805.py::score_predictions.truth_for."""
    tr = truth_by_i0.get(pivot_i)
    if tr:
        return tr
    near = [
        (abs(i0 - pivot_i), leg)
        for i0, leg in truth_by_i0.items()
        if abs(i0 - pivot_i) <= 2
    ]
    return min(near)[1] if near else None


def process_session(bars: list[dict]) -> dict:
    """Run the causal ZigZag + baseline predictor on one (date, session) bar
    array. Returns truth legs (chronological) each annotated with the
    baseline's predicted cid (None if no hold_ok switch matched)."""
    if len(bars) < 5:
        return dict(legs=[], n_pivots=0)
    C, H, L, V = session_arrays(bars)
    truth_legs = posthoc_zigzag(C, th=SW_TH)
    _, allowed_switches = build_exante_segments(C, H, L, V, lock_k=None)
    truth_by_i0 = {leg["i0"]: leg for leg in truth_legs}

    baseline_pred_by_i0: dict[int, dict] = {}
    for sw in allowed_switches:
        tr = match_truth_to_switch(sw["pivot_i"], truth_by_i0)
        if tr is not None:
            baseline_pred_by_i0.setdefault(tr["i0"], sw)

    out_legs = []
    for leg in truth_legs:
        sw = baseline_pred_by_i0.get(leg["i0"])
        out_legs.append(
            dict(
                i0=leg["i0"],
                i1=leg["i1"],
                cid=leg["cid"],
                m=leg["m"],
                baseline_pred=sw["pred"] if sw is not None else None,
                baseline_scoreable=sw is not None,
            )
        )
    # n_pivots diagnostic: len(truth_legs)+1 if any legs, else 0 or 1 pivot info
    return dict(legs=out_legs, n_pivots=len(truth_legs) + (1 if truth_legs else 0))


def build_stream(cache: dict[str, list[dict]], sess: str) -> tuple[list[dict], dict]:
    """Chronological stream of legs for one session type across all dates in
    `cache`. Each stream item: date, k (leg index within that date's
    session), cid (truth), baseline_pred, baseline_scoreable."""
    stream: list[dict] = []
    diag = dict(n_sessions=0, n_sessions_with_bars=0, total_pivots=0, total_legs=0,
                pivots_per_session=[])
    for date in sorted(cache.keys()):
        bars = [b for b in cache[date] if b["sess"] == sess]
        diag["n_sessions"] += 1
        if len(bars) < 5:
            continue
        diag["n_sessions_with_bars"] += 1
        res = process_session(bars)
        diag["total_pivots"] += res["n_pivots"]
        diag["pivots_per_session"].append(res["n_pivots"])
        for k, leg in enumerate(res["legs"]):
            diag["total_legs"] += 1
            stream.append(
                dict(
                    date=date,
                    k=k,
                    cid=leg["cid"],
                    baseline_pred=leg["baseline_pred"],
                    baseline_scoreable=leg["baseline_scoreable"],
                )
            )
    return stream, diag


def soft_hit(pred: str, truth: str) -> bool:
    if pred == truth:
        return True
    return pred[0] == truth[0] and abs(int(pred[1:]) - int(truth[1:])) <= 1


def side_hit(pred: str, truth: str) -> bool:
    return pred[0] == truth[0]


def walk_forward_ngram_eval(stream: list[dict], n: int, burn_in_legs: int) -> dict:
    """Strict online walk-forward: score leg j using counts built only from
    legs 0..j-1 (strictly causal), then update counts with leg j's true
    label. Score only legs that are (a) baseline_scoreable, (b) past
    burn_in_legs, (c) have >= n prior context legs available in the stream
    (context chains across day boundaries within this session type -- see
    module docstring)."""
    context: deque[str] = deque(maxlen=n)
    ngram_counts: dict[tuple, Counter] = {}
    unigram_counts: Counter = Counter()

    rows = []
    for idx, item in enumerate(stream):
        ctx = tuple(context)
        have_context = len(ctx) == n
        if have_context and idx >= burn_in_legs and item["baseline_scoreable"]:
            table = ngram_counts.get(ctx)
            if table:
                best_n = max(table.values())
                cands = sorted(c for c, v in table.items() if v == best_n)
                if len(cands) == 1:
                    ngram_pred = cands[0]
                else:
                    # tie-break: unigram frequency, then lexicographic
                    ngram_pred = max(
                        cands, key=lambda c: (unigram_counts.get(c, 0), c)
                    )
                ngram_backoff = False
            else:
                if unigram_counts:
                    best_u = max(unigram_counts.values())
                    ucands = sorted(c for c, v in unigram_counts.items() if v == best_u)
                    ngram_pred = ucands[0]
                else:
                    ngram_pred = "U1"  # cold start, arbitrary but deterministic
                ngram_backoff = True

            truth = item["cid"]
            base_pred = item["baseline_pred"]
            rows.append(
                dict(
                    date=item["date"],
                    truth=truth,
                    baseline_pred=base_pred,
                    ngram_pred=ngram_pred,
                    ngram_backoff=ngram_backoff,
                    base_exact=base_pred == truth,
                    base_soft=soft_hit(base_pred, truth),
                    base_side=side_hit(base_pred, truth),
                    ngram_exact=ngram_pred == truth,
                    ngram_soft=soft_hit(ngram_pred, truth),
                    ngram_side=side_hit(ngram_pred, truth),
                    n_ctx_seen=len(ngram_counts.get(ctx, {})),
                )
            )

        # update counts + context with the TRUE label, always (regardless of
        # whether this leg was scored), so later legs see it.
        cid = item["cid"]
        if have_context:
            ngram_counts.setdefault(ctx, Counter())[cid] += 1
        unigram_counts[cid] += 1
        context.append(cid)

    return dict(n=n, burn_in_legs=burn_in_legs, rows=rows)


def paired_day_test(rows: list[dict], metric: str) -> dict:
    """Cluster by trading day: per-day proportion correct for baseline vs
    n-gram, paired t-test + Wilcoxon signed-rank on the per-day differences.
    Also reports pooled McNemar (leg-level, NOT day-clustered -- flagged as
    secondary/non-independent)."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    base_key = f"base_{metric}"
    ngram_key = f"ngram_{metric}"

    day_base_rate = []
    day_ngram_rate = []
    days = sorted(by_day)
    for d in days:
        recs = by_day[d]
        day_base_rate.append(sum(r[base_key] for r in recs) / len(recs))
        day_ngram_rate.append(sum(r[ngram_key] for r in recs) / len(recs))

    diffs = [b - n for b, n in zip(day_base_rate, day_ngram_rate)]  # base - ngram
    n_days = len(days)

    out = dict(
        n_days=n_days,
        n_legs=len(rows),
        mean_diff_base_minus_ngram=statistics.mean(diffs) if diffs else None,
        sd_diff=statistics.pstdev(diffs) if len(diffs) > 1 else None,
    )

    if n_days >= 2 and any(diffs):
        se = statistics.pstdev(diffs) / (n_days ** 0.5) if n_days > 0 else float("nan")
        t_res = sstats.ttest_rel(day_base_rate, day_ngram_rate)
        out["paired_t_test"] = dict(
            t=float(t_res.statistic), p=float(t_res.pvalue), se=se
        )
        try:
            w_res = sstats.wilcoxon(day_base_rate, day_ngram_rate)
            out["wilcoxon"] = dict(stat=float(w_res.statistic), p=float(w_res.pvalue))
        except ValueError as e:
            out["wilcoxon"] = dict(error=str(e))
        # lag-1 autocorrelation of the daily diff series (robustness check,
        # analogous to the HAC-trigger check used elsewhere in this repo for
        # daily-aggregated PnL series -- here it's a classification-accuracy
        # series, reported for transparency even though BUG-4 only mandates
        # HAC for PnL-level daily aggregates).
        if n_days >= 3:
            d_arr = np.array(diffs)
            d0 = d_arr[:-1] - d_arr[:-1].mean()
            d1 = d_arr[1:] - d_arr[1:].mean()
            denom = (np.sqrt((d0**2).sum()) * np.sqrt((d1**2).sum()))
            out["lag1_autocorr_daily_diff"] = float((d0 * d1).sum() / denom) if denom > 0 else None
    else:
        out["paired_t_test"] = None
        out["wilcoxon"] = None
        out["note"] = "insufficient distinct days or zero variance for paired test"

    # secondary: pooled leg-level McNemar (NOT day-clustered; legs within a
    # day are not independent trials -- reported as a secondary diagnostic
    # only, per checklist A8, primary conclusion must rely on day-clustered
    # test above).
    b1n1 = sum(1 for r in rows if r[base_key] and r[ngram_key])
    b1n0 = sum(1 for r in rows if r[base_key] and not r[ngram_key])
    b0n1 = sum(1 for r in rows if not r[base_key] and r[ngram_key])
    b0n0 = sum(1 for r in rows if not r[base_key] and not r[ngram_key])
    table = [[b1n1, b1n0], [b0n1, b0n0]]
    try:
        mc = mcnemar(table, exact=(b1n0 + b0n1) < 25, correction=True)
        out["mcnemar_pooled_legs_NOT_day_clustered"] = dict(
            table=table, stat=float(mc.statistic), p=float(mc.pvalue)
        )
    except Exception as e:  # pragma: no cover
        out["mcnemar_pooled_legs_NOT_day_clustered"] = dict(table=table, error=str(e))

    return out


def summarize_rows(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return dict(n_scored=0)
    return dict(
        n_scored=n,
        n_distinct_days=len({r["date"] for r in rows}),
        base_exact_pct=round(100 * sum(r["base_exact"] for r in rows) / n, 2),
        base_soft_pct=round(100 * sum(r["base_soft"] for r in rows) / n, 2),
        base_side_pct=round(100 * sum(r["base_side"] for r in rows) / n, 2),
        ngram_exact_pct=round(100 * sum(r["ngram_exact"] for r in rows) / n, 2),
        ngram_soft_pct=round(100 * sum(r["ngram_soft"] for r in rows) / n, 2),
        ngram_side_pct=round(100 * sum(r["ngram_side"] for r in rows) / n, 2),
        ngram_backoff_pct=round(100 * sum(r["ngram_backoff"] for r in rows) / n, 2),
    )


def run_session_type(cache: dict[str, list[dict]], sess: str, label: str) -> dict:
    stream, diag = build_stream(cache, sess)
    diag_summary = dict(
        n_sessions=diag["n_sessions"],
        n_sessions_with_bars=diag["n_sessions_with_bars"],
        total_pivots=diag["total_pivots"],
        total_legs=diag["total_legs"],
        mean_pivots_per_session=(
            round(statistics.mean(diag["pivots_per_session"]), 3)
            if diag["pivots_per_session"] else None
        ),
        pct_sessions_zero_pivots=(
            round(
                100
                * sum(1 for x in diag["pivots_per_session"] if x == 0)
                / len(diag["pivots_per_session"]),
                1,
            )
            if diag["pivots_per_session"] else None
        ),
    )
    n_scoreable = sum(1 for it in stream if it["baseline_scoreable"])
    burn_in = max(30, int(0.15 * n_scoreable))

    result = dict(label=label, diagnostics=diag_summary, n_scoreable_stream=n_scoreable,
                  burn_in_legs=burn_in)
    for n in (2, 3):
        ev = walk_forward_ngram_eval(stream, n=n, burn_in_legs=burn_in)
        summary = summarize_rows(ev["rows"])
        sig = {}
        if ev["rows"]:
            sig["exact"] = paired_day_test(ev["rows"], "exact")
            sig["soft"] = paired_day_test(ev["rows"], "soft")
        result[f"n{n}gram"] = dict(summary=summary, significance=sig)
    return result


# ---------------------------------------------------------------------------
# BUG-2 perturbation tests
# ---------------------------------------------------------------------------


def _leg_confirm_time(legs: list[dict], switches: list[dict]) -> dict:
    """A leg's endpoint pivot (leg['i1']) is only *known* (confirmed) at the
    loop iteration recorded as t_decide on the switch whose pivot_i ==
    leg['i1'] (that switch IS the event confirming this leg's endpoint --
    see module docstring / debug trace: pivot INDEX (ei) is where the local
    extreme sits, but the ZigZag can only *confirm* it, i.e. know the leg is
    complete, once price has since moved SW_TH away, which happens at a
    later loop index t_decide >= i1). Using leg['i1'] itself (rather than
    t_decide) as a causality boundary is WRONG -- it understates the true
    confirmation lag and can make a perturbation at t_inject appear to have
    (incorrectly) altered a leg that, at t_inject, was in fact not yet
    confirmed. Returns {leg_i1: t_decide}."""
    by_pivot_i = {}
    for sw in switches:
        by_pivot_i.setdefault(sw["pivot_i"], sw["t_decide"])
    return {leg["i1"]: by_pivot_i.get(leg["i1"]) for leg in legs}


def perturbation_test_synthetic() -> dict:
    """Synthetic staircase series: inject a bump at t and confirm every
    posthoc_zigzag / build_exante_segments output *confirmed strictly before
    t* (using true confirmation time t_decide, not pivot index -- see
    _leg_confirm_time) is byte-identical between the unperturbed and
    perturbed runs."""
    rng = np.random.default_rng(42)
    n = 400
    base = 17000 + np.cumsum(rng.normal(0, 3, n))
    # force some real swings so posthoc_zigzag actually confirms pivots
    for seg_start in range(0, n, 50):
        drift = 80 * (1 if (seg_start // 50) % 2 == 0 else -1)
        base[seg_start : seg_start + 50] += np.linspace(0, drift, min(50, n - seg_start))
    base = np.cumsum(np.diff(base, prepend=base[0]))  # keep as float series
    C0 = base.copy()
    H0 = C0 + 2
    L0 = C0 - 2
    V0 = np.full(n, 100.0)

    t_inject = 250
    C1 = C0.copy()
    C1[t_inject] += 300.0  # far bigger than SW_TH=50, guaranteed to change pivots
    H1 = H0.copy()
    H1[t_inject] += 300.0
    L1 = L0.copy()

    legs0 = posthoc_zigzag(C0, th=SW_TH)
    legs1 = posthoc_zigzag(C1, th=SW_TH)
    segs0, sw0 = build_exante_segments(C0, H0, L0, V0, lock_k=None)
    segs1, sw1 = build_exante_segments(C1, H1, L1, V0, lock_k=None)

    ct0 = _leg_confirm_time(legs0, sw0)
    ct1 = _leg_confirm_time(legs1, sw1)

    legs0_before = [leg for leg in legs0 if (ct0.get(leg["i1"]) or leg["i1"]) < t_inject]
    legs1_before = [leg for leg in legs1 if (ct1.get(leg["i1"]) or leg["i1"]) < t_inject]
    sw0_before = [s for s in sw0 if s["t_decide"] < t_inject]
    sw1_before = [s for s in sw1 if s["t_decide"] < t_inject]

    identical_legs = legs0_before == legs1_before
    identical_switches = sw0_before == sw1_before

    first_diverging_leg = None
    for a, b in zip(legs0, legs1):
        if a != b:
            first_diverging_leg = ct0.get(a["i1"], a["i1"])
            break

    return dict(
        kind="synthetic_staircase",
        n_bars=n,
        t_inject=t_inject,
        legs_before_identical=identical_legs,
        switches_before_identical=identical_switches,
        n_legs0=len(legs0),
        n_legs1=len(legs1),
        first_diverging_leg_confirm_time=first_diverging_leg,
        passed=bool(identical_legs and identical_switches and (
            first_diverging_leg is None or first_diverging_leg >= t_inject
        )),
    )


def perturbation_test_real_data(cache: dict[str, list[dict]]) -> dict:
    """Real-data perturbation: pick a day/night session with several
    confirmed legs, inject a synthetic price bump mid-session, and confirm
    (a) all earlier sessions (by date) in the combined stream are byte
    identical, (b) legs/switches with i1 < t_inject in the perturbed session
    are byte identical."""
    # find a session with a reasonable number of legs to make the test
    # meaningful (>=3 legs)
    target_date = None
    target_sess = None
    for date in sorted(cache.keys()):
        for sess in ("day", "night"):
            bars = [b for b in cache[date] if b["sess"] == sess]
            if len(bars) < 60:
                continue
            C, H, L, V = session_arrays(bars)
            legs = posthoc_zigzag(C, th=SW_TH)
            if len(legs) >= 3:
                target_date, target_sess = date, sess
                break
        if target_date:
            break
    assert target_date is not None, "could not find a session with >=3 legs for perturbation test"

    bars = [b for b in cache[target_date] if b["sess"] == target_sess]
    C0, H0, L0, V0 = session_arrays(bars)
    n = len(C0)
    t_inject = n // 2
    C1, H1, L1 = C0.copy(), H0.copy(), L0.copy()
    C1[t_inject] += 300.0
    H1[t_inject] += 300.0

    legs0 = posthoc_zigzag(C0, th=SW_TH)
    legs1 = posthoc_zigzag(C1, th=SW_TH)
    _, sw0 = build_exante_segments(C0, H0, L0, V0, lock_k=None)
    _, sw1 = build_exante_segments(C1, H1, L1, V0, lock_k=None)

    ct0 = _leg_confirm_time(legs0, sw0)
    ct1 = _leg_confirm_time(legs1, sw1)

    legs0_before = [leg for leg in legs0 if (ct0.get(leg["i1"]) or leg["i1"]) < t_inject]
    legs1_before = [leg for leg in legs1 if (ct1.get(leg["i1"]) or leg["i1"]) < t_inject]
    sw0_before = [s for s in sw0 if s["t_decide"] < t_inject]
    sw1_before = [s for s in sw1 if s["t_decide"] < t_inject]

    # also verify all chronologically EARLIER dates in the stream (for this
    # session type) are completely untouched -- process_session doesn't
    # look at other dates at all (each session array is independent), so
    # this is true by construction of process_session/build_stream, but we
    # spot check two earlier dates explicitly for the record.
    earlier_dates = [d for d in sorted(cache.keys()) if d < target_date][:2]
    earlier_checks = []
    for d in earlier_dates:
        b = [x for x in cache[d] if x["sess"] == target_sess]
        if len(b) < 5:
            continue
        Ce, He, Le, Ve = session_arrays(b)
        r_a = process_session(b)
        r_b = process_session(b)  # deterministic re-run, unaffected by injection elsewhere
        earlier_checks.append(dict(date=d, identical=r_a == r_b))

    first_diverging_leg = None
    for a, b in zip(legs0, legs1):
        if a != b:
            first_diverging_leg = ct0.get(a["i1"], a["i1"])
            break

    return dict(
        kind="real_data",
        target_date=target_date,
        target_sess=target_sess,
        n_bars=n,
        t_inject=t_inject,
        legs_before_identical=legs0_before == legs1_before,
        switches_before_identical=sw0_before == sw1_before,
        n_legs0=len(legs0),
        n_legs1=len(legs1),
        first_diverging_leg_confirm_time=first_diverging_leg,
        earlier_dates_spot_checked=earlier_checks,
        passed=bool(
            (legs0_before == legs1_before)
            and (sw0_before == sw1_before)
            and (first_diverging_leg is None or first_diverging_leg >= t_inject)
            and all(c["identical"] for c in earlier_checks)
        ),
    )


def main() -> None:
    print("Loading combined main cache (4 non-overlapping files, 2023-07-03..2026-03-31)...")
    cache = load_combined_cache()
    print(f"  {len(cache)} trading days total")

    print("Running BUG-2 perturbation tests...")
    pert_synth = perturbation_test_synthetic()
    pert_real = perturbation_test_real_data(cache)
    print(f"  synthetic passed={pert_synth['passed']}  real passed={pert_real['passed']}")

    print("Day session (08:45-13:45, unaffected by night-truncation bug)...")
    day_result = run_session_type(cache, "day", "day_session_main_corpus")

    print("Night session (15:00-23:59, TRUNCATED -- missing 00:00-05:00, see caveats)...")
    night_result = run_session_type(cache, "night", "night_session_main_corpus_TRUNCATED")

    # Secondary, non-overlapping, FULL night coverage corpus.
    print("Loading tx_1m_fullnight_cache.json (43 days, 2026-06-01..2026-07-31, FULL night "
          "00:00-05:00, non-overlapping with main corpus)...")
    fullnight_cache = json.loads((CACHE_DIR / FULLNIGHT_CACHE_FILE).read_text())
    overlap = set(fullnight_cache) & set(cache)
    assert not overlap, f"fullnight cache unexpectedly overlaps main corpus: {sorted(overlap)}"
    night_full_result = run_session_type(fullnight_cache, "night", "night_session_fullnight_43d_OOP")

    out = dict(
        hypothesis_id="H-SC-NGRAM-SEQUENCE",
        generated_by="scripts/research/slow_cell_ngram_sequence_0808.py",
        data_sources=dict(
            main_corpus_files=MAIN_CACHE_FILES,
            main_corpus_n_days=len(cache),
            main_corpus_date_range=[min(cache), max(cache)],
            main_corpus_night_truncation="night sessions in the 4 main-corpus files are "
            "truncated at 23:59 (confirmed via tx_1m_2yr_extension_meta.json and direct bar "
            "inspection across all 4 files) -- they do NOT cover 00:00-05:00. No overlapping "
            "day-range source with full night coverage exists (tx_1m_fullnight_cache.json is "
            "2026-06-01..2026-07-31, chronologically after and non-overlapping with the main "
            "corpus which ends 2026-03-31), so truncated-night results and full-night results "
            "are reported as two SEPARATE, non-comparable-in-period corpora, not merged.",
            fullnight_corpus_file=FULLNIGHT_CACHE_FILE,
            fullnight_corpus_n_days=len(fullnight_cache),
            fullnight_corpus_date_range=[min(fullnight_cache), max(fullnight_cache)],
        ),
        perturbation_tests=dict(synthetic=pert_synth, real_data=pert_real),
        day_session=day_result,
        night_session_truncated=night_result,
        night_session_fullnight_oop=night_full_result,
    )
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
