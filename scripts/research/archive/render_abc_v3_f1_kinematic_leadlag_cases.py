#!/usr/bin/env python3
"""ABC v3+f1 · champion vs failure kinematic trajectory PDF.

Reads lead-lag study JSON · rebuilds timeline for selected legs only ·
multi-page PDF + PNG (same pattern as render_rrg_abc_teaching_deck.py).

  PYTHONPATH=src python3 scripts/research/archive/render_abc_v3_f1_kinematic_leadlag_cases.py
  PYTHONPATH=src python3 scripts/research/archive/render_abc_v3_f1_kinematic_leadlag_cases.py \\
    --json reports/research/rrg/20260709_abc_v3_f1_kinematic_leadlag_study.json \\
    --n-per-cohort 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.abc_v3_f1_overnight_regime_annot import (  # noqa: E402
    load_abc_f1_trade_ledger_from_json,
)
from research.backtest.archive.abc_v3_f1_kinematic_leadlag_study import (  # noqa: E402
    _ledger_row_to_period,
    mark_leg_events,
)
from research.backtest.archive.c18acc_kinematic_timeline_cache import (  # noqa: E402
    KinematicSnap,
    _build_kinematic_timeline,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (  # noqa: E402
    _collect_annot_dates,
    _exit_frame_idx_for_date,
)
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (  # noqa: E402
    _frame_idx_for_minute,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps  # noqa: E402
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from rrg_universe_intraday_panel import (  # noqa: E402
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
for _fp in _FONT_CANDIDATES:
    if os.path.exists(_fp):
        fm.fontManager.addfont(_fp)
        plt.rcParams["font.family"] = fm.FontProperties(fname=_fp).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False

C_W3 = "#C0392B"
C_W5 = "#E67E22"
C_W20 = "#0B6E4F"
C_PRICE = "#2E6DB4"
INK = "#1A1A1A"
GREY = "#8A8A8A"


@dataclass(frozen=True)
class CaseLeg:
    leg_id: str
    cohort: str
    stock_id: str
    stock_name: str
    entry_date: str
    exit_date: str
    entry_minute: str | None
    net_pct: float
    events: dict[str, Any]
    lead_lag: dict[str, Any]
    timeline: list[KinematicSnap]


def _select_cases(
    leg_rows: list[dict[str, Any]],
    *,
    n_per_cohort: int,
) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {"champion": [], "failure": []}
    for row in leg_rows:
        c = row.get("cohort")
        if c in by:
            by[c].append(row)
    ch = sorted(by["champion"], key=lambda r: float(r.get("net_pct") or 0), reverse=True)
    fa = sorted(by["failure"], key=lambda r: float(r.get("net_pct") or 0))
    out: list[dict[str, Any]] = []
    for r in ch[:n_per_cohort]:
        out.append(r)
    for r in fa[:n_per_cohort]:
        out.append(r)
    return out


def _ledger_index(ledger: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for row in ledger:
        key = f"{row.get('entry_date')}|{row.get('stock_id')}|{row.get('idx', 0)}"
        idx[key] = row
    return idx


def _rebuild_case_timelines(
    conn,
    *,
    ledger_path: Path,
    case_rows: list[dict[str, Any]],
) -> list[CaseLeg]:
    ledger, _meta = load_abc_f1_trade_ledger_from_json(ledger_path)
    lidx = _ledger_index(ledger)
    periods = []
    for case in case_rows:
        row = lidx.get(case["leg_id"])
        if row:
            periods.append(_ledger_row_to_period(row))

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, full_dates)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=annot_dates,
        stock_ids=stock_ids,
        poll_minutes=intraday_poll_minutes(),
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        kbar_bar_minutes=5,
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    cases: list[CaseLeg] = []
    for case in case_rows:
        row = lidx.get(case["leg_id"])
        if not row:
            continue
        period = _ledger_row_to_period(row)
        sid = str(period["stock_id"])
        entry_px = period.get("entry_px")
        if not entry_px or float(entry_px) <= 0:
            continue
        points = stock_maps.get(sid)
        if not points:
            continue
        entry_i = _frame_idx_for_minute(frames, str(period["entry_date"]), period.get("entry_minute"))
        exit_i = _exit_frame_idx_for_date(frames, str(period.get("exit_date") or ""))
        if exit_i < entry_i:
            continue
        timeline = _build_kinematic_timeline(
            conn,
            bench,
            stock_id=sid,
            entry_i=entry_i,
            exit_i=exit_i,
            frames=frames,
            points_by_idx=points,
            entry_px=float(entry_px),
        )
        if len(timeline) < 2:
            continue
        events = mark_leg_events(timeline)
        cases.append(
            CaseLeg(
                leg_id=case["leg_id"],
                cohort=str(case.get("cohort") or ""),
                stock_id=sid,
                stock_name=str(period.get("stock_name") or sid),
                entry_date=str(period["entry_date"]),
                exit_date=str(period.get("exit_date") or ""),
                entry_minute=period.get("entry_minute"),
                net_pct=float(case.get("net_pct") or 0),
                events=events,
                lead_lag=case.get("lead_lag") or {},
                timeline=timeline,
            )
        )
    return cases


def _x_labels(timeline: list[KinematicSnap]) -> list[str]:
    return [f"{s.trade_date[-5:]}\n{s.minute[:5]}" for s in timeline]


def _mark_events(ax, events: dict[str, Any], *, color_peak: str, color_gb: str) -> None:
    peak_i = events.get("T_price_peak")
    if peak_i is not None:
        ax.axvline(peak_i, color=color_peak, ls="-", lw=1.6, alpha=0.85, label="價格 peak")
    for g in (1, 2):
        gi = events.get(f"T_giveback_{g}pct")
        if gi is not None:
            ax.axvline(gi, color=color_gb, ls=":", lw=1.2, alpha=0.7, label=f"回吐 {g}%")
    for leg, col in (("w3", C_W3), ("w5", C_W5), ("w20", C_W20)):
        di = events.get(f"T_{leg}_mv_drop")
        if di is not None:
            ax.axvline(di, color=col, ls="--", lw=1.0, alpha=0.65, label=f"{leg.upper()} MV↓")


def _draw_case_page(fig: plt.Figure, case: CaseLeg, *, page_no: int, total: int) -> None:
    fig.clf()
    tl = case.timeline
    xs = list(range(len(tl)))
    labels = _x_labels(tl)
    cohort_zh = "贏家 Champion" if case.cohort == "champion" else "輸家 Failure"
    title = (
        f"{cohort_zh} · {case.stock_id} {case.stock_name} · "
        f"{case.entry_date} → {case.exit_date} · net {case.net_pct:+.2f}%"
    )
    fig.suptitle(title, fontsize=13, fontweight="bold", color=INK, y=0.98)

    ax1 = fig.add_subplot(3, 1, 1)
    rets = [s.ret_from_entry_pct for s in tl]
    ax1.plot(xs, rets, color=C_PRICE, lw=2.2, marker="o", ms=3)
    ax1.axhline(0, color=GREY, lw=0.8)
    _mark_events(ax1, case.events, color_peak="#27AE60", color_gb="#8E44AD")
    ax1.set_ylabel("報酬 %")
    ax1.set_title("價格軌跡（自進場）", fontsize=11, loc="left")
    ax1.grid(True, alpha=0.25)

    ax2 = fig.add_subplot(3, 1, 2, sharex=ax1)
    for leg, col in (("w3", C_W3), ("w5", C_W5), ("w20", C_W20)):
        vals = [getattr(s, f"{leg}_mv") for s in tl]
        ax2.plot(xs, vals, color=col, lw=1.8, marker=".", ms=3, label=leg.upper())
    ax2.axhline(100, color=GREY, ls="--", lw=0.8)
    _mark_events(ax2, case.events, color_peak="#27AE60", color_gb="#8E44AD")
    ax2.set_ylabel("MV")
    ax2.set_title("WMA 動能 MV（20/5/3）", fontsize=11, loc="left")
    ax2.legend(loc="upper right", fontsize=8, ncol=3)
    ax2.grid(True, alpha=0.25)

    ax3 = fig.add_subplot(3, 1, 3, sharex=ax1)
    for leg, col in (("w3", C_W3), ("w5", C_W5), ("w20", C_W20)):
        vals = [getattr(s, f"dmv_{leg}") for s in tl]
        ax3.plot(xs, vals, color=col, lw=1.6, marker=".", ms=3, label=f"ΔMV {leg.upper()}")
    ax3.axhline(0, color=GREY, lw=0.8)
    _mark_events(ax3, case.events, color_peak="#27AE60", color_gb="#8E44AD")
    ax3.set_ylabel("Δ from peak")
    ax3.set_title("相對運動學 · MV 距持有期高點", fontsize=11, loc="left")
    ax3.legend(loc="lower left", fontsize=8, ncol=3)
    ax3.grid(True, alpha=0.25)

    step = max(1, len(xs) // 8)
    tick_idx = xs[::step]
    ax3.set_xticks(tick_idx)
    ax3.set_xticklabels([labels[i] for i in tick_idx], fontsize=7)
    ax3.set_xlabel("盤中 poll（30m）")

    ll = case.lead_lag
    cascade = (case.events or {}).get("mv_drop_cascade") or "—"
    note = (
        f"W3 領先 peak: {ll.get('w3_before_peak')} · lead {ll.get('w3_lead_peak_polls')} polls · "
        f"cascade {cascade} · n_snaps={ll.get('n_snaps')}"
    )
    fig.text(0.05, 0.02, note, fontsize=9, color=GREY)
    fig.text(0.95, 0.02, f"{page_no}/{total}", ha="right", fontsize=9, color=GREY)
    fig.subplots_adjust(top=0.90, bottom=0.10, hspace=0.38)


def _draw_overview_page(
    fig: plt.Figure,
    payload: dict[str, Any],
    *,
    cases: list[CaseLeg],
) -> None:
    fig.clf()
    fig.suptitle(
        "ABC v3+f1 · Kinematic lead-lag · Champion vs Failure 軌跡對照",
        fontsize=14,
        fontweight="bold",
        y=0.96,
    )
    summaries = {s["cohort"]: s for s in payload.get("cohort_summaries") or []}
    champ = summaries.get("champion") or {}
    fail = summaries.get("failure") or {}

    lines = [
        f"窗口 {payload.get('date_start')} .. {payload.get('date_end')} · "
        f"legs {payload.get('n_legs_analyzed')} · poll {payload.get('poll')}",
        "",
        "【全樣本摘要】",
        f"  Champion n={champ.get('n')} · 均 net {champ.get('mean_net_pct')}% · "
        f"W3 領先 peak {champ.get('pct_w3_before_peak')}% · mean lead {champ.get('mean_w3_lead_peak_polls')} polls",
        f"  Failure  n={fail.get('n')} · 均 net {fail.get('mean_net_pct')}% · "
        f"W3 領先 peak {fail.get('pct_w3_before_peak')}% · mean lead {fail.get('mean_w3_lead_peak_polls')} polls",
        "",
        "【解讀】贏家：MV 常早於 price peak 轉弱，價格仍可創高。",
        "        輸家：MV 與 peak 同步或滯後，警示較晚。",
        "",
        "【本 PDF 案例】",
    ]
    for i, c in enumerate(cases, 1):
        tag = "贏" if c.cohort == "champion" else "輸"
        lines.append(
            f"  {i}. [{tag}] {c.stock_id} {c.entry_date} net {c.net_pct:+.2f}% · "
            f"W3 before peak={c.lead_lag.get('w3_before_peak')}"
        )
    lines.extend(
        [
            "",
            "圖例：綠實線=價格 peak · 紫點線=回吐 · 紅/橘/綠虛線=W3/W5/W20 MV 首次下降",
            f"來源：{Path(str(payload.get('ledger_source') or '')).name}",
        ]
    )
    fig.text(0.08, 0.88, "\n".join(lines), va="top", fontsize=11, color=INK)
    fig.text(0.5, 0.04, "下一頁起為逐筆軌跡（價格 · MV · ΔMV）", ha="center", fontsize=10, color=GREY)


def render_kinematic_leadlag_cases_pdf(
    payload: dict[str, Any],
    cases: list[CaseLeg],
    *,
    out_pdf: Path,
    png_dir: Path | None = None,
) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if png_dir:
        png_dir.mkdir(parents=True, exist_ok=True)

    total = 1 + len(cases)
    fig = plt.figure(figsize=(13.33, 7.5))

    with PdfPages(out_pdf) as pdf:
        _draw_overview_page(fig, payload, cases=cases)
        pdf.savefig(fig, dpi=150)
        if png_dir:
            fig.savefig(png_dir / "page_00_overview.png", dpi=150)

        for i, case in enumerate(cases, start=1):
            _draw_case_page(fig, case, page_no=i + 1, total=total)
            pdf.savefig(fig, dpi=150)
            if png_dir:
                tag = "champ" if case.cohort == "champion" else "fail"
                fig.savefig(png_dir / f"page_{i:02d}_{tag}_{case.stock_id}.png", dpi=150)

    plt.close(fig)
    return out_pdf


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render ABC f1 kinematic case trajectory PDF")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--json",
        type=Path,
        default=RESEARCH_RRG / "20260709_abc_v3_f1_kinematic_leadlag_study.json",
    )
    ap.add_argument("--n-per-cohort", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.json.exists():
        print(f"BLOCKER: JSON not found: {args.json}", file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    payload = json.loads(args.json.read_text(encoding="utf-8"))
    leg_rows = payload.get("leg_rows") or []
    if not leg_rows:
        print("BLOCKER: leg_rows empty in JSON", file=sys.stderr)
        return 1

    selected = _select_cases(leg_rows, n_per_cohort=args.n_per_cohort)
    ledger_path = Path(str(payload.get("ledger_source") or ""))
    if not ledger_path.exists():
        print(f"BLOCKER: ledger_source missing: {ledger_path}", file=sys.stderr)
        return 1

    print(
        f"rebuild timelines: {len(selected)} cases · ledger={ledger_path.name}",
        flush=True,
    )
    conn = connect(args.db)
    try:
        cases = _rebuild_case_timelines(conn, ledger_path=ledger_path, case_rows=selected)
    finally:
        conn.close()

    if not cases:
        print("BLOCKER: no timelines rebuilt", file=sys.stderr)
        return 1

    stamp = date.today().strftime("%Y%m%d")
    out_pdf = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_kinematic_leadlag_cases.pdf"
    png_dir = out_pdf.parent / f"{out_pdf.stem}_pages"

    render_kinematic_leadlag_cases_pdf(payload, cases, out_pdf=out_pdf, png_dir=png_dir)
    print(f"PDF : {out_pdf}")
    print(f"PNG : {png_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
