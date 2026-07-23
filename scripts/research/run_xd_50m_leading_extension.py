#!/usr/bin/env python3
"""新店 50M×Leading·L1H8 延伸包：金額桶 / 2025H1 解剖 / 跨分點 / H8vsH10 / forward。

Research-only · 不改 Strategy／Order。主契約對照臂，不升級。

  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_50m_leading_extension.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_50m_leading_extension.py --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median
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
    select_executed_signal_days,
)
from research.backtest.fubon_xindian_filter_study import (  # noqa: E402
    FORWARD_START,
    FilterConfig,
    _count_trading_days,
    _readiness_slice,
    _run_readiness_config,
    build_config_grouped,
    build_raw_xindian_amount_grouped,
    evaluate_forward_gate,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "fubon-xindian-copytrade"
FILE_STEM = "xd_50m_leading_extension"

BRANCHES = (
    ("xindian", "富邦-新店", "9661"),
    ("songshan", "凱基-松山", "9217"),
    ("anhe", "國票-安和", "779Z"),
)

# Amount buckets in NTD (lo inclusive, hi exclusive; None = open).
BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("40-50", 40e6, 50e6),
    ("50-80", 50e6, 80e6),
    ("80-150", 80e6, 150e6),
    ("150-300", 150e6, 300e6),
    (">=300", 300e6, None),
)

CONTRACT = FilterConfig(
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

HOLD = 8


def _leg_amount(
    day: str,
    leg: CopytradeSignal,
    features: dict[tuple[str, str], dict[str, Any]],
) -> float | None:
    feat = features.get((day, str(leg.stock_id)))
    if feat is None or feat.get("signal_close") is None:
        return None
    return float(leg.share_delta) * float(feat["signal_close"])


def _metrics_block(
    conn,
    day_dicts: list[dict[str, Any]],
    *,
    start: str,
    end_exclusive: str,
    hold: int = HOLD,
) -> dict[str, Any]:
    slice_ = _readiness_slice(conn, day_dicts, start=start, end=end_exclusive)
    med = slice_.get("median_return_pct")
    slice_["median_per_hold_day_pct"] = (
        round(float(med) / hold, 4) if med is not None else None
    )
    return slice_


def _arm_summary(arm: dict[str, Any], hold: int = HOLD) -> dict[str, Any]:
    full = arm["metrics"]["full"]
    oos = arm["metrics"]["oos"]
    med = full.get("median_return_pct")
    return {
        "config_id": arm["config_id"],
        "cost_bps": arm["cost_bps"],
        "funnel": arm.get("funnel"),
        "n": full.get("n_cycles"),
        "win_rate_pct": full.get("win_rate_pct"),
        "median_pct": full.get("median_return_pct"),
        "mean_pct": full.get("mean_return_pct"),
        "median_per_hold_day_pct": (
            round(float(med) / hold, 4) if med is not None else None
        ),
        "mean_excess_pct": full.get("mean_excess_pct"),
        "oos_n": oos.get("n_cycles"),
        "oos_median_pct": oos.get("median_return_pct"),
        "oos_win_rate_pct": oos.get("win_rate_pct"),
        "max_drawdown_pct": full.get("max_drawdown_pct"),
    }


def build_leading_top1_with_amounts(
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    mother_floor_ntd: float = 40_000_000.0,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, float]]:
    """Leading · vol≥200k · Top1 on mother ≥floor; return day→amount of top1."""
    cfg = replace(CONTRACT, threshold=mother_floor_ntd)
    grouped, _ = build_config_grouped(raw, features, cfg)
    amounts: dict[str, float] = {}
    for day, legs in grouped.items():
        amt = _leg_amount(day, legs[0], features)
        if amt is not None:
            amounts[day] = amt
    return grouped, amounts


def run_amount_buckets(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    leading, amounts = build_leading_top1_with_amounts(raw, features)
    by_bucket: dict[str, dict[str, list[CopytradeSignal]]] = {
        label: {} for label, _, _ in BUCKETS
    }
    for day, legs in leading.items():
        amt = amounts.get(day)
        if amt is None:
            continue
        for label, lo, hi in BUCKETS:
            if amt >= lo and (hi is None or amt < hi):
                by_bucket[label][day] = legs
                break

    out_rows: list[dict[str, Any]] = []
    for label, _, _ in BUCKETS:
        grouped = by_bucket[label]
        n_days = len(grouped)
        if n_days == 0:
            out_rows.append({"bucket": label, "n_signal_days": 0, "costs": {}})
            continue
        # Re-use readiness runner: pass a config that won't re-filter amounts
        # by feeding pre-bucketed raw with a low threshold and Leading.
        costs: dict[str, Any] = {}
        for cost_bps in (0.0, 60.0):
            arm = _run_readiness_config(
                conn,
                grouped,
                features,
                replace(CONTRACT, threshold=0.0),  # already bucketed
                window_start=window_start,
                window_end=window_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            arm.pop("_day_dicts", None)
            costs[f"{cost_bps:g}bps"] = _arm_summary(arm)
        bucket_amts = [amounts[d] for d in grouped if d in amounts]
        out_rows.append(
            {
                "bucket": label,
                "n_signal_days": n_days,
                "amount_median_m": round(median(bucket_amts) / 1e6, 2),
                "amount_mean_m": round(mean(bucket_amts) / 1e6, 2),
                "costs": costs,
            }
        )
    # Contract reference: ≥50M Leading (all buckets ≥50)
    contract_rows = {}
    for cost_bps in (0.0, 60.0):
        arm = _run_readiness_config(
            conn,
            raw,
            features,
            CONTRACT,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
            cost_bps=cost_bps,
        )
        arm.pop("_day_dicts", None)
        contract_rows[f"{cost_bps:g}bps"] = _arm_summary(arm)
    return {
        "universe": "xindian Leading Top1 vol≥200k on mother≥40M, then amount bucket",
        "hold": HOLD,
        "buckets": out_rows,
        "contract_ge50m_leading": contract_rows,
    }


def run_fold_autopsy(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    hist_end = min(
        window_end,
        (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat(),
    )
    arm = _run_readiness_config(
        conn,
        raw,
        features,
        CONTRACT,
        window_start=window_start,
        window_end=hist_end,
        oos_cut=oos_cut,
        cost_bps=60.0,
    )
    day_dicts = arm.pop("_day_dicts")
    fold_bounds = (
        ("2024H2", "2024-07-01", "2025-01-01"),
        ("2025H1", "2025-01-01", "2025-07-01"),
        ("2025H2", "2025-07-01", "2026-01-01"),
        ("2026H1+", "2026-01-01", FORWARD_START),
    )
    folds = []
    for label, start, end in fold_bounds:
        if start > hist_end or end <= window_start:
            continue
        slice_ = _metrics_block(
            conn,
            day_dicts,
            start=max(start, window_start),
            end_exclusive=min(end, FORWARD_START),
        )
        slice_["label"] = label
        folds.append(slice_)

    # Per-trade feature profile for 2025H1 vs rest
    complete = [r for r in day_dicts if r.get("status") == "complete"]
    executed, _ = select_executed_signal_days(complete, n_slots=1)

    def profile(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
        rets = [float(r["return_pct"]) for r in rows]
        benches = [float(r.get("bench_return_pct") or 0.0) for r in rows]
        months: Counter[str] = Counter()
        for r in rows:
            months[str(r["signal_date"])[:7]] += 1
        return {
            "label": label,
            "n": len(rows),
            "win_rate_pct": (
                round(100.0 * sum(x > 0 for x in rets) / len(rets), 2)
                if rets
                else None
            ),
            "median_pct": round(median(rets), 4) if rets else None,
            "mean_pct": round(mean(rets), 4) if rets else None,
            "median_bench_hold_pct": (
                round(median(benches), 4) if benches else None
            ),
            "mean_bench_hold_pct": round(mean(benches), 4) if benches else None,
            "mean_excess_pct": (
                round(mean([a - b for a, b in zip(rets, benches)]), 4)
                if rets
                else None
            ),
            "months": dict(sorted(months.items())),
        }

    # Enrich with feature joins using contract grouped
    grouped, funnel = build_config_grouped(raw, features, CONTRACT)
    day_to_sid = {
        day: str(legs[0].stock_id) for day, legs in grouped.items() if legs
    }

    def enrich(rows: list[dict[str, Any]]) -> dict[str, Any]:
        base = profile(rows, "")
        amounts: list[float] = []
        bench20: list[float] = []
        rs_ratios: list[float] = []
        rs_moms: list[float] = []
        mild_down = 0
        stocks: list[str] = []
        for r in rows:
            day = str(r["signal_date"])
            sid = day_to_sid.get(day)
            if sid is None:
                continue
            stocks.append(sid)
            feat = features.get((day, sid)) or {}
            amt = _leg_amount(day, grouped[day][0], features)
            if amt is not None:
                amounts.append(amt)
            b20 = feat.get("bench_ret20")
            if b20 is not None:
                bench20.append(float(b20))
                if -5.0 <= float(b20) < 0.0:
                    mild_down += 1
            if feat.get("rs_ratio") is not None:
                rs_ratios.append(float(feat["rs_ratio"]))
            if feat.get("rs_momentum") is not None:
                rs_moms.append(float(feat["rs_momentum"]))
        top_stocks = Counter(stocks).most_common(8)
        base.update(
            {
                "amount_median_m": (
                    round(median(amounts) / 1e6, 2) if amounts else None
                ),
                "amount_mean_m": (
                    round(mean(amounts) / 1e6, 2) if amounts else None
                ),
                "bench_ret20_median_pct": (
                    round(median(bench20), 4) if bench20 else None
                ),
                "bench_ret20_mean_pct": (
                    round(mean(bench20), 4) if bench20 else None
                ),
                "mild_down_market_share_pct": (
                    round(100.0 * mild_down / len(bench20), 1)
                    if bench20
                    else None
                ),
                "rs_ratio_median": (
                    round(median(rs_ratios), 2) if rs_ratios else None
                ),
                "rs_momentum_median": (
                    round(median(rs_moms), 2) if rs_moms else None
                ),
                "top_stocks": [
                    {"stock_id": s, "n": n} for s, n in top_stocks
                ],
                "unique_stocks": len(set(stocks)),
            }
        )
        return base

    h1 = [
        r
        for r in executed
        if "2025-01-01" <= str(r.get("signal_date") or "") < "2025-07-01"
    ]
    rest = [
        r
        for r in executed
        if not (
            "2025-01-01" <= str(r.get("signal_date") or "") < "2025-07-01"
        )
    ]
    # Losing vs winning within H1
    h1_loss = [r for r in h1 if float(r["return_pct"]) <= 0]
    h1_win = [r for r in h1 if float(r["return_pct"]) > 0]

    return {
        "cost_bps": 60.0,
        "contract": CONTRACT.id,
        "funnel": funnel,
        "folds": folds,
        "h1_vs_rest": {
            "2025H1": enrich(h1),
            "other_folds": enrich(rest),
            "2025H1_winners": enrich(h1_win),
            "2025H1_losers": enrich(h1_loss),
        },
    }


def run_cross_branch(
    conn,
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    hist_end = min(
        window_end,
        (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat(),
    )
    rows = []
    for key, label, tid in BRANCHES:
        n_tape = conn.execute(
            """
            SELECT COUNT(*) FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            (tid, window_start, hist_end),
        ).fetchone()[0]
        if int(n_tape) == 0:
            rows.append(
                {
                    "branch": key,
                    "label": label,
                    "trader_id": tid,
                    "tape_rows": 0,
                    "ok": False,
                    "reason": "no_tape",
                }
            )
            continue
        raw = build_raw_xindian_amount_grouped(
            conn,
            window_start=window_start,
            window_end=hist_end,
            min_net_amount_ntd=30_000_000.0,
            trader_id=tid,
        )
        features = build_funnel_feature_index(conn, raw)
        costs = {}
        for cost_bps in (0.0, 60.0):
            arm = _run_readiness_config(
                conn,
                raw,
                features,
                CONTRACT,
                window_start=window_start,
                window_end=hist_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            arm.pop("_day_dicts", None)
            costs[f"{cost_bps:g}bps"] = _arm_summary(arm)
        rows.append(
            {
                "branch": key,
                "label": label,
                "trader_id": tid,
                "tape_rows": int(n_tape),
                "ok": True,
                "costs": costs,
            }
        )
    return {
        "contract": "≥50M · Leading · Top1 · L1H8 · 1槽 · vol≥200k",
        "window": f"{window_start}..{hist_end}",
        "branches": rows,
    }


def run_hold_neighborhood(
    conn,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    hist_end = min(
        window_end,
        (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat(),
    )
    rows = []
    for hold in (8, 10):
        costs = {}
        for cost_bps in (0.0, 60.0):
            arm = _run_readiness_config(
                conn,
                raw,
                features,
                replace(CONTRACT, hold=hold),
                window_start=window_start,
                window_end=hist_end,
                oos_cut=oos_cut,
                cost_bps=cost_bps,
            )
            arm.pop("_day_dicts", None)
            costs[f"{cost_bps:g}bps"] = _arm_summary(arm, hold=hold)
        rows.append({"hold": hold, "label": f"L1H{hold}", "costs": costs})
    return {
        "note": "preregistered neighborhood only; does not change main contract",
        "universe": "xindian ≥50M Leading Top1",
        "arms": rows,
    }


def run_forward_check(conn, *, window_end: str) -> dict[str, Any]:
    end = max(window_end, date.today().isoformat())
    trading_days = _count_trading_days(
        conn, start=FORWARD_START, end_inclusive=end
    )
    forward_metrics = {
        "n_cycles": 0,
        "median_return_pct": None,
        "mean_excess_pct": None,
        "win_rate_pct": None,
        "max_drawdown_pct": None,
    }
    gate = evaluate_forward_gate(
        trading_days=trading_days,
        forward_metrics=forward_metrics,
    )
    return {
        "forward_start": FORWARD_START,
        "bars_end_probe": end,
        "tape_hint": "Book replica bars may lag live; treat as research snapshot",
        "gate": gate,
        "how_to_accumulate": [
            "Keep frozen contract: ≥50M · Leading · Top1 · L1H8 · 1槽 · vol≥200k",
            "After each session with tape+bars ≥ FORWARD_START, re-run: "
            "PYTHONPATH=src .venv/bin/python "
            "scripts/research/run_fubon_xindian_copytrade_sweep.py "
            "--strategy-readiness --readiness-amount-m 50",
            "Do not change config/strategy.yaml or config/order.yaml until "
            "forward_gate.passed == true (≥90 trading days, ≥12 cycles, "
            "median>0, mean excess>0, win≥50%, MaxDD≤20% at 60bps)",
        ],
        "paper_checklist": [
            "Signal day close: xd net amount ≥50M, RRG quadrant=leading, "
            "avg vol20≥200k, Top1 only",
            "Entry: next open (L1) · Exit: hold 8 trading days close (H8)",
            "Log: signal_date, stock_id, amount_m, entry/exit, return_pct, "
            "bench_return_pct, cost_bps=60 assumption",
            "Weekly: count completed cycles; never early-adopt on compound luck",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="XD 50M Leading L1H8 extension pack (A–E)"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=DEFAULT_WINDOW_START)
    ap.add_argument("--end", default=DEFAULT_WINDOW_END)
    ap.add_argument("--oos-cut", default=DEFAULT_OOS_CUT)
    ap.add_argument("--trader-id", default=DEFAULT_TRADER_ID_FUBON_XINDIAN)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    hist_end = min(
        args.end,
        (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat(),
    )
    conn = connect(args.db)
    try:
        print(
            f"window={args.start}..{hist_end} (hist) "
            f"forward_start={FORWARD_START} end_arg={args.end}"
        )
        raw = build_raw_xindian_amount_grouped(
            conn,
            window_start=args.start,
            window_end=hist_end,
            min_net_amount_ntd=30_000_000.0,
            trader_id=str(args.trader_id),
        )
        print(f"mother_days={len(raw)} building features…")
        features = build_funnel_feature_index(conn, raw)

        print("A amount buckets…")
        section_a = run_amount_buckets(
            conn,
            raw,
            features,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )
        print("B 2025H1 autopsy…")
        section_b = run_fold_autopsy(
            conn,
            raw,
            features,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )
        print("C cross-branch…")
        section_c = run_cross_branch(
            conn,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )
        print("D H8 vs H10…")
        section_d = run_hold_neighborhood(
            conn,
            raw,
            features,
            window_start=args.start,
            window_end=hist_end,
            oos_cut=args.oos_cut,
        )
        print("E forward check…")
        section_e = run_forward_check(conn, window_end=args.end)

        payload = {
            "schema": "xd_50m_leading_extension-v1",
            "generated_at": date.today().isoformat(),
            "contract_frozen": {
                "branch": "9661 富邦-新店",
                "spec": "≥50M · Leading · Top1 · L1H8 · 1槽 · vol≥200k",
                "decision": "paper_forward_required",
                "forward_start": FORWARD_START,
                "hypothesis": "H-XD-50M-LEADING-L1H8-ADOPTION",
            },
            "A_amount_buckets": section_a,
            "B_fold_autopsy_2025H1": section_b,
            "C_cross_branch": section_c,
            "D_hold_neighborhood": section_d,
            "E_forward_readiness": section_e,
        }

        # Compact stdout tables
        print("\n=== A buckets (60bps) ===")
        for row in section_a["buckets"]:
            c60 = (row.get("costs") or {}).get("60bps") or {}
            print(
                f"  {row['bucket']:8s} n={row.get('n_signal_days', 0):3d} "
                f"cycles={c60.get('n')} win={c60.get('win_rate_pct')} "
                f"med={c60.get('median_pct')} mean={c60.get('mean_pct')} "
                f"med/H={c60.get('median_per_hold_day_pct')}"
            )
        print("\n=== B folds (60bps) ===")
        for f in section_b["folds"]:
            print(
                f"  {f['label']:8s} n={f.get('n_cycles')} "
                f"win={f.get('win_rate_pct')} med={f.get('median_return_pct')} "
                f"excess={f.get('mean_excess_pct')}"
            )
        print("\n=== C branches (60bps) ===")
        for b in section_c["branches"]:
            if not b.get("ok"):
                print(f"  {b['label']}: NO TAPE")
                continue
            c60 = b["costs"]["60bps"]
            print(
                f"  {b['label']:10s} n={c60.get('n')} "
                f"win={c60.get('win_rate_pct')} med={c60.get('median_pct')} "
                f"oos_med={c60.get('oos_median_pct')}"
            )
        print("\n=== D hold (60bps) ===")
        for a in section_d["arms"]:
            c60 = a["costs"]["60bps"]
            print(
                f"  {a['label']} n={c60.get('n')} win={c60.get('win_rate_pct')} "
                f"med={c60.get('median_pct')} med/H={c60.get('median_per_hold_day_pct')}"
            )
        fwd = section_e["gate"]
        print(
            f"\n=== E forward === days={fwd.get('trading_days')} "
            f"passed={fwd.get('passed')} cycles="
            f"{(fwd.get('metrics') or {}).get('n_cycles')}"
        )

        if not args.no_write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = date.today().strftime("%Y%m%d")
            path = OUT_DIR / f"{stamp}_{FILE_STEM}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"wrote {path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
