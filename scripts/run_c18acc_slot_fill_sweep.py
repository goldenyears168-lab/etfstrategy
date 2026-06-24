#!/usr/bin/env python3
"""C18acc 空槽填倉 sweep · H1–H5 假說驗證。

目標：降低「2/3 填滿日（12.4%）」· 評估強制填倉對 mean_excess 的邊際影響。

用法：
  PYTHONPATH=src .venv/bin/python scripts/run_c18acc_slot_fill_sweep.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402
from market_breadth_ma import build_breadth_panel  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods  # noqa: E402
from research.backtest.rrg_mono_backtest import (  # noqa: E402
    build_fresh_mono_calendar,
    build_mono_up_calendar,
    build_mono_up_fresh_calendar,
)
from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    RRG_MONO_SWAP_ACCEL_SHORT,
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    _candidate_shortlist,
    _champion_accel_fields,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar  # noqa: E402
from rrg_mono_daily_brief import LENGTH, LOOKBACK  # noqa: E402
from rrg_rotation import compute_rrg_panel  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DATE_START = "2024-01-01"
DATE_END = "2026-06-23"
WARM_START = "2024-01-01"


# ---------------------------------------------------------------------------
# Shared helper (mirrors analyze_c18acc_slot_utilization._analyze_slots)
# ---------------------------------------------------------------------------

from rrg_mono_daily_brief import MAX_SLOTS  # noqa: E402


def _analyze_slots(
    slot_snapshots: dict[str, list[dict]],
    fresh_by_date: dict[str, list],
) -> dict:
    """Compute slot utilization stats from EOD snapshots."""
    from collections import Counter

    trade_dates = sorted(slot_snapshots)
    n_days = len(trade_dates)
    if n_days == 0:
        return {}

    fill_counts: list[int] = []
    fresh_sizes: list[int] = []
    zero_fresh_days: list[str] = []

    for d in trade_dates:
        snap = slot_snapshots[d]
        fill_counts.append(len(snap))
        fs = len(fresh_by_date.get(d, []))
        fresh_sizes.append(fs)
        if fs == 0:
            zero_fresh_days.append(d)

    dist = Counter(fill_counts)
    mean_fill = sum(fill_counts) / n_days
    mean_empty = MAX_SLOTS - mean_fill
    pct_all_full = dist[MAX_SLOTS] / n_days * 100
    pct_any_empty = (n_days - dist[MAX_SLOTS]) / n_days * 100
    pct_0_slots = dist[0] / n_days * 100
    pct_1_slot = dist[1] / n_days * 100
    pct_2_slots = dist[2] / n_days * 100

    return {
        "n_trade_days": n_days,
        "max_slots": MAX_SLOTS,
        "mean_fill": round(mean_fill, 3),
        "mean_empty": round(mean_empty, 3),
        "pct_all_full_3_3": round(pct_all_full, 1),
        "pct_any_empty": round(pct_any_empty, 1),
        "pct_0_filled": round(pct_0_slots, 1),
        "pct_1_filled": round(pct_1_slot, 1),
        "pct_2_filled": round(pct_2_slots, 1),
        "fill_dist": {str(k): v for k, v in sorted(dist.items())},
        "mean_fresh_pool_size": round(sum(fresh_sizes) / n_days, 2),
        "pct_days_fresh_pool_zero": round(len(zero_fresh_days) / n_days * 100, 1),
        "n_days_fresh_pool_zero": len(zero_fresh_days),
        "zero_fresh_sample": zero_fresh_days[-10:],
    }


# ---------------------------------------------------------------------------
# Sweep variant definitions
# ---------------------------------------------------------------------------

def _slot_fill_variants() -> list[ScoreSwapCConfig]:
    """Return [champion-baseline, H1, H2, H3, H4a, H4b, H5]."""
    champ = champion_score_swap_c_config()

    # H1: entry 放寬 union / swap 維持 fresh
    h1 = ScoreSwapCConfig(
        "C18acc-slot-H1-entry-union",
        "空槽進場 fresh∪accel · 換倉仍 fresh",
        entry_pool="fresh_union_accel",
        swap_pool="fresh",
        **_champion_accel_fields(),
    )

    # H2: candidate_lookback 4→6（fresh 窗加長）
    _h2_fields = {**_champion_accel_fields(), "candidate_lookback": 6}
    h2 = ScoreSwapCConfig(
        "C18acc-slot-H2-lb6",
        "candidate_lookback=6（fresh 窗加長）",
        **_h2_fields,
    )

    # H3: entry fallback pool = mono_tier2（fresh=0 時降級補倉）
    h3 = ScoreSwapCConfig(
        "C18acc-slot-H3-fallback-mono",
        "entry fallback=mono_tier2（fresh=0 降級）",
        entry_fallback_pool="mono_tier2",
        **_champion_accel_fields(),
    )

    # H4a: max_hold_days 10→12
    _h4a_fields = {**_champion_accel_fields(), "max_hold_days": 12}
    h4a = ScoreSwapCConfig(
        "C18acc-slot-H4a-mh12",
        "max_hold_days=12",
        **_h4a_fields,
    )

    # H4b: max_hold_days 10→15
    _h4b_fields = {**_champion_accel_fields(), "max_hold_days": 15}
    h4b = ScoreSwapCConfig(
        "C18acc-slot-H4b-mh15",
        "max_hold_days=15",
        **_h4b_fields,
    )

    # H5: breadth_pool_mode="mono_in_hot_zones"（附空槽統計 · 參考資訊）
    h5 = ScoreSwapCConfig(
        "C18acc-slot-H5-breadth-hot",
        "breadth_pool_mode=mono_in_hot_zones（參考）",
        breadth_pool_mode="mono_in_hot_zones",
        **_champion_accel_fields(),
    )

    return [champ, h1, h2, h3, h4a, h4b, h5]


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

def _load_context(
    conn,
    *,
    date_start: str,
    date_end: str,
    lookback: int = LOOKBACK,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates, lookback=lookback)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    mono_up_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=True)
    mono_up_fresh_by_date = build_mono_up_fresh_calendar(conn, trade_dates)
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
        "mono_up_by_date": mono_up_by_date,
        "mono_up_fresh_by_date": mono_up_fresh_by_date,
        "zone_by_date": zone_by_date,
    }


def _run_variant(
    conn,
    ctx: dict[str, Any],
    cfg: ScoreSwapCConfig,
    kbar_cache: dict,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[dict]]]:
    slot_snapshots: dict[str, list[dict]] = {}
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
        mono_up_by_date=ctx["mono_up_by_date"],
        mono_up_fresh_by_date=ctx["mono_up_fresh_by_date"],
        kbar_cache=kbar_cache,
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        slot_snapshots=slot_snapshots,
    )
    return periods, summary, slot_snapshots


def _by_entry_subperiod(
    periods: list[dict[str, Any]],
    zone_by_date: dict[str, str],
) -> dict[str, Any]:
    """Sub-period and breadth-zone breakdown for 2025 stability re-verification."""
    HOT_ZONES = {"strong", "overbought"}

    def _stats(sub: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(sub)
        if n == 0:
            return {"n_periods": 0, "mean_excess_pct": None, "swaps_total": 0}
        return {
            "n_periods": n,
            "mean_excess_pct": round(sum(x["excess_pct"] for x in sub) / n, 4),
            "swaps_total": sum(1 for x in sub if x.get("exit_reason") == "score_swap"),
        }

    p2025 = [p for p in periods if "2025-01-01" <= p["entry_date"] <= "2025-12-31"]
    p2025h1 = [p for p in p2025 if p["entry_date"] <= "2025-06-30"]
    p2025h2 = [p for p in p2025 if p["entry_date"] >= "2025-07-01"]

    zone_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in p2025:
        z = zone_by_date.get(p["entry_date"], "unknown")
        zone_groups[z].append(p)

    hot = [p for p in p2025 if zone_by_date.get(p["entry_date"], "unknown") in HOT_ZONES]
    other = [p for p in p2025 if zone_by_date.get(p["entry_date"], "unknown") not in HOT_ZONES]

    return {
        "full_2025": _stats(p2025),
        "h1_2025": _stats(p2025h1),
        "h2_2025": _stats(p2025h2),
        "hot_zones_2025": _stats(hot),
        "other_zones_2025": _stats(other),
        "by_zone_2025": {z: _stats(v) for z, v in sorted(zone_groups.items())},
    }


def _by_entry_year(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in periods:
        buckets[str(p["entry_date"])[:4]].append(p)
    rows = []
    for year in sorted(buckets):
        sub = buckets[year]
        n = len(sub)
        rows.append({
            "year": year,
            "n_periods": n,
            "mean_excess_pct": round(sum(x["excess_pct"] for x in sub) / n, 4) if n else None,
            "swaps_total": sum(1 for x in sub if x.get("exit_reason") == "score_swap"),
        })
    return rows


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def run_sweep(
    conn,
    *,
    date_start: str = DATE_START,
    date_end: str = DATE_END,
) -> dict[str, Any]:
    variants_cfgs = _slot_fill_variants()
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    # Default context (lookback=4)
    ctx = _load_context(conn, date_start=date_start, date_end=date_end, lookback=LOOKBACK)
    # H2 needs lookback=6 fresh calendar
    ctx_lb6 = _load_context(conn, date_start=date_start, date_end=date_end, lookback=6)

    champ_ex: float | None = None
    champ_slots: dict | None = None
    rows: list[dict[str, Any]] = []

    for cfg in variants_cfgs:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)

        # H2 uses lb=6 fresh calendar
        run_ctx = ctx_lb6 if cfg.candidate_lookback == 6 else ctx

        periods, summary, slot_snaps = _run_variant(conn, run_ctx, cfg, kbar_cache)
        slot_stats = _analyze_slots(slot_snaps, run_ctx["fresh_by_date"])

        ex = summary.get("mean_excess_pct")
        n_p = summary.get("n_periods")
        swaps = summary.get("swaps_total")
        pct_full = slot_stats.get("pct_all_full_3_3")
        pct_empty = slot_stats.get("pct_any_empty")
        mean_empty = slot_stats.get("mean_empty")

        is_champ = cfg.variant_id == CHAMPION_SCORE_SWAP_C_VARIANT_ID
        if is_champ:
            champ_ex = ex
            champ_slots = slot_stats

        delta_ex = (
            round(float(ex) - float(champ_ex), 4)
            if ex is not None and champ_ex is not None and not is_champ
            else None
        )
        delta_pct_full = (
            round(float(pct_full) - float(champ_slots["pct_all_full_3_3"]), 1)
            if pct_full is not None and champ_slots is not None and not is_champ
            else None
        )

        print(
            f"  {cfg.variant_id}: excess={ex}%  n={n_p}  swaps={swaps}"
            f"  3/3={pct_full}%  any_empty={pct_empty}%  mean_empty={mean_empty}"
            + (f"  Δexcess={delta_ex:+.4f}pp  Δfull={delta_pct_full:+.1f}pp" if delta_ex is not None else ""),
            flush=True,
        )

        rows.append({
            "variant_id": cfg.variant_id,
            "label": cfg.label,
            "mean_excess_pct": ex,
            "n_periods": n_p,
            "swaps_total": swaps,
            "pct_3_3_full": pct_full,
            "pct_any_empty": pct_empty,
            "mean_empty": mean_empty,
            "delta_excess_pp": delta_ex,
            "delta_pct_full_pp": delta_pct_full,
            "slot_utilization": slot_stats,
            "by_entry_year": _by_entry_year(periods),
            "config": cfg.to_dict(),
        })

    best = max(
        (r for r in rows if r["variant_id"] != CHAMPION_SCORE_SWAP_C_VARIANT_ID and r["mean_excess_pct"] is not None),
        key=lambda r: r["mean_excess_pct"],
        default=None,
    )
    best_fill = max(
        (r for r in rows if r["variant_id"] != CHAMPION_SCORE_SWAP_C_VARIANT_ID and r["pct_3_3_full"] is not None),
        key=lambda r: r["pct_3_3_full"],
        default=None,
    )

    return {
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "date_start": date_start,
        "date_end": date_end,
        "champion": {
            "variant_id": CHAMPION_SCORE_SWAP_C_VARIANT_ID,
            "mean_excess_pct": champ_ex,
            "slot_utilization": champ_slots,
        },
        "variants": rows,
        "best_excess": best["variant_id"] if best else None,
        "best_slot_fill": best_fill["variant_id"] if best_fill else None,
    }


def run_stability_2025(
    conn,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2025-12-31",
) -> dict[str, Any]:
    """Champion vs H1 (entry_pool=fresh_union_accel) · 2025 stability re-verification.

    Runs the full simulation from *date_start* (warm-up) through *date_end*,
    then slices periods by entry_date into 2025, 2025-H1, and 2025-H2.
    Also includes a breadth-zone breakdown for full-2025 entries.
    """
    champ = champion_score_swap_c_config()
    h1 = ScoreSwapCConfig(
        "C18acc-slot-H1-entry-union",
        "空槽進場 fresh∪accel · 換倉仍 fresh",
        entry_pool="fresh_union_accel",
        swap_pool="fresh",
        **_champion_accel_fields(),
    )
    variants = [champ, h1]

    ctx = _load_context(conn, date_start=date_start, date_end=date_end)
    kbar_cache: dict = {}

    variant_results: list[dict[str, Any]] = []
    for cfg in variants:
        print(f"stability-2025: {cfg.variant_id} ...", flush=True)
        periods, summary, slot_snaps = _run_variant(conn, ctx, cfg, kbar_cache)
        slot_stats = _analyze_slots(slot_snaps, ctx["fresh_by_date"])
        subperiod = _by_entry_subperiod(periods, ctx["zone_by_date"])

        ex_full = subperiod["full_2025"].get("mean_excess_pct")
        ex_h1 = subperiod["h1_2025"].get("mean_excess_pct")
        ex_h2 = subperiod["h2_2025"].get("mean_excess_pct")
        print(
            f"  {cfg.variant_id}: 2025={ex_full}%  H1={ex_h1}%  H2={ex_h2}%"
            f"  3/3={slot_stats.get('pct_all_full_3_3')}%",
            flush=True,
        )

        variant_results.append({
            "variant_id": cfg.variant_id,
            "label": cfg.label,
            "full_sample_summary": summary,
            "slot_utilization": slot_stats,
            "stability_2025": subperiod,
            "config": cfg.to_dict(),
        })

    def _ex(vid: str, subkey: str) -> float | None:
        for v in variant_results:
            if v["variant_id"] == vid:
                return (v.get("stability_2025") or {}).get(subkey, {}).get("mean_excess_pct")
        return None

    champ_id = CHAMPION_SCORE_SWAP_C_VARIANT_ID
    h1_id = "C18acc-slot-H1-entry-union"

    def _delta(subkey: str) -> float | None:
        c, h = _ex(champ_id, subkey), _ex(h1_id, subkey)
        return round(float(h) - float(c), 4) if c is not None and h is not None else None

    comparison = {
        "full_2025_delta_excess_pp": _delta("full_2025"),
        "h1_2025_delta_excess_pp": _delta("h1_2025"),
        "h2_2025_delta_excess_pp": _delta("h2_2025"),
        "hot_zones_delta_excess_pp": _delta("hot_zones_2025"),
        "other_zones_delta_excess_pp": _delta("other_zones_2025"),
    }

    print("\n2025 stability comparison (H1 - champion):", flush=True)
    for k, v in comparison.items():
        sign = f"{v:+.4f}pp" if v is not None else "n/a"
        print(f"  {k}: {sign}", flush=True)

    return {
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "mode": "stability-2025",
        "date_start": date_start,
        "date_end": date_end,
        "variants": variant_results,
        "comparison_h1_vs_champion": comparison,
    }


def _print_table(payload: dict[str, Any]) -> None:
    champ = payload["champion"]
    print(
        f"\nChampion {champ['variant_id']}: {champ['mean_excess_pct']}%"
        f"  3/3 full: {(champ.get('slot_utilization') or {}).get('pct_all_full_3_3')}%"
    )
    print(
        f"\n{'variant_id':<36} {'excess%':>8} {'n':>5} {'swaps':>6}"
        f" {'3/3%':>6} {'empty%':>7} {'Δexc':>7} {'Δfull':>6}  label"
    )
    print("-" * 110)
    for v in payload["variants"]:
        de = v.get("delta_excess_pp")
        df = v.get("delta_pct_full_pp")
        de_s = f"{de:+.2f}" if de is not None else "  base"
        df_s = f"{df:+.1f}" if df is not None else "  base"
        print(
            f"{v['variant_id']:<36} {v.get('mean_excess_pct', '—'):>8}"
            f" {v.get('n_periods', 0):>5} {v.get('swaps_total', 0):>6}"
            f" {v.get('pct_3_3_full', '—'):>6} {v.get('pct_any_empty', '—'):>7}"
            f" {de_s:>7} {df_s:>6}  {v.get('label', '')}"
        )

    print("\nBy entry year (mean excess %):")
    years = sorted({y["year"] for v in payload["variants"] for y in v.get("by_entry_year", [])})
    print(f"{'variant_id':<36}" + "".join(f"{y:>8}" for y in years))
    for v in payload["variants"]:
        by_y = {y["year"]: y.get("mean_excess_pct") for y in v.get("by_entry_year", [])}
        row = f"{v['variant_id']:<36}" + "".join(
            f"{by_y[y]:>8}" if by_y.get(y) is not None else f"{'—':>8}" for y in years
        )
        print(row)

    best_ex = payload.get("best_excess")
    best_fill = payload.get("best_slot_fill")
    print(f"\nBest mean_excess variant : {best_ex}")
    print(f"Best slot fill variant   : {best_fill}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc 空槽填倉 sweep H1–H5")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DATE_START)
    parser.add_argument("--date-end", default=DATE_END)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--mode",
        choices=["sweep", "stability-2025"],
        default="sweep",
        help="sweep: full H1–H5 sweep (default); stability-2025: champion vs H1 · 2025 sub-periods",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)
    stamp = date.today().strftime("%Y%m%d")
    try:
        if args.mode == "stability-2025":
            payload = run_stability_2025(
                conn,
                date_start=args.date_start if args.date_start != DATE_START else "2024-01-01",
                date_end=args.date_end if args.date_end != DATE_END else "2025-12-31",
            )
            out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_h1_2025_stability.json"
        else:
            payload = run_sweep(conn, date_start=args.date_start, date_end=args.date_end)
            out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_slot_fill_sweep.json"
    finally:
        conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    if args.mode == "sweep":
        _print_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
