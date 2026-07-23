#!/usr/bin/env python3
"""凱基-松山 40M裸 L1H10 優勢延伸（觀察／衛星假說；不降級新店主契約）。

Research-only · 1槽 · 成本 0bps + 60bps · 不改 Strategy／Order。

  PYTHONPATH=src .venv/bin/python scripts/research/run_songshan_h10_edge_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_songshan_h10_edge_study.py --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from copytrade.branch_signals import (  # noqa: E402
    DEFAULT_TRADER_ID_FUBON_XINDIAN,
)
from copytrade.signals import CopytradeSignal  # noqa: E402
from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.amount_rrg_copytrade_funnel_study import (  # noqa: E402
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    build_funnel_feature_index,
)
from research.backtest.copytrade_backtest import (  # noqa: E402
    DEFAULT_SIGNAL_CAPITAL_NTD,
    select_executed_signal_days,
)
from research.backtest.fubon_xindian_filter_study import (  # noqa: E402
    FORWARD_START,
    FilterConfig,
    _readiness_slice,
    _run_readiness_config,
    build_config_grouped,
    build_raw_xindian_amount_grouped,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "kgi-songshan-copytrade"
FILE_STEM = "songshan_h10_edge_study"

TRADER_SONGSHAN = "9217"
TRADER_XINDIAN = DEFAULT_TRADER_ID_FUBON_XINDIAN

# Amount buckets (lo inclusive, hi exclusive; None = open).
BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("10-40", 10e6, 40e6),
    ("40-80", 40e6, 80e6),
    ("80-150", 80e6, 150e6),
    ("150-300", 150e6, 300e6),
    (">=300", 300e6, None),
)

FOLD_BOUNDS = (
    ("2024H2", "2024-07-01", "2025-01-01"),
    ("2025H1", "2025-01-01", "2025-07-01"),
    ("2025H2", "2025-07-01", "2026-01-01"),
    ("2026H1+", "2026-01-01", FORWARD_START),
)

# Hypotheses under study (Songshan = observation / satellite).
SS_BARE_H10 = FilterConfig(
    metric="amount",
    threshold=40_000_000.0,
    min_avg_volume_shares=200_000.0,
    w20_momentum_min=None,
    rs_ratio_min=None,
    rrg_quadrant=None,
    top_n=1,
    entry_lag=0,
    hold=10,
    n_slots=1,
)
XD_LEADING_H8 = FilterConfig(
    metric="amount",
    threshold=50_000_000.0,
    min_avg_volume_shares=200_000.0,
    w20_momentum_min=None,
    rs_ratio_min=None,
    rrg_quadrant="leading",
    top_n=1,
    entry_lag=0,
    hold=8,
    n_slots=1,
)
XD_RS_H10 = FilterConfig(
    metric="amount",
    threshold=80_000_000.0,
    min_avg_volume_shares=200_000.0,
    w20_momentum_min=None,
    rs_ratio_min=100.0,
    rrg_quadrant=None,
    top_n=1,
    entry_lag=0,
    hold=10,
    n_slots=1,
)


def _sharpe_like(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    sd = pstdev(xs)
    if sd <= 1e-12:
        return None
    return round(mean(xs) / sd, 4)


def _leg_amount(
    day: str,
    leg: CopytradeSignal,
    features: dict[tuple[str, str], dict[str, Any]],
) -> float | None:
    feat = features.get((day, str(leg.stock_id)))
    if feat is None or feat.get("signal_close") is None:
        return None
    return float(leg.share_delta) * float(feat["signal_close"])


def _enrich_slice(
    conn,
    day_dicts: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    hold: int,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> dict[str, Any]:
    slice_ = _readiness_slice(conn, day_dicts, start=start, end=end, n_slots=1)
    complete = [
        row
        for row in day_dicts
        if row.get("status") == "complete"
        and start <= str(row.get("signal_date") or "") < end
    ]
    executed, _ = select_executed_signal_days(complete, n_slots=1)
    rets = [float(r["return_pct"]) for r in executed]
    pnls = [float(r.get("pnl_ntd") or 0.0) for r in executed]
    med = slice_.get("median_return_pct")
    slice_["median_per_hold_day_pct"] = (
        round(float(med) / hold, 4) if med is not None else None
    )
    slice_["period_return_pct"] = (
        round(100.0 * sum(pnls) / capital_ntd, 4) if capital_ntd else None
    )
    slice_["sharpe_like"] = _sharpe_like(rets)
    return slice_


def _arm_block(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    cfg: FilterConfig,
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
    cost_bps: float,
) -> dict[str, Any]:
    arm = _run_readiness_config(
        conn,
        raw,
        features,
        cfg,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        cost_bps=cost_bps,
    )
    day_dicts = arm.pop("_day_dicts")
    end_exclusive = (
        date.fromisoformat(window_end) + timedelta(days=1)
    ).isoformat()
    hold = int(cfg.hold)
    metrics = {
        "full": _enrich_slice(
            conn, day_dicts, start=window_start, end=end_exclusive, hold=hold
        ),
        "is": _enrich_slice(
            conn, day_dicts, start=window_start, end=oos_cut, hold=hold
        ),
        "oos": _enrich_slice(
            conn, day_dicts, start=oos_cut, end=end_exclusive, hold=hold
        ),
    }
    return {
        "config_id": arm["config_id"],
        "cost_bps": cost_bps,
        "funnel": arm.get("funnel"),
        "metrics": metrics,
        "_day_dicts": day_dicts,
        "_grouped": build_config_grouped(raw, features, cfg)[0],
    }


def _compact(arm: dict[str, Any], period: str = "full") -> dict[str, Any]:
    m = arm["metrics"][period]
    return {
        "n": m.get("n_cycles"),
        "win_rate_pct": m.get("win_rate_pct"),
        "median_pct": m.get("median_return_pct"),
        "mean_pct": m.get("mean_return_pct"),
        "median_per_hold_day_pct": m.get("median_per_hold_day_pct"),
        "sharpe_like": m.get("sharpe_like"),
        "period_return_pct": m.get("period_return_pct"),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "mean_excess_pct": m.get("mean_excess_pct"),
    }


def run_head_to_head(
    conn,
    *,
    ss_raw: dict[str, list[CopytradeSignal]],
    ss_feat: dict[tuple[str, str], dict[str, Any]],
    xd_raw: dict[str, list[CopytradeSignal]],
    xd_feat: dict[tuple[str, str], dict[str, Any]],
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    specs = (
        ("songshan_40m_bare_L1H10", "凱基-松山", ss_raw, ss_feat, SS_BARE_H10),
        (
            "xindian_50m_leading_L1H8",
            "富邦-新店主契約",
            xd_raw,
            xd_feat,
            XD_LEADING_H8,
        ),
        ("xindian_80m_rs_L1H10", "富邦-新店rs對照", xd_raw, xd_feat, XD_RS_H10),
    )
    arms: dict[str, Any] = {}
    for key, label, raw, feat, cfg in specs:
        costs: dict[str, Any] = {}
        for cost_bps in (0.0, 60.0):
            arm = _arm_block(
                conn,
                raw,
                feat,
                cfg,
                window_start=window_start,
                window_end=window_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            day_dicts = arm.pop("_day_dicts")
            grouped = arm.pop("_grouped")
            costs[f"{cost_bps:g}bps"] = {
                "config_id": arm["config_id"],
                "funnel": arm["funnel"],
                "full": _compact(arm, "full"),
                "is": _compact(arm, "is"),
                "oos": _compact(arm, "oos"),
            }
            if cost_bps == 60.0:
                arms[key] = {
                    "label": label,
                    "cfg": cfg,
                    "day_dicts": day_dicts,
                    "grouped": grouped,
                }
        arms[key]["costs"] = costs
        arms[key]["label"] = label
    # Drop heavy fields from JSON payload later
    out_rows = []
    for key, _, _, _, cfg in specs:
        out_rows.append(
            {
                "arm": key,
                "label": arms[key]["label"],
                "spec": cfg.id,
                "costs": arms[key]["costs"],
            }
        )
    return {"rows": out_rows, "_arms60": arms}


def run_hold_sweep(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    rows = []
    for hold in range(5, 13):
        cfg = replace(SS_BARE_H10, hold=hold)
        costs = {}
        for cost_bps in (0.0, 60.0):
            arm = _arm_block(
                conn,
                raw,
                features,
                cfg,
                window_start=window_start,
                window_end=window_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            arm.pop("_day_dicts", None)
            arm.pop("_grouped", None)
            costs[f"{cost_bps:g}bps"] = {
                "full": _compact(arm, "full"),
                "is": _compact(arm, "is"),
                "oos": _compact(arm, "oos"),
            }
        rows.append({"hold": hold, "label": f"L1H{hold}", "costs": costs})
    return {
        "universe": "松山 ≥40M 裸 · Top1 · vol≥200k · 1槽",
        "holds": rows,
    }


def run_amount_buckets(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
    label: str,
) -> dict[str, Any]:
    """Top1 on mother≥10M bare, then bucket by top1 amount; backtest at H10."""
    mother = replace(SS_BARE_H10, threshold=10_000_000.0)
    grouped, funnel = build_config_grouped(raw, features, mother)
    amounts: dict[str, float] = {}
    for day, legs in grouped.items():
        amt = _leg_amount(day, legs[0], features)
        if amt is not None:
            amounts[day] = amt

    by_bucket: dict[str, dict[str, list[CopytradeSignal]]] = {
        b: {} for b, _, _ in BUCKETS
    }
    for day, legs in grouped.items():
        amt = amounts.get(day)
        if amt is None:
            continue
        for b, lo, hi in BUCKETS:
            if amt >= lo and (hi is None or amt < hi):
                by_bucket[b][day] = legs
                break

    out_rows = []
    for b, _, _ in BUCKETS:
        g = by_bucket[b]
        n_days = len(g)
        if n_days == 0:
            out_rows.append({"bucket": b, "n_signal_days": 0, "costs": {}})
            continue
        costs = {}
        for cost_bps in (0.0, 60.0):
            # Already filtered+bucketed Top1; pass threshold=0 bare H10.
            arm = _arm_block(
                conn,
                g,
                features,
                replace(SS_BARE_H10, threshold=0.0),
                window_start=window_start,
                window_end=window_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            arm.pop("_day_dicts", None)
            arm.pop("_grouped", None)
            costs[f"{cost_bps:g}bps"] = {
                "full": _compact(arm, "full"),
                "oos": _compact(arm, "oos"),
            }
        amts = [amounts[d] for d in g if d in amounts]
        out_rows.append(
            {
                "bucket": b,
                "n_signal_days": n_days,
                "amount_median_m": round(median(amts) / 1e6, 2),
                "amount_mean_m": round(mean(amts) / 1e6, 2),
                "costs": costs,
            }
        )
    return {
        "branch": label,
        "universe": "Top1 bare vol≥200k on mother≥10M, then amount bucket · L1H10",
        "mother_funnel": funnel,
        "buckets": out_rows,
    }


def run_folds(
    conn,
    arms60: dict[str, Any],
    *,
    window_start: str,
    hist_end: str,
) -> dict[str, Any]:
    """Half-year folds for Songshan H10 vs Xindian main contract (60bps)."""
    pairs = (
        ("songshan_40m_bare_L1H10", 10),
        ("xindian_50m_leading_L1H8", 8),
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for key, hold in pairs:
        day_dicts = arms60[key]["day_dicts"]
        folds = []
        for label, start, end in FOLD_BOUNDS:
            if start > hist_end or end <= window_start:
                continue
            slice_ = _enrich_slice(
                conn,
                day_dicts,
                start=max(start, window_start),
                end=min(end, FORWARD_START),
                hold=hold,
            )
            slice_["label"] = label
            folds.append(slice_)
        out[key] = folds
    return {"cost_bps": 60.0, "folds": out}


def run_gate_interaction(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    gates = (
        ("none", None, None),
        ("leading", "leading", None),
        ("rs_ratio_gt100", None, 100.0),
    )
    rows = []
    for name, quad, rs_min in gates:
        cfg = replace(
            SS_BARE_H10,
            rrg_quadrant=quad,
            rs_ratio_min=rs_min,
        )
        costs = {}
        for cost_bps in (0.0, 60.0):
            arm = _arm_block(
                conn,
                raw,
                features,
                cfg,
                window_start=window_start,
                window_end=window_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            arm.pop("_day_dicts", None)
            arm.pop("_grouped", None)
            costs[f"{cost_bps:g}bps"] = {
                "config_id": arm["config_id"],
                "funnel": arm["funnel"],
                "full": _compact(arm, "full"),
                "is": _compact(arm, "is"),
                "oos": _compact(arm, "oos"),
            }
        rows.append({"gate": name, "costs": costs})
    return {
        "universe": "松山 ≥40M · Top1 · L1H10 · 1槽 · vol≥200k",
        "gates": rows,
    }


def run_overlap(
    ss_grouped: dict[str, list[CopytradeSignal]],
    xd_grouped: dict[str, list[CopytradeSignal]],
) -> dict[str, Any]:
    ss_days = set(ss_grouped)
    xd_days = set(xd_grouped)
    both_days = ss_days & xd_days
    same_stock_days = []
    for day in sorted(both_days):
        ss_sid = str(ss_grouped[day][0].stock_id)
        xd_sid = str(xd_grouped[day][0].stock_id)
        if ss_sid == xd_sid:
            same_stock_days.append({"signal_date": day, "stock_id": ss_sid})
    n_ss = len(ss_days)
    n_xd = len(xd_days)
    n_both = len(both_days)
    n_same = len(same_stock_days)
    return {
        "songshan_signal_days": n_ss,
        "xindian_signal_days": n_xd,
        "same_day_overlap": n_both,
        "same_day_overlap_pct_of_songshan": (
            round(100.0 * n_both / n_ss, 2) if n_ss else None
        ),
        "same_day_overlap_pct_of_xindian": (
            round(100.0 * n_both / n_xd, 2) if n_xd else None
        ),
        "same_day_same_stock": n_same,
        "same_day_same_stock_pct_of_songshan": (
            round(100.0 * n_same / n_ss, 2) if n_ss else None
        ),
        "same_day_same_stock_examples": same_stock_days[:12],
        "note": "低同日同股重疊 = 組合資訊／正交優勢（觀察假說）",
    }


def _print_compact(payload: dict[str, Any]) -> None:
    print("\n=== 1 head-to-head (60bps) ===")
    for row in payload["1_head_to_head"]["rows"]:
        for period in ("full", "is", "oos"):
            m = row["costs"]["60bps"][period]
            print(
                f"  {row['arm'][:28]:28s} {period:4s} "
                f"n={m.get('n')} win={m.get('win_rate_pct')} "
                f"med={m.get('median_pct')} mean={m.get('mean_pct')} "
                f"med/H={m.get('median_per_hold_day_pct')} "
                f"sharpe={m.get('sharpe_like')} "
                f"ret={m.get('period_return_pct')} "
                f"dd={m.get('max_drawdown_pct')}"
            )
    print("\n=== 2 hold sweep songshan bare (60bps) ===")
    for h in payload["2_hold_sweep"]["holds"]:
        full = h["costs"]["60bps"]["full"]
        oos = h["costs"]["60bps"]["oos"]
        print(
            f"  H{h['hold']:2d} full med={full.get('median_pct')} "
            f"med/H={full.get('median_per_hold_day_pct')} "
            f"win={full.get('win_rate_pct')} | "
            f"oos med={oos.get('median_pct')} "
            f"med/H={oos.get('median_per_hold_day_pct')} "
            f"win={oos.get('win_rate_pct')}"
        )
    print("\n=== 3 amount buckets songshan (60bps full) ===")
    for b in payload["3_amount_buckets_songshan"]["buckets"]:
        c = (b.get("costs") or {}).get("60bps") or {}
        full = c.get("full") or {}
        print(
            f"  {b['bucket']:8s} days={b.get('n_signal_days', 0):3d} "
            f"n={full.get('n')} win={full.get('win_rate_pct')} "
            f"med={full.get('median_pct')}"
        )
    print("\n=== 3b amount buckets xindian (60bps full) ===")
    for b in payload["3_amount_buckets_xindian"]["buckets"]:
        c = (b.get("costs") or {}).get("60bps") or {}
        full = c.get("full") or {}
        print(
            f"  {b['bucket']:8s} days={b.get('n_signal_days', 0):3d} "
            f"n={full.get('n')} win={full.get('win_rate_pct')} "
            f"med={full.get('median_pct')}"
        )
    print("\n=== 4 folds (60bps) ===")
    folds = payload["4_folds"]["folds"]
    for key in ("songshan_40m_bare_L1H10", "xindian_50m_leading_L1H8"):
        print(f"  -- {key}")
        for f in folds[key]:
            print(
                f"    {f['label']:8s} n={f.get('n_cycles')} "
                f"win={f.get('win_rate_pct')} med={f.get('median_return_pct')} "
                f"med/H={f.get('median_per_hold_day_pct')} "
                f"ret={f.get('period_return_pct')}"
            )
    print("\n=== 5 gate interaction songshan (60bps) ===")
    for g in payload["5_gate_interaction"]["gates"]:
        full = g["costs"]["60bps"]["full"]
        oos = g["costs"]["60bps"]["oos"]
        print(
            f"  {g['gate']:16s} full n={full.get('n')} "
            f"win={full.get('win_rate_pct')} med={full.get('median_pct')} | "
            f"oos n={oos.get('n')} win={oos.get('win_rate_pct')} "
            f"med={oos.get('median_pct')}"
        )
    ov = payload["6_overlap"]
    print(
        f"\n=== 6 overlap === same_day={ov['same_day_overlap']} "
        f"({ov['same_day_overlap_pct_of_songshan']}% of SS) "
        f"same_stock={ov['same_day_same_stock']} "
        f"({ov['same_day_same_stock_pct_of_songshan']}% of SS)"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Songshan 40M bare L1H10 edge study (research-only)"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=DEFAULT_WINDOW_START)
    ap.add_argument("--end", default=DEFAULT_WINDOW_END)
    ap.add_argument("--oos-cut", default=DEFAULT_OOS_CUT)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    hist_end = min(
        args.end,
        (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat(),
    )
    conn = connect(args.db)
    try:
        print(
            f"window={args.start}..{hist_end} oos_cut={args.oos_cut} "
            f"forward_start={FORWARD_START}"
        )
        print("load songshan mother…")
        ss_raw = build_raw_xindian_amount_grouped(
            conn,
            window_start=args.start,
            window_end=hist_end,
            min_net_amount_ntd=10_000_000.0,
            trader_id=TRADER_SONGSHAN,
        )
        print(f"  songshan mother_days={len(ss_raw)} features…")
        ss_feat = build_funnel_feature_index(conn, ss_raw)

        print("load xindian mother…")
        xd_raw = build_raw_xindian_amount_grouped(
            conn,
            window_start=args.start,
            window_end=hist_end,
            min_net_amount_ntd=10_000_000.0,
            trader_id=TRADER_XINDIAN,
        )
        print(f"  xindian mother_days={len(xd_raw)} features…")
        xd_feat = build_funnel_feature_index(conn, xd_raw)

        print("1 head-to-head…")
        h2h = run_head_to_head(
            conn,
            ss_raw=ss_raw,
            ss_feat=ss_feat,
            xd_raw=xd_raw,
            xd_feat=xd_feat,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )
        arms60 = h2h.pop("_arms60")

        print("2 hold sweep…")
        hold_sweep = run_hold_sweep(
            conn,
            ss_raw,
            ss_feat,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )

        print("3 amount buckets…")
        buckets_ss = run_amount_buckets(
            conn,
            ss_raw,
            ss_feat,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
            label="凱基-松山",
        )
        buckets_xd = run_amount_buckets(
            conn,
            xd_raw,
            xd_feat,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
            label="富邦-新店",
        )

        print("4 folds…")
        folds = run_folds(
            conn,
            arms60,
            window_start=args.start,
            hist_end=hist_end,
        )

        print("5 gate interaction…")
        gates = run_gate_interaction(
            conn,
            ss_raw,
            ss_feat,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )

        print("6 overlap…")
        overlap = run_overlap(
            arms60["songshan_40m_bare_L1H10"]["grouped"],
            arms60["xindian_50m_leading_L1H8"]["grouped"],
        )

        payload = {
            "schema": "songshan_h10_edge_study-v1",
            "generated_at": date.today().isoformat(),
            "window": f"{args.start}..{hist_end}",
            "oos_cut": args.oos_cut,
            "n_slots": 1,
            "costs_bps": [0.0, 60.0],
            "role": {
                "songshan": "observation / satellite hypothesis (not adoption)",
                "xindian_main": (
                    "≥50M · Leading · L1H8 · 1槽 · 60bps · "
                    "paper_forward_required (do not downgrade)"
                ),
            },
            "1_head_to_head": h2h,
            "2_hold_sweep": hold_sweep,
            "3_amount_buckets_songshan": buckets_ss,
            "3_amount_buckets_xindian": buckets_xd,
            "4_folds": folds,
            "5_gate_interaction": gates,
            "6_overlap": overlap,
        }

        _print_compact(payload)

        if not args.no_write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = date.today().strftime("%Y%m%d")
            path = OUT_DIR / f"{stamp}_{FILE_STEM}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"\nwrote {path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
