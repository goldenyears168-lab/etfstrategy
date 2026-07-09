"""C18acc+S2 · W-EXIT sub-cohort save_vs_orig analysis (Phase W-EXIT).

Counterfactual: exit at first W5 rotation (or other W5 rule) vs actual leg outcome.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any

from analytics.bench import bench_return_entry_to_exit
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    ROTATION_RULE_IDS,
    W5_EXIT_RULES,
    load_c18acc_s2_periods,
)
from flow_returns import stock_close
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_universe_snapshot import kbar_close_at_minute


def _period_key(period: dict[str, Any]) -> str:
    return f"{period['entry_date']}|{period['stock_id']}|{period.get('slot', 0)}"


def _entry_px(conn, period: dict[str, Any], close) -> float | None:
    if period.get("entry_px") is not None:
        px = float(period["entry_px"])
        return px if px > 0 else None
    sid = str(period["stock_id"])
    entry = str(period["entry_date"])
    if entry in close.index and sid in close.columns:
        px = float(close.at[entry, sid])
        return px if px == px and px > 0 else None
    px = stock_close(conn, sid, entry)
    return float(px) if px and px > 0 else None


def _exit_px_close(conn, period: dict[str, Any], close) -> float | None:
    if period.get("exit_px") is not None:
        px = float(period["exit_px"])
        return px if px > 0 else None
    sid = str(period["stock_id"])
    exit_d = str(period["exit_date"])
    if exit_d in close.index and sid in close.columns:
        px = float(close.at[exit_d, sid])
        return px if px == px and px > 0 else None
    px = stock_close(conn, sid, exit_d)
    return float(px) if px and px > 0 else None


def _w5_exit_px(
    conn,
    *,
    stock_id: str,
    trade_date: str,
    minute: str,
    close,
) -> float | None:
    px = kbar_close_at_minute(conn, stock_id, trade_date, minute)
    if px is not None and px > 0:
        return float(px)
    if trade_date in close.index and stock_id in close.columns:
        px = float(close.at[trade_date, stock_id])
        return px if px == px and px > 0 else None
    px = stock_close(conn, stock_id, trade_date)
    return float(px) if px and px > 0 else None


def _return_pct(entry_px: float, exit_px: float) -> float:
    return round((exit_px / entry_px - 1.0) * 100.0, 4)


def compute_w5_counterfactual(
    conn: sqlite3.Connection,
    period: dict[str, Any],
    trigger: dict[str, Any],
    *,
    close=None,
) -> dict[str, Any] | None:
    if close is None:
        close, _, _ = load_price_panels(conn)

    entry_px = _entry_px(conn, period, close)
    orig_exit_px = _exit_px_close(conn, period, close)
    if entry_px is None or orig_exit_px is None:
        return None

    sid = str(period["stock_id"])
    entry = str(period["entry_date"])
    orig_exit = str(period["exit_date"])
    w5_date = str(trigger["trade_date"])
    w5_minute = str(trigger.get("minute") or "13:30")

    w5_px = _w5_exit_px(conn, stock_id=sid, trade_date=w5_date, minute=w5_minute, close=close)
    if w5_px is None:
        return None

    orig_ret = float(period.get("return_pct") or _return_pct(entry_px, orig_exit_px))
    w5_ret = _return_pct(entry_px, w5_px)
  # save > 0 => W5 exit better than holding to original
    save_vs_orig = round(orig_ret - w5_ret, 4)

    bench_orig = bench_return_entry_to_exit(conn, entry, orig_exit, entry_price_mode="close")
    bench_w5 = bench_return_entry_to_exit(conn, entry, w5_date, entry_price_mode="close")
    orig_excess = round(orig_ret - bench_orig, 4) if bench_orig is not None else None
    w5_excess = round(w5_ret - bench_w5, 4) if bench_w5 is not None else None
    excess_delta = (
        round(w5_excess - orig_excess, 4)
        if w5_excess is not None and orig_excess is not None
        else None
    )

    from research.backtest.rrg_mono_intraday_exit import _trading_days_between

    full_dates = close.index.astype(str).tolist()
    hold_orig = _trading_days_between(full_dates, entry, orig_exit)
    hold_w5 = _trading_days_between(full_dates, entry, w5_date)

    return {
        "rule_id": trigger.get("rule_id"),
        "w5_exit_date": w5_date,
        "w5_exit_minute": w5_minute,
        "entry_px": round(entry_px, 4),
        "orig_exit_px": round(orig_exit_px, 4),
        "w5_exit_px": round(w5_px, 4),
        "orig_return_pct": orig_ret,
        "w5_return_pct": w5_ret,
        "save_vs_orig_pct": save_vs_orig,
        "orig_excess_pct": orig_excess,
        "w5_excess_pct": w5_excess,
        "excess_delta_pp": excess_delta,
        "hold_days_orig": hold_orig,
        "hold_days_w5": hold_w5,
        "hold_days_saved": hold_orig - hold_w5,
        "w5_helped": save_vs_orig > 0,
    }


def _cohort_label(leg: dict[str, Any]) -> str:
    s2 = bool(leg.get("s2_force_exit"))
    w5 = bool(leg.get("w5_first_rotation"))
    if w5 and not s2:
        return "w5_only"
    if s2 and w5:
        return "both"
    if s2 and not w5:
        return "s2_only"
    return "neither"


def _summarize_save(rows: list[dict[str, Any]]) -> dict[str, Any]:
    saves = [float(r["save_vs_orig_pct"]) for r in rows if r.get("save_vs_orig_pct") is not None]
    excess_d = [float(r["excess_delta_pp"]) for r in rows if r.get("excess_delta_pp") is not None]
    holds_saved = [int(r["hold_days_saved"]) for r in rows if r.get("hold_days_saved") is not None]
    if not saves:
        return {"n": 0}
    return {
        "n": len(saves),
        "mean_save_vs_orig_pct": round(statistics.mean(saves), 4),
        "median_save_vs_orig_pct": round(statistics.median(saves), 4),
        "pct_save_positive": round(sum(1 for s in saves if s > 0) / len(saves) * 100, 2),
        "mean_excess_delta_pp": round(statistics.mean(excess_d), 4) if excess_d else None,
        "median_hold_days_saved": round(statistics.median(holds_saved), 2) if holds_saved else None,
        "mean_orig_return_pct": round(
            statistics.mean(float(r["orig_return_pct"]) for r in rows), 4
        ),
        "mean_w5_return_pct": round(statistics.mean(float(r["w5_return_pct"]) for r in rows), 4),
    }


def run_w_exit_save_analysis(
    conn: sqlite3.Connection,
    annot: dict[str, Any],
    *,
    periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if periods is None:
        periods, _ = load_c18acc_s2_periods(
            conn,
            date_start=annot["date_start"],
            date_end=annot["date_end"],
            n_slots=int(annot.get("n_slots") or 3),
        )
    period_by_key = {_period_key(p): p for p in periods}
    close, _, _ = load_price_panels(conn)

    leg_rows: list[dict[str, Any]] = []
    by_rule: dict[str, list[dict[str, Any]]] = {rid: [] for rid, _ in W5_EXIT_RULES}

    for leg in annot.get("legs") or []:
        pk = leg.get("leg_id") or f"{leg['entry_date']}|{leg['stock_id']}|0"
        period = period_by_key.get(pk)
        if not period:
            # fallback match without slot
            period = next(
                (
                    p
                    for p in periods
                    if str(p["entry_date"]) == leg["entry_date"]
                    and str(p["stock_id"]) == leg["stock_id"]
                ),
                None,
            )
        if not period:
            continue

        cohort = _cohort_label(leg)
        rot = leg.get("w5_first_rotation")
        cf_rot = (
            compute_w5_counterfactual(conn, period, rot, close=close) if rot else None
        )

        rule_cfs: dict[str, Any] = {}
        for trig in leg.get("w5_triggers") or []:
            rid = str(trig.get("rule_id") or "")
            if rid in rule_cfs:
                continue
            cf = compute_w5_counterfactual(conn, period, trig, close=close)
            if cf:
                rule_cfs[rid] = cf
                by_rule.setdefault(rid, []).append({**cf, "cohort": cohort, "stock_id": leg["stock_id"]})

        row = {
            "leg_id": pk,
            "stock_id": leg["stock_id"],
            "stock_name": leg.get("stock_name"),
            "entry_date": leg["entry_date"],
            "exit_date": leg["exit_date"],
            "exit_reason": leg.get("exit_reason"),
            "cohort": cohort,
            "s2_force_exit": leg.get("s2_force_exit"),
            "orig_return_pct": leg.get("return_pct"),
            "orig_excess_pct": leg.get("excess_pct"),
            "w5_first_rotation": rot,
            "w5_rotation_save": cf_rot,
            "w5_rule_saves": rule_cfs,
        }
        leg_rows.append(row)

    cohort_rows: dict[str, list[dict[str, Any]]] = {
        "w5_only": [],
        "both": [],
        "s2_only": [],
        "all_rotation": [],
    }
    for row in leg_rows:
        cf = row.get("w5_rotation_save")
        if not cf:
            continue
        cohort_rows["all_rotation"].append(cf)
        c = row["cohort"]
        if c in cohort_rows:
            cohort_rows[c].append(cf)

    # stricter rotation: deep_pullback or w20_weakening only
    strict_rows: list[dict[str, Any]] = []
    for row in leg_rows:
        rot = row.get("w5_first_rotation") or {}
        rid = rot.get("rule_id")
        if rid not in ("w5_deep_pullback", "exit_w20_weakening"):
            continue
        cf = row.get("w5_rotation_save")
        if cf and row["cohort"] == "w5_only":
            strict_rows.append(cf)

    summaries = {
        "all_rotation": _summarize_save(cohort_rows["all_rotation"]),
        "w5_only": _summarize_save(cohort_rows["w5_only"]),
        "both": _summarize_save(cohort_rows["both"]),
        "s2_only": _summarize_save(cohort_rows["s2_only"]),
        "w5_only_strict_rotation": _summarize_save(strict_rows),
        "by_rule": {rid: _summarize_save(rows) for rid, rows in by_rule.items() if rows},
    }

    hypotheses = {
        "H-W-X-save-1": {
            "statement": "w5_only cohort · median save_vs_orig > 0",
            "pass": (summaries["w5_only"].get("median_save_vs_orig_pct") or 0) > 0,
            "value": summaries["w5_only"].get("median_save_vs_orig_pct"),
        },
        "H-W-X-save-2": {
            "statement": "w5_only · pct save>0 ≥ 50%",
            "pass": (summaries["w5_only"].get("pct_save_positive") or 0) >= 50,
            "value": summaries["w5_only"].get("pct_save_positive"),
        },
        "H-W-X-save-3": {
            "statement": "w5_only strict (deep_pullback|w20_weak) · median save > 0",
            "pass": (summaries["w5_only_strict_rotation"].get("median_save_vs_orig_pct") or 0) > 0,
            "value": summaries["w5_only_strict_rotation"].get("median_save_vs_orig_pct"),
        },
        "H-W-X-save-4": {
            "statement": "both cohort · W5 rotation save ≥ 0 (S2 legs W5 would not hurt)",
            "pass": (summaries["both"].get("median_save_vs_orig_pct") or -999) >= 0,
            "value": summaries["both"].get("median_save_vs_orig_pct"),
        },
    }

    return {
        "schema": "c18acc_s2_dual_wma_exit_save-v1",
        "phase": "W-EXIT",
        "source_annot": annot.get("schema"),
        "date_start": annot["date_start"],
        "date_end": annot["date_end"],
        "n_slots": annot.get("n_slots"),
        "n_legs": len(leg_rows),
        "summaries": summaries,
        "hypotheses": hypotheses,
        "legs": leg_rows,
    }


def load_annot_payload(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def render_w_exit_save_md(payload: dict[str, Any]) -> str:
    s = payload["summaries"]
    lines = [
        "# C18acc+S2 · W-EXIT save 分析（Phase W-EXIT）",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽",
        "",
        "反事實：在 **W5 首個 rotation 訊號** 盤中價出場 vs 實際 leg 收盤出場。",
        "`save_vs_orig` = 實際報酬% − W5 出場報酬% · **>0 表示 W5 提早出場較佳**。",
        "",
        "## 子樣本摘要",
        "",
        "| cohort | n | median save% | mean save% | save>0% | median 縮短持有日 | mean excess Δpp |",
        "|--------|---|--------------|------------|---------|------------------|-----------------|",
    ]

    labels = {
        "w5_only": "僅 W5（S2 未 force）",
        "both": "W5 + S2 皆有",
        "s2_only": "僅 S2 force",
        "all_rotation": "全部有 rotation 訊號",
        "w5_only_strict_rotation": "僅 W5 · 嚴格 rotation*",
    }
    for key, label in labels.items():
        sm = s.get(key) or {}
        if not sm.get("n"):
            continue
        lines.append(
            f"| {label} | {sm['n']} | {sm.get('median_save_vs_orig_pct')} "
            f"| {sm.get('mean_save_vs_orig_pct')} | {sm.get('pct_save_positive')} "
            f"| {sm.get('median_hold_days_saved')} | {sm.get('mean_excess_delta_pp')} |"
        )

    lines += [
        "",
        "*嚴格 rotation = 首個訊號為 `w5_deep_pullback` 或 `exit_w20_weakening`（排除單純 w5_lagging）。",
        "",
        "## 依 W5 規則（每 leg 最早觸發）",
        "",
        "| rule | n | median save% | save>0% |",
        "|------|---|--------------|---------|",
    ]
    for rid, _ in W5_EXIT_RULES:
        sm = (s.get("by_rule") or {}).get(rid) or {}
        if not sm.get("n"):
            continue
        lines.append(
            f"| {rid} | {sm['n']} | {sm.get('median_save_vs_orig_pct')} "
            f"| {sm.get('pct_save_positive')} |"
        )

    lines += ["", "## 假說", ""]
    for hid, h in (payload.get("hypotheses") or {}).items():
        mark = "✓" if h.get("pass") else "✗"
        lines.append(f"- {mark} **{hid}**：{h.get('statement')} · value={h.get('value')}")

    lines += [
        "",
        "## w5_only · save 最差 / 最好（rotation）",
        "",
        "| stock | entry | W5 exit | save% | orig% | w5% | rule |",
        "|-------|-------|---------|-------|-------|-----|------|",
    ]
    w5_only_rows = [
        r
        for r in payload["legs"]
        if r["cohort"] == "w5_only" and r.get("w5_rotation_save")
    ]
    ranked = sorted(
        w5_only_rows,
        key=lambda r: float((r.get("w5_rotation_save") or {}).get("save_vs_orig_pct") or 0),
    )
    for row in ranked[:5] + list(reversed(ranked[-5:])):
        cf = row["w5_rotation_save"]
        rot = row.get("w5_first_rotation") or {}
        lines.append(
            f"| {row['stock_id']} | {row['entry_date']} | {cf['w5_exit_date']} "
            f"| {cf['save_vs_orig_pct']} | {cf['orig_return_pct']} | {cf['w5_return_pct']} "
            f"| {rot.get('rule_id')} |"
        )

    lines += [
        "",
        "---",
        "模組：`scripts/run_c18acc_s2_dual_wma_exit_save.py`",
        "",
    ]
    return "\n".join(lines)
