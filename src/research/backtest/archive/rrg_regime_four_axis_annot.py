"""RRG Improving lifecycle × Regime four-axis · Phase 0 annotation (no rule change)."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from market_breadth_ma import BREADTH_ZONES_ORDER, build_breadth_panel
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_improving_lifecycle_backtest import build_lifecycle_events
from regime_snapshot import _load_ix_bars
from rrg_rotation import compute_rrg_panel
from stage_analysis import classify_ix_trend_posture, vectorized_minervini_pass_pct

IS_START = "2024-01-01"
IS_END = "2025-12-31"
OOS_START = "2026-01-01"

STAGE2_HIGH_PCT = 60.0
STAGE2_LOW_PCT = 40.0


def _finite_floats(values: list[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:
            out.append(f)
    return out


def _prior_trade_date(full_dates: list[str], d: str) -> str | None:
    prior = [x for x in full_dates if x < d]
    return prior[-1] if prior else None


def _bench_bucket(bench_today_ret: float | None) -> str:
    if bench_today_ret is None or bench_today_ret != bench_today_ret:
        return "unknown"
    if bench_today_ret >= 2.0:
        return "b2"
    if bench_today_ret >= 1.0:
        return "b1"
    if bench_today_ret >= 0.0:
        return "b0"
    return "neg"


def _stage2_bucket(pass_pct: float | None) -> str:
    if pass_pct is None or pass_pct != pass_pct:
        return "unknown"
    if pass_pct >= STAGE2_HIGH_PCT:
        return "high"
    if pass_pct < STAGE2_LOW_PCT:
        return "low"
    return "mid"


def _mean(values: list[Any]) -> float | None:
    nums = _finite_floats(values)
    return round(sum(nums) / len(nums), 4) if nums else None


def _win_rate(values: list[Any]) -> float | None:
    nums = _finite_floats(values)
    if not nums:
        return None
    return round(sum(1 for x in nums if x > 0) / len(nums), 4)


def _outcome_compare(
    events: list[dict[str, Any]],
    *,
    flag_field: str | None = None,
    flag_fn: Callable[[dict[str, Any]], bool] | None = None,
    outcome_key: str = "nxt_ret",
    min_flagged_n: int = 20,
    pass_delta_pp: float | None = None,
    pass_direction: str = "ge",
) -> dict[str, Any]:
    if flag_fn is None:
        field = flag_field or ""

        def flag_fn(row: dict[str, Any]) -> bool:
            return bool(row.get(field))

    on = [e for e in events if flag_fn(e)]
    off = [e for e in events if not flag_fn(e)]
    on_mean = _mean([e.get(outcome_key) for e in on])
    off_mean = _mean([e.get(outcome_key) for e in off])
    delta = round(on_mean - off_mean, 4) if on_mean is not None and off_mean is not None else None
    passed: bool | None = None
    if delta is not None and pass_delta_pp is not None and len(on) >= min_flagged_n:
        if pass_direction == "ge":
            passed = delta >= pass_delta_pp
        else:
            passed = delta <= pass_delta_pp
    return {
        "flag_field": flag_field,
        "flagged_n": len(on),
        "unflagged_n": len(off),
        "flagged_mean": on_mean,
        "unflagged_mean": off_mean,
        "delta_pp": delta,
        "flagged_win_rate": _win_rate([e.get(outcome_key) for e in on]),
        "unflagged_win_rate": _win_rate([e.get(outcome_key) for e in off]),
        "min_flagged_n": min_flagged_n,
        "pass_delta_pp": pass_delta_pp,
        "pass": passed,
    }


def _universe_rrg_stats_from_quad(quad: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in quad.index:
        row = quad.loc[d]
        counts = {"leading": 0, "weakening": 0, "lagging": 0, "improving": 0}
        for q in row:
            if pd.isna(q):
                continue
            key = str(q)
            if key in counts:
                counts[key] += 1
        total = sum(counts.values())
        if total <= 0:
            continue
        pcts = {k: round(v / total * 100.0, 1) for k, v in counts.items()}
        dominant = max(counts, key=counts.get)
        out[str(d)] = {
            "dominant_quadrant": dominant,
            "rotation_health_pct": round(pcts["leading"] + pcts["improving"], 1),
            "improving_pct": pcts["improving"],
            "leading_pct": pcts["leading"],
            "weakening_pct": pcts["weakening"],
            "lagging_pct": pcts["lagging"],
        }
    return out


def _build_trend_posture_cache(conn: sqlite3.Connection, dates: list[str]) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    for d in sorted(set(dates)):
        df = _load_ix_bars(conn, d, code="IX0001")
        if len(df) < 200:
            continue
        axis = classify_ix_trend_posture(df)
        cache[d] = {
            "trend_posture": axis.get("trend_posture"),
            "weinstein_stage": int(axis.get("stage") or 0),
            "trend_score": axis.get("trend_score"),
        }
    return cache


def _build_stage2_pass_cache(close: pd.DataFrame) -> dict[str, float]:
    close = close.copy()
    close.index = close.index.astype(str)
    pct_series = vectorized_minervini_pass_pct(close, min_pass=7)
    return {str(d): round(float(v) * 100.0, 1) for d, v in pct_series.items()}


def annotate_lifecycle_event(
    row: dict[str, Any],
    *,
    prior_date: str | None,
    breadth_zone_200: str | None,
    trend_posture: str | None,
    weinstein_stage: int | None,
    stage2_pass_pct: float | None,
    universe_rrg: dict[str, Any] | None,
) -> dict[str, Any]:
    bench_today = row.get("bench_today_ret")
    bench_nxt = row.get("bench_nxt_ret")
    nxt_ret = row.get("nxt_ret")
    excess_nxt = (
        round(float(nxt_ret) - float(bench_nxt), 4)
        if nxt_ret is not None and bench_nxt is not None and nxt_ret == nxt_ret and bench_nxt == bench_nxt
        else None
    )
    uni = universe_rrg or {}
    return {
        "date": str(row["date"]),
        "stock_id": str(row["sid"]),
        "end_q": str(row.get("end_q") or ""),
        "prev_q": str(row.get("prev_q") or ""),
        "sig_ret": row.get("sig_ret"),
        "nxt_ret": nxt_ret,
        "excess_nxt": excess_nxt,
        "bench_today_ret": bench_today,
        "bench_nxt_ret": bench_nxt,
        "bench_bucket": _bench_bucket(bench_today),
        "s0_fresh_flip": bool(row.get("s0_fresh_flip")),
        "s1_early": bool(row.get("s1_early")),
        "s2_setup_core": bool(row.get("s2_setup_core")),
        "s3_burst": bool(row.get("s3_burst")),
        "improve_streak": int(row.get("improve_streak") or 0),
        "rv": row.get("rv"),
        "regime_t1_as_of": prior_date,
        "breadth_zone_200": breadth_zone_200 or "unknown",
        "trend_posture": trend_posture or "unknown",
        "weinstein_stage": weinstein_stage,
        "stage2_pass_pct": stage2_pass_pct,
        "stage2_bucket": _stage2_bucket(stage2_pass_pct),
        "universe_dominant_quadrant": uni.get("dominant_quadrant"),
        "universe_rotation_health_pct": uni.get("rotation_health_pct"),
        "universe_improving_pct": uni.get("improving_pct"),
        "bench_b0": bench_today is not None and bench_today >= 0,
        "bench_neg": bench_today is not None and bench_today < 0,
        "weinstein_stage2": weinstein_stage == 2,
        "weinstein_stage4": weinstein_stage == 4,
        "breadth_neutral_strong": (breadth_zone_200 or "") in {"neutral", "strong"},
        "breadth_oversold_weak": (breadth_zone_200 or "") in {"oversold", "weak"},
        "breadth_overbought": breadth_zone_200 == "overbought",
        "stock_weakening": str(row.get("end_q") or "") == "weakening",
        "stock_leading": str(row.get("end_q") or "") == "leading",
    }


def _filter_stage(events: list[dict[str, Any]], flag: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get(flag)]


def _hypothesis_suite(events: list[dict[str, Any]]) -> dict[str, Any]:
    s0 = _filter_stage(events, "s0_fresh_flip")
    s2 = _filter_stage(events, "s2_setup_core")
    s3 = _filter_stage(events, "s3_burst")

    return {
        "H-RA-1": {
            "statement": "S0 fresh flip · Weinstein stage 2 vs stage 4 · D+1 nxt_ret",
            "compare": _outcome_compare(
                s0,
                flag_fn=lambda e: bool(e.get("weinstein_stage2")),
                min_flagged_n=20,
                pass_delta_pp=0.15,
            ),
            "baseline_n_stage4": sum(1 for e in s0 if e.get("weinstein_stage4")),
        },
        "H-RA-2": {
            "statement": "S2 setup core · bench≥0 vs bench<0 · D+1 nxt_ret",
            "compare": _outcome_compare(
                s2,
                flag_field="bench_b0",
                min_flagged_n=100,
                pass_delta_pp=0.10,
            ),
        },
        "H-RA-3": {
            "statement": "stock weakening · Weinstein stage 4 vs stage 2 · D+1 nxt_ret",
            "compare": _outcome_compare(
                [e for e in events if e.get("stock_weakening")],
                flag_fn=lambda e: bool(e.get("weinstein_stage4")),
                min_flagged_n=20,
                pass_delta_pp=0.0,
                pass_direction="le",
            ),
        },
        "H-RB-1": {
            "statement": "S2 setup core · breadth neutral/strong vs oversold/weak (T-1) · D+1",
            "compare": _outcome_compare(
                s2,
                flag_field="breadth_neutral_strong",
                min_flagged_n=50,
                pass_delta_pp=0.10,
            ),
        },
        "H-RB-2": {
            "statement": "stock weakening · breadth overbought (T-1) · D+1 nxt_ret",
            "compare": _outcome_compare(
                [e for e in events if e.get("stock_weakening")],
                flag_field="breadth_overbought",
                min_flagged_n=20,
                pass_delta_pp=0.0,
                pass_direction="le",
            ),
        },
        "H-RC-1": {
            "statement": "S2 setup core · stage2 participation high vs low (T-1) · D+1",
            "compare": _outcome_compare(
                s2,
                flag_fn=lambda e: e.get("stage2_bucket") == "high",
                min_flagged_n=50,
                pass_delta_pp=0.20,
            ),
            "low_bucket_n": sum(1 for e in s2 if e.get("stage2_bucket") == "low"),
        },
        "H-RD-1": {
            "statement": "S0 fresh · universe improving_pct≥40 vs <40 (T-1) · D+1",
            "compare": _outcome_compare(
                s0,
                flag_fn=lambda e: float(e.get("universe_improving_pct") or 0) >= 40.0,
                min_flagged_n=20,
                pass_delta_pp=0.20,
            ),
        },
        "H-S3-BENCH": {
            "statement": "S3 burst · D+1 nxt_ret when bench_nxt≥0 vs <0 (eval context only)",
            "compare": _outcome_compare(
                s3,
                flag_fn=lambda e: float(e.get("bench_nxt_ret") or -999) >= 0,
                min_flagged_n=20,
            ),
            "eval_only": True,
        },
    }


def _stage_summary(events: list[dict[str, Any]], flag: str, label: str) -> dict[str, Any]:
    sub = _filter_stage(events, flag)
    return {
        "stage": label,
        "n": len(sub),
        "nxt_mean_pct": _mean([e.get("nxt_ret") for e in sub]),
        "nxt_win_rate": _win_rate([e.get("nxt_ret") for e in sub]),
        "bench_b0_share": round(sum(1 for e in sub if e.get("bench_b0")) / len(sub), 4) if sub else None,
    }


def attach_regime_t1_columns(conn: sqlite3.Connection, df: pd.DataFrame) -> pd.DataFrame:
    """Attach T-1 Regime four-axis columns to a lifecycle dataframe (PIT)."""
    if df.empty:
        return df

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    start = str(df["date"].min())
    end = str(df["date"].max())

    breadth_panel = build_breadth_panel(conn, date_start=start, date_end=end)
    breadth_by_date = {str(r.trade_date): str(r.zone_200) for r in breadth_panel.itertuples()}
    stage2_by_date = _build_stage2_pass_cache(close)

    bench = load_benchmark_close(conn)
    _, _, quad = compute_rrg_panel(close, bench)
    universe_rrg_by_date = _universe_rrg_stats_from_quad(quad)

    signal_dates = df["date"].astype(str).unique().tolist()
    prior_dates = [_prior_trade_date(full_dates, d) for d in signal_dates]
    prior_map = dict(zip(signal_dates, prior_dates, strict=True))
    trend_cache = _build_trend_posture_cache(conn, [d for d in prior_dates if d])

    out = df.copy()
    out["regime_t1_as_of"] = out["date"].astype(str).map(prior_map)
    out["breadth_zone_200"] = out["regime_t1_as_of"].map(
        lambda d: breadth_by_date.get(str(d or ""), "unknown")
    )
    out["stage2_pass_pct"] = out["regime_t1_as_of"].map(
        lambda d: stage2_by_date.get(str(d or ""))
    )
    out["stage2_bucket"] = out["stage2_pass_pct"].map(_stage2_bucket)
    out["trend_posture"] = out["regime_t1_as_of"].map(
        lambda d: str((trend_cache.get(str(d or "")) or {}).get("trend_posture") or "unknown")
    )
    out["weinstein_stage"] = out["regime_t1_as_of"].map(
        lambda d: (trend_cache.get(str(d or "")) or {}).get("weinstein_stage")
    )

    def _uni(col: str, d: Any) -> Any:
        uni = universe_rrg_by_date.get(str(d or "")) or {}
        return uni.get(col)

    out["universe_rotation_health_pct"] = out["regime_t1_as_of"].map(
        lambda d: _uni("rotation_health_pct", d)
    )
    out["universe_improving_pct"] = out["regime_t1_as_of"].map(lambda d: _uni("improving_pct", d))
    out["breadth_neutral_strong"] = out["breadth_zone_200"].isin(["neutral", "strong"])
    out["breadth_oversold_weak"] = out["breadth_zone_200"].isin(["oversold", "weak"])
    out["breadth_overbought"] = out["breadth_zone_200"] == "overbought"
    out["breadth_not_ob"] = ~out["breadth_overbought"]
    out["bench_b0"] = out["bench_today_ret"] >= 0
    out["weinstein_stage2"] = out["weinstein_stage"] == 2
    out["universe_rh50"] = out["universe_rotation_health_pct"].fillna(0) >= 50.0
    out["stage2_mid_plus"] = out["stage2_pass_pct"].fillna(0) >= 45.0
    return out


def run_rrg_regime_four_axis_annot(
    conn: sqlite3.Connection,
    *,
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    df, meta = build_lifecycle_events(conn)
    if df.empty:
        raise ValueError("no lifecycle events from build_lifecycle_events")

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    start = date_start or str(meta["date_start"])
    end = date_end or str(meta["date_end"])
    df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    if df.empty:
        raise ValueError(f"no events in [{start}, {end}]")

    breadth_panel = build_breadth_panel(conn, date_start=start, date_end=end)
    breadth_by_date = {str(r.trade_date): str(r.zone_200) for r in breadth_panel.itertuples()}
    stage2_by_date = _build_stage2_pass_cache(close)

    bench = load_benchmark_close(conn)
    _, _, quad = compute_rrg_panel(close, bench)
    universe_rrg_by_date = _universe_rrg_stats_from_quad(quad)

    signal_dates = df["date"].astype(str).unique().tolist()
    t1_dates = [_prior_trade_date(full_dates, d) for d in signal_dates]
    trend_cache = _build_trend_posture_cache(conn, [d for d in t1_dates if d])

    events: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        sig_d = str(row["date"])
        prior = _prior_trade_date(full_dates, sig_d)
        trend = trend_cache.get(prior or "", {})
        events.append(
            annotate_lifecycle_event(
                row,
                prior_date=prior,
                breadth_zone_200=breadth_by_date.get(prior or "", "unknown"),
                trend_posture=str(trend.get("trend_posture") or "unknown"),
                weinstein_stage=trend.get("weinstein_stage"),
                stage2_pass_pct=stage2_by_date.get(prior or ""),
                universe_rrg=universe_rrg_by_date.get(prior or ""),
            )
        )

    is_events = [e for e in events if IS_START <= e["date"] <= IS_END]
    oos_events = [e for e in events if e["date"] >= OOS_START]

    return {
        "schema": "rrg_regime_four_axis_annot-v1",
        "topic": "rrg-regime-four-axis-annot",
        "phase": "phase0_annot",
        "parent_research": "rrg-improving-lifecycle",
        "date_start": start,
        "date_end": end,
        "regime_timing": {
            "breadth_zone_200": "T-1 prior trading day close (PIT)",
            "trend_posture": "T-1 prior trading day close (PIT)",
            "stage2_participation": "T-1 prior trading day close (PIT)",
            "universe_rrg_rotation": "T-1 prior trading day close (PIT)",
            "bench_today_ret": "signal-day close (PIT)",
            "bench_nxt_ret": "eval-only · not for entry gate",
        },
        "meta": meta,
        "n_events": len(events),
        "stage_summary": [
            _stage_summary(events, "s0_fresh_flip", "S0 fresh flip"),
            _stage_summary(events, "s1_early", "S1 improving 1-3d"),
            _stage_summary(events, "s2_setup_core", "S2 setup core"),
            _stage_summary(events, "s3_burst", "S3 improving burst"),
        ],
        "hypotheses": _hypothesis_suite(events),
        "breadth_zone_counts": {
            z: sum(1 for e in events if e.get("breadth_zone_200") == z) for z in (*BREADTH_ZONES_ORDER, "unknown")
        },
        "splits": {
            "FULL": {"n_events": len(events), "hypotheses": _hypothesis_suite(events)},
            "IS": {
                "date_start": IS_START,
                "date_end": IS_END,
                "n_events": len(is_events),
                "hypotheses": _hypothesis_suite(is_events),
            },
            "OOS": {
                "date_start": OOS_START,
                "date_end": end,
                "n_events": len(oos_events),
                "hypotheses": _hypothesis_suite(oos_events),
            },
        },
        "events": events,
    }


def render_rrg_regime_four_axis_annot_md(payload: dict[str, Any]) -> str:
    hypos = payload.get("hypotheses") or {}
    stages = payload.get("stage_summary") or []
    timing = payload.get("regime_timing") or {}

    lines = [
        "# RRG × Regime four-axis · Phase 0 描述統計",
        "",
        f"區間：**{payload['date_start']}** → **{payload['date_end']}** · "
        f"**{payload['n_events']}** lifecycle events · schema `{payload.get('schema')}`",
        "",
        "## PIT 標記時點",
        "",
    ]
    for key, note in timing.items():
        lines.append(f"- `{key}`：{note}")

    lines += [
        "",
        "## Stage 摘要（D+1 close）",
        "",
        "| Stage | n | D+1 mean | win_rate | bench≥0 share |",
        "|-------|---|----------|----------|---------------|",
    ]
    for row in stages:
        lines.append(
            f"| {row['stage']} | {row['n']} | {row['nxt_mean_pct']} | "
            f"{row['nxt_win_rate']} | {row['bench_b0_share']} |"
        )

    lines += [
        "",
        "## 預登記假設 · FULL",
        "",
        "| ID | flagged n | flagged mean | unflagged mean | Δ pp | pass |",
        "|----|-----------|--------------|----------------|------|------|",
    ]
    for hid, block in hypos.items():
        cmp = block.get("compare") or {}
        ev = " (eval)" if block.get("eval_only") else ""
        lines.append(
            f"| {hid}{ev} | {cmp.get('flagged_n')} | {cmp.get('flagged_mean')} | "
            f"{cmp.get('unflagged_mean')} | {cmp.get('delta_pp')} | {cmp.get('pass')} |"
        )

    lines += [
        "",
        "## Breadth zone 覆蓋（T-1）",
        "",
    ]
    for zone, n in (payload.get("breadth_zone_counts") or {}).items():
        lines.append(f"- `{zone}`：{n}")

    lines += [
        "",
        "## 解讀備註",
        "",
        "- 本報告 **不改** RRG mono / lifecycle 規則 · 僅標記四軸環境與 D+1 結果。",
        "- `bench_nxt_ret` 僅供 H-S3-BENCH 事後分層 · **不可**作進場 gate。",
        "- Phase 1+ 才對通過 Phase 0 的假設做 sweep / OOS holdout。",
        "",
    ]
    return "\n".join(lines)
