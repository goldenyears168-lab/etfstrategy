#!/usr/bin/env python3
"""Fair fixed-hold sweep · Leading Dip ↔ C18acc (Research only).

Both arms keep native exit-calendar helpers; labels are nominal hold days.
  * Dip: T+N close via ``collect_leading_dip_events(..., hold_days=N)``
  * C18acc: ``exit_close_date_from_entry`` (inclusive · hold N → calendar T+(N−1))
    at 13:30 via ``compute_hold_nd_event_leg``

  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_c18acc_hold_sweep.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_c18acc_hold_sweep.py --collect-c18
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_hold3d_event_study import (  # noqa: E402
    C18ACC_SLOTS_DEFAULT,
    _legs_to_hold3d_stats,
    collect_c18acc_simulation_entries,
)
from research.backtest.dual_wma_signal_backtest import ROUND_TRIP_COST_PCT  # noqa: E402
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from research.backtest.leading_dip_sleeve_validate import (  # noqa: E402
    OOS_CUT_DEFAULT,
    START_DEFAULT,
    TOTAL_OPEN_CAP,
    apply_frozen_policy,
    collect_leading_dip_events,
)
from research.backtest.leading_dip_vs_abc_fair_compare import (  # noqa: E402
    apply_cost_haircut,
    summarize_fair_arm,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

SCHEMA = "leading_dip_vs_c18acc_hold_sweep-v1"
COST_HAIRCUT_PP = float(ROUND_TRIP_COST_PCT)
HOLD_SWEEP_DEFAULT = (3, 5, 7, 10)
DEFAULT_STEM = RESEARCH_RRG / "20260716_leading_dip_vs_c18acc_hold_sweep"
DEFAULT_C18_CACHE = RESEARCH_RRG / "_cache" / "c18acc_fair_entries_from202501.json"


def _fmt_pct(x: Any) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{100.0 * float(x):.0f}%"


def _fmt_pp(x: Any) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{float(x):+.2f}"


def _summarize_from_series(
    dates: list[str],
    px_net: list[float],
    ex_net: list[float],
    *,
    oos_cut: str,
    n_trading_days: int | None,
) -> dict[str, Any]:
    df = pd.DataFrame({"date": dates, "px_net": px_net, "ex_net": ex_net})
    return summarize_fair_arm(
        df, oos_cut=oos_cut, n_trading_days=n_trading_days
    )


def dip_events_to_fair(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    rows = []
    for r in df.itertuples(index=False):
        px_g = float(r.px3)
        ex_g = float(r.ex3)
        px_n, ex_n = apply_cost_haircut(px_g, ex_g, haircut_pp=COST_HAIRCUT_PP)
        rows.append(
            {
                "date": str(r.date),
                "sid": str(r.sid),
                "minute": str(r.minute),
                "exit_date": str(r.exit_date),
                "T2": bool(r.T2),
                "ex0": float(r.ex0),
                "gap0": float(r.gap0),
                "rB_min": float(r.rB_min),
                "px_gross": px_g,
                "ex_gross": ex_g,
                "px_net": px_n,
                "ex_net": ex_n,
                "source": "leading_dip",
            }
        )
    return pd.DataFrame(rows)


def run_dip_hold(
    con: Any,
    *,
    hold_days: int,
    start: str,
    oos_cut: str,
) -> dict[str, Any]:
    print(f"  Dip hold={hold_days}d …", flush=True)
    t0 = time.time()
    raw, trading = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        universe_mode="leading",
        hold_days=hold_days,
    )
    fair = dip_events_to_fair(raw)
    raw_sum = summarize_fair_arm(
        fair, oos_cut=oos_cut, n_trading_days=len(trading)
    )
    slots_sum: dict[str, Any]
    if fair.empty:
        slots_sum = summarize_fair_arm(
            fair, oos_cut=oos_cut, n_trading_days=len(trading)
        )
    else:
        slotted = apply_frozen_policy(
            fair, trading, require_t2=False, max_open=TOTAL_OPEN_CAP
        )
        slots_sum = summarize_fair_arm(
            slotted, oos_cut=oos_cut, n_trading_days=len(trading)
        )
    return {
        "hold_days": hold_days,
        "runtime_s": round(time.time() - t0, 1),
        "raw": raw_sum,
        "slots": slots_sum,
        "calendar": "dip_T_plus_N_close",
    }


def load_or_collect_c18_entries(
    con: Any,
    *,
    trade_dates: list[str],
    cache_path: Path,
    collect: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if cache_path.exists() and not collect:
        blob = json.loads(cache_path.read_text(encoding="utf-8"))
        entries = list(blob.get("entries") or [])
        meta = dict(blob.get("meta") or {})
        print(f"Loaded C18acc cache n={len(entries)} from {cache_path}", flush=True)
        return entries, meta
    if not collect and not cache_path.exists():
        raise FileNotFoundError(
            f"C18acc cache missing: {cache_path}. Re-run with --collect-c18"
        )
    print(
        f"Collecting C18acc 3-slot entries over {len(trade_dates)} days…",
        flush=True,
    )
    t0 = time.time()
    entries, meta = collect_c18acc_simulation_entries(
        con, trade_dates, n_slots=C18ACC_SLOTS_DEFAULT
    )
    meta = {
        **meta,
        "runtime_s": round(time.time() - t0, 1),
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_trade_dates": len(trade_dates),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema": "c18acc_fair_entries-v1",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "entries": entries,
                "meta": meta,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Wrote C18acc cache n={len(entries)} → {cache_path} "
        f"({meta['runtime_s']}s)",
        flush=True,
    )
    return entries, meta


def run_c18_hold(
    con: Any,
    entries: list[dict[str, Any]],
    *,
    hold_days: int,
    oos_cut: str,
    n_trading_days: int,
    bench: Any,
) -> dict[str, Any]:
    print(f"  C18acc hold={hold_days}d …", flush=True)
    t0 = time.time()
    legs, _legacy = _legs_to_hold3d_stats(
        con, entries, hold_days=hold_days, bench=bench
    )
    dates = [str(lg["entry_date"]) for lg in legs]
    px = [float(lg["net_pct"]) for lg in legs]
    ex = [float(lg["excess_pct"]) for lg in legs]
    summ = _summarize_from_series(
        dates, px, ex, oos_cut=oos_cut, n_trading_days=n_trading_days
    )
    # actual S2 (once; independent of counterfactual hold)
    return {
        "hold_days": hold_days,
        "runtime_s": round(time.time() - t0, 1),
        "fixed": summ,
        "n_legs": len(legs),
        "calendar": "c18_inclusive_N_days_1330",
    }


def actual_s2_summary(
    entries: list[dict[str, Any]],
    *,
    oos_cut: str,
    n_trading_days: int,
) -> dict[str, Any]:
    rows = [
        e
        for e in entries
        if e.get("actual_return_pct") is not None and e.get("entry_date")
    ]
    if not rows:
        return summarize_fair_arm(
            pd.DataFrame(), oos_cut=oos_cut, n_trading_days=n_trading_days
        )
    dates = [str(e["entry_date"]) for e in rows]
    # actual_return_pct is already net of costs in sim
    px = [float(e["actual_return_pct"]) for e in rows]
    # no excess in raw sim field for all rows — leave ex = px as portfolio
    # relative placeholder is wrong; compute none. Prefer leave ex as net
    # without bench when missing (mark note).
    df = pd.DataFrame({"date": dates, "px_net": px, "ex_net": px})
    out = summarize_fair_arm(df, oos_cut=oos_cut, n_trading_days=n_trading_days)
    out["note"] = "actual S2/dynamic exit · excess_proxy=net (no bench strip here)"
    return out


def decide_verdict(by_hold: dict[str, Any]) -> dict[str, Any]:
    """Prefer OOS med_ex at hold 5 & 10 (C18acc live band), slots for Dip."""
    reasons: list[str] = []
    scores = {"c18": 0, "dip": 0, "tie": 0}
    for h in (5, 10):
        key = str(h)
        block = by_hold.get(key) or {}
        c18 = (block.get("c18") or {}).get("fixed") or {}
        dip = (block.get("dip") or {}).get("slots") or {}
        c_med = c18.get("oos_med_ex")
        d_med = dip.get("oos_med_ex")
        c_hit = c18.get("oos_hit_ex")
        d_hit = dip.get("oos_hit_ex")
        if c_med is None or d_med is None:
            reasons.append(f"hold{h}: insufficient OOS")
            continue
        # edge = med then hit as tie-break
        if float(d_med) > float(c_med) + 0.5:
            scores["dip"] += 1
            winner = "Dip"
        elif float(c_med) > float(d_med) + 0.5:
            scores["c18"] += 1
            winner = "C18acc"
        elif (d_hit or 0) > (c_hit or 0) + 0.05:
            scores["dip"] += 1
            winner = "Dip"
        elif (c_hit or 0) > (d_hit or 0) + 0.05:
            scores["c18"] += 1
            winner = "C18acc"
        else:
            scores["tie"] += 1
            winner = "tie"
        reasons.append(
            f"hold{h}: {winner} · Dip slots OOS med_ex={_fmt_pp(d_med)} "
            f"hit={_fmt_pct(d_hit)} n={dip.get('oos_n')} vs C18 "
            f"med_ex={_fmt_pp(c_med)} hit={_fmt_pct(c_hit)} n={c18.get('oos_n')}"
        )
    if scores["dip"] > scores["c18"]:
        choice = "leading_dip"
        prose = (
            "在固定 hold 5/10（對齊 C18acc 持有帶）下，Leading Dip（日槽+總在外）"
            "OOS med excess 優於 C18acc 3-slot 反事實固定出場。"
        )
    elif scores["c18"] > scores["dip"]:
        choice = "c18acc"
        prose = (
            "在固定 hold 5/10 下，C18acc 3-slot 反事實固定出場 OOS med excess "
            "優於 Leading Dip slots。"
        )
    else:
        choice = "mixed_or_tie"
        prose = (
            "固定 hold 5 vs 10 領先方不一致或差距 <0.5pp；兩者 alpha 不同，"
            "不宜用單一 hold 宣告淘汰。另：此為固定出場反事實，非 C18acc live S2。"
        )
    return {"choice": choice, "prose": prose, "reasons": reasons, "scores": scores}


def render_md(payload: dict[str, Any]) -> str:
    c = payload.get("contract") or {}
    v = payload.get("verdict") or {}
    lines = [
        "# Fair fixed-hold · Leading Dip ↔ C18acc",
        "",
        f"日期：{payload.get('generated_at', '')[:10]} · Research only · **未採納**",
        "",
        "## 共同契約",
        "",
        f"- 主窗：`{c.get('start')}` → `{c.get('end')}` · OOS cut `{c.get('oos_cut')}`",
        f"- hold 掃：`{c.get('hold_days')}`（名義天數）",
        f"- 成本：flat haircut **{c.get('cost_haircut_pp')}** pp",
        "- Dip：coverage（含開盤）· raw + slots（日≤2／忙crash≤4 · 總在外≤6）",
        "- C18acc：champion fresh · E@13:00 · 3-slot **進場日**；出場改固定 hold（關掉 S2）",
        "- 日曆註記（兩邊各用原函式，**名義同 N、實際出場日可能差 1 日**）：",
        "  - Dip = T+N close",
        "  - C18acc = inclusive N 日 → T+(N−1) @ 13:30",
        "",
        "## Verdict",
        "",
        f"**選擇：`{v.get('choice')}`**",
        "",
        v.get("prose") or "",
        "",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(
        [
            "",
            "## Head-to-head · fixed hold",
            "",
            "| hold | side | n | hit_ex | med_ex | med_px | OOS n | OOS hit_ex | OOS med_ex |",
            "|--:|--|--:|--:|--:|--:|--:|--:|--:|",
        ]
    )
    for h in c.get("hold_days") or []:
        block = (payload.get("by_hold") or {}).get(str(h)) or {}
        for side, key, nest in (
            ("C18acc", "c18", "fixed"),
            ("Dip raw", "dip", "raw"),
            ("Dip slots", "dip", "slots"),
        ):
            s = ((block.get(key) or {}).get(nest)) or {}
            lines.append(
                f"| {h} | {side} | {s.get('n', 0)} | {_fmt_pct(s.get('hit_ex'))} | "
                f"{_fmt_pp(s.get('med_ex'))} | {_fmt_pp(s.get('med_px'))} | "
                f"{s.get('oos_n', 0)} | {_fmt_pct(s.get('oos_hit_ex'))} | "
                f"{_fmt_pp(s.get('oos_med_ex'))} |"
            )
    act = payload.get("c18_actual_s2") or {}
    lines.extend(
        [
            "",
            "## Reference · C18acc live S2（非固定 hold）",
            "",
            f"- n={act.get('n')} · hit_px={_fmt_pct(act.get('hit_px'))} · "
            f"med_px={_fmt_pp(act.get('med_px'))} · "
            f"OOS n={act.get('oos_n')} hit_px={_fmt_pct(act.get('oos_hit_px'))} "
            f"med_px={_fmt_pp(act.get('oos_med_px'))}",
            f"- note: {act.get('note') or '—'}",
            "",
            "## Caveats",
            "",
            "- 固定 hold 是進場日上的反事實；C18acc live 仍是 min5／max10 + S2 動態出場。",
            "- Dip 與 C18acc 進場宇宙／時機完全不同（Leading 深跌 vs fresh RRG 換倉）。",
            "- 本報告不改變 Order 層採納狀態。",
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python "
            "scripts/research/run_leading_dip_vs_c18acc_hold_sweep.py",
            "PYTHONPATH=src .venv/bin/python "
            "scripts/research/run_leading_dip_vs_c18acc_hold_sweep.py --collect-c18",
            "```",
            "",
            f"Artifact：`{payload.get('artifact_stem')}.{{md,json}}`",
            "",
        ]
    )
    return "\n".join(lines)


def format_console(payload: dict[str, Any]) -> str:
    lines = ["=== Leading Dip ↔ C18acc · fixed-hold sweep ==="]
    lines.append(
        f"{'hold':>4} {'side':10} {'n':>5} {'hit_ex':>7} {'med_ex':>8} "
        f"{'oos_n':>5} {'oos_hit':>7} {'oos_med':>8}"
    )
    for h in (payload.get("contract") or {}).get("hold_days") or []:
        block = (payload.get("by_hold") or {}).get(str(h)) or {}
        for side, key, nest in (
            ("C18acc", "c18", "fixed"),
            ("Dip_slots", "dip", "slots"),
        ):
            s = ((block.get(key) or {}).get(nest)) or {}
            lines.append(
                f"{h:4d} {side:10} {s.get('n',0):5d} {_fmt_pct(s.get('hit_ex')):>7} "
                f"{_fmt_pp(s.get('med_ex')):>8} {s.get('oos_n',0):5d} "
                f"{_fmt_pct(s.get('oos_hit_ex')):>7} {_fmt_pp(s.get('oos_med_ex')):>8}"
            )
    v = payload.get("verdict") or {}
    lines.append(f"\nVerdict: {v.get('choice')} — {v.get('prose')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fixed-hold fair sweep · Leading Dip vs C18acc"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--oos-cut", default=OOS_CUT_DEFAULT)
    ap.add_argument(
        "--holds",
        default=",".join(str(h) for h in HOLD_SWEEP_DEFAULT),
        help="comma-separated hold days (default 3,5,7,10)",
    )
    ap.add_argument("--c18-cache", type=Path, default=DEFAULT_C18_CACHE)
    ap.add_argument(
        "--collect-c18",
        action="store_true",
        help="Re-run C18acc 3-slot sim and refresh entry cache",
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_STEM)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--skip-dip", action="store_true")
    ap.add_argument("--skip-c18", action="store_true")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    holds = [int(x.strip()) for x in str(args.holds).split(",") if x.strip()]
    if not holds:
        print("BLOCKER: empty --holds", file=sys.stderr)
        return 1

    con = connect(args.db)
    try:
        close, _, _ = load_price_panels(con)
        full = close.index.astype(str).tolist()
        end = full[-1]
        trade_dates = [d for d in full if args.start <= d <= end]
        if len(trade_dates) < 20:
            print(f"BLOCKER: too few trade dates ({len(trade_dates)})", file=sys.stderr)
            return 1

        from market_benchmark import load_benchmark_close

        bench = load_benchmark_close(con).reindex(close.index).astype(float)

        c18_entries: list[dict[str, Any]] = []
        c18_meta: dict[str, Any] = {}
        blockers: list[str] = []
        if not args.skip_c18:
            try:
                c18_entries, c18_meta = load_or_collect_c18_entries(
                    con,
                    trade_dates=trade_dates,
                    cache_path=args.c18_cache,
                    collect=bool(args.collect_c18),
                )
            except FileNotFoundError as exc:
                blockers.append(str(exc))
        else:
            blockers.append("C18acc skipped by flag")

        by_hold: dict[str, Any] = {}
        for h in holds:
            block: dict[str, Any] = {}
            if c18_entries and not args.skip_c18:
                block["c18"] = run_c18_hold(
                    con,
                    c18_entries,
                    hold_days=h,
                    oos_cut=args.oos_cut,
                    n_trading_days=len(trade_dates),
                    bench=bench,
                )
            if not args.skip_dip:
                block["dip"] = run_dip_hold(
                    con,
                    hold_days=h,
                    start=args.start,
                    oos_cut=args.oos_cut,
                )
            by_hold[str(h)] = block

        c18_actual = (
            actual_s2_summary(
                c18_entries,
                oos_cut=args.oos_cut,
                n_trading_days=len(trade_dates),
            )
            if c18_entries
            else {}
        )
        verdict = decide_verdict(by_hold) if by_hold else {
            "choice": "blocked",
            "prose": "no holds produced",
            "reasons": blockers,
            "scores": {},
        }

        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "contract": {
                "start": args.start,
                "end": end,
                "oos_cut": args.oos_cut,
                "hold_days": holds,
                "cost_haircut_pp": COST_HAIRCUT_PP,
                "benchmark": "IX0001",
                "dip_track": "leading_dip_cov (open incl · slots)",
                "c18_entry": "champion fresh · E@13:00 · 3-slot sim entries",
                "calendar_note": (
                    "nominal same N; Dip T+N close vs C18 inclusive N → T+(N-1)@13:30"
                ),
            },
            "by_hold": by_hold,
            "c18_actual_s2": c18_actual,
            "c18_meta": {
                k: c18_meta.get(k)
                for k in (
                    "n_entries",
                    "n_slots",
                    "runtime_s",
                    "date_start",
                    "date_end",
                    "n_trade_dates",
                )
                if k in c18_meta
            },
            "c18_cache": str(args.c18_cache),
            "verdict": verdict,
            "blockers": blockers,
            "artifact_stem": str(args.out),
        }
    finally:
        con.close()

    print(format_console(payload))
    print()
    if not args.no_write:
        stem = args.out.with_suffix("") if args.out.suffix in (".md", ".json") else args.out
        out_json = Path(str(stem) + ".json")
        out_md = Path(str(stem) + ".md")
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        out_md.write_text(render_md(payload), encoding="utf-8")
        print(f"Wrote {out_json}")
        print(f"Wrote {out_md}")

    if blockers and not any(
        (b.get("c18") or {}).get("fixed", {}).get("n")
        or (b.get("dip") or {}).get("slots", {}).get("n")
        for b in (payload.get("by_hold") or {}).values()
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
