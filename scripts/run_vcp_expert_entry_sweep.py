#!/usr/bin/env python3
"""VCP Pivot Gate · 突破當日 1m 確認 K 線進場 sweep。

SSG 不變：vcp_screen 當日收盤 PIT 候選 · near pivot · composite≥45 · hold20。
僅 `entry_fill_mode` 對照（`pivot_stop` 日線基線 vs 盤中專家觸發）：
  - baseline · pivot_stop（high≥pivot 日線 fill）
  - vwap_reclaim · bone_zone · pivot_retest
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_breadth_ma import build_breadth_panel
from research.backtest.chunge_funnel_backtest import (
    VCP_EXPERT_ENTRY_MODES,
    VCP_PIVOT_GATE,
    build_chunge_candidates_calendar,
    simulate_chunge_pivot_stop,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from report_paths import RESEARCH_VCP
from stock_db import DEFAULT_DB_PATH, connect
from vcp_funnel_screen import MODEL_ID as VCP_FUNNEL_MODEL_ID

DATE_START = "2024-01-01"
DATE_END = "2026-06-22"

VCP_ENTRY_SWEEP: list[dict[str, str | None]] = [
    {"variant_id": "vcp-baseline", "label": "pivot_stop 日線基線", "entry_fill_mode": None},
    {"variant_id": "vcp-vwap", "label": "VWAP reclaim 1m", "entry_fill_mode": "vwap_reclaim"},
    {"variant_id": "vcp-bone", "label": "Bone Zone 1m", "entry_fill_mode": "bone_zone"},
    {"variant_id": "vcp-retest", "label": "Pivot retest 1m", "entry_fill_mode": "pivot_retest"},
]


def _load_context(conn, date_start: str, date_end: str) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    gate = VCP_PIVOT_GATE
    candidates = build_chunge_candidates_calendar(
        conn,
        trade_dates,
        model_id=VCP_FUNNEL_MODEL_ID,
        min_composite=float(gate["min_composite"]),
        execution_states=tuple(gate["execution_states"]),
        entry_ready_only=bool(gate.get("entry_ready_only", False)),
        require_pivot=bool(gate.get("require_pivot", True)),
        min_dist_pivot_pct=float(gate["min_dist_pivot_pct"]),
        max_dist_pivot_pct=float(gate["max_dist_pivot_pct"]),
    )
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    return {
        "close": close,
        "full_dates": full_dates,
        "trade_dates": trade_dates,
        "candidates": candidates,
        "zone_by_date": zone_by_date,
    }


def _run_variant(
    conn,
    ctx: dict[str, Any],
    *,
    variant_id: str,
    label: str,
    entry_fill_mode: str | None,
) -> dict[str, Any]:
    gate = VCP_PIVOT_GATE
    periods, summary = simulate_chunge_pivot_stop(
        conn,
        trade_dates=ctx["trade_dates"],
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        candidates_by_date=ctx["candidates"],
        n_slots=int(gate["n_slots"]),
        hold_days=int(gate["hold_days"]),
        top_n=15,
        max_entry_wait_days=int(gate["max_entry_wait_days"]),
        stop_lookback_days=int(gate["stop_lookback_days"]),
        entry_mode="pivot_stop",
        entry_fill_mode=entry_fill_mode,
        zone_by_date=ctx["zone_by_date"],
    )
    n = len(periods)
    mean_ex = round(sum(p["excess_pct"] for p in periods) / n, 4) if n else None
    return {
        "variant_id": variant_id,
        "label": label,
        "entry_fill_mode": entry_fill_mode or "pivot_stop",
        "n_periods": summary.get("n_periods", n),
        "mean_excess_pct": mean_ex,
        "win_rate_vs_bench_pct": summary.get("win_rate_vs_bench_pct"),
        "mean_return_pct": summary.get("mean_return_pct"),
        "n_stopped": summary.get("n_stopped"),
        "n_pending_expired": summary.get("n_pending_expired"),
        "kbar_coverage_pct": summary.get("kbar_coverage_pct"),
        "screen_coverage_pct": summary.get("screen_coverage_pct"),
    }


def run_vcp_expert_entry_sweep(
    conn,
    *,
    date_start: str = DATE_START,
    date_end: str = DATE_END,
    grid: list[dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    ctx = _load_context(conn, date_start, date_end)
    variants: list[dict[str, Any]] = []
    for spec in grid or VCP_ENTRY_SWEEP:
        variants.append(
            _run_variant(
                conn,
                ctx,
                variant_id=str(spec["variant_id"]),
                label=str(spec["label"]),
                entry_fill_mode=spec["entry_fill_mode"],  # type: ignore[arg-type]
            )
        )
    baseline_ex = next(
        (v["mean_excess_pct"] for v in variants if v["variant_id"] == "vcp-baseline"),
        variants[0]["mean_excess_pct"] if variants else None,
    )
    for v in variants:
        ex = v.get("mean_excess_pct")
        v["delta_vs_baseline_pp"] = (
            round(ex - baseline_ex, 4) if ex is not None and baseline_ex is not None else None
        )
    return {
        "date_start": date_start,
        "date_end": date_end,
        "strategy": "vcp_pivot_gate",
        "entry_path": "chunge_funnel pivot_stop same-day breakout",
        "ssg_note": "vcp_screen PIT 收盤 · near pivot gate · SSG 不變 · 僅執行層 fill",
        "expert_modes_available": list(VCP_EXPERT_ENTRY_MODES),
        "baseline_entry": "pivot_stop daily high≥pivot",
        "baseline_mean_excess_pct": baseline_ex,
        "variants": variants,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VCP pivot gate expert entry sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DATE_START)
    parser.add_argument("--date-end", default=DATE_END)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_vcp_expert_entry_sweep(
            conn, date_start=args.date_start, date_end=args.date_end
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out_json or RESEARCH_VCP / f"{stamp}_vcp_expert_entry_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Baseline: {payload['baseline_mean_excess_pct']}%")
    for v in payload["variants"]:
        d = v.get("delta_vs_baseline_pp")
        d_s = f"{d:+.4f}" if d is not None else "n/a"
        print(
            f"  {v['variant_id']:<14} n={v.get('n_periods'):<4} "
            f"ex={v.get('mean_excess_pct')}% Δ={d_s}pp "
            f"kbar={v.get('kbar_coverage_pct')}% · {v.get('entry_fill_mode')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
