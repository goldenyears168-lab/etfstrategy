"""Pre-release conviction score → D+1 return backtest.

Mathematical model (SSOT for reports):
  Universe U = {(s,t) : eligible pre-release stock-days}
  Score S_{s,t} = g(X_{s,t}) · Π ρ_k(X) · (1 + Σ w_j φ_j(X))
  Tier τ(S) → position weight ω(S)
  Outcome R_{s,t+1} = close-to-close return from t to t+1
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, build_lifecycle_events
from research.backtest.rrg_improving_s3rv_executable import (
    MIN_SID_HIST,
    PRE_RELEASE_TIERS,
    compute_pre_release_conviction_score,
)

# --- Model coefficients (match compute_pre_release_conviction_score) ---
PENALTY_RHO: dict[str, float] = {
    "bench_neg": 0.55,
    "st23_bad": 0.65,
    "chase_bad": 0.70,
    "disp_exp": 0.85,
    "excess_hot": 0.80,
    "sid_bad": 0.75,
}
BOOST_W: dict[str, float] = {
    "disp": 0.30,
    "quasi": 0.22,
    "st6": 0.18,
    "base": 0.15,
    "sid_ok": 0.12,
    "prev_lag": 0.10,
    "st1": 0.08,
    "flat": 0.08,
    "bench_str": 0.05,
}
DISPLAY_SCALE = 1.5  # raw_mul / DISPLAY_SCALE * 100 → display score


@dataclass(frozen=True)
class BucketStats:
    bucket_id: str
    label: str
    n_events: int
    n_signal_days: int
    next_mean_pct: float
    next_win_rate: float
    p_next_ge_burst: float
    p_next_le_loss5: float
    avg_score: float
    avg_weight: float


@dataclass(frozen=True)
class PortfolioStats:
    variant_id: str
    label: str
    n_trade_days: int
    n_calendar_days: int
    event_mean_pct: float
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    capital_util_pct: float


def model_spec_markdown() -> str:
  """LaTeX-free math spec for reports."""
  lines = [
        "## 0. Mathematical model",
        "",
        "**Sets**",
        "",
        "- Universe \\( \\mathcal{U} \\): stock-days \\((s,t)\\) with `end_q ∈ {improving, lagging}`",
        "  · signal-day return \\( r_{s,t} < 5\\% \\) (pre-burst)",
        "  · lagging pre-flip: `disp_contract` or \\( r_{s,t} > 0.5\\% \\)",
        "- Gate \\( g(X_{s,t}) \\in \\{0,1\\} \\): outlier / ineligible → score 0",
        "- Outcome \\( R_{s,t+1} \\): close→close return from \\(t\\) to \\(t+1\\)",
        "",
        "**Feature vector** \\( X_{s,t} \\) (PIT at \\(t\\) close):",
        "",
        "bench\\(_t\\), \\(r_{s,t}\\), streak, disp\\(_\\downarrow\\), Δdisp, base\\(_\\setup\\),",
        "excess, prev\\(_\\quadrant\\), sid\\(_\\hist\\)",
        "",
        "**Multiplicative score**",
        "",
        "\\[",
        "S_{s,t} = g(X) \\cdot \\underbrace{\\prod_{k} \\rho_k(X)}_{\\text{penalty core}}",
        " \\cdot \\underbrace{\\left(1 + \\sum_j w_j \\, \\phi_j(X)\\right)}_{\\text{boost}}",
        "\\]",
        "",
        "Penalty factors \\( \\rho_k \\):",
        "",
    ]
  for k, v in PENALTY_RHO.items():
        lines.append(f"- \\( \\rho_\\text{{{k}}} = {v} \\)")
  lines.extend(["", "Boost weights \\( w_j \\):", ""])
  for k, v in BOOST_W.items():
        lines.append(f"- \\( w_\\text{{{k}}} = {v} \\)")
  lines.extend(
        [
            "",
            "**Display score** \\( \\hat{S} = \\min(100,\\; 100 \\cdot S / "
            f"{DISPLAY_SCALE}) \\).",
            "",
            "**Tier map** \\( \\tau(\\hat{S}) \\to \\omega \\):",
            "",
            "| \\( \\hat{S} \\) | tier | \\( \\omega \\) |",
            "|---|------|-----|",
        ]
  )
  for thr, tid, label, w in PRE_RELEASE_TIERS:
        lines.append(f"| ≥ {thr:.0f} | {label} | {w:.2f} |")
  lines.extend(
        [
            "",
            "**Portfolio (signal day \\(t\\), realize \\(t+1\\))**",
            "",
            "- Equal-weight: \\( \\bar{R}_{t+1} = \\frac{1}{|A_t|} \\sum_{s \\in A_t} R_{s,t+1} \\),",
            "  \\( A_t = \\{s : g=1,\\, S>0\\} \\)",
            "- Score-weighted: \\( \\bar{R}_{t+1} = \\frac{\\sum_s \\omega_s R_{s,t+1}}{\\sum_s \\omega_s} \\)",
            "- Calendar compound: no-signal days return 0%",
            "",
            "**Hypotheses**",
            "",
            "- H\\(_\\uparrow\\): \\( \\mathbb{E}[R \\mid Q_5] > \\mathbb{E}[R \\mid Q_1] \\)",
            "- H\\(_\\tail\\): \\( P(R \\ge 5\\% \\mid Q_5) > P(R \\ge 5\\% \\mid Q_1) \\)",
            "- H\\(_\\downarrow\\): \\( P(R \\le -5\\% \\mid Q_5) < P(R \\le -5\\% \\mid Q_1) \\)",
            "",
        ]
  )
  return "\n".join(lines)


def _attach_sid_burst_hist(df: pd.DataFrame) -> pd.DataFrame:
    prior: dict[str, list[float]] = {}
    hist_n, hist_ok = [], []
    for _, row in df.sort_values(["sid", "date"]).iterrows():
        sid = str(row["sid"])
        prev = prior.get(sid, [])
        if len(prev) >= MIN_SID_HIST:
            hist_n.append(len(prev))
            hist_ok.append(float(np.mean(prev)) > 0)
        else:
            hist_n.append(len(prev))
            hist_ok.append(False)
        if bool(row["s3_burst"]):
            prior.setdefault(sid, []).append(float(row["nxt_ret"]))
    out = df.copy()
    out["sid_burst_hist_n"] = hist_n
    out["sid_burst_hist_ok"] = hist_ok
    return out


def _score_row(row: pd.Series) -> dict[str, Any]:
    disp_d = float(row["disp_d"]) if pd.notna(row.get("disp_d")) else 0.0
    conv = compute_pre_release_conviction_score(
        end_q=str(row["end_q"]),
        bench_today_ret=float(row["bench_today_ret"]),
        sig_ret=float(row["sig_ret"]),
        improve_streak=int(row["improve_streak"]),
        disp_contract=bool(row["disp_contract"]),
        disp_d=disp_d,
        base_setup=bool(row["base_setup"]),
        prev_end_q=str(row.get("prev_end_q", "")),
        excess=float(row["excess"]),
        sid_burst_hist_ok=bool(row["sid_burst_hist_ok"]),
        sid_burst_hist_n=int(row["sid_burst_hist_n"]),
        s3_burst=bool(row["s3_burst"]),
    )
    return {
        "raw_mul": conv.raw_mul,
        "display_score": conv.score,
        "tier_id": conv.tier_id,
        "tier_label": conv.tier_label,
        "position_weight": conv.position_weight,
        "blocked": conv.blocked,
    }


def build_pre_release_panel(df: pd.DataFrame) -> pd.DataFrame:
    """Score all stock-days · retain rows for analysis."""
    panel = df.sort_values(["sid", "date"]).copy()
    panel["prev_end_q"] = panel.groupby("sid")["end_q"].shift(1).fillna("")
    panel = _attach_sid_burst_hist(panel)
    scored = panel.apply(_score_row, axis=1, result_type="expand")
    out = pd.concat([panel, scored], axis=1)
    out["nxt_win5"] = out["nxt_ret"] >= BURST_PCT
    out["nxt_loss5"] = out["nxt_ret"] <= -BURST_PCT
    out["eligible_pre"] = (
        (out["end_q"].isin(["improving", "lagging"]))
        & (out["sig_ret"] < BURST_PCT)
        & (out["blocked"] == "")
        & (out["raw_mul"] > 0)
    )
    out["sig_z"] = out.groupby("date")["sig_ret"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-9)
    )
    out["eligible_trim"] = out["eligible_pre"] & out["sig_z"].between(-2, 2)
    return out


def _bucket_stats(sub: pd.DataFrame, bucket_id: str, label: str) -> BucketStats:
    if sub.empty:
        return BucketStats(bucket_id, label, 0, 0, 0, 0, 0, 0, 0, 0)
    return BucketStats(
        bucket_id=bucket_id,
        label=label,
        n_events=len(sub),
        n_signal_days=int(sub["date"].nunique()),
        next_mean_pct=round(float(sub["nxt_ret"].mean()), 4),
        next_win_rate=round(float((sub["nxt_ret"] > 0).mean()), 4),
        p_next_ge_burst=round(float(sub["nxt_win5"].mean()), 4),
        p_next_le_loss5=round(float(sub["nxt_loss5"].mean()), 4),
        avg_score=round(float(sub["display_score"].mean()), 2),
        avg_weight=round(float(sub["position_weight"].mean()), 3),
    )


def _portfolio_curve(
    picks: pd.DataFrame,
    all_dates: list[str],
    *,
    weight_col: str | None,
    full_calendar: bool,
) -> tuple[pd.Series, float]:
    if picks.empty:
        return pd.Series(0.0, index=all_dates), 0.0
    date_idx = {d: i for i, d in enumerate(all_dates)}
    daily_map: dict[str, float] = {}
    for d, grp in picks.groupby("date"):
        si = date_idx.get(str(d))
        if si is None or si + 1 >= len(all_dates):
            continue
        realize = all_dates[si + 1]
        if weight_col:
            w = grp[weight_col].astype(float)
            daily_map[realize] = float((grp["nxt_ret"] * w).sum() / w.sum()) if w.sum() > 0 else 0.0
        else:
            daily_map[realize] = float(grp["nxt_ret"].mean())
    if full_calendar:
        daily = pd.Series(0.0, index=all_dates)
        for rd, r in daily_map.items():
            daily.at[rd] = r
        util = len(daily_map) / len(all_dates) * 100
        return daily, util
    trade_dates = sorted(daily_map)
    daily = pd.Series({d: daily_map[d] for d in trade_dates})
    return daily, 100.0


def _portfolio_stats(
    variant_id: str,
    label: str,
    daily: pd.Series,
    all_dates: list[str],
    util: float,
) -> PortfolioStats:
    years = max(len(all_dates) / 252, 1 / 252)
    if daily.empty or (daily == 0).all():
        return PortfolioStats(variant_id, label, 0, len(all_dates), 0, 0, 0, 0, 0, util)
    eq = (1 + daily / 100).cumprod()
    total = (eq.iloc[-1] - 1) * 100
    cagr = ((eq.iloc[-1]) ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100.0
    peak = eq.cummax()
    dd = float((eq / peak - 1).min() * 100)
    active = daily[daily != 0]
    sharpe = 0.0
    if len(active) > 1 and active.std() > 0:
        sharpe = float(active.mean() / active.std() * np.sqrt(252))
    return PortfolioStats(
        variant_id=variant_id,
        label=label,
        n_trade_days=int((daily != 0).sum()),
        n_calendar_days=len(all_dates),
        event_mean_pct=round(float(active.mean()) if len(active) else 0.0, 4),
        total_return_pct=round(total, 2),
        cagr_pct=round(cagr, 2),
        sharpe=round(sharpe, 2),
        max_drawdown_pct=round(dd, 2),
        capital_util_pct=round(util, 2),
    )


def run_pre_release_backtest(*, oos_days: int = 252) -> dict[str, Any]:
    df, meta = build_lifecycle_events()
    all_dates = sorted(df["date"].unique())
    panel = build_pre_release_panel(df)
    panel["nxt_s3"] = panel.groupby("sid")["s3_burst"].shift(-1)

    baseline = panel[(panel["end_q"] == "improving") & (panel["sig_ret"] < BURST_PCT)]
    eligible = panel[panel["eligible_pre"]]
    trimmed = panel[panel["eligible_trim"]]

    # Quintiles on display score
    quintiles: list[BucketStats] = []
    if len(trimmed) >= 50:
        trimmed = trimmed.copy()
        trimmed["q"] = pd.qcut(trimmed["display_score"], 5, duplicates="drop")
        for i, (q, g) in enumerate(trimmed.groupby("q", observed=True)):
            quintiles.append(_bucket_stats(g, f"Q{i+1}", f"Quintile {i+1}"))

    # Fixed tiers
    tiers: list[BucketStats] = []
    for thr, tid, label, _w in PRE_RELEASE_TIERS:
        tiers.append(
            _bucket_stats(
                eligible[eligible["display_score"] >= thr],
                tid,
                f"{label} (≥{thr:.0f})",
            )
        )

    # Reverse tail: blocked vs eligible
    blocked = panel[(panel["blocked"] != "") & (panel["end_q"] == "improving") & (panel["sig_ret"] < BURST_PCT)]
    reverse = [
        _bucket_stats(baseline, "baseline", "Improving sig<5% (no gate)"),
        _bucket_stats(eligible, "eligible", "Eligible g=1"),
        _bucket_stats(blocked, "blocked", "Blocked / outlier"),
    ]

    # Portfolios: realize on D+1
    top_q = None
    if len(trimmed) >= 50 and "q" in trimmed.columns:
        top_q = trimmed["q"].cat.categories[-1]
    top_sub = trimmed[trimmed["q"] == top_q] if top_q is not None else trimmed.iloc[0:0]

    portfolios: list[PortfolioStats] = []
    for vid, label, sub, wcol, cal in [
        ("eq_all", "Equal · improving sig<5%", baseline, None, True),
        ("eq_elig", "Equal · eligible score>0", eligible, None, True),
        ("w_elig", "Score-weighted · eligible", eligible, "position_weight", True),
        ("w_top20", "Score-weighted · top quintile", top_sub, "position_weight", True),
        ("w_deployed", "Score-weighted · deployed only", eligible, "position_weight", False),
    ]:
        daily, util = _portfolio_curve(sub, all_dates, weight_col=wcol, full_calendar=cal)
        portfolios.append(_portfolio_stats(vid, label, daily, all_dates, util))

    # OOS
    oos_start = all_dates[-oos_days] if len(all_dates) > oos_days else all_dates[0]
    oos_elig = eligible[eligible["date"] >= oos_start]
    oos_dates = [d for d in all_dates if d >= oos_start]
    oos_daily, oos_util = _portfolio_curve(oos_elig, oos_dates, weight_col="position_weight", full_calendar=True)
    oos_port = _portfolio_stats("oos_weighted", f"OOS score-weighted · last {oos_days}d", oos_daily, oos_dates, oos_util)

    # Next-day burst prediction
    burst_lift = {
        "base_rate": round(float(eligible["nxt_s3"].mean()), 4) if len(eligible) else 0,
        "top_quintile": round(float(top_sub["nxt_s3"].mean()), 4) if len(top_sub) else 0,
    }

    return {
        "meta": {
            **meta,
            "oos_days": oos_days,
            "generated_on": date.today().isoformat(),
            "model": {
                "penalty_rho": PENALTY_RHO,
                "boost_w": BOOST_W,
                "display_scale": DISPLAY_SCALE,
                "tiers": [list(t) for t in PRE_RELEASE_TIERS],
            },
        },
        "universe": {
            "n_panel": len(panel),
            "n_baseline": len(baseline),
            "n_eligible": len(eligible),
            "n_trimmed": len(trimmed),
            "baseline_mean_nxt": round(float(baseline["nxt_ret"].mean()), 4),
            "eligible_mean_nxt": round(float(eligible["nxt_ret"].mean()), 4) if len(eligible) else 0,
        },
        "quintiles": [asdict(q) for q in quintiles],
        "tiers": [asdict(t) for t in tiers],
        "reverse_buckets": [asdict(r) for r in reverse],
        "portfolios": [asdict(p) for p in portfolios],
        "oos_portfolio": asdict(oos_port),
        "burst_lift": burst_lift,
    }


def render_backtest_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    u = result["universe"]
    lines = [
        "# Pre-release conviction score → D+1 backtest",
        "",
        model_spec_markdown(),
        f"- Universe: **{meta['universe_size']}** names · {meta['date_start']} → {meta['date_end']}",
        f"- Generated: {meta['generated_on']}",
        "",
        "## 1. Universe",
        "",
        f"| Pool | N | mean(D+1) |",
        f"|------|---|-----------|",
        f"| Improving sig<5% | {u['n_baseline']} | {u['baseline_mean_nxt']:+.3f}% |",
        f"| Eligible g=1, S>0 | {u['n_eligible']} | {u['eligible_mean_nxt']:+.3f}% |",
        f"| Trimmed \\|sig_z\\|≤2 | {u['n_trimmed']} | — |",
        "",
        "## 2. Score quintiles → D+1 (trimmed)",
        "",
        "| Q | N | mean | win | P(≥5%) | P(≤−5%) | avg score |",
        "|---|---|------|-----|--------|--------|-----------|",
    ]
    for q in result["quintiles"]:
        lines.append(
            f"| {q['label']} | {q['n_events']} | {q['next_mean_pct']:+.3f}% | "
            f"{q['next_win_rate']:.1%} | {q['p_next_ge_burst']:.1%} | "
            f"{q['p_next_le_loss5']:.1%} | {q['avg_score']:.1f} |"
        )

    lines.extend(["", "## 3. Fixed tiers", ""])
    lines.append("| Tier | N | mean | P(≥5%) | P(≤−5%) |")
    lines.append("|------|---|------|--------|--------|")
    for t in result["tiers"]:
        if t["n_events"] == 0:
            continue
        lines.append(
            f"| {t['label']} | {t['n_events']} | {t['next_mean_pct']:+.3f}% | "
            f"{t['p_next_ge_burst']:.1%} | {t['p_next_le_loss5']:.1%} |"
        )

    lines.extend(["", "## 4. Reverse / blocked", ""])
    lines.append("| Bucket | N | mean | P(≤−5%) |")
    lines.append("|--------|---|------|--------|")
    for r in result["reverse_buckets"]:
        lines.append(
            f"| {r['label']} | {r['n_events']} | {r['next_mean_pct']:+.3f}% | "
            f"{r['p_next_le_loss5']:.1%} |"
        )

    bl = result["burst_lift"]
    lines.extend(
        [
            "",
            f"**P(next-day S3 burst)**: base eligible {bl['base_rate']:.2%} · top quintile {bl['top_quintile']:.2%}",
            "",
            "## 5. Portfolios (buy D close · sell D+1 close)",
            "",
            "| Variant | 投入日 | mean/trade | CAGR | Total | Sharpe | MaxDD | Util% |",
            "|---------|--------|-----------|------|-------|--------|-------|-------|",
        ]
    )
    for p in result["portfolios"]:
        lines.append(
            f"| {p['label']} | {p['n_trade_days']} | {p['event_mean_pct']:+.3f}% | "
            f"{p['cagr_pct']:+.1f}% | {p['total_return_pct']:+.0f}% | {p['sharpe']:.2f} | "
            f"{p['max_drawdown_pct']:.1f}% | {p['capital_util_pct']:.0f}% |"
        )
    o = result["oos_portfolio"]
    lines.extend(
        [
            "",
            f"## 6. OOS ({meta['oos_days']} calendar days)",
            "",
            f"- Score-weighted eligible: CAGR **{o['cagr_pct']:+.1f}%** · total **{o['total_return_pct']:+.0f}%** · "
            f"Sharpe **{o['sharpe']:.2f}** · MaxDD **{o['max_drawdown_pct']:.1f}%**",
            "",
            "> Calendar CAGR uses 0% on no-position days. Deployed-only variant compounds only trade days.",
            "",
        ]
    )
    return "\n".join(lines)
