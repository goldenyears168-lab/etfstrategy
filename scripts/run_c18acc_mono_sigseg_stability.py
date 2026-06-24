#!/usr/bin/env python3
"""C18acc vs C18acc-mono-sigseg · 分年 / 子區間 / breadth 穩定性對照。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    RRG_MONO_SWAP_ACCEL_SHORT,
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    _pooled_by_entry_zone,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from report_paths import RESEARCH_RRG
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect

CHAMPION_ID = CHAMPION_SCORE_SWAP_C_VARIANT_ID
CHALLENGER_ID = "C18acc-mono-sigseg"

WINDOWS: list[tuple[str, str, str]] = [
    ("full", "2024-01-01", "2026-06-22"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026-YTD", "2026-01-01", "2026-06-22"),
    ("H1-24~25H1", "2024-01-01", "2025-06-30"),
    ("H2-25H2~26", "2025-07-01", "2026-06-22"),
]


def _mono_sigseg_config() -> ScoreSwapCConfig:
    d = champion_score_swap_c_config().to_dict()
    d.update(
        {
            "variant_id": CHALLENGER_ID,
            "label": "mono_tier2 全池 · 信号日 seg_last 建仓",
            "candidate_pool": "mono_tier2",
        }
    )
    return ScoreSwapCConfig(**{k: d[k] for k in ScoreSwapCConfig.__dataclass_fields__})


def _entry_sigseg():
    c0 = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    return replace(c0, variant_id="sigseg", score_mode="signal_seg_last")


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


def _summarize_legs(periods: list[dict[str, Any]]) -> dict[str, Any]:
    s = summarize_periods(periods)
    n = len(periods)
    if n:
        s["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4)
        s["total_excess_pct"] = round(sum(p["excess_pct"] for p in periods), 4)
        s["mean_hold_days"] = round(sum(p["hold_days"] for p in periods) / n, 2)
    else:
        s["mean_excess_pct"] = None
        s["total_excess_pct"] = None
        s["mean_hold_days"] = None
    s["n_periods"] = n
    return s


def _by_entry_year(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in periods:
        buckets[str(p["entry_date"])[:4]].append(p)
    return [{"year": y, **_summarize_legs(buckets[y])} for y in sorted(buckets)]


def _run_cfg(
    conn,
    ctx: dict[str, Any],
    cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    *,
    entry_c_config=None,
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
        entry_c_config=entry_c_config,
    )
    return periods, _summarize_legs(periods)


def _breadth_gate(pooled: dict[str, dict[str, Any]]) -> dict[str, Any]:
    strong_ex = pooled.get("strong", {}).get("mean_excess_pct")
    ob_ex = pooled.get("overbought", {}).get("mean_excess_pct")
    passed = (
        strong_ex is not None
        and ob_ex is not None
        and float(strong_ex) > 0
        and float(ob_ex) > 0
    )
    thin = [
        f"{z}: n={pooled.get(z, {}).get('n_periods', 0)}"
        for z in BREADTH_ZONES_ORDER
        if 0 < (pooled.get(z, {}).get("n_periods") or 0) < 15
    ]
    return {
        "passed": passed,
        "strong_overbought_positive": passed,
        "strong_excess_pct": strong_ex,
        "overbought_excess_pct": ob_ex,
        "thin_buckets": thin,
    }


def run_comparison(
    conn,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-22",
) -> dict[str, Any]:
    ctx = _load_context(conn, date_start, date_end)
    kbar: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    champion = champion_score_swap_c_config()
    challenger = _mono_sigseg_config()
    sigseg = _entry_sigseg()

    champ_p, champ_s = _run_cfg(conn, ctx, champion, kbar)
    chal_p, chal_s = _run_cfg(conn, ctx, challenger, kbar, entry_c_config=sigseg)

    champ_ex = champ_s.get("mean_excess_pct")
    chal_ex = chal_s.get("mean_excess_pct")

    window_rows: list[dict[str, Any]] = []
    for label, w_start, w_end in WINDOWS:
        if label == "full":
            continue
        wctx = _load_context(conn, w_start, w_end)
        wcache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
        _, s_c = _run_cfg(conn, wctx, champion, wcache)
        _, s_m = _run_cfg(conn, wctx, challenger, wcache, entry_c_config=sigseg)
        ce = s_c.get("mean_excess_pct")
        me = s_m.get("mean_excess_pct")
        window_rows.append(
            {
                "window": label,
                "date_start": w_start,
                "date_end": w_end,
                CHAMPION_ID: s_c,
                CHALLENGER_ID: s_m,
                "delta_mono_sigseg_minus_champion_pp": round(float(me) - float(ce), 4)
                if ce is not None and me is not None
                else None,
            }
        )

    champ_breadth = _pooled_by_entry_zone(champ_p)
    chal_breadth = _pooled_by_entry_zone(chal_p)
    breadth_compare: dict[str, Any] = {}
    for zone in BREADTH_ZONES_ORDER:
        c = champ_breadth.get(zone, {})
        m = chal_breadth.get(zone, {})
        ce = c.get("mean_excess_pct")
        me = m.get("mean_excess_pct")
        breadth_compare[zone] = {
            "zh": BREADTH_ZONE_ZH[zone],
            CHAMPION_ID: c,
            CHALLENGER_ID: m,
            "delta_mono_sigseg_minus_champion_pp": round(float(me) - float(ce), 4)
            if ce is not None and me is not None
            else None,
        }

    champ_gate = _breadth_gate(champ_breadth)
    chal_gate = _breadth_gate(chal_breadth)

    year_rows: list[dict[str, Any]] = []
    years = sorted({str(p["entry_date"])[:4] for p in champ_p} | {str(p["entry_date"])[:4] for p in chal_p})
    champ_by_year = {r["year"]: r for r in _by_entry_year(champ_p)}
    chal_by_year = {r["year"]: r for r in _by_entry_year(chal_p)}
    wins_year = 0
    for y in years:
        c = champ_by_year.get(y, {})
        m = chal_by_year.get(y, {})
        ce = c.get("mean_excess_pct")
        me = m.get("mean_excess_pct")
        delta = round(float(me) - float(ce), 4) if ce is not None and me is not None else None
        if delta is not None and delta > 0:
            wins_year += 1
        year_rows.append(
            {
                "year": y,
                CHAMPION_ID: c,
                CHALLENGER_ID: m,
                "delta_mono_sigseg_minus_champion_pp": delta,
            }
        )

    wins_window = sum(
        1 for w in window_rows if (w.get("delta_mono_sigseg_minus_champion_pp") or 0) > 0
    )
    wins_breadth = sum(
        1
        for z in BREADTH_ZONES_ORDER
        if (breadth_compare[z].get("delta_mono_sigseg_minus_champion_pp") or 0) > 0
        and (breadth_compare[z][CHALLENGER_ID].get("n_periods") or 0) > 0
    )

    full_delta = round(float(chal_ex) - float(champ_ex), 4) if champ_ex and chal_ex else None
    stable = (
        full_delta is not None
        and full_delta > 0
        and wins_year >= 2
        and all(
            (row.get("delta_mono_sigseg_minus_champion_pp") or 0) >= -0.15
            for row in year_rows
        )
        and wins_window >= 4
        and chal_gate["passed"]
    )

    return {
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "champion_id": CHAMPION_ID,
        "challenger_id": CHALLENGER_ID,
        "challenger_label": "mono_tier2 全池 · 信号日 seg_last 建仓 · 换仓规则同冠军",
        "date_start": date_start,
        "date_end": date_end,
        "full_sample": {
            CHAMPION_ID: champ_s,
            CHALLENGER_ID: chal_s,
            "delta_mono_sigseg_minus_champion_pp": full_delta,
        },
        "by_entry_year": year_rows,
        "window_reruns": window_rows,
        "breadth_by_entry_zone": breadth_compare,
        "breadth_gate": {
            CHAMPION_ID: champ_gate,
            CHALLENGER_ID: chal_gate,
        },
        "stability_scorecard": {
            "full_sample_delta_pp": full_delta,
            "years_mono_sigseg_wins": wins_year,
            "years_total": len(year_rows),
            "windows_mono_sigseg_wins": wins_window,
            "windows_total": len(window_rows),
            "breadth_zones_mono_sigseg_wins": wins_breadth,
            "challenger_breadth_gate_passed": chal_gate["passed"],
            "verdict": "stable_upgrade_candidate" if stable else "mixed_not_ready_to_replace_champion",
        },
    }


def _print_report(payload: dict[str, Any]) -> None:
    fs = payload["full_sample"]
    print(f"\n全样本 Δ({CHALLENGER_ID} − {CHAMPION_ID}) = {fs['delta_mono_sigseg_minus_champion_pp']:+.4f} pp")
    print(f"  {CHAMPION_ID}: {fs[CHAMPION_ID]['mean_excess_pct']}% · n={fs[CHAMPION_ID]['n_periods']}")
    print(f"  {CHALLENGER_ID}: {fs[CHALLENGER_ID]['mean_excess_pct']}% · n={fs[CHALLENGER_ID]['n_periods']}")

    print("\n=== 依進場年 ===")
    print(f"{'year':<6} {'champion':>10} {'mono-sig':>10} {'Δpp':>8}")
    for row in payload["by_entry_year"]:
        c = row[CHAMPION_ID].get("mean_excess_pct")
        m = row[CHALLENGER_ID].get("mean_excess_pct")
        d = row.get("delta_mono_sigseg_minus_champion_pp")
        print(f"{row['year']:<6} {c or '—':>9}% {m or '—':>9}% {d if d is not None else '—':>+7}")

    print("\n=== 子區間獨立重跑 ===")
    print(f"{'window':<14} {'champion':>10} {'mono-sig':>10} {'Δpp':>8}")
    for w in payload["window_reruns"]:
        c = w[CHAMPION_ID].get("mean_excess_pct")
        m = w[CHALLENGER_ID].get("mean_excess_pct")
        d = w.get("delta_mono_sigseg_minus_champion_pp")
        print(f"{w['window']:<14} {c or '—':>9}% {m or '—':>9}% {d if d is not None else '—':>+7}")

    print("\n=== Breadth · 進場日分桶 ===")
    print(f"{'zone':<12} {'n_c':>4} {'n_m':>4} {'champ':>8} {'mono-sig':>8} {'Δpp':>8}")
    for zone, row in payload["breadth_by_entry_zone"].items():
        c = row[CHAMPION_ID]
        m = row[CHALLENGER_ID]
        print(
            f"{row['zh']:<12} {c.get('n_periods', 0):>4} {m.get('n_periods', 0):>4} "
            f"{c.get('mean_excess_pct') or '—':>7}% {m.get('mean_excess_pct') or '—':>7}% "
            f"{row.get('delta_mono_sigseg_minus_champion_pp') if row.get('delta_mono_sigseg_minus_champion_pp') is not None else '—':>+7}"
        )

    sc = payload["stability_scorecard"]
    gate = payload["breadth_gate"]
    print(f"\nBreadth gate · champion: {'PASS' if gate[CHAMPION_ID]['passed'] else 'FAIL'}")
    print(f"Breadth gate · mono-sigseg: {'PASS' if gate[CHALLENGER_ID]['passed'] else 'FAIL'}")
    print(f"Verdict: {sc['verdict']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc vs mono-sigseg stability")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_comparison(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_mono_sigseg_stability.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    _print_report(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
