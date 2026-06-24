#!/usr/bin/env python3
"""C18acc · Market breadth（廣度）zone 漏斗層 sweep · 單一 spec 切換。

用法：
  PYTHONPATH=src python scripts/run_c18acc_breadth_funnel_sweep.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close
from market_breadth_ma import BREADTH_ZONES_ORDER, build_breadth_panel
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    C18acc_BREADTH_FUNNEL_SWEEP,
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    RRG_MONO_SWAP_ACCEL_SHORT,
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    _pooled_by_entry_zone,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from report_paths import RESEARCH_RRG
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect

DATE_START = "2024-01-01"
DATE_END = "2026-06-22"
EXTENDED_START = "2019-01-01"


def _load_context(conn, date_start: str, date_end: str) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    return {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "trade_dates": trade_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
    }


def _by_entry_year(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in periods:
        buckets[str(p["entry_date"])[:4]].append(p)
    rows = []
    for year in sorted(buckets):
        sub = buckets[year]
        s = summarize_periods(sub)
        n = len(sub)
        s["n_periods"] = n
        s["mean_excess_pct"] = round(sum(x["excess_pct"] for x in sub) / n, 4) if n else None
        s["swaps_total"] = sum(1 for x in sub if x.get("exit_reason") == "score_swap")
        rows.append({"year": year, **s})
    return rows


def _swap_legs(periods: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    legs = []
    for p in periods:
        if p.get("exit_reason") != "score_swap":
            continue
        legs.append(
            (
                str(p["stock_id"]),
                str(p.get("challenger_id") or ""),
                str(p.get("signal_date") or p.get("entry_date") or ""),
            )
        )
    return legs


def _compare_swap_legs(
    base: list[tuple[str, str, str]], other: list[tuple[str, str, str]]
) -> dict[str, Any]:
    return {
        "n_base": len(base),
        "n_other": len(other),
        "all_match": base == other,
        "n_matching_prefix": sum(1 for a, b in zip(base, other) if a == b),
    }


def _run_variant(
    conn,
    ctx: dict[str, Any],
    cfg: ScoreSwapCConfig,
    kbar_cache: dict,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=ctx["trade_dates"],
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=kbar_cache,
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
    )
    return periods, summary


def _what_changes(cfg: ScoreSwapCConfig) -> str:
    if cfg.variant_id == CHAMPION_SCORE_SWAP_C_VARIANT_ID:
        return "baseline"
    parts: list[str] = []
    if cfg.breadth_entry_zones:
        parts.append(f"entry∈{cfg.breadth_entry_zones}")
    if cfg.breadth_swap_zones:
        parts.append(f"swap∈{cfg.breadth_swap_zones}")
    if cfg.breadth_pool_mode != "always_fresh":
        parts.append(f"pool_mode={cfg.breadth_pool_mode}")
    return " · ".join(parts) or "same"


def run_sweep(
    conn,
    *,
    date_start: str = DATE_START,
    date_end: str = DATE_END,
    configs: list[ScoreSwapCConfig] | None = None,
    include_extended: bool = True,
) -> dict[str, Any]:
    ctx = _load_context(conn, date_start, date_end)
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    grid = configs or C18acc_BREADTH_FUNNEL_SWEEP

    champ_periods: list[dict[str, Any]] = []
    champ_legs: list[tuple[str, str, str]] = []
    champ_ex: float | None = None
    champ_swaps: int | None = None
    variants: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        periods, summary = _run_variant(conn, ctx, cfg, kbar_cache)
        ex = summary.get("mean_excess_pct")
        swaps = summary.get("swaps_total")
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"swaps={swaps} mean_excess={ex}",
            flush=True,
        )

        if cfg.variant_id == CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            champ_periods = periods
            champ_legs = _swap_legs(periods)
            champ_ex = ex
            champ_swaps = swaps

        legs = _swap_legs(periods)
        row: dict[str, Any] = {
            "variant_id": cfg.variant_id,
            "label": cfg.label,
            "what_changes": _what_changes(cfg),
            "mean_excess_pct": ex,
            "swaps_total": swaps,
            "n_periods": summary.get("n_periods"),
            "delta_vs_champion_pp": round(float(ex) - float(champ_ex), 4)
            if ex is not None and champ_ex is not None and cfg.variant_id != CHAMPION_SCORE_SWAP_C_VARIANT_ID
            else None,
            "by_entry_year": _by_entry_year(periods),
            "pooled_by_entry_zone": _pooled_by_entry_zone(periods),
            "config": cfg.to_dict(),
        }
        if cfg.variant_id != CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            row["swap_leg_match"] = _compare_swap_legs(champ_legs, legs)
        variants.append(row)

    extended: dict[str, Any] | None = None
    if include_extended:
        ext_ctx = _load_context(conn, EXTENDED_START, date_end)
        ext_kbar: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
        ext_rows: list[dict[str, Any]] = []
        for cfg in grid:
            _, summary = _run_variant(conn, ext_ctx, cfg, ext_kbar)
            ext_rows.append(
                {
                    "variant_id": cfg.variant_id,
                    "mean_excess_pct": summary.get("mean_excess_pct"),
                    "swaps_total": summary.get("swaps_total"),
                    "n_periods": summary.get("n_periods"),
                }
            )
        extended = {
            "date_start": EXTENDED_START,
            "date_end": date_end,
            "variants": ext_rows,
        }

    stable_beats = [
        v["variant_id"]
        for v in variants
        if v["variant_id"] != CHAMPION_SCORE_SWAP_C_VARIANT_ID
        and v.get("delta_vs_champion_pp") is not None
        and v["delta_vs_champion_pp"] > 0
        and all(
            (y.get("mean_excess_pct") or -999) >= (champ_ex or -999)
            for y in v.get("by_entry_year", [])
            if y.get("year") in ("2024", "2025", "2026")
        )
    ]

    return {
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "date_start": date_start,
        "date_end": date_end,
        "champion": {
            "variant_id": CHAMPION_SCORE_SWAP_C_VARIANT_ID,
            "mean_excess_pct": champ_ex,
            "swaps_total": champ_swaps,
            "n_periods": len(champ_periods),
            "n_swap_legs": len(champ_legs),
        },
        "variants": variants,
        "extended_summary": extended,
        "recommendation": {
            "stable_beats_champion": stable_beats,
            "note": "stable = full-sample Δ>0 and every entry year 2024–2026 ≥ champion mean excess",
        },
    }


def _print_table(payload: dict[str, Any]) -> None:
    champ = payload["champion"]
    print(
        f"\nChampion {champ['variant_id']}: {champ['mean_excess_pct']}% · "
        f"{champ['swaps_total']} swaps · n={champ['n_periods']}"
    )
    print(
        f"{'variant_id':<32} {'excess':>7} {'swaps':>5} {'n':>4} "
        f"{'Δpp':>6} {'prefix':>6}  changes"
    )
    for v in sorted(payload["variants"], key=lambda x: -(x.get("mean_excess_pct") or -999)):
        delta = v.get("delta_vs_champion_pp")
        delta_s = f"{delta:+.2f}" if delta is not None else "  —"
        prefix = ""
        if "swap_leg_match" in v:
            prefix = str(v["swap_leg_match"].get("n_matching_prefix", ""))
        print(
            f"{v['variant_id']:<32} {v.get('mean_excess_pct', '—'):>7} "
            f"{v.get('swaps_total', 0):>5} {v.get('n_periods', 0):>4} "
            f"{delta_s:>6} {prefix:>6}  {v.get('what_changes', '')}"
        )

    print("\nBy entry year (mean excess %):")
    years = sorted({y["year"] for v in payload["variants"] for y in v.get("by_entry_year", [])})
    header = f"{'variant_id':<32}" + "".join(f"{y:>8}" for y in years)
    print(header)
    for v in payload["variants"]:
        by_y = {y["year"]: y.get("mean_excess_pct") for y in v.get("by_entry_year", [])}
        row = f"{v['variant_id']:<32}" + "".join(
            f"{by_y.get(y, '—'):>8}" if by_y.get(y) is not None else f"{'—':>8}" for y in years
        )
        print(row)

    print("\nBreadth pooled by entry zone (mean excess %):")
    zones = list(BREADTH_ZONES_ORDER)
    header = f"{'variant_id':<32}" + "".join(f"{z[:6]:>8}" for z in zones)
    print(header)
    for v in payload["variants"]:
        pooled = v.get("pooled_by_entry_zone") or {}
        row = f"{v['variant_id']:<32}" + "".join(
            f"{(pooled.get(z) or {}).get('mean_excess_pct', '—'):>8}"
            if (pooled.get(z) or {}).get("mean_excess_pct") is not None
            else f"{'—':>8}"
            for z in zones
        )
        print(row)

    rec = payload.get("recommendation") or {}
    stable = rec.get("stable_beats_champion") or []
    if stable:
        print(f"\nStable beats champion: {', '.join(stable)}")
    else:
        print("\nNo variant stably beats champion on 2024–2026 entry years + full sample.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc breadth funnel sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DATE_START)
    parser.add_argument("--date-end", default=DATE_END)
    parser.add_argument("--no-extended", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            include_extended=not args.no_extended,
        )
    finally:
        conn.close()

    out = args.out or RESEARCH_RRG / "20260624_c18acc_breadth_funnel_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    _print_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
