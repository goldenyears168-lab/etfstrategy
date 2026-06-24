#!/usr/bin/env python3
"""C18-dl1 鄰域 sweep · 減速 + down_left · margin 0.08 中心 · vs C18 / C0 hold7。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from report_paths import RESEARCH_RRG
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, simulate_leg_c_variant
from research.backtest.rrg_mono_score_swap_c import (
    C18_DL_NEIGHBORHOOD_SWEEP,
    ScoreSwapCConfig,
    simulate_score_swap_c,
)
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect

WINDOWS: list[tuple[str, str, str]] = [
    ("full", "2024-01-01", "2026-06-22"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026_h1", "2026-01-01", "2026-06-22"),
]


def _variant_row(summary: dict, *, c0_excess: float | None, c18_excess: float | None) -> dict:
    ex = summary.get("mean_excess_pct")
    delta_c0 = round(float(ex) - float(c0_excess), 4) if ex is not None and c0_excess is not None else None
    delta_c18 = round(float(ex) - float(c18_excess), 4) if ex is not None and c18_excess is not None else None
    return {
        "variant_id": summary.get("variant_id"),
        "label": summary.get("label"),
        "sort_key": summary.get("sort_key"),
        "effective_margin": summary.get("effective_margin"),
        "decel_gate": summary.get("decel_gate"),
        "structural_gate": summary.get("structural_gate"),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": ex,
        "mean_hold_days": summary.get("mean_hold_days"),
        "swaps_total": summary.get("swaps_total"),
        "delta_vs_c0_hold7_pp": delta_c0,
        "delta_vs_c18_pp": delta_c18,
        "beats_c0": delta_c0 is not None and delta_c0 > 0,
        "beats_c18": delta_c18 is not None and delta_c18 > 0,
    }


def _run_window(
    conn,
    *,
    label: str,
    date_start: str,
    date_end: str,
    configs: list[ScoreSwapCConfig],
    kbar_cache: dict,
) -> dict:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    _, c0_sum = simulate_leg_c_variant(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        config=c0_cfg,
    )
    c0_excess = c0_sum.get("mean_excess_pct")

    variants: list[dict] = []
    c18_excess: float | None = None
    for cfg in configs:
        print(f"  {label} · {cfg.variant_id} ...", flush=True)
        _, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=cfg,
            kbar_cache=kbar_cache,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
        )
        if cfg.variant_id == "C18":
            c18_excess = summary.get("mean_excess_pct")
        variants.append(_variant_row(summary, c0_excess=c0_excess, c18_excess=c18_excess))

    if c18_excess is None:
        c18_row = next((v for v in variants if v["variant_id"] == "C18"), None)
        c18_excess = c18_row.get("mean_excess_pct") if c18_row else None
        for v in variants:
            ex = v.get("mean_excess_pct")
            v["delta_vs_c18_pp"] = (
                round(float(ex) - float(c18_excess), 4) if ex is not None and c18_excess is not None else None
            )
            v["beats_c18"] = v["delta_vs_c18_pp"] is not None and v["delta_vs_c18_pp"] > 0

    ranked = sorted(
        variants,
        key=lambda v: (-(v.get("mean_excess_pct") or -999.0), -(v.get("n_periods") or 0)),
    )
    return {
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "c0_hold7": {"n_periods": c0_sum.get("n_periods"), "mean_excess_pct": c0_excess},
        "c18_ref": {"mean_excess_pct": c18_excess},
        "variants": variants,
        "ranked_by_excess": [v["variant_id"] for v in ranked],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18-dl neighborhood sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    kbar_cache: dict = {}
    conn = connect(args.db)
    try:
        windows = [
            _run_window(
                conn,
                label=label,
                date_start=ds,
                date_end=de,
                configs=C18_DL_NEIGHBORHOOD_SWEEP,
                kbar_cache=kbar_cache,
            )
            for label, ds, de in WINDOWS
        ]
    finally:
        conn.close()

    full = windows[0]
    ranked = full.get("ranked_by_excess") or []
    champ = next((v for v in full["variants"] if v["variant_id"] == ranked[0]), None) if ranked else None
    dl1 = next((v for v in full["variants"] if v["variant_id"] == "C18-dl1"), None)
    dl8 = next((v for v in full["variants"] if v["variant_id"] == "C18-dl8"), None)

    payload = {
        "hypothesis": "C18-dl1 鄰域 · 減速+down_left · margin 0.08 中心",
        "reference_c0_hold7": full["c0_hold7"],
        "reference_c18": full["c18_ref"],
        "windows": windows,
        "champion": champ,
        "c18_dl1": dl1,
        "c18_dl8_center": dl8,
    }

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18_dl_neighborhood_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"C18 ref={full['c18_ref'].get('mean_excess_pct')}% · C0={full['c0_hold7'].get('mean_excess_pct')}%")
    for v in sorted(full["variants"], key=lambda x: -(x.get("mean_excess_pct") or -999)):
        print(
            f"  {v['variant_id']:10} margin={v.get('effective_margin')} "
            f"sort={v.get('sort_key'):14} dl={v.get('structural_gate')} "
            f"decel={v.get('decel_gate')} excess={v.get('mean_excess_pct')}% "
            f"Δc18={v.get('delta_vs_c18_pp')} swaps={v.get('swaps_total')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
