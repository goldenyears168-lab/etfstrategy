"""Track A: Trend posture / exposure stratification · hold horizon (PIT · L1 copytrade)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Literal

from regime_config import load_regime_config
from stage_analysis import classify_ix_trend_posture
from stock_db import load_copytrade_horizon_decay, load_copytrade_signal_days_for_run
from vcp_nse_port.bars import rows_to_ohlcv_df

BAR_LOOKBACK = 280
ENTRY_ROW = "L1"

ExposureDecision = Literal["allowed", "restrictive", "cash-priority"]
Recommendation = Literal["NEW_ENTRY_ALLOWED", "REDUCE_ONLY", "CASH_PRIORITY"]

RECOMMENDATION_TO_EXPOSURE: dict[str, ExposureDecision] = {
    "NEW_ENTRY_ALLOWED": "allowed",
    "REDUCE_ONLY": "restrictive",
    "CASH_PRIORITY": "cash-priority",
}


def score_top_risk_from_tech(
    *,
    tsm_daily_return_pct: float | None,
    tx_gap_pct: float | None,
    sox_daily_return_pct: float | None,
    tsm_risk_threshold: float = -2.0,
    tx_risk_threshold: float = -1.5,
    sox_risk_threshold: float = -2.0,
) -> int:
    """Overnight tech tail risk score for copytrade PIT labels (lower = worse)."""
    score = 85
    if tsm_daily_return_pct is not None and tsm_daily_return_pct <= tsm_risk_threshold:
        score = min(score, 20)
    if tx_gap_pct is not None and tx_gap_pct <= tx_risk_threshold:
        score = min(score, 35)
    if sox_daily_return_pct is not None and sox_daily_return_pct <= sox_risk_threshold:
        score = min(score, 40)
    return max(0, score)


def determine_exposure_recommendation(
    composite: float,
    top_risk_score: int | None,
    missing_critical: int,
) -> Recommendation:
    if composite < 30:
        return "CASH_PRIORITY"
    if top_risk_score is not None and top_risk_score < 25:
        return "CASH_PRIORITY"
    if composite < 50:
        return "REDUCE_ONLY"
    if top_risk_score is not None and top_risk_score < 40:
        return "REDUCE_ONLY"
    if missing_critical >= 2:
        return "REDUCE_ONLY"
    return "NEW_ENTRY_ALLOWED"


@dataclass(frozen=True)
class RegimeLabel:
    signal_date: str
    trend_posture: str
    exposure_decision: str
    trend_posture_score: int
    top_risk_score: int | None
    composite_score: float
    ix_stage: int
    ix_trend_score: float
    tx_gap_pct: float | None


def load_ix_bars_as_of(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    code: str = "IX0001",
    lookback: int = BAR_LOOKBACK,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE code = ? AND source = 'tej' AND date <= ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, as_of_date, lookback),
    ).fetchall()
    return list(reversed(rows))


def classify_regime_pit(
    conn: sqlite3.Connection,
    signal_date: str,
    *,
    cfg: dict | None = None,
) -> RegimeLabel | None:
    """讯号日 T 收盘后可知的 regime（仅用 date<=T 的台指 K 与当日 tech_risk）。"""
    cfg = cfg or load_regime_config()
    bench_code = str(cfg.get("benchmark_code") or "IX0001")
    thresholds = cfg.get("thresholds") or {}

    bench_rows = load_ix_bars_as_of(conn, signal_date, code=bench_code)
    if len(bench_rows) < 200:
        return None

    bench_df = rows_to_ohlcv_df(bench_rows)
    if bench_df.empty:
        return None

    regime = classify_ix_trend_posture(bench_df)
    stage = int(regime.get("stage") or 0)
    trend_score = float(regime.get("trend_score") or 0.0)
    trend_posture = str(
        regime.get("trend_posture") or regime.get("regime_name") or "transitional"
    )
    trend_posture_score = int(
        regime.get("trend_posture_score") or regime.get("regime_score") or 50
    )

    tech = None
    try:
        tech = conn.execute(
            """
            SELECT session_date, tsm_daily_return_pct, sox_daily_return_pct, tx_gap_pct
            FROM tech_risk_daily_snapshot
            WHERE session_date = ?
            """,
            (signal_date,),
        ).fetchone()
        if tech is None:
            tech = conn.execute(
                """
                SELECT session_date, tsm_daily_return_pct, sox_daily_return_pct, tx_gap_pct
                FROM tech_risk_daily_snapshot
                WHERE session_date <= ?
                ORDER BY session_date DESC
                LIMIT 1
                """,
                (signal_date,),
            ).fetchone()
    except sqlite3.OperationalError:
        tech = None

    top_risk_score: int | None = None
    tx_gap: float | None = None
    if tech is not None:
        tx_gap = (
            float(tech["tx_gap_pct"]) if tech["tx_gap_pct"] is not None else None
        )
        top_risk_score = score_top_risk_from_tech(
            tsm_daily_return_pct=(
                float(tech["tsm_daily_return_pct"])
                if tech["tsm_daily_return_pct"] is not None
                else None
            ),
            tx_gap_pct=tx_gap,
            sox_daily_return_pct=(
                float(tech["sox_daily_return_pct"])
                if tech["sox_daily_return_pct"] is not None
                else None
            ),
            tsm_risk_threshold=float(thresholds.get("tsm_adr_risk_pct", -2.0)),
            tx_risk_threshold=float(thresholds.get("tx_gap_risk_pct", -1.5)),
            sox_risk_threshold=float(thresholds.get("sox_risk_pct", -2.0)),
        )

    if top_risk_score is not None:
        composite = 0.55 * trend_posture_score + 0.45 * top_risk_score
        missing_critical = 0
    else:
        composite = float(trend_posture_score)
        missing_critical = 1

    recommendation = determine_exposure_recommendation(
        composite, top_risk_score, missing_critical
    )
    exposure_decision = RECOMMENDATION_TO_EXPOSURE[recommendation]

    return RegimeLabel(
        signal_date=signal_date,
        trend_posture=trend_posture,
        exposure_decision=exposure_decision,
        trend_posture_score=trend_posture_score,
        top_risk_score=top_risk_score,
        composite_score=round(composite, 2),
        ix_stage=stage,
        ix_trend_score=round(trend_score, 2),
        tx_gap_pct=round(tx_gap, 4) if tx_gap is not None else None,
    )


def _wilcoxon_vs_zero(values: list[float]) -> float | None:
    if len(values) < 8:
        return None
    try:
        from scipy.stats import wilcoxon

        nz = [v for v in values if abs(v) > 1e-12]
        if len(nz) < 6:
            return None
        _, p = wilcoxon(nz)
        return float(p)
    except Exception:
        return None


def build_regime_horizon_rows(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    etf_code: str,
    bucket_field: str = "trend_posture",
    max_horizon: int = 45,
    entry_row: str = ENTRY_ROW,
) -> tuple[list[dict], list[dict]]:
    """按 regime 桶汇总各 H 的讯号日 α / 超額%。返回 (summary_rows, label_rows)。"""
    decay = [
        dict(r)
        for r in load_copytrade_horizon_decay(conn, batch_id)
        if str(r["entry_row"]) == entry_row and int(r["horizon"]) <= max_horizon
    ]
    if not decay:
        return [], []

    by_h: dict[int, dict] = {int(r["horizon"]): r for r in decay}
    horizons = sorted(by_h)

    # 讯号日标签（以 H9 run 的 complete 日为基准）
    ref_h = min(9, max(horizons))
    ref_days = load_copytrade_signal_days_for_run(conn, by_h[ref_h]["run_id"])
    labels: dict[str, RegimeLabel] = {}
    label_rows: list[dict] = []
    for d in ref_days:
        if d["status"] != "complete":
            continue
        sd = str(d["signal_date"])
        lab = classify_regime_pit(conn, sd)
        if lab is None:
            continue
        labels[sd] = lab
        label_rows.append(
            {
                "signal_date": sd,
                "trend_posture": lab.trend_posture,
                "exposure_decision": lab.exposure_decision,
                "trend_posture_score": lab.trend_posture_score,
                "top_risk_score": lab.top_risk_score,
                "composite_score": lab.composite_score,
                "ix_stage": lab.ix_stage,
                "ix_trend_score": lab.ix_trend_score,
                "tx_gap_pct": lab.tx_gap_pct,
            }
        )

    # bucket -> horizon -> lists
    bucket_days: dict[str, dict[int, list[dict]]] = {}
    for h in horizons:
        run_id = by_h[h]["run_id"]
        for d in load_copytrade_signal_days_for_run(conn, run_id):
            if d["status"] != "complete":
                continue
            sd = str(d["signal_date"])
            lab = labels.get(sd)
            if lab is None:
                continue
            bucket = getattr(lab, bucket_field)
            bucket_days.setdefault(bucket, {}).setdefault(h, []).append(dict(d))

    summary_rows: list[dict] = []
    for bucket in sorted(bucket_days):
        prev_total = 0.0
        for h in horizons:
            days = bucket_days[bucket].get(h, [])
            excess = [
                float(d["return_pct"] or 0) - float(d["bench_return_pct"] or 0)
                for d in days
            ]
            alphas = [float(d["alpha_ntd"] or 0) for d in days]
            total_alpha = sum(alphas)
            n = len(days)
            mean_excess = sum(excess) / n if n else None
            p_w = _wilcoxon_vs_zero(excess)
            summary_rows.append(
                {
                    "etf_code": etf_code,
                    "entry_row": entry_row,
                    "bucket_field": bucket_field,
                    "bucket_value": bucket,
                    "horizon": h,
                    "n_signal_days": n,
                    "total_alpha_ntd": round(total_alpha, 2),
                    "mean_excess_pct": round(mean_excess, 4) if mean_excess is not None else None,
                    "p_value_wilcoxon": p_w,
                    "is_significant": int(p_w is not None and p_w < 0.05),
                    "marginal_total_alpha_ntd": round(total_alpha - prev_total, 2),
                }
            )
            prev_total = total_alpha

    return summary_rows, label_rows


def summarize_regime_sweet_spots(
    summary_rows: list[dict],
    *,
    bucket_field: str = "trend_posture",
) -> list[dict]:
    """每个 regime 桶内 total_alpha 最大的 H。"""
    by_bucket: dict[str, list[dict]] = {}
    for r in summary_rows:
        if r["bucket_field"] != bucket_field:
            continue
        by_bucket.setdefault(str(r["bucket_value"]), []).append(r)

    out: list[dict] = []
    for bucket, rows in sorted(by_bucket.items()):
        if not rows:
            continue
        best = max(rows, key=lambda x: float(x["total_alpha_ntd"] or 0))
        # After H*: first marginal < 25% of H* marginal
        sweet_h = int(best["horizon"])
        sweet_marg = float(best.get("marginal_total_alpha_ntd") or 0)
        threshold = max(sweet_marg * 0.25, 200.0)
        hold_through = sweet_h
        for r in sorted(rows, key=lambda x: int(x["horizon"])):
            h = int(r["horizon"])
            if h <= sweet_h:
                continue
            if float(r.get("marginal_total_alpha_ntd") or 0) < threshold:
                hold_through = h - 1
                break
        else:
            hold_through = int(max(rows, key=lambda x: int(x["horizon"]))["horizon"])

        out.append(
            {
                "bucket_field": bucket_field,
                "bucket_value": bucket,
                "sweet_spot_h": sweet_h,
                "sweet_spot_total_alpha_ntd": best["total_alpha_ntd"],
                "n_signal_days_at_sweet": best["n_signal_days"],
                "hold_through_h": hold_through,
                "mean_excess_at_sweet": best["mean_excess_pct"],
            }
        )
    return out


def format_regime_horizon_markdown(
    *,
    etf_code: str,
    batch_id: str,
    summary_rows: list[dict],
    label_rows: list[dict],
    sweet_spots: list[dict],
    bucket_field: str = "trend_posture",
    max_horizon: int = 45,
) -> str:
    today = date.today().strftime("%Y%m%d")
    lines = [
        f"# {etf_code} Trend posture stratification · hold horizon（轨 A · L1 Copytrade）",
        "",
        f"> batch `{batch_id}` · PIT labels @ signal day T · H1–H{max_horizon} · report {today}",
        "",
        "## Method",
        "",
        "- **PIT labels**：`date ≤ T` IX0001 bars → Weinstein Stage Analysis + Minervini score;",
        "  `tech_risk_daily_snapshot` with `session_date ≤ T` → top risk score.",
        "- **Exposure decision**：trend_posture_score (55%) + top_risk (45%) → allowed / restrictive / cash-priority.",
        "- **Stratification metric**：`total_alpha_ntd` per complete signal day (10k NTD · same L1 matrix).",
        "- **Optimal hold (H\\*)**：H maximizing bucket `total_alpha_ntd` (not rotation model).",
        "",
        "## Signal-day trend posture distribution",
        "",
        "| Bucket | Signal days |",
        "|--------|-------------|",
    ]
    counts: dict[str, int] = {}
    key_field = (
        "exposure_decision"
        if bucket_field == "exposure_decision"
        else "trend_posture"
    )
    for lab in label_rows:
        key = str(lab[key_field])
        counts[key] = counts.get(key, 0) + 1
    for k in sorted(counts, key=counts.get, reverse=True):
        lines.append(f"| {k} | {counts[k]} |")

    lines.extend(["", "## Optimal hold (H*) by bucket", ""])
    lines.append("| Stratification | Bucket | H* | Cumulative α | n |")
    lines.append("|----------------|--------|----|--------------|---|")
    for s in sweet_spots:
        lines.append(
            f"| {s['bucket_field']} | {s['bucket_value']} | H{s['sweet_spot_h']} | "
            f"{s['sweet_spot_total_alpha_ntd']:+,.0f} | "
            f"{s['n_signal_days_at_sweet']} |"
        )

    for bf in ("trend_posture", "regime_name", "exposure_decision"):
        buckets = sorted(
            {str(r["bucket_value"]) for r in summary_rows if r["bucket_field"] == bf}
        )
        if not buckets:
            continue
        lines.extend(["", f"## Stratification: {bf}", ""])
        sweet_bf = [s for s in sweet_spots if s["bucket_field"] == bf]
        for bucket in buckets:
            sub = sorted(
                [
                    r
                    for r in summary_rows
                    if r["bucket_field"] == bf and r["bucket_value"] == bucket
                ],
                key=lambda x: int(x["horizon"]),
            )
            if not sub:
                continue
            sweet = next(
                (s for s in sweet_bf if s["bucket_value"] == bucket),
                None,
            )
            lines.extend(["", f"### {bucket}", ""])
            if sweet:
                lines.append(
                    f"Optimal hold **H{sweet['sweet_spot_h']}** (α {sweet['sweet_spot_total_alpha_ntd']:+,.0f})"
                )
            lines.append("")
            lines.append("| H | n | 累计α | 日均超额% | Δ累计α | p(W) |")
            lines.append("|---|-----|-------|-----------|--------|------|")
            for r in sub:
                mark = (
                    " **"
                    if sweet and int(r["horizon"]) == int(sweet["sweet_spot_h"])
                    else ""
                )
                end = "**" if mark else ""
                p = r["p_value_wilcoxon"]
                p_s = f"{p:.4f}" if p is not None else "—"
                lines.append(
                    f"| {mark}H{r['horizon']}{end} | {r['n_signal_days']} | "
                    f"{r['total_alpha_ntd']:+,.0f} | "
                    f"{r['mean_excess_pct'] or 0:.3f} | "
                    f"{r['marginal_total_alpha_ntd']:+,.0f} | {p_s} |"
                )

    lines.extend(["", "## 解读提示", ""])
    lines.append(
        "- If **risk_on (broadening/concentration)** optimal H* is clearly longer than **contraction**,"
        " supports holding longer in strong trend posture buckets."
    )
    lines.append(
        "- If bucket H* values are similar, global H20 is sufficient; no stratification needed."
    )
    lines.append(
        "- 本分析未用 rotation 10 万模型；桶内 n 过小时结论仅供探索。"
    )
    lines.append("")
    return "\n".join(lines)


def run_regime_horizon_analysis(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    etf_code: str,
    max_horizon: int = 45,
    persist: bool = True,
) -> dict[str, object]:
    from stock_db import persist_copytrade_regime_horizon

    summary_name, labels_name = build_regime_horizon_rows(
        conn,
        batch_id=batch_id,
        etf_code=etf_code,
        bucket_field="trend_posture",
        max_horizon=max_horizon,
    )
    summary_expo, _ = build_regime_horizon_rows(
        conn,
        batch_id=batch_id,
        etf_code=etf_code,
        bucket_field="exposure_decision",
        max_horizon=max_horizon,
    )
    summary_rows = summary_name + summary_expo
    sweet_regime = summarize_regime_sweet_spots(summary_name, bucket_field="trend_posture")
    sweet_expo = summarize_regime_sweet_spots(summary_expo, bucket_field="exposure_decision")

    if persist:
        persist_copytrade_regime_horizon(
            conn,
            batch_id,
            summary_rows,
            label_rows=labels_name,
            sweet_spots=sweet_regime + sweet_expo,
        )

    return {
        "summary_rows": summary_rows,
        "label_rows": labels_name,
        "sweet_regime": sweet_regime,
        "sweet_exposure": sweet_expo,
    }
