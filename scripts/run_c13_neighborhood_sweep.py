#!/usr/bin/env python3
"""C13 鄰域 sweep · 各變體 vs C0 hold7 · 全樣本 + 分年。"""

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
    C13_NEIGHBORHOOD_SWEEP,
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


def _variant_row(summary: dict, *, c0_excess: float | None) -> dict:
    ex = summary.get("mean_excess_pct")
    delta = round(float(ex) - float(c0_excess), 4) if ex is not None and c0_excess is not None else None
    return {
        "variant_id": summary.get("variant_id"),
        "label": summary.get("label"),
        "min_hold_days": summary.get("min_hold_days"),
        "max_hold_days": summary.get("max_hold_days"),
        "seg_margin": summary.get("seg_margin"),
        "timing_mode": summary.get("timing_mode"),
        "max_swaps_per_day": summary.get("max_swaps_per_day"),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": ex,
        "mean_hold_days": summary.get("mean_hold_days"),
        "swaps_total": summary.get("swaps_total"),
        "max_hold_exits": summary.get("max_hold_exits"),
        "delta_vs_c0_hold7_pp": delta,
        "beats_c0": delta is not None and delta > 0,
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
        )
        summary["min_hold_days"] = cfg.min_hold_days
        summary["max_hold_days"] = cfg.max_hold_days
        summary["timing_mode"] = cfg.timing_mode
        variants.append(_variant_row(summary, c0_excess=c0_excess))

    ranked = sorted(
        variants,
        key=lambda v: (-(v.get("mean_excess_pct") or -999.0), -(v.get("n_periods") or 0)),
    )
    return {
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "c0_hold7": {
            "n_periods": c0_sum.get("n_periods"),
            "mean_excess_pct": c0_excess,
            "mean_hold_days": 7.0,
        },
        "variants": variants,
        "ranked_by_excess": [v["variant_id"] for v in ranked],
        "ranked_by_delta_vs_c0": sorted(
            variants,
            key=lambda v: (-(v.get("delta_vs_c0_hold7_pp") or -999.0), -(v.get("n_periods") or 0)),
        ),
    }


def _stability(full_window: dict, year_windows: list[dict]) -> dict[str, dict]:
    """Per variant: wins C0 in how many year slices."""
    year_labels = [w["label"] for w in year_windows]
    out: dict[str, dict] = {}
    for vid in {v["variant_id"] for v in full_window["variants"]}:
        year_deltas = {}
        for w in year_windows:
            row = next((v for v in w["variants"] if v["variant_id"] == vid), None)
            if row:
                year_deltas[w["label"]] = row.get("delta_vs_c0_hold7_pp")
        wins = sum(1 for d in year_deltas.values() if d is not None and d > 0)
        out[vid] = {
            "year_deltas_pp": year_deltas,
            "years_beating_c0": wins,
            "stable_all_years": wins == len(year_labels) and len(year_labels) > 0,
        }
    return out


def _build_md(payload: dict) -> str:
    full = payload["windows"][0]
    stability = payload.get("stability") or {}
    ranked = full.get("ranked_by_delta_vs_c0") or []
    champion = ranked[0] if ranked else None
    c13 = next((v for v in full["variants"] if v["variant_id"] == "C13"), None)
    c0 = full["c0_hold7"]["mean_excess_pct"]

    lines = [
        "# C13 鄰域 sweep",
        "",
        f"樣本：{full['date_start']} .. {full['date_end']} · 對照 C0 hold7（均超額 {c0}%）",
        "",
        "## 全樣本排名（Δ vs C0 hold7）",
        "",
        "| rank | id | min | max | margin | timing | swaps | n | 均超額% | Δ pp | 三年穩? |",
        "| ---: | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | :---: |",
    ]
    for i, v in enumerate(ranked, 1):
        st = stability.get(v["variant_id"], {})
        stable = "✓" if st.get("stable_all_years") else str(st.get("years_beating_c0", 0))
        lines.append(
            f"| {i} | {v['variant_id']} | {v.get('min_hold_days')} | {v.get('max_hold_days')} "
            f"| {v.get('seg_margin')} | {v.get('timing_mode')} | {v.get('swaps_total')} "
            f"| {v.get('n_periods')} | {v.get('mean_excess_pct')} | {v.get('delta_vs_c0_hold7_pp')} | {stable} |"
        )

    lines.extend(["", "## 分年 Δ vs C0 hold7（pp）", ""])
    year_wins = [w for w in payload["windows"] if w["label"] != "full"]
    header = "| id | full |" + " | ".join(w["label"] for w in year_wins) + " |"
    sep = "| --- | ---:|" + " | ".join("---:" for _ in year_wins) + " |"
    lines.extend([header, sep])
    for v in ranked:
        st = stability.get(v["variant_id"], {})
        yd = st.get("year_deltas_pp") or {}
        cells = [str(v.get("delta_vs_c0_hold7_pp"))]
        for w in year_wins:
            cells.append(str(yd.get(w["label"], "—")))
        lines.append(f"| {v['variant_id']} | " + " | ".join(cells) + " |")

    lines.extend(["", "## 結論", ""])
    if champion:
        beats_c13 = (
            c13 is not None
            and champion["variant_id"] != "C13"
            and (champion.get("mean_excess_pct") or -999) > (c13.get("mean_excess_pct") or -999)
        )
        lines.append(
            f"- **Champion**：{champion['variant_id']} · Δ={champion.get('delta_vs_c0_hold7_pp')} pp "
            f"· swaps={champion.get('swaps_total')} · n={champion.get('n_periods')}"
        )
        if c13:
            lines.append(
                f"- **C13 baseline**：Δ={c13.get('delta_vs_c0_hold7_pp')} pp · "
                f"rank #{next(i for i, x in enumerate(ranked, 1) if x['variant_id'] == 'C13')}"
            )
            lines.append(f"- C13 仍為 champion：**{'否' if beats_c13 else '是'}**")
        st_ch = stability.get(champion["variant_id"], {})
        lines.append(
            f"- 分年穩定（三年皆勝 C0）：{'是' if st_ch.get('stable_all_years') else '否'}"
        )
    lines.append("")
    lines.append("> C13d（entry confirm=2）未納入：ScoreSwapC 填倉尚無 confirm_bars hook。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C13 neighborhood sweep vs C0 hold7")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    configs = C13_NEIGHBORHOOD_SWEEP
    kbar_cache: dict = {}
    conn = connect(args.db)
    try:
        windows = []
        for label, ds, de in WINDOWS:
            print(f"window {label} ({ds} .. {de})", flush=True)
            windows.append(
                _run_window(conn, label=label, date_start=ds, date_end=de, configs=configs, kbar_cache=kbar_cache)
            )
    finally:
        conn.close()

    full = windows[0]
    year_windows = [w for w in windows if w["label"] != "full"]
    stability = _stability(full, year_windows)
    ranked = full.get("ranked_by_delta_vs_c0") or []

    payload = {
        "hypothesis": "C13 鄰域參數在長樣本穩定優於 C0 hold7；微調 min/max_hold · margin · timing",
        "date_start": args.date_start,
        "date_end": args.date_end,
        "reference": "C0 hold7 · scale 5m confirm=1",
        "skipped_variants": [
            {"id": "C13d", "reason": "entry confirm=2 需 intraday entry hook，ScoreSwapC 填倉未支援"},
        ],
        "windows": windows,
        "stability": stability,
        "champion": ranked[0] if ranked else None,
        "c13_rank": next(
            (i for i, v in enumerate(ranked, 1) if v["variant_id"] == "C13"),
            None,
        ),
    }

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c13_neighborhood_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_build_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")

    champ = payload.get("champion") or {}
    c13 = next((v for v in full["variants"] if v["variant_id"] == "C13"), {})
    print(
        f"Champion {champ.get('variant_id')}: Δ={champ.get('delta_vs_c0_hold7_pp')} pp · "
        f"C13 rank #{payload.get('c13_rank')} Δ={c13.get('delta_vs_c0_hold7_pp')} pp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
