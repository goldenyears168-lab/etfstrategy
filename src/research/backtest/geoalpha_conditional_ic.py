"""GeoAlpha Phase 1 · conditional IC by geometry phase φ."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.geoalpha_panel import (
    PHI_LABELS,
    build_geoalpha_daily_panel,
    build_geoalpha_intraday_panel,
)
from research.backtest.rrg_drs5d_score_backtest import _DRS_COL, _RET_COL, pooled_spearman_ic
from stock_db import PROJECT_ROOT

SCORE_COLS: tuple[str, ...] = ("s_struct", "s_alpha", "s_joint")
LABEL_COLS: tuple[tuple[str, str], ...] = (
    (_DRS_COL, "drs_5d"),
    (_RET_COL, "ret_5d"),
    ("excess", "excess"),
)


def _ic_pair(df: pd.DataFrame, score_col: str, label_col: str) -> dict[str, float | int]:
    if score_col not in df.columns or label_col not in df.columns:
        return {"ic": 0.0, "p": 1.0, "n": 0}
    sub = df[[score_col, label_col]].dropna()
    if len(sub) < 10:
        return {"ic": 0.0, "p": 1.0, "n": len(sub)}
    ic, p, n = pooled_spearman_ic(sub, score_col=score_col, label_col=label_col)
    return {"ic": ic, "p": p, "n": n}


def phi_outcome_summary(panel: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phi in PHI_LABELS:
        g = panel[panel["phi"] == phi]
        if g.empty:
            continue
        row: dict[str, Any] = {"phi": phi, "n": len(g)}
        for col, name in ((_DRS_COL, "mean_drs_5d"), (_RET_COL, "mean_ret_5d"), ("excess", "mean_excess")):
            if col in g.columns:
                vals = g[col].dropna()
                row[name] = round(float(vals.mean()), 4) if len(vals) else None
        rows.append(row)
    return rows


def conditional_ic_by_phi(panel: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phi in PHI_LABELS:
        g = panel[panel["phi"] == phi]
        if len(g) < 20:
            continue
        for score_col in SCORE_COLS:
            for label_col, label_name in LABEL_COLS:
                if label_col not in g.columns:
                    continue
                ic_row = _ic_pair(g, score_col, label_col)
                rows.append(
                    {
                        "phi": phi,
                        "score": score_col,
                        "label": label_name,
                        "ic": ic_row["ic"],
                        "p": ic_row["p"],
                        "n": ic_row["n"],
                    }
                )
    return rows


def pooled_ic_overall(panel: pd.DataFrame) -> dict[str, dict[str, dict[str, float | int]]]:
    out: dict[str, dict[str, dict[str, float | int]]] = {}
    for score_col in SCORE_COLS:
        out[score_col] = {}
        for label_col, label_name in LABEL_COLS:
            if label_col not in panel.columns:
                continue
            out[score_col][label_name] = _ic_pair(panel, score_col, label_col)
    return out


def _hypothesis_pass(ic: float, *, min_ic: float = 0.02) -> bool:
    return bool(np.isfinite(ic) and ic >= min_ic)


def validate_hypotheses(
    conditional_ic: list[dict[str, Any]],
    *,
    min_ic: float = 0.02,
) -> dict[str, Any]:
    """H-G1 EXT_OPEN → drs IC+ · H-G2 HOOK_IMP/right → ret/excess IC+ · H-A1 joint decoupling."""
    ic_index: dict[tuple[str, str, str], float] = {}
    for row in conditional_ic:
        ic_index[(row["phi"], row["score"], row["label"])] = float(row["ic"])

    def _get(phi: str, score: str, label: str) -> float:
        return ic_index.get((phi, score, label), 0.0)

    h_g1_struct = _hypothesis_pass(_get("EXT_OPEN", "s_struct", "drs_5d"), min_ic=min_ic)
    h_g1_joint = _hypothesis_pass(_get("EXT_OPEN", "s_joint", "drs_5d"), min_ic=min_ic)

    hook_phis = ("HOOK_IMP", "HOOK_STRICT")
    h_g2_ret = any(
        _hypothesis_pass(_get(phi, "s_alpha", "ret_5d"), min_ic=min_ic) for phi in hook_phis
    )
    h_g2_excess = any(
        _hypothesis_pass(_get(phi, "s_alpha", "excess"), min_ic=min_ic) for phi in hook_phis
    )

    ext_drs = _get("EXT_OPEN", "s_alpha", "drs_5d")
    hook_ret = max(_get("HOOK_IMP", "s_struct", "ret_5d"), _get("HOOK_STRICT", "s_struct", "ret_5d"))
    h_a1 = abs(ext_drs - hook_ret) > 0.02 or (ext_drs > 0 and hook_ret <= 0)

    return {
        "H-G1": {
            "statement": "EXT_OPEN · left arc → drs_5d IC+ for S_struct / S_joint",
            "s_struct_ic": round(_get("EXT_OPEN", "s_struct", "drs_5d"), 4),
            "s_joint_ic": round(_get("EXT_OPEN", "s_joint", "drs_5d"), 4),
            "passed": h_g1_struct or h_g1_joint,
            "detail": "s_struct" if h_g1_struct else ("s_joint" if h_g1_joint else "none"),
        },
        "H-G2": {
            "statement": "HOOK_IMP / HOOK_STRICT · right arc → ret_5d / excess IC+ for S_alpha",
            "hook_imp_ret_ic": round(_get("HOOK_IMP", "s_alpha", "ret_5d"), 4),
            "hook_strict_ret_ic": round(_get("HOOK_STRICT", "s_alpha", "ret_5d"), 4),
            "hook_imp_excess_ic": round(_get("HOOK_IMP", "s_alpha", "excess"), 4),
            "passed": h_g2_ret or h_g2_excess,
            "detail": "ret_5d" if h_g2_ret else ("excess" if h_g2_excess else "none"),
        },
        "H-A1": {
            "statement": "S_struct vs S_alpha tracks decouple by geometry phase",
            "ext_open_alpha_vs_drs": round(ext_drs, 4),
            "hook_struct_vs_ret": round(hook_ret, 4),
            "passed": h_a1,
        },
    }


def run_geoalpha_conditional_ic(
    conn: Any | None = None,
    *,
    date_from: str | None = "2020-01-01",
    date_to: str | None = None,
    intraday: bool = False,
    intraday_from: str = "2026-04-01",
    intraday_to: str = "2026-07-03",
) -> dict[str, Any]:
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()

    if intraday:
        close, _, _ = __import__(
            "research.backtest.finpilot_local_backtest",
            fromlist=["load_price_panels"],
        ).load_price_panels(conn)
        all_dates = sorted(
            d
            for d in close.index.astype(str).tolist()
            if intraday_from <= d <= (intraday_to or date.today().isoformat())
        )
        panel, meta = build_geoalpha_intraday_panel(conn, all_dates)
    else:
        panel, meta = build_geoalpha_daily_panel(
            conn, date_from=date_from, date_to=date_to
        )

    panel = panel.dropna(subset=[_DRS_COL], how="all")
    phi_summary = phi_outcome_summary(panel)
    conditional_ic = conditional_ic_by_phi(panel)
    overall_ic = pooled_ic_overall(panel)
    hypotheses = validate_hypotheses(conditional_ic)

    return {
        "meta": {
            **meta,
            "generated_on": date.today().isoformat(),
            "date_from": date_from if not intraday else intraday_from,
            "date_to": date_to if not intraday else intraday_to,
            "intraday": intraday,
            "n_panel": len(panel),
            "n_phi_buckets": len(phi_summary),
        },
        "phi_summary": phi_summary,
        "conditional_ic": conditional_ic,
        "overall_ic": overall_ic,
        "hypotheses": hypotheses,
    }


def render_geoalpha_conditional_ic_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    phi_summary = result["phi_summary"]
    conditional_ic = result["conditional_ic"]
    overall = result["overall_ic"]
    hyp = result["hypotheses"]

    lines = [
        "# GeoAlpha conditional IC · geometry phase φ",
        "",
        "> Geometry layer φ on dual-WMA spread · Track A S_struct · Track B S_alpha · Track J S_joint",
        "",
        f"- Mode: **{'intraday' if meta.get('intraday') else 'daily-close'}**",
        f"- Window: {meta.get('date_from', '—')} → {meta.get('date_to', '—')}",
        f"- Panel n: **{meta.get('n_panel', 0)}** · generated {meta.get('generated_on', '')}",
        "",
        "## 1. Outcomes by φ",
        "",
        "| φ | n | mean drs_5d | mean ret_5d | mean excess |",
        "|---|--:|------------:|------------:|------------:|",
    ]
    for row in phi_summary:
        lines.append(
            f"| {row['phi']} | {row['n']} | "
            f"{row.get('mean_drs_5d', '—')} | {row.get('mean_ret_5d', '—')} | "
            f"{row.get('mean_excess', '—')} |"
        )

    lines.extend(["", "## 2. Pooled IC (all φ)", "", "| Score | IC vs drs_5d | IC vs ret_5d | IC vs excess |", "|-------|-------------:|-------------:|-------------:|"])
    for score_col in SCORE_COLS:
        sc = overall.get(score_col, {})
        lines.append(
            f"| {score_col} | "
            f"{sc.get('drs_5d', {}).get('ic', '—')} | "
            f"{sc.get('ret_5d', {}).get('ic', '—')} | "
            f"{sc.get('excess', {}).get('ic', '—')} |"
        )

    lines.extend(["", "## 3. Conditional IC by φ", "", "| φ | score | label | IC | p | n |", "|---|-------|-------|---:|--:|--:|"])
    for row in sorted(conditional_ic, key=lambda r: (r["phi"], r["score"], r["label"])):
        lines.append(
            f"| {row['phi']} | {row['score']} | {row['label']} | "
            f"{row['ic']:+.4f} | {row['p']:.4f} | {row['n']} |"
        )

    lines.extend(["", "## 4. Hypothesis validation", ""])
    for hid in ("H-G1", "H-G2", "H-A1"):
        h = hyp.get(hid, {})
        status = "pass" if h.get("passed") else "fail"
        lines.append(f"- **{hid}** ({status}): {h.get('statement', '')}")
        for k, v in h.items():
            if k in ("statement", "passed"):
                continue
            lines.append(f"  - {k}: {v}")

    lines.extend(
        [
            "",
            "## 5. Phase 2 follow-up",
            "",
            "- Ablation: drop P_geo gate · compare S_joint vs S_struct+S_alpha sum",
            "- Intraday φ timing: first EXT_OPEN vs last HOOK_IMP per session",
            "- Regime layer（環境層）stratification by bench_tier",
        ]
    )
    return "\n".join(lines)


def write_geoalpha_conditional_ic_report(
    result: dict[str, Any],
    out_dir: Path | None = None,
    *,
    stamp: str | None = None,
) -> tuple[Any, Any]:
    out_dir = out_dir or (PROJECT_ROOT / "reports" / "research" / "rrg")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    json_path = out_dir / f"geoalpha_conditional_ic_{stamp}.json"
    md_path = out_dir / f"geoalpha_conditional_ic_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_geoalpha_conditional_ic_markdown(result), encoding="utf-8")
    return json_path, md_path
