#!/usr/bin/env python3
"""Chunge funnel · Minervini-faithful parameter sweep vs RRG mono hold7."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.chunge_funnel_backtest import (  # noqa: E402
    MINERVINI_MAX_ABOVE_PIVOT,
    MINERVINI_MAX_BELOW_PIVOT,
    MINERVINI_NEAR_PIVOT_STATES,
    MINERVINI_SECTION_A_STATES,
    run_chunge_slot_backtest,
)
from research.backtest.slot_backtest_summary import SlotBacktestConfig  # noqa: E402
from report_paths import RESEARCH_VCP  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

RRG_H7_2026 = {
    "n_periods": 39,
    "mean_excess_pct": 6.9973,
    "total_excess_pct": 272.8948,
    "win_rate_vs_bench_pct": 58.97,
}

FILTER_PROFILES: dict[str, dict] = {
    "section_a": {
        "label": "Mark Section A · entry_ready",
        "entry_ready_only": True,
        "execution_states": MINERVINI_SECTION_A_STATES,
        "require_pivot": True,
        "min_dist_pivot_pct": MINERVINI_MAX_BELOW_PIVOT,
        "max_dist_pivot_pct": MINERVINI_MAX_ABOVE_PIVOT,
    },
    "near_pivot": {
        "label": "Near pivot · Pre/Breakout/Early",
        "entry_ready_only": False,
        "execution_states": MINERVINI_NEAR_PIVOT_STATES,
        "require_pivot": True,
        "min_dist_pivot_pct": MINERVINI_MAX_BELOW_PIVOT,
        "max_dist_pivot_pct": 5.0,
    },
    "pre_forming": {
        "label": "Pre-breakout forming · below pivot",
        "entry_ready_only": False,
        "execution_states": ("Pre-breakout",),
        "require_pivot": True,
        "min_dist_pivot_pct": MINERVINI_MAX_BELOW_PIVOT,
        "max_dist_pivot_pct": 0.0,
    },
    "breakout_zone": {
        "label": "Breakout zone · 0–5% above pivot",
        "entry_ready_only": False,
        "execution_states": ("Breakout", "Early-post-breakout"),
        "require_pivot": True,
        "min_dist_pivot_pct": 0.0,
        "max_dist_pivot_pct": 5.0,
    },
}


def _score(summary: dict, *, min_n: int) -> tuple[float, float, float, int]:
    n = int(summary.get("n_periods") or 0)
    me = float(summary.get("mean_excess_pct") or 0.0)
    te = float(summary.get("total_excess_pct") or 0.0)
    wr = float(summary.get("win_rate_vs_bench_pct") or 0.0)
    if n < min_n:
        return (-1e9, me, wr, n)
    parity = 0.0
    if me >= RRG_H7_2026["mean_excess_pct"]:
        parity += 500.0
    if te >= RRG_H7_2026["total_excess_pct"]:
        parity += 500.0
    elif te >= RRG_H7_2026["total_excess_pct"] * 0.6:
        parity += 200.0
    if wr >= RRG_H7_2026["win_rate_vs_bench_pct"]:
        parity += 100.0
    return (parity + te + me * n * 0.25, me, wr, n)


def _variant_name(profile: str, entry_mode: str, hold: int, slots: int) -> str:
    return f"chunge_{profile}_{entry_mode}_s{slots}_h{hold}"


def run_sweep(
    conn,
    *,
    date_start: str,
    date_end: str,
    min_n: int,
    profiles: tuple[str, ...],
    entry_modes: tuple[str, ...],
    n_slots_list: tuple[int, ...],
    hold_days_list: tuple[int, ...],
    min_composite_list: tuple[float, ...],
    max_wait_list: tuple[int, ...],
) -> list[dict]:
    rows: list[dict] = []
    combos = list(
        itertools.product(
            profiles,
            entry_modes,
            n_slots_list,
            hold_days_list,
            min_composite_list,
            max_wait_list,
        )
    )
    total = len(combos)
    for i, (prof, entry_mode, n_slots, hold, min_comp, max_wait) in enumerate(combos, 1):
        fp = FILTER_PROFILES[prof]
        if entry_mode == "close":
            max_wait_eff = 0
        else:
            max_wait_eff = max_wait
        if entry_mode == "close" and max_wait != max_wait_list[0]:
            continue
        cfg = SlotBacktestConfig(
            date_start=date_start,
            date_end=date_end,
            n_slots=n_slots,
            hold_days=hold,
            min_composite=min_comp,
            model_id="vcp-funnel",
            execution_states=tuple(fp["execution_states"]),
            entry_ready_only=bool(fp["entry_ready_only"]),
            require_pivot=bool(fp["require_pivot"]),
            min_dist_pivot_pct=fp.get("min_dist_pivot_pct"),
            max_dist_pivot_pct=fp.get("max_dist_pivot_pct"),
            entry_price_mode=entry_mode,
            max_entry_wait_days=max_wait_eff,
            variant=_variant_name(prof, entry_mode, hold, n_slots),
        )
        result = run_chunge_slot_backtest(conn, config=cfg)
        s = result["summary"]
        sc = _score(s, min_n=min_n)
        rows.append(
            {
                "profile": prof,
                "profile_label": fp["label"],
                "entry_mode": entry_mode,
                "n_slots": n_slots,
                "hold_days": hold,
                "min_composite": min_comp,
                "max_entry_wait": max_wait_eff,
                "variant": cfg.variant,
                "n_periods": s.get("n_periods"),
                "mean_excess_pct": s.get("mean_excess_pct"),
                "total_excess_pct": s.get("total_excess_pct"),
                "win_rate_vs_bench_pct": s.get("win_rate_vs_bench_pct"),
                "mean_return_pct": s.get("mean_return_pct"),
                "n_stopped": s.get("n_stopped"),
                "n_time_exit": s.get("n_time_exit"),
                "n_pending_expired": s.get("n_pending_expired"),
                "screen_coverage_pct": s.get("screen_coverage_pct"),
                "score": round(sc[0], 2),
            }
        )
        if i % 50 == 0 or i == total:
            print(f"  [{i}/{total}] latest score={rows[-1]['score']:.0f} n={rows[-1]['n_periods']}", file=sys.stderr)
    rows.sort(key=lambda r: (r["score"], r["mean_excess_pct"] or 0, r["n_periods"] or 0), reverse=True)
    return rows


def render_markdown(
    rows: list[dict],
    *,
    date_start: str,
    date_end: str,
    top_n: int,
    title: str = "Minervini sweep",
) -> str:
    stamp = date.today().isoformat()
    lines = [
        f"# Chunge funnel · {title} · {date_start}～{date_end}",
        "",
        f"> 產出 {stamp} · 對照 **RRG mono hold7**："
        f" n={RRG_H7_2026['n_periods']} · mean excess {RRG_H7_2026['mean_excess_pct']:.2f}% · "
        f"total excess {RRG_H7_2026['total_excess_pct']:.1f}% · "
        f"win vs bench {RRG_H7_2026['win_rate_vs_bench_pct']:.1f}%",
        "",
        "Mark / vcp-tm 核心：Section A（`entry_ready`）· pivot −8%～+8% · pivot 突破進 · contraction low 停損。",
        "",
        "## Top combinations",
        "",
        "| # | profile | entry | slots | hold | min | wait | n | mean α | total α | win% | score |",
        "|---|---------|-------|-------|------|-----|------|---|--------|---------|------|-------|",
    ]
    for i, r in enumerate(rows[:top_n], 1):
        lines.append(
            f"| {i} | {r['profile']} | {r['entry_mode']} | {r['n_slots']} | {r['hold_days']} | "
            f"{r['min_composite']:.0f} | {r['max_entry_wait']} | {r['n_periods']} | "
            f"{r['mean_excess_pct']} | {r['total_excess_pct']} | {r['win_rate_vs_bench_pct']} | {r['score']} |"
        )
    best = rows[0] if rows else None
    if best:
        beats = (
            (best.get("mean_excess_pct") or 0) >= RRG_H7_2026["mean_excess_pct"]
            and (best.get("total_excess_pct") or 0) >= RRG_H7_2026["total_excess_pct"]
        )
        lines.extend(
            [
                "",
                "## Best pick",
                "",
                f"- **variant**: `{best['variant']}`",
                f"- **profile**: {best['profile_label']}",
                f"- entry `{best['entry_mode']}` · {best['n_slots']} slots · hold{best['hold_days']} · "
                f"composite≥{best['min_composite']} · wait {best['max_entry_wait']}",
                f"- n={best['n_periods']} · mean excess {best['mean_excess_pct']}% · "
                f"total excess {best['total_excess_pct']}% · win {best['win_rate_vs_bench_pct']}%",
                f"- **RRG parity**: {'✓ 達標' if beats else '✗ 未達標（見 total α / mean α）'}",
            ]
        )
    return "\n".join(lines) + "\n"


def screen_db_stats(conn, *, date_start: str, date_end: str) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS n_rows,
            COUNT(DISTINCT as_of_date) AS n_days,
            SUM(CASE WHEN entry_ready = 1 THEN 1 ELSE 0 END) AS n_entry_ready,
            SUM(CASE WHEN stop_loss IS NOT NULL AND stop_loss > 0 THEN 1 ELSE 0 END) AS n_with_stop,
            SUM(CASE WHEN risk_pct IS NOT NULL AND risk_pct > 0 THEN 1 ELSE 0 END) AS n_with_risk,
            SUM(CASE WHEN entry_ready = 1 AND stop_loss IS NOT NULL THEN 1 ELSE 0 END) AS n_ready_with_stop
        FROM vcp_screen_scores_v2
        WHERE model_id IN ('vcp-funnel', 'chunge-funnel')
          AND as_of_date >= ? AND as_of_date <= ?
        """,
        (date_start, date_end),
    ).fetchone()
    return dict(row) if row else {}


def render_screen_stats_md(stats: dict, *, date_start: str, date_end: str) -> str:
    return "\n".join(
        [
            "## Screen DB（chunge-funnel）",
            "",
            f"- 區間：{date_start}～{date_end}",
            f"- score 列：{stats.get('n_rows', 0)} · 交易日 {stats.get('n_days', 0)}",
            f"- `entry_ready=1`：{stats.get('n_entry_ready', 0)}",
            f"- 有 `stop_loss`：{stats.get('n_with_stop', 0)} · 有 `risk_pct`：{stats.get('n_with_risk', 0)}",
            f"- Section A 完整（ready+stop）：{stats.get('n_ready_with_stop', 0)}",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chunge funnel Minervini parameter sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-18")
    parser.add_argument("--min-n", type=int, default=8, help="最低成交筆數門檻")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--write-best-json", action="store_true")
    parser.add_argument(
        "--hold7",
        action="store_true",
        help="RRG 對照基準子 sweep：3 槽 · hold7 · near_pivot/section_a/pre_forming",
    )
    args = parser.parse_args(argv)

    if args.hold7:
        profiles = ("near_pivot", "section_a", "pre_forming")
        entry_modes = ("close", "pivot_stop", "breakout_close")
        n_slots_list = (3,)
        hold_days_list = (7,)
        min_composite_list = (45.0, 50.0, 55.0, 60.0)
        max_wait_list = (5, 10, 15)
        sweep_title = "Hold7 × 3-slot sweep（RRG 對照基準）"
        report_stem = "chunge_funnel_hold7_sweep"
        min_n = max(5, args.min_n)
    else:
        profiles = tuple(FILTER_PROFILES)
        entry_modes = ("close", "pivot_stop", "breakout_close")
        n_slots_list = (3, 5)
        hold_days_list = (7, 10, 20)
        min_composite_list = (45.0, 50.0, 55.0, 60.0)
        max_wait_list = (5, 10, 15)
        sweep_title = "Minervini sweep"
        report_stem = "chunge_funnel_minervini_sweep"
        min_n = args.min_n

    n_combos = (
        len(profiles)
        * len(entry_modes)
        * len(n_slots_list)
        * len(hold_days_list)
        * len(min_composite_list)
        * len(max_wait_list)
    )
    if not args.hold7:
        n_combos = n_combos // 3 + len(profiles) * len(entry_modes) * len(n_slots_list) * len(hold_days_list) * 1  # close dedup approx
    print(
        f"Sweep chunge-funnel {args.date_start}..{args.date_end} (~{n_combos} combos)...",
        file=sys.stderr,
    )
    conn = connect(args.db)
    try:
        screen_stats = screen_db_stats(conn, date_start=args.date_start, date_end=args.date_end)
        rows = run_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            min_n=min_n,
            profiles=profiles,
            entry_modes=entry_modes,
            n_slots_list=n_slots_list,
            hold_days_list=hold_days_list,
            min_composite_list=min_composite_list,
            max_wait_list=max_wait_list,
        )
    finally:
        conn.close()

    md = render_markdown(
        rows,
        date_start=args.date_start,
        date_end=args.date_end,
        top_n=args.top,
        title=sweep_title,
    )
    md = render_screen_stats_md(screen_stats, date_start=args.date_start, date_end=args.date_end) + md
    print(md)

    if args.write_report:
        out = RESEARCH_VCP / f"{date.today().strftime('%Y%m%d')}_{report_stem}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        json_out = out.with_suffix(".json")
        json_out.write_text(json.dumps(rows[: args.top], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
        print(f"Wrote {json_out}", file=sys.stderr)

    if args.write_best_json and rows and not args.hold7:
        faithful = [
            r
            for r in rows
            if r["entry_mode"] in ("breakout_close", "pivot_stop")
            and (r.get("mean_excess_pct") or 0) >= RRG_H7_2026["mean_excess_pct"]
            and (r.get("total_excess_pct") or 0) >= RRG_H7_2026["total_excess_pct"]
        ]
        best = faithful[0] if faithful else rows[0]
        fp = FILTER_PROFILES[best["profile"]]
        cfg = SlotBacktestConfig(
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=best["n_slots"],
            hold_days=best["hold_days"],
            min_composite=best["min_composite"],
            model_id="vcp-funnel",
            execution_states=tuple(fp["execution_states"]),
            entry_ready_only=bool(fp["entry_ready_only"]),
            require_pivot=bool(fp["require_pivot"]),
            min_dist_pivot_pct=fp.get("min_dist_pivot_pct"),
            max_dist_pivot_pct=fp.get("max_dist_pivot_pct"),
            entry_price_mode=best["entry_mode"],
            max_entry_wait_days=best["max_entry_wait"],
            variant="vcp-pivot-gate-h20",
            source_summary=str(
                RESEARCH_VCP / "vcp_pivot_gate_slot_backtest_2026.json"
            ),
        )
        conn = connect(args.db)
        try:
            result = run_chunge_slot_backtest(conn, config=cfg)
        finally:
            conn.close()
        from research.backtest.slot_backtest_summary import (  # noqa: E402
            build_summary_payload,
            write_slot_backtest_summary,
        )

        payload = build_summary_payload(
            track_id="vcp-pivot-gate",
            config=cfg,
            summary=result["summary"],
            source_module="chunge_funnel_backtest",
            extra={
                "sweep_profile": best["profile"],
                "sweep_rank": 1,
                "sweep_score": best["score"],
                "rrg_parity_target": RRG_H7_2026,
            },
        )
        path = RESEARCH_VCP / "vcp_pivot_gate_slot_backtest_2026.json"
        write_slot_backtest_summary(path, payload)
        print(f"Wrote {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
