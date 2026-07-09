"""RRG × Regime four-axis · Phase 2 · pre-release scorer + hard overlay wiring."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Any, Callable

import pandas as pd

from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT
from research.backtest.archive.rrg_improving_pre_release_v2 import (
    V2_PENALTY_LAMBDA,
    V2_POS_WEIGHTS,
    _summarize_score_column,
    build_v2_scored_events,
    compute_pre_release_v2_score,
)
from research.backtest.rrg_improving_s3rv_executable import enrich_lifecycle_events
from research.backtest.archive.rrg_regime_four_axis_annot import (
    IS_END,
    IS_START,
    OOS_START,
    _mean,
    attach_regime_t1_columns,
)
from research.backtest.archive.rrg_regime_four_axis_sweep import _event_metrics

OOS_MIN_N = 20
OOS_MAX_DELTA_EXCESS_PP = -0.2
MIN_CAPTURE_RATE = 0.25

V2_REGIME_POS_WEIGHTS: dict[str, float] = {
    **V2_POS_WEIGHTS,
    "breadth_ns": 0.15,
    "universe_rh50": 0.10,
}
V2_REGIME_PENALTY: dict[str, float] = {
    **V2_PENALTY_LAMBDA,
    "breadth_ob": 0.88,
}


def _regime_extended_factors(row: pd.Series, base: dict[str, float]) -> dict[str, float]:
    out = dict(base)
    out["breadth_ns"] = 1.0 if bool(row.get("breadth_neutral_strong")) else 0.0
    out["breadth_ob"] = 1.0 if bool(row.get("breadth_overbought")) else 0.0
    out["universe_rh50"] = 1.0 if bool(row.get("universe_rh50")) else 0.0
    return out


def build_v2_regime_scored_events(df: pd.DataFrame) -> pd.DataFrame:
    """Pre-release v2 with Phase 1 regime factors in boost/penalty."""
    from research.backtest.archive.rrg_improving_pre_release_v2 import (
        _cross_sectional_z,
        _extended_factors,
        _outlier_block_v2,
    )

    pre = df[(~df["s3_burst"]) & (df["sig_ret"] < BURST_PCT)].copy()
    pre = pre[pre["end_q"].isin(["improving", "lagging"])].copy()
    pre = _cross_sectional_z(pre)

    factor_rows = pre.apply(
        lambda r: pd.Series(_regime_extended_factors(r, _extended_factors(r))),
        axis=1,
    ).add_prefix("f_")
    pre = pd.concat([pre.reset_index(drop=True), factor_rows.reset_index(drop=True)], axis=1)
    pre["v2_blocked"] = pre.apply(_outlier_block_v2, axis=1)

    v2 = pre.apply(
        lambda r: pd.Series(
            asdict(
                compute_pre_release_v2_score(
                    r,
                    pos_weights=V2_REGIME_POS_WEIGHTS,
                    extra_pen=V2_REGIME_PENALTY,
                )
            )
        ),
        axis=1,
    )
    v2 = v2.rename(
        columns={
            "score": "v2_score",
            "raw_mul": "v2_raw_mul",
            "tier_id": "v2_tier_id",
            "tier_label": "v2_tier_label",
            "position_weight": "v2_weight",
            "blocked": "v2_blocked_score",
        }
    )
    out = pd.concat([pre, v2], axis=1)
    out.loc[out["v2_blocked"] != "", "v2_blocked_score"] = out.loc[out["v2_blocked"] != "", "v2_blocked"]
    return out


def phase2_overlay_specs() -> list[dict[str, Any]]:
    return [
        {
            "overlay_id": "R0",
            "label": "pre-release v2 baseline",
            "track": "pre_release_v2",
            "phase1_ref": "—",
            "predicate": lambda _: True,
        },
        {
            "overlay_id": "P2-H-BREADTH-NOT-OB",
            "label": "hard gate · T-1 breadth not overbought",
            "track": "pre_release_hard",
            "phase1_ref": "G-BREADTH-NOT-OB",
            "predicate": lambda r: bool(r.get("breadth_not_ob")),
        },
        {
            "overlay_id": "P2-H-BREADTH-NS",
            "label": "hard gate · T-1 breadth neutral/strong",
            "track": "pre_release_hard",
            "phase1_ref": "G-BREADTH-NS",
            "predicate": lambda r: bool(r.get("breadth_neutral_strong")),
        },
        {
            "overlay_id": "P2-H-C1",
            "label": "hard gate · S2 · bench≥0 ∧ breadth NS",
            "track": "s2_hard",
            "phase1_ref": "C1",
            "predicate": lambda r: bool(
                r.get("s2_setup_core") and r.get("bench_b0") and r.get("breadth_neutral_strong")
            ),
        },
        {
            "overlay_id": "P2-H-RH50",
            "label": "hard gate · T-1 universe rotation health≥50%",
            "track": "pre_release_hard",
            "phase1_ref": "G-UNIVERSE-RH50",
            "predicate": lambda r: bool(r.get("universe_rh50")),
        },
        {
            "overlay_id": "P2-H-STAGE2-MID",
            "label": "hard gate · T-1 stage2 pass≥45%",
            "track": "pre_release_hard",
            "phase1_ref": "H-RC-1-retry",
            "predicate": lambda r: bool(r.get("stage2_mid_plus")),
        },
        {
            "overlay_id": "P2-V2-REGIME",
            "label": "v2 scorer + regime factors",
            "track": "pre_release_v2_regime",
            "phase1_ref": "scorer",
            "predicate": lambda _: True,
        },
        {
            "overlay_id": "P2-S3-B0",
            "label": "S3 burst · signal-day bench≥0",
            "track": "s3_hard",
            "phase1_ref": "G-B0",
            "predicate": lambda r: bool(r.get("s3_burst") and r.get("bench_b0")),
        },
    ]


def _split_mask(dates: pd.Series, label: str) -> pd.Series:
    if label == "FULL":
        return pd.Series(True, index=dates.index)
    if label == "IS":
        return dates.between(IS_START, IS_END)
    if label == "OOS":
        return dates >= OOS_START
    raise ValueError(label)


def _delta_pp(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    try:
        fa, fb = float(a), float(b)
        if fa != fa or fb != fb:
            return None
        return round(fa - fb, 4)
    except (TypeError, ValueError):
        return None


def _metrics_with_excess(part: pd.DataFrame) -> dict[str, Any]:
    rows = part.to_dict(orient="records")
    m = _event_metrics(rows)
    if "alpha_nxt" in part.columns:
        m["mean_excess_nxt_pct"] = _mean(part["alpha_nxt"].tolist())
    elif "nxt_ret" in part.columns and "bench_nxt_ret" in part.columns:
        ex = part["nxt_ret"].astype(float) - part["bench_nxt_ret"].astype(float)
        m["mean_excess_nxt_pct"] = _mean(ex.tolist())
    return m


def _portfolio_block(scored: pd.DataFrame, all_dates: list[str]) -> dict[str, Any]:
    return _summarize_score_column(
        scored,
        all_dates,
        score_col="v2_score",
        weight_col="v2_weight",
        block_col="v2_blocked_score",
    )


def _evaluate_pre_release_overlay(
    scored: pd.DataFrame,
    *,
    predicate: Callable[[pd.Series], bool],
    all_dates: list[str],
    r0_full_n: int,
    spec: dict[str, Any],
) -> dict[str, Any]:
    active = scored[scored["v2_blocked_score"] == ""].copy()
    sub = active[active.apply(predicate, axis=1)].copy()
    block = _portfolio_block(sub, all_dates)
    splits: dict[str, Any] = {}
    for label in ("FULL", "IS", "OOS"):
        part = sub[_split_mask(sub["date"], label)]
        m = _metrics_with_excess(part)
        m["portfolio_full"] = _portfolio_block(part, all_dates).get("portfolio_full")
        splits[label] = m

    full_tier = next((t for t in block.get("tier_stats", []) if t["tier_id"] == "full"), {})
    return {
        "overlay_id": spec["overlay_id"],
        "label": spec["label"],
        "track": spec["track"],
        "phase1_ref": spec["phase1_ref"],
        "capture_rate_full": round(len(sub) / r0_full_n, 4) if r0_full_n else None,
        "full_tier_mean_nxt_pct": full_tier.get("mean_nxt_pct"),
        "FULL": splits["FULL"],
        "IS": splits["IS"],
        "OOS": splits["OOS"],
    }


def _evaluate_event_overlay(
    df: pd.DataFrame,
    *,
    universe_mask: pd.Series,
    predicate: Callable[[pd.Series], bool],
    spec: dict[str, Any],
) -> dict[str, Any]:
    base = df[universe_mask].copy()
    sub = base[base.apply(predicate, axis=1)].copy()
    splits: dict[str, Any] = {}
    for label in ("FULL", "IS", "OOS"):
        r0_part = base[_split_mask(base["date"], label)]
        sub_part = sub[_split_mask(sub["date"], label)]
        r0_m = _metrics_with_excess(r0_part)
        sub_m = _metrics_with_excess(sub_part)
        splits[label] = {
            **sub_m,
            "delta_nxt_vs_r0_pp": _delta_pp(sub_m.get("mean_nxt_ret_pct"), r0_m.get("mean_nxt_ret_pct")),
            "delta_excess_vs_r0_pp": _delta_pp(
                sub_m.get("mean_excess_nxt_pct"), r0_m.get("mean_excess_nxt_pct")
            ),
        }
    return {
        "overlay_id": spec["overlay_id"],
        "label": spec["label"],
        "track": spec["track"],
        "phase1_ref": spec["phase1_ref"],
        "capture_rate_full": round(len(sub) / len(base), 4) if len(base) else None,
        "FULL": splits["FULL"],
        "IS": splits["IS"],
        "OOS": splits["OOS"],
    }


def _pass_verdict(row: dict[str, Any], *, r0_row: dict[str, Any]) -> dict[str, Any]:
    if row.get("overlay_id") == "R0":
        return {"passed": None, "reasons": ["baseline"]}
    if row.get("track") in {"s3_hard", "s2_hard"}:
        oos = row.get("OOS") or {}
        passed = (
            int(oos.get("n_events") or 0) >= OOS_MIN_N
            and (oos.get("delta_excess_vs_r0_pp") is None or float(oos["delta_excess_vs_r0_pp"]) >= OOS_MAX_DELTA_EXCESS_PP)
        )
        return {
            "passed": passed,
            "reasons": ["s2/s3 event overlay pass" if passed else "OOS fail"],
        }

    oos = row.get("OOS") or {}
    reasons: list[str] = []
    passed = True
    capture = row.get("capture_rate_full")
    if capture is not None and float(capture) < MIN_CAPTURE_RATE:
        passed = False
        reasons.append(f"capture {capture} < {MIN_CAPTURE_RATE}")
    if int(oos.get("n_events") or 0) < OOS_MIN_N:
        passed = False
        reasons.append(f"OOS n={oos.get('n_events')} < {OOS_MIN_N}")
    delta_ex = oos.get("delta_excess_vs_r0_pp")
    if delta_ex is not None and float(delta_ex) < OOS_MAX_DELTA_EXCESS_PP:
        passed = False
        reasons.append(f"OOS Δexcess {delta_ex}pp")
    r0_sh = ((r0_row.get("OOS") or {}).get("portfolio_full") or {}).get("sharpe")
    oos_sh = (oos.get("portfolio_full") or {}).get("sharpe")
    if r0_sh is not None and oos_sh is not None and float(oos_sh) < float(r0_sh) - 0.5:
        passed = False
        reasons.append(f"OOS Sharpe {oos_sh} << R0 {r0_sh}")
    if passed:
        reasons.append("OOS within band vs R0")
    return {"passed": passed, "reasons": reasons}


def _attach_deltas(row: dict[str, Any], r0_row: dict[str, Any]) -> None:
    if row.get("track") in {"s3_hard", "s2_hard"}:
        return
    for label in ("FULL", "IS", "OOS"):
        part = row.get(label) or {}
        r0_part = r0_row.get(label) or {}
        part["delta_nxt_vs_r0_pp"] = _delta_pp(
            part.get("mean_nxt_ret_pct"), r0_part.get("mean_nxt_ret_pct")
        )
        part["delta_excess_vs_r0_pp"] = _delta_pp(
            part.get("mean_excess_nxt_pct"), r0_part.get("mean_excess_nxt_pct")
        )


def run_rrg_regime_four_axis_phase2(conn: sqlite3.Connection) -> dict[str, Any]:
    df, meta = enrich_lifecycle_events(conn)
    df = attach_regime_t1_columns(conn, df)
    all_dates = sorted(df["date"].astype(str).unique().tolist())

    scored_baseline = build_v2_scored_events(df)
    scored_regime = build_v2_regime_scored_events(df)
    r0_full_n = int((scored_baseline["v2_blocked_score"] == "").sum())

    rows: list[dict[str, Any]] = []
    for spec in phase2_overlay_specs():
        pred = spec["predicate"]
        track = spec["track"]
        if track == "pre_release_v2_regime":
            row = _evaluate_pre_release_overlay(
                scored_regime,
                predicate=pred,
                all_dates=all_dates,
                r0_full_n=r0_full_n,
                spec=spec,
            )
        elif track == "pre_release_v2":
            row = _evaluate_pre_release_overlay(
                scored_baseline,
                predicate=pred,
                all_dates=all_dates,
                r0_full_n=r0_full_n,
                spec=spec,
            )
        elif track == "pre_release_hard":
            row = _evaluate_pre_release_overlay(
                scored_baseline,
                predicate=pred,
                all_dates=all_dates,
                r0_full_n=r0_full_n,
                spec=spec,
            )
        elif track == "s2_hard":
            row = _evaluate_event_overlay(
                df,
                universe_mask=df["s2_setup_core"].astype(bool),
                predicate=pred,
                spec=spec,
            )
        elif track == "s3_hard":
            row = _evaluate_event_overlay(
                df,
                universe_mask=df["s3_burst"].astype(bool),
                predicate=lambda r: bool(r.get("bench_b0")),
                spec=spec,
            )
        else:
            raise ValueError(track)
        rows.append(row)

    r0_row = next(r for r in rows if r["overlay_id"] == "R0")
    for row in rows:
        _attach_deltas(row, r0_row)
        row["verdict"] = _pass_verdict(row, r0_row=r0_row)

    winners = [r for r in rows if r["overlay_id"] != "R0" and (r.get("verdict") or {}).get("passed")]
    winners.sort(
        key=lambda r: float((r.get("OOS") or {}).get("delta_excess_vs_r0_pp") or -999),
        reverse=True,
    )

    return {
        "schema": "rrg_regime_four_axis_phase2-v1",
        "topic": "rrg-regime-four-axis-annot",
        "phase": "phase2_overlay",
        "parent_research": "rrg-improving-lifecycle",
        "meta": meta,
        "v2_regime_weights": V2_REGIME_POS_WEIGHTS,
        "r0_pre_release_n": r0_full_n,
        "n_overlays": len(rows),
        "n_passed": len(winners),
        "winners": [
            {
                "overlay_id": w["overlay_id"],
                "track": w["track"],
                "capture_rate_full": w.get("capture_rate_full"),
                "OOS_delta_excess_pp": (w.get("OOS") or {}).get("delta_excess_vs_r0_pp"),
                "OOS_sharpe": ((w.get("OOS") or {}).get("portfolio_full") or {}).get("sharpe"),
            }
            for w in winners
        ],
        "overlays": rows,
    }


def render_rrg_regime_four_axis_phase2_md(payload: dict[str, Any]) -> str:
    lines = [
        "# RRG × Regime four-axis · Phase 2 · pre-release overlay",
        "",
        f"pre-release v2 active n **{payload.get('r0_pre_release_n')}** · "
        f"overlays **{payload.get('n_overlays')}** · passed **{payload.get('n_passed')}**",
        "",
        "## Winners",
        "",
    ]
    winners = payload.get("winners") or []
    if winners:
        lines += [
            "| overlay | track | capture | OOS Δexcess | OOS Sharpe |",
            "|---------|-------|---------|-------------|------------|",
        ]
        for w in winners:
            lines.append(
                f"| {w['overlay_id']} | {w['track']} | {w.get('capture_rate_full')} | "
                f"{w.get('OOS_delta_excess_pp')} | {w.get('OOS_sharpe')} |"
            )
    else:
        lines.append("_無 overlay 通過 Phase 2 pass 規則_")

    lines += [
        "",
        "## Pre-release overlays · FULL",
        "",
        "| overlay | track | n | mean_nxt | Δexcess vs R0 | capture | pass |",
        "|---------|-------|---|----------|---------------|---------|------|",
    ]
    for row in payload.get("overlays") or []:
        if row.get("track") not in {"pre_release_hard", "pre_release_v2", "pre_release_v2_regime"}:
            continue
        full = row.get("FULL") or {}
        lines.append(
            f"| {row['overlay_id']} | {row['track']} | {full.get('n_events')} | "
            f"{full.get('mean_nxt_ret_pct')} | {full.get('delta_excess_vs_r0_pp')} | "
            f"{row.get('capture_rate_full')} | {(row.get('verdict') or {}).get('passed')} |"
        )

    lines += [
        "",
        "## S2 / S3 hard overlays · FULL",
        "",
        "| overlay | n | mean_nxt | Δexcess vs R0 | capture |",
        "|---------|---|----------|---------------|---------|",
    ]
    for row in payload.get("overlays") or []:
        if row.get("track") not in {"s2_hard", "s3_hard"}:
            continue
        full = row.get("FULL") or {}
        lines.append(
            f"| {row['overlay_id']} | {full.get('n_events')} | {full.get('mean_nxt_ret_pct')} | "
            f"{full.get('delta_excess_vs_r0_pp')} | {row.get('capture_rate_full')} |"
        )

    lines += [
        "",
        "## 解讀",
        "",
        "- **P2-H-***：pre-release 事件硬篩 · v2 分數不變。",
        "- **P2-V2-REGIME**：v2 boost `breadth_ns`/`universe_rh50` · penalty `breadth_ob`。",
        "- **P2-S3-B0**：S3 觀察 gate · 僅用 signal-day bench≥0。",
        "",
    ]
    return "\n".join(lines)
