"""Extension radar · Phase 2b · 高點特徵機率分析。

以 peak 標籤 + 條件 lift + Wilson CI + FDR 檢定，取代粗粒度 🟢🟡🔴 邊際表。
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any, Callable, Literal

from scipy import stats

from intraday_extension_radar import (
    NO_TRADE_BEFORE,
    OPENING_SPIKE_END,
    ExtensionScanConfig,
    build_bar_snapshots,
    pick_exit_for_mode,
    scan_session,
)
from stock_db.kbar import load_kbar_day_bars

OutcomeKey = Literal[
    "is_session_peak",
    "fade_30m_ge2",
    "fade_eod_ge3",
    "post_peak_no_hh_30m",
]


@dataclass(frozen=True)
class ObsRow:
    stock_id: str
    trade_date: str
    minute: str
    ext_prev_pct: float
    ext_vwap_sigma: float
    ext_ma20_pct: float | None
    vol_z: float
    heat_score: int
    fade_from_high_pct: float
    is_climax_bar: bool
    is_session_high: bool
    below_vwap: bool
    opening_spike: bool
    is_session_peak: bool
    fade_30m_min_pct: float
    fade_eod_pct: float
    post_peak_no_hh_30m: bool


Predicate = Callable[[ObsRow], bool]


def wilson_ci(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    z = stats.norm.ppf(1 - alpha / 2)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def two_proportion_ztest(a_succ: int, a_n: int, b_succ: int, b_n: int) -> float:
    if a_n <= 0 or b_n <= 0:
        return float("nan")
    p_pool = (a_succ + b_succ) / (a_n + b_n)
    if p_pool <= 0 or p_pool >= 1:
        return float("nan")
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / a_n + 1 / b_n))
    if se <= 0:
        return float("nan")
    z = (a_succ / a_n - b_succ / b_n) / se
    return float(2 * stats.norm.sf(abs(z)))


def fisher_exact_p(a_succ: int, a_fail: int, b_succ: int, b_fail: int) -> float:
    table = [[a_succ, a_fail], [b_succ, b_fail]]
    _, p = stats.fisher_exact(table)
    return float(p)


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    adj = [1.0] * m
    prev = 1.0
    for rank, (idx, p) in enumerate(reversed(indexed), start=1):
        r = m - rank + 1
        val = min(prev, p * m / r)
        adj[idx] = val
        prev = val
    return adj


def _prior_close(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    prior = [d for d in full_dates if d < trade_date]
    if not prior:
        return None
    row = conn.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=?",
        (stock_id, prior[-1]),
    ).fetchone()
    return float(row[0]) if row else None


def _ma20(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    dates = [d for d in full_dates if d <= trade_date][-20:]
    if len(dates) < 10:
        return None
    rows = conn.execute(
        f"""
        SELECT close FROM stock_daily_bars
        WHERE stock_id=? AND trade_date IN ({",".join("?" * len(dates))})
        ORDER BY trade_date
        """,
        (stock_id, *dates),
    ).fetchall()
    if len(rows) < 10:
        return None
    return sum(float(r[0]) for r in rows) / len(rows)


def _poll_minutes(snaps: tuple, interval: int) -> set[str]:
    out: set[str] = set()
    for s in snaps:
        parts = s.minute.split(":")
        hh, mm = int(parts[0]), int(parts[1])
        total = (hh * 60 + mm) // interval * interval
        out.add(f"{total // 60:02d}:{total % 60:02d}:00")
    return out


def _opening_spike(minute: str, ext_prev_pct: float) -> bool:
    return minute >= NO_TRADE_BEFORE and minute <= f"{OPENING_SPIKE_END}:00" and ext_prev_pct >= 5.0


def collect_observations(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    poll_interval_min: int = 5,
    min_bars: int = 50,
) -> list[ObsRow]:
    conn.row_factory = sqlite3.Row
    full_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
        ).fetchall()
    ]
    sessions = conn.execute(
        """
        SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, stock_id
        """,
        (date_start, date_end),
    ).fetchall()

    rows: list[ObsRow] = []
    for row in sessions:
        sid, td = str(row["stock_id"]), str(row["trade_date"])
        prev = _prior_close(conn, sid, td, full_dates)
        if prev is None:
            continue
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < min_bars:
            continue
        ma20 = _ma20(conn, sid, td, full_dates)
        snaps = build_bar_snapshots(bars, prev_close=prev, ma20=ma20)
        if len(snaps) < 30:
            continue

        peak_idx = max(range(len(snaps)), key=lambda i: snaps[i].close)
        peak_close = snaps[peak_idx].close
        poll_set = _poll_minutes(snaps, poll_interval_min)
        last_close = snaps[-1].close

        for i, s in enumerate(snaps):
            if s.minute not in poll_set:
                continue
            future = snaps[i + 1 : i + 31]
            if not future:
                continue
            min_fwd = min(x.close for x in future)
            fade_30m = (min_fwd / s.close - 1.0) * 100.0
            fade_eod = (last_close / s.close - 1.0) * 100.0
            post_peak = i > peak_idx and all(x.close <= peak_close for x in future)
            fade_from_high = (s.close / peak_close - 1.0) * 100.0 if peak_close > 0 else 0.0
            is_peak = i == peak_idx
            rows.append(
                ObsRow(
                    stock_id=sid,
                    trade_date=td,
                    minute=s.minute,
                    ext_prev_pct=s.ext_prev_pct,
                    ext_vwap_sigma=s.ext_vwap_sigma,
                    ext_ma20_pct=s.ext_ma20_pct,
                    vol_z=s.vol_z,
                    heat_score=s.heat_score,
                    fade_from_high_pct=round(fade_from_high, 3),
                    is_climax_bar=s.is_climax_bar,
                    is_session_high=s.is_session_high,
                    below_vwap=s.below_vwap,
                    opening_spike=_opening_spike(s.minute, s.ext_prev_pct),
                    is_session_peak=is_peak,
                    fade_30m_min_pct=fade_30m,
                    fade_eod_pct=fade_eod,
                    post_peak_no_hh_30m=post_peak,
                )
            )
    return rows


def _outcome_true(obs: ObsRow, key: OutcomeKey) -> bool:
    if key == "is_session_peak":
        return obs.is_session_peak
    if key == "fade_30m_ge2":
        return obs.fade_30m_min_pct <= -2.0
    if key == "fade_eod_ge3":
        return obs.fade_eod_pct <= -3.0
    if key == "post_peak_no_hh_30m":
        return obs.post_peak_no_hh_30m
    raise ValueError(key)


PREDICATES: list[tuple[str, Predicate]] = [
    ("climax@high", lambda o: o.is_climax_bar and o.is_session_high),
    ("climax", lambda o: o.is_climax_bar),
    ("heat≥70", lambda o: o.heat_score >= 70),
    ("heat≥60", lambda o: o.heat_score >= 60),
    ("vol_z≥2", lambda o: o.vol_z >= 2.0),
    ("vol_z≥2.5", lambda o: o.vol_z >= 2.5),
    ("ext_prev≥5%", lambda o: o.ext_prev_pct >= 5.0),
    ("ext_prev≥7%", lambda o: o.ext_prev_pct >= 7.0),
    ("ext_vwap≥2σ", lambda o: o.ext_vwap_sigma >= 2.0),
    ("ext_vwap≥1.5σ", lambda o: o.ext_vwap_sigma >= 1.5),
    ("opening_spike", lambda o: o.opening_spike),
    ("below_vwap", lambda o: o.below_vwap),
    ("fade_from_high≤−1%", lambda o: o.fade_from_high_pct <= -1.0),
    ("fade_from_high≤−2%", lambda o: o.fade_from_high_pct <= -2.0),
    ("spike+climax", lambda o: o.ext_prev_pct >= 5.0 and o.is_climax_bar),
    ("spike+heat≥70", lambda o: o.ext_prev_pct >= 5.0 and o.heat_score >= 70),
    ("spike+climax@high", lambda o: o.ext_prev_pct >= 5.0 and o.is_climax_bar and o.is_session_high),
    ("climax+2σ", lambda o: o.is_climax_bar and o.ext_vwap_sigma >= 2.0),
    ("heat≥70+2σ", lambda o: o.heat_score >= 70 and o.ext_vwap_sigma >= 2.0),
    ("spike+climax+2σ", lambda o: o.ext_prev_pct >= 5.0 and o.is_climax_bar and o.ext_vwap_sigma >= 2.0),
    ("post_peak+fade≤−1%", lambda o: o.fade_from_high_pct <= -1.0 and not o.is_session_peak),
    ("post_peak+below_vwap", lambda o: o.below_vwap and o.fade_from_high_pct <= -0.5 and not o.is_session_peak),
    ("combo_b_proxy", lambda o: o.opening_spike and o.is_climax_bar and o.below_vwap and o.fade_from_high_pct <= -0.5),
]


def _subset(rows: list[ObsRow], spike_only: bool) -> list[ObsRow]:
    if not spike_only:
        return rows
    return [r for r in rows if r.ext_prev_pct >= 5.0]


def evaluate_predicates(
    rows: list[ObsRow],
    *,
    outcome: OutcomeKey,
    spike_only: bool = False,
    min_n: int = 500,
) -> list[dict[str, Any]]:
    data = _subset(rows, spike_only)
    if not data:
        return []

    base_succ = sum(1 for o in data if _outcome_true(o, outcome))
    base_n = len(data)
    base_p = base_succ / base_n if base_n else 0.0

    results: list[dict[str, Any]] = []
    for name, pred in PREDICATES:
        feat = [o for o in data if pred(o)]
        rest = [o for o in data if not pred(o)]
        n_feat = len(feat)
        if n_feat < min_n:
            continue
        n_rest = len(rest)
        s_feat = sum(1 for o in feat if _outcome_true(o, outcome))
        s_rest = sum(1 for o in rest if _outcome_true(o, outcome))
        p_feat = s_feat / n_feat
        p_rest = s_rest / n_rest if n_rest else 0.0
        lift = p_feat / base_p if base_p > 0 else float("nan")
        lo, hi = wilson_ci(s_feat, n_feat)
        p_z = two_proportion_ztest(s_feat, n_feat, s_rest, n_rest)
        p_fisher = fisher_exact_p(s_feat, n_feat - s_feat, s_rest, n_rest - s_rest)
        results.append(
            {
                "predicate": name,
                "outcome": outcome,
                "n": n_feat,
                "P": round(p_feat * 100, 2),
                "P_rest": round(p_rest * 100, 2),
                "P_base": round(base_p * 100, 2),
                "lift": round(lift, 2),
                "ci_lo": round(lo * 100, 2),
                "ci_hi": round(hi * 100, 2),
                "p_z": p_z,
                "p_fisher": p_fisher,
            }
        )

    p_vals = [r["p_fisher"] for r in results]
    q_vals = benjamini_hochberg(p_vals)
    for r, q in zip(results, q_vals):
        r["q_fdr"] = round(q, 4)
        r["significant"] = q < 0.05 and r["lift"] > 1.05
    results.sort(key=lambda x: (-x["lift"], x["q_fdr"]))
    return results


def peak_feature_distribution(
    rows: list[ObsRow],
    *,
    spike_only: bool = False,
) -> dict[str, Any]:
    data = _subset(rows, spike_only)
    peaks = [o for o in data if o.is_session_peak]
    non_peaks = [o for o in data if not o.is_session_peak]
    if not peaks or not non_peaks:
        return {}

    def _summarize(vals: list[float]) -> dict[str, float]:
        if not vals:
            return {}
        return {
            "mean": round(sum(vals) / len(vals), 2),
            "median": round(float(statistics.median(vals)), 2),
            "p75": round(float(statistics.quantiles(vals, n=4)[-1]), 2),
        }

    fields = [
        ("heat_score", lambda o: float(o.heat_score)),
        ("ext_prev_pct", lambda o: o.ext_prev_pct),
        ("vol_z", lambda o: o.vol_z),
        ("ext_vwap_sigma", lambda o: o.ext_vwap_sigma),
    ]
    out: dict[str, Any] = {"n_peak": len(peaks), "n_non_peak": len(non_peaks)}
    for fname, fn in fields:
        pv = [fn(o) for o in peaks]
        nv = [fn(o) for o in non_peaks]
        u_stat, p_mw = stats.mannwhitneyu(pv, nv, alternative="two-sided")
        out[fname] = {
            "at_peak": _summarize(pv),
            "non_peak": _summarize(nv),
            "mannwhitney_p": round(float(p_mw), 6),
            "cohens_d": round(_cohens_d(pv, nv), 3),
        }

    for label, pred in [
        ("climax@peak", lambda o: o.is_climax_bar),
        ("heat≥70@peak", lambda o: o.heat_score >= 70),
        ("vol_z≥2@peak", lambda o: o.vol_z >= 2.0),
        ("ext_prev≥5%@peak", lambda o: o.ext_prev_pct >= 5.0),
    ]:
        pp = sum(1 for o in peaks if pred(o)) / len(peaks)
        pn = sum(1 for o in non_peaks if pred(o)) / len(non_peaks)
        s_p = sum(1 for o in peaks if pred(o))
        f_p = len(peaks) - s_p
        s_n = sum(1 for o in non_peaks if pred(o))
        f_n = len(non_peaks) - s_n
        out[label] = {
            "P_at_peak": round(pp * 100, 1),
            "P_non_peak": round(pn * 100, 1),
            "lift": round(pp / pn, 2) if pn > 0 else float("nan"),
            "p_fisher": round(fisher_exact_p(s_p, f_p, s_n, f_n), 6),
        }
    return out


def _cohens_d(a: list[float], b: list[float]) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled <= 0:
        return float("nan")
    return (ma - mb) / pooled


def logistic_peak_regression(
    rows: list[ObsRow],
    *,
    spike_only: bool = True,
    max_samples: int = 80_000,
) -> dict[str, Any]:
    """Multivariate logit · outcome = is_session_peak（spike 日子集）。"""
    import random

    data = _subset(rows, spike_only)
    if len(data) < 1000:
        return {"skipped": True, "reason": "insufficient spike rows"}

    if len(data) > max_samples:
        random.seed(42)
        data = random.sample(data, max_samples)

    y = [1.0 if o.is_session_peak else 0.0 for o in data]

    def _vec(o: ObsRow) -> list[float]:
        return [
            1.0,
            o.ext_prev_pct / 10.0,
            o.ext_vwap_sigma,
            o.vol_z,
            o.heat_score / 100.0,
            float(o.is_climax_bar),
            float(o.is_session_high),
            float(o.opening_spike),
        ]

    X = [_vec(o) for o in data]
    names = [
        "intercept",
        "ext_prev_scaled",
        "ext_vwap_sigma",
        "vol_z",
        "heat_scaled",
        "climax",
        "session_high",
        "opening_spike",
    ]

    beta = [0.0] * len(names)

    def sigmoid(z: float) -> float:
        if z >= 0:
            e = math.exp(-z)
            return 1 / (1 + e)
        e = math.exp(z)
        return e / (1 + e)

    for _ in range(40):
        grad = [0.0] * len(beta)
        for xi, yi in zip(X, y):
            z = sum(b * x for b, x in zip(beta, xi))
            p = sigmoid(z)
            err = p - yi
            for j, xj in enumerate(xi):
                grad[j] += err * xj
        n = len(y)
        lr = 0.05 / math.sqrt(n / 10000)
        for j in range(len(beta)):
            beta[j] -= lr * grad[j] / n

    # Wald-style SE via Hessian diagonal approx
    coefs = []
    for j, name in enumerate(names):
        coefs.append({"feature": name, "beta": round(beta[j], 4), "odds_ratio": round(math.exp(beta[j]), 3)})

    coefs.sort(key=lambda c: abs(c["beta"]), reverse=True)
    return {
        "spike_only": spike_only,
        "n": len(data),
        "base_rate_pct": round(sum(y) / len(y) * 100, 3),
        "coefficients": coefs,
    }


def run_peak_feature_analysis(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    poll_interval_min: int = 5,
) -> dict[str, Any]:
    rows = collect_observations(
        conn,
        date_start=date_start,
        date_end=date_end,
        poll_interval_min=poll_interval_min,
    )
    n_sessions = len({(r.stock_id, r.trade_date) for r in rows})
    spike_rows = [r for r in rows if r.ext_prev_pct >= 5.0]

    outcomes: list[OutcomeKey] = [
        "is_session_peak",
        "fade_30m_ge2",
        "fade_eod_ge3",
        "post_peak_no_hh_30m",
    ]
    eval_all: dict[str, list[dict[str, Any]]] = {}
    eval_spike: dict[str, list[dict[str, Any]]] = {}
    for oc in outcomes:
        eval_all[oc] = evaluate_predicates(rows, outcome=oc, spike_only=False)
        eval_spike[oc] = evaluate_predicates(rows, outcome=oc, spike_only=True, min_n=200)

    dist_all = peak_feature_distribution(rows, spike_only=False)
    dist_spike = peak_feature_distribution(rows, spike_only=True)
    logit = logistic_peak_regression(rows, spike_only=True)

    top_peak = eval_spike.get("is_session_peak", [])[:5]
    top_fade = eval_spike.get("fade_30m_ge2", [])[:5]
    top_confirm = eval_spike.get("post_peak_no_hh_30m", [])[:5]

    sig_peak = [r for r in eval_spike.get("is_session_peak", []) if r.get("significant")]
    sig_fade = [r for r in eval_spike.get("fade_30m_ge2", []) if r.get("significant")]

    return {
        "date_start": date_start,
        "date_end": date_end,
        "poll_interval_min": poll_interval_min,
        "n_observations": len(rows),
        "n_spike_observations": len(spike_rows),
        "n_sessions": n_sessions,
        "baseline_rates": {
            oc: round(sum(1 for r in rows if _outcome_true(r, oc)) / len(rows) * 100, 3)
            for oc in outcomes
        },
        "baseline_rates_spike": {
            oc: round(sum(1 for r in spike_rows if _outcome_true(r, oc)) / len(spike_rows) * 100, 3)
            if spike_rows
            else 0.0
            for oc in outcomes
        },
        "predicate_eval_all": eval_all,
        "predicate_eval_spike": eval_spike,
        "peak_distribution_all": dist_all,
        "peak_distribution_spike": dist_spike,
        "logistic_spike_peak": logit,
        "top_signatures": {
            "peak_discrimination_spike": top_peak,
            "fade_30m_spike": top_fade,
            "peak_confirm_spike": top_confirm,
        },
        "n_significant_fdr": {
            "peak_spike": len(sig_peak),
            "fade_spike": len(sig_fade),
        },
        "recommended_signature": _recommend_signature(eval_spike, dist_spike, logit),
    }


def run_i36_combo_universality(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    min_spike_pct: float = 4.0,
    min_fade_pct: float = 1.0,
    phase2b_anchor_pct: float = 9.37,
    min_trigger_n: int = 200,
    min_bars: int = 50,
) -> dict[str, Any]:
    """H-PURE-2 · 全市 1m 標的 · I36 combo_spike 觸發後 30m fade 機率（脫離 C18acc leg）。"""
    conn.row_factory = sqlite3.Row
    full_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
        ).fetchall()
    ]
    sessions = conn.execute(
        """
        SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, stock_id
        """,
        (date_start, date_end),
    ).fetchall()

    scan_cfg = ExtensionScanConfig(
        exit_mode="combo_spike",
        poll_interval_min=1,
        min_ext_prev_pct=min_spike_pct,
        min_fade_from_high_pct=min_fade_pct,
        require_opening_spike=False,
    )

    n_trigger = 0
    n_fade = 0
    for row in sessions:
        sid, td = str(row["stock_id"]), str(row["trade_date"])
        prev = _prior_close(conn, sid, td, full_dates)
        if prev is None:
            continue
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < min_bars:
            continue
        ma20 = _ma20(conn, sid, td, full_dates)
        scan = scan_session(
            bars,
            trade_date=td,
            stock_id=sid,
            prev_close=prev,
            ma20=ma20,
            config=scan_cfg,
        )
        sig = pick_exit_for_mode(scan, "combo_spike")
        if sig is None:
            continue
        snap = next((b for b in scan.bars if b.minute == sig.minute), None)
        if snap is None or snap.ext_prev_pct < min_spike_pct:
            continue
        idx = next((i for i, b in enumerate(scan.bars) if b.minute == sig.minute), None)
        if idx is None:
            continue
        future = scan.bars[idx + 1 : idx + 31]
        if not future:
            continue
        n_trigger += 1
        min_fwd = min(b.close for b in future)
        fade_pct = (min_fwd / sig.price - 1.0) * 100.0
        if fade_pct <= -2.0:
            n_fade += 1

    if n_trigger <= 0:
        return {
            "pass": False,
            "n_trigger": 0,
            "n_fade": 0,
            "P_fade_pct": None,
            "wilson_lo_pct": None,
            "wilson_hi_pct": None,
            "phase2b_anchor_pct": phase2b_anchor_pct,
            "min_trigger_n": min_trigger_n,
            "reject_reason": "樣本不足",
        }

    p = n_fade / n_trigger
    lo, hi = wilson_ci(n_fade, n_trigger)
    pass_gate = (
        n_trigger >= min_trigger_n
        and p * 100 >= 9.0
        and lo * 100 >= 7.0
    )
    return {
        "pass": True if pass_gate else False,
        "n_trigger": int(n_trigger),
        "n_fade": int(n_fade),
        "P_fade_pct": round(p * 100, 2),
        "wilson_lo_pct": round(lo * 100, 2),
        "wilson_hi_pct": round(hi * 100, 2),
        "phase2b_anchor_pct": phase2b_anchor_pct,
        "min_trigger_n": min_trigger_n,
        "reject_reason": None if pass_gate else (
            "樣本不足" if n_trigger < min_trigger_n else "機率坍塌"
        ),
    }


def _recommend_signature(
    eval_spike: dict[str, list[dict[str, Any]]],
    dist_spike: dict[str, Any],
    logit: dict[str, Any],
) -> dict[str, Any]:
    peak_rows = eval_spike.get("is_session_peak", [])
    fade_rows = eval_spike.get("fade_30m_ge2", [])
    confirm_rows = eval_spike.get("post_peak_no_hh_30m", [])

    best_peak = next((r for r in peak_rows if r.get("significant")), peak_rows[0] if peak_rows else None)
    best_fade = next((r for r in fade_rows if r.get("significant")), fade_rows[0] if fade_rows else None)
    best_confirm = next((r for r in confirm_rows if r.get("significant")), confirm_rows[0] if confirm_rows else None)

    logit_top = (logit.get("coefficients") or [])[:3] if not logit.get("skipped") else []

    return {
        "alert_layer": {
            "predicate": best_peak["predicate"] if best_peak else None,
            "P_peak_pct": best_peak["P"] if best_peak else None,
            "lift_vs_base": best_peak["lift"] if best_peak else None,
            "q_fdr": best_peak.get("q_fdr") if best_peak else None,
            "interpretation": "尖峰當下辨識 · 對應 X4 heat / climax 預警",
        },
        "confirm_layer": {
            "predicate": best_confirm["predicate"] if best_confirm else None,
            "P_confirm_pct": best_confirm["P"] if best_confirm else None,
            "lift_vs_base": best_confirm["lift"] if best_confirm else None,
            "q_fdr": best_confirm.get("q_fdr") if best_confirm else None,
            "interpretation": "高點已過 + 30m 無新高 · 對應 X3 combo_b / VWAP loss",
        },
        "fade_layer": {
            "predicate": best_fade["predicate"] if best_fade else None,
            "P_fade_30m_pct": best_fade["P"] if best_fade else None,
            "lift_vs_base": best_fade["lift"] if best_fade else None,
            "q_fdr": best_fade.get("q_fdr") if best_fade else None,
        },
        "logit_top_features": logit_top,
        "climax_at_peak_spike": dist_spike.get("climax@peak"),
    }


def render_peak_analysis_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Extension radar · 高點特徵機率分析（Phase 2b）",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"5m poll 觀測：{payload['n_observations']} · spike 觀測：{payload['n_spike_observations']} · sessions：{payload['n_sessions']}",
        "",
        "## 研究假設（預先登記）",
        "",
        "| ID | 假設 | 檢定 |",
        "|----|------|------|",
        "| H-PEAK-1 | spike 日 `climax@high` 的 P(is_session_peak) lift ≥ 3× baseline | Fisher + FDR |",
        "| H-PEAK-2 | `post_peak+below_vwap` 的 P(post_peak_no_hh_30m) lift ≥ 2× baseline | Fisher + FDR |",
        "| H-PEAK-3 | `spike+climax+2σ` 同時成立時 P(fade_30m≥2%) > P(heat≥70) | 兩比例 z |",
        "| H-PEAK-4 | spike 日 peak bar 的 heat / vol_z 中位數 > 非 peak（Mann-Whitney p<0.01） | MWU |",
        "| H-PEAK-5 | 多變量 logit · climax + session_high 係數為正且 OR>1.5 | 梯度 logit |",
        "",
        "## Baseline 機率",
        "",
        "| Outcome | 全樣本 | spike 子樣本 |",
        "|---------|--------|--------------|",
    ]
    for oc in ("is_session_peak", "fade_30m_ge2", "fade_eod_ge3", "post_peak_no_hh_30m"):
        labels = {
            "is_session_peak": "P(當根=日高)",
            "fade_30m_ge2": "P(30m fade≥2%)",
            "fade_eod_ge3": "P(EOD fade≥3%)",
            "post_peak_no_hh_30m": "P(高點已過·30m無新高)",
        }
        lines.append(
            f"| {labels[oc]} | {payload['baseline_rates'][oc]}% | {payload['baseline_rates_spike'][oc]}% |"
        )

    rec = payload.get("recommended_signature") or {}
    lines += [
        "",
        "## 建議簽名（資料驅動）",
        "",
        f"**預警層**：`{rec.get('alert_layer', {}).get('predicate')}` · "
        f"P(peak)={rec.get('alert_layer', {}).get('P_peak_pct')}% · "
        f"lift={rec.get('alert_layer', {}).get('lift_vs_base')}× · "
        f"q={rec.get('alert_layer', {}).get('q_fdr')}",
        "",
        f"**確認層**：`{rec.get('confirm_layer', {}).get('predicate')}` · "
        f"P(confirm)={rec.get('confirm_layer', {}).get('P_confirm_pct')}% · "
        f"lift={rec.get('confirm_layer', {}).get('lift_vs_base')}× · "
        f"q={rec.get('confirm_layer', {}).get('q_fdr')}",
        "",
        f"**fade 層**：`{rec.get('fade_layer', {}).get('predicate')}` · "
        f"P(fade)={rec.get('fade_layer', {}).get('P_fade_30m_pct')}% · "
        f"lift={rec.get('fade_layer', {}).get('lift_vs_base')}×",
        "",
    ]

    def _table(title: str, rows: list[dict[str, Any]], max_rows: int = 12) -> None:
        lines.extend([f"## {title}", ""])
        if not rows:
            lines.append("_無足夠樣本_")
            lines.append("")
            return
        lines.append(
            "| 特徵 | n | P | P_rest | lift | 95% CI | p(Fisher) | q(FDR) | sig |"
        )
        lines.append("|------|---|----|--------|------|--------|-----------|--------|-----|")
        for r in rows[:max_rows]:
            sig = "✓" if r.get("significant") else ""
            lines.append(
                f"| {r['predicate']} | {r['n']} | {r['P']}% | {r['P_rest']}% | {r['lift']}× "
                f"| [{r['ci_lo']},{r['ci_hi']}] | {r['p_fisher']:.2e} | {r['q_fdr']} | {sig} |"
            )
        lines.append("")

    spike_eval = payload.get("predicate_eval_spike") or {}
    _table("spike 子樣本 · P(當根=日高)", spike_eval.get("is_session_peak", []))
    _table("spike 子樣本 · P(30m fade≥2%)", spike_eval.get("fade_30m_ge2", []))
    _table("spike 子樣本 · P(高點已過·30m無新高)", spike_eval.get("post_peak_no_hh_30m", []))

    dist = payload.get("peak_distribution_spike") or {}
    if dist:
        lines += ["## spike 日 · peak vs non-peak 分佈", ""]
        for key in ("heat_score", "ext_prev_pct", "vol_z", "ext_vwap_sigma"):
            d = dist.get(key)
            if not d:
                continue
            lines.append(
                f"- **{key}** · peak median={d['at_peak'].get('median')} "
                f"vs non-peak={d['non_peak'].get('median')} · "
                f"Cohen d={d.get('cohens_d')} · MWU p={d.get('mannwhitney_p')}"
            )
        for key in ("climax@peak", "heat≥70@peak", "vol_z≥2@peak"):
            d = dist.get(key)
            if d:
                lines.append(
                    f"- **{key}** · P(peak)={d['P_at_peak']}% vs P(non)={d['P_non_peak']}% · "
                    f"lift={d['lift']}× · p={d['p_fisher']}"
                )
        lines.append("")

    logit = payload.get("logistic_spike_peak") or {}
    if not logit.get("skipped"):
        lines += [
            "## spike 日 · 多變量 logit（outcome = is_session_peak）",
            "",
            f"base rate={logit.get('base_rate_pct')}% · n={logit.get('n')}",
            "",
            "| feature | β | OR |",
            "|---------|---|-----|",
        ]
        for c in logit.get("coefficients") or []:
            lines.append(f"| {c['feature']} | {c['beta']} | {c['odds_ratio']} |")
        lines.append("")

    lines += [
        "---",
        "模組：`scripts/tools/analyze_extension_peak_features.py`",
        "",
    ]
    return "\n".join(lines)


# ── Phase 2c · 交互 logit · 生存分析 · OOS · 回測對照 ─────────────────────


def _minute_index(minute: str) -> int:
    parts = minute.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _is_alert_climax_2sigma(o: ObsRow) -> bool:
    return (
        o.ext_prev_pct >= 5.0
        and o.is_climax_bar
        and o.ext_vwap_sigma >= 2.0
    )


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _logit_feature_matrix(o: ObsRow) -> tuple[list[float], list[str]]:
    climax = float(o.is_climax_bar)
    sigma2 = float(o.ext_vwap_sigma >= 2.0)
    opening = float(o.opening_spike)
    names = [
        "intercept",
        "ext_prev_scaled",
        "vol_z",
        "climax",
        "sigma2",
        "opening",
        "climax×sigma2",
        "climax×opening",
        "sigma2×opening",
        "climax×sigma2×opening",
    ]
    x = [
        1.0,
        o.ext_prev_pct / 10.0,
        o.vol_z,
        climax,
        sigma2,
        opening,
        climax * sigma2,
        climax * opening,
        sigma2 * opening,
        climax * sigma2 * opening,
    ]
    return x, names


def interaction_logit_regression(
    rows: list[ObsRow],
    *,
    spike_only: bool = True,
    max_samples: int = 60_000,
) -> dict[str, Any]:
    """Newton-Raphson logit · outcome = is_session_peak · 含交互項。"""
    import random

    data = _subset(rows, spike_only)
    if len(data) < 500:
        return {"skipped": True, "reason": "insufficient rows"}

    if len(data) > max_samples:
        random.seed(42)
        data = random.sample(data, max_samples)

    xs: list[list[float]] = []
    ys: list[float] = []
    names: list[str] = []
    for o in data:
        x, names = _logit_feature_matrix(o)
        xs.append(x)
        ys.append(1.0 if o.is_session_peak else 0.0)

    k = len(names)
    beta = [0.0] * k
    n = len(ys)

    for _ in range(80):
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        for x, y in zip(xs, ys):
            z = sum(b * xi for b, xi in zip(beta, x))
            p = _sigmoid(z)
            w = p * (1.0 - p)
            err = p - y
            for i in range(k):
                grad[i] += err * x[i]
                for j in range(k):
                    hess[i][j] += w * x[i] * x[j]
        try:
            delta = _solve_linear(hess, [-g for g in grad])
        except ValueError:
            break
        step = 0.5
        for _try in range(8):
            trial = [b + step * d for b, d in zip(beta, delta)]
            if all(math.isfinite(t) for t in trial):
                beta = trial
                break
            step *= 0.5

    coefs: list[dict[str, Any]] = []
    for j, name in enumerate(names):
        se = _hessian_diag_se(hess, j, n)
        z = beta[j] / se if se > 1e-9 else float("nan")
        p = float(2 * stats.norm.sf(abs(z))) if math.isfinite(z) else float("nan")
        coefs.append(
            {
                "feature": name,
                "beta": round(beta[j], 4),
                "se": round(se, 4) if math.isfinite(se) else None,
                "odds_ratio": round(math.exp(beta[j]), 3),
                "p_value": round(p, 6) if math.isfinite(p) else None,
            }
        )

    coefs.sort(key=lambda c: abs(c["beta"]), reverse=True)
    sig = [c for c in coefs if c.get("p_value") is not None and c["p_value"] < 0.05 and c["feature"] != "intercept"]
    return {
        "spike_only": spike_only,
        "n": n,
        "base_rate_pct": round(sum(ys) / n * 100, 3),
        "coefficients": coefs,
        "n_significant": len(sig),
        "top_interaction": next(
            (c for c in coefs if "×" in c["feature"] and c.get("p_value", 1) < 0.05),
            None,
        ),
    }


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular")
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= div
        for row in range(n):
            if row == col:
                continue
            factor = m[row][col]
            for j in range(col, n + 1):
                m[row][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def _hessian_diag_se(hess: list[list[float]], j: int, n: int) -> float:
    try:
        inv = _invert_matrix(hess)
        return math.sqrt(max(inv[j][j], 0.0) / n)
    except ValueError:
        return float("nan")


def _invert_matrix(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("singular")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        for j in range(2 * n):
            aug[col][j] /= div
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            for j in range(2 * n):
                aug[row][j] -= factor * aug[col][j]
    return [row[n:] for row in aug]


def collect_alert_survival_events(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    poll_interval_min: int = 5,
) -> list[dict[str, Any]]:
    """spike 日 · 首次 climax+2σ alert 到 session high 的分鐘差。"""
    rows = collect_observations(
        conn,
        date_start=date_start,
        date_end=date_end,
        poll_interval_min=poll_interval_min,
    )
    by_session: dict[tuple[str, str], list[ObsRow]] = {}
    for r in rows:
        key = (r.stock_id, r.trade_date)
        by_session.setdefault(key, []).append(r)

    events: list[dict[str, Any]] = []
    for (sid, td), obs in by_session.items():
        peak_obs = next((o for o in obs if o.is_session_peak), None)
        if peak_obs is None or peak_obs.ext_prev_pct < 5.0:
            continue
        peak_idx = _minute_index(peak_obs.minute)
        alert_obs = next((o for o in obs if _is_alert_climax_2sigma(o)), None)
        if alert_obs is None:
            continue
        alert_idx = _minute_index(alert_obs.minute)
        delta = peak_idx - alert_idx
        events.append(
            {
                "stock_id": sid,
                "trade_date": td,
                "alert_minute": alert_obs.minute,
                "peak_minute": peak_obs.minute,
                "minutes_to_peak": delta,
                "alert_is_peak": alert_obs.is_session_peak,
                "peak_within_15m": 0 <= delta <= 15,
                "peak_within_30m": 0 <= delta <= 30,
                "alert_before_peak": delta >= 0,
            }
        )
    return events


def summarize_survival(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"n": 0}
    n = len(events)
    at_peak = sum(1 for e in events if e["alert_is_peak"])
    before = [e for e in events if e["alert_before_peak"]]
    deltas = sorted(e["minutes_to_peak"] for e in before)
    within_15 = sum(1 for e in events if e["peak_within_15m"])
    within_30 = sum(1 for e in events if e["peak_within_30m"])
    after_peak = sum(1 for e in events if e["minutes_to_peak"] < 0)

    def _pct(x: int) -> float:
        return round(x / n * 100, 1)

    return {
        "n_spike_sessions_with_alert": n,
        "P_alert_is_peak": _pct(at_peak),
        "P_alert_before_peak": _pct(len(before)),
        "P_peak_within_15m": _pct(within_15),
        "P_peak_within_30m": _pct(within_30),
        "P_alert_after_peak": _pct(after_peak),
        "median_minutes_to_peak": statistics.median(deltas) if deltas else None,
        "p75_minutes_to_peak": (
            statistics.quantiles(deltas, n=4)[-1] if len(deltas) >= 4 else None
        ),
    }


OOS_SIGNATURES = ("climax+2σ", "climax@high", "below_vwap", "post_peak+below_vwap")


def oos_holdout_validation(
    conn: sqlite3.Connection,
    *,
    train_start: str = "2024-01-01",
    train_end: str = "2025-12-31",
    test_start: str = "2026-01-01",
    test_end: str = "2026-06-26",
    poll_interval_min: int = 5,
    lift_retention_min: float = 0.65,
) -> dict[str, Any]:
    train_rows = collect_observations(
        conn,
        date_start=train_start,
        date_end=train_end,
        poll_interval_min=poll_interval_min,
    )
    test_rows = collect_observations(
        conn,
        date_start=test_start,
        date_end=test_end,
        poll_interval_min=poll_interval_min,
    )
    pred_map = {name: pred for name, pred in PREDICATES}

    outcomes: list[OutcomeKey] = ["is_session_peak", "fade_30m_ge2", "post_peak_no_hh_30m"]
    comparisons: list[dict[str, Any]] = []
    passes = 0
    checks = 0

    for sig in OOS_SIGNATURES:
        pred = pred_map.get(sig)
        if pred is None:
            continue
        for oc in outcomes:
            train_eval = evaluate_predicates(train_rows, outcome=oc, spike_only=True, min_n=100)
            test_eval = evaluate_predicates(test_rows, outcome=oc, spike_only=True, min_n=50)
            tr = next((r for r in train_eval if r["predicate"] == sig), None)
            te = next((r for r in test_eval if r["predicate"] == sig), None)
            if not tr or not te:
                continue
            checks += 1
            retention = te["lift"] / tr["lift"] if tr["lift"] else float("nan")
            ok = retention >= lift_retention_min and te["lift"] > 1.05
            if ok:
                passes += 1
            comparisons.append(
                {
                    "signature": sig,
                    "outcome": oc,
                    "train_lift": tr["lift"],
                    "test_lift": te["lift"],
                    "lift_retention": round(retention, 2),
                    "train_P": tr["P"],
                    "test_P": te["P"],
                    "pass": ok,
                }
            )

    return {
        "train": f"{train_start}..{train_end}",
        "test": f"{test_start}..{test_end}",
        "lift_retention_min": lift_retention_min,
        "n_checks": checks,
        "n_pass": passes,
        "gate_pass": passes >= max(1, checks * 2 // 3) if checks else False,
        "comparisons": comparisons,
    }


def run_phase_2c_analysis(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    poll_interval_min: int = 5,
    skip_backtest: bool = False,
) -> dict[str, Any]:
    from research.backtest.c18acc_extension_exit import (
        PHASE_2C_EXTENSION_SWEEP,
        run_c18acc_extension_exit_sweep,
    )

    rows = collect_observations(
        conn,
        date_start=date_start,
        date_end=date_end,
        poll_interval_min=poll_interval_min,
    )
    logit = interaction_logit_regression(rows, spike_only=True)
    survival_events = collect_alert_survival_events(
        conn,
        date_start=date_start,
        date_end=date_end,
        poll_interval_min=poll_interval_min,
    )
    survival = summarize_survival(survival_events)
    oos = oos_holdout_validation(conn, test_end=date_end)

    backtest_full: dict[str, Any] | None = None
    backtest_oos: dict[str, Any] | None = None
    if not skip_backtest:
        backtest_full = run_c18acc_extension_exit_sweep(
            conn,
            date_start=date_start,
            date_end=date_end,
            configs=PHASE_2C_EXTENSION_SWEEP,
        )
        backtest_oos = run_c18acc_extension_exit_sweep(
            conn,
            date_start="2026-01-01",
            date_end=date_end,
            configs=PHASE_2C_EXTENSION_SWEEP,
        )

    h2c: dict[str, Any] = {}
    top_ix = logit.get("top_interaction")
    if top_ix:
        h2c["H-2C-1"] = {
            "pass": top_ix.get("p_value", 1) < 0.05 and top_ix.get("odds_ratio", 0) > 1.5,
            "detail": top_ix,
        }
    h2c["H-2C-2"] = {
        "pass": survival.get("P_alert_is_peak", 0) >= 5.0,
        "detail": survival,
    }
    h2c["H-2C-3"] = {"pass": oos.get("gate_pass"), "detail": oos}

    if backtest_full and backtest_oos:
        def _summary_map(payload: dict) -> dict[str, dict]:
            return {s["variant_id"]: s for s in payload.get("summaries") or []}

        full_m = _summary_map(backtest_full)
        oos_m = _summary_map(backtest_oos)
        x0_ex = full_m.get("X0", {}).get("mean_excess_pct")
        x6 = full_m.get("X6", {})
        x7 = full_m.get("X7", {})
        x4 = full_m.get("X4", {})
        h2c["H-2C-4"] = {
            "pass": (
                x6.get("median_save_spike_pct") is not None
                and x7.get("median_save_spike_pct") is not None
                and (x7.get("median_save_spike_pct") or 0) >= (x4.get("median_save_spike_pct") or -999)
            ),
            "X6_spike_median_save": x6.get("median_save_spike_pct"),
            "X7_spike_median_save": x7.get("median_save_spike_pct"),
            "X4_spike_median_save": x4.get("median_save_spike_pct"),
        }
        h2c["H-2C-5"] = {
            "pass": (
                x0_ex is not None
                and x7.get("mean_excess_pct") is not None
                and float(x7["mean_excess_pct"]) >= float(x0_ex) - 0.3
            ),
            "X7_delta_excess": x7.get("delta_vs_baseline_pp"),
            "X6_delta_excess": x6.get("delta_vs_baseline_pp"),
        }

    return {
        "date_start": date_start,
        "date_end": date_end,
        "interaction_logit": logit,
        "survival": survival,
        "survival_n_events": len(survival_events),
        "oos_holdout": oos,
        "backtest_full": backtest_full,
        "backtest_oos": backtest_oos,
        "hypothesis_results": h2c,
    }


def render_phase_2c_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Extension radar · Phase 2c · 交互 logit · 生存 · OOS · 回測",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        "",
        "## 假設檢定",
        "",
        "| ID | 內容 | 結果 |",
        "|----|------|------|",
    ]
    labels = {
        "H-2C-1": "交互 logit · climax×2σ×opening OR>1.5 · p<0.05",
        "H-2C-2": "alert 當根=日高 P ≥ 5%",
        "H-2C-3": "OOS lift 保留 ≥65% · 2/3 checks pass",
        "H-2C-4": "X7 spike median save ≥ X4",
        "H-2C-5": "X7 全樣本 Δexcess ≥ X0−0.3pp",
    }
    for hid, label in labels.items():
        r = (payload.get("hypothesis_results") or {}).get(hid, {})
        status = "✓" if r.get("pass") else "✗"
        lines.append(f"| {hid} | {label} | {status} |")

    logit = payload.get("interaction_logit") or {}
    if not logit.get("skipped"):
        lines += [
            "",
            "## 交互項 logit（spike · outcome=is_session_peak）",
            "",
            f"n={logit.get('n')} · base={logit.get('base_rate_pct')}% · sig={logit.get('n_significant')}",
            "",
            "| feature | β | SE | OR | p |",
            "|---------|---|----|----|---|",
        ]
        for c in logit.get("coefficients") or []:
            lines.append(
                f"| {c['feature']} | {c['beta']} | {c.get('se')} | {c['odds_ratio']} | {c.get('p_value')} |"
            )

    surv = payload.get("survival") or {}
    lines += [
        "",
        "## Time-to-peak 生存摘要（首次 climax+2σ alert · spike 日）",
        "",
        f"sessions={surv.get('n_spike_sessions_with_alert')} · "
        f"P(alert=peak)={surv.get('P_alert_is_peak')}% · "
        f"P(peak≤15m)={surv.get('P_peak_within_15m')}% · "
        f"P(peak≤30m)={surv.get('P_peak_within_30m')}% · "
        f"median min={surv.get('median_minutes_to_peak')}",
        "",
    ]

    oos = payload.get("oos_holdout") or {}
    lines += [
        "## OOS holdout（train 2024–2025 · test 2026H1）",
        "",
        f"checks={oos.get('n_checks')} · pass={oos.get('n_pass')} · gate={'✓' if oos.get('gate_pass') else '✗'}",
        "",
        "| signature | outcome | train lift | test lift | retention | pass |",
        "|-----------|---------|------------|-----------|-----------|------|",
    ]
    for c in oos.get("comparisons") or []:
        lines.append(
            f"| {c['signature']} | {c['outcome']} | {c['train_lift']}× | {c['test_lift']}× "
            f"| {c['lift_retention']} | {'✓' if c['pass'] else '✗'} |"
        )

    for title, key in (("全樣本回測", "backtest_full"), ("OOS 2026H1 回測", "backtest_oos")):
        bt = payload.get(key)
        if not bt:
            continue
        lines += [
            "",
            f"## {title}（X0/X3/X4/X6/X7/X8）",
            "",
            "| id | mode | triggers | 均超額% | Δ vs X0 | median save | spike median save |",
            "|----|------|----------|---------|---------|-------------|-------------------|",
        ]
        for s in bt.get("summaries") or []:
            lines.append(
                f"| {s.get('variant_id')} | {s.get('exit_mode')} | {s.get('overlay_triggers')} "
                f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_baseline_pp')} "
                f"| {s.get('median_save_vs_orig_pct')} | {s.get('median_save_spike_pct')} |"
            )

    lines += [
        "",
        "---",
        "模組：`scripts/tools/analyze_extension_peak_features_2c.py`",
        "",
    ]
    return "\n".join(lines)
