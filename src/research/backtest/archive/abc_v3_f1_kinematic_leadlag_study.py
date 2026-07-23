"""ABC v3+f1 · kinematic lead-lag study (MV decline vs price peak/giveback).

Loads executed trade ledger from comparison JSON · builds reduced intraday panel
(stock_ids subset · 5m kbar) · per-leg WMA20/5/3 kinematic timeline.
"""

from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from report_paths import RESEARCH_RRG
from research.backtest.archive.abc_v3_f1_overnight_regime_annot import (
    load_abc_f1_trade_ledger_from_json,
)
from research.backtest.archive.c18acc_kinematic_timeline_cache import (
    KinematicSnap,
    _build_kinematic_timeline,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    _collect_annot_dates,
    _exit_frame_idx_for_date,
)
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (
    _frame_idx_for_minute,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.w3_rv_hl_winner_profile import _mean, _median
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)

SCHEMA = "abc_v3_f1_kinematic_leadlag-v1"
COHORT_TOP_PCT = 15.0
COHORT_BOTTOM_PCT = 15.0
CHAMPION_NET_MIN = 5.0
FAILURE_NET_MAX = -2.0
GIVEBACK_PCTS = (1.0, 2.0)
MV_LEGS = ("w3", "w5", "w20")


def find_latest_comparison_json(
    reports_dir: Path | None = None,
) -> Path | None:
    """Latest ``*_c18acc_abc_v3_f1_comparison.json`` under reports/research/rrg/."""
    root = reports_dir or RESEARCH_RRG
    if not root.is_dir():
        return None
    matches = sorted(root.glob("*_c18acc_abc_v3_f1_comparison.json"))
    return matches[-1] if matches else None


def resolve_ledger_json(path: Path | None = None) -> Path:
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"ledger JSON not found: {path}")
        return path
    latest = find_latest_comparison_json()
    if latest is None:
        raise FileNotFoundError(
            "no *_c18acc_abc_v3_f1_comparison.json under reports/research/rrg/"
        )
    return latest


def _ledger_row_to_period(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_id": str(row.get("stock_id") or ""),
        "stock_name": str(row.get("stock_name") or ""),
        "entry_date": str(row.get("entry_date") or ""),
        "exit_date": str(row.get("exit_date") or ""),
        "return_pct": row.get("net_pct"),
        "net_pct": row.get("net_pct"),
        "entry_minute": row.get("entry_minute"),
        "entry_px": row.get("entry_px"),
        "exit_px": row.get("exit_px"),
        "hold_days": row.get("hold_days"),
        "rank_score": row.get("rank_score"),
        "slot": row.get("slot", 0),
    }


def split_cohorts(
    rows: list[dict[str, Any]],
    *,
    top_pct: float = COHORT_TOP_PCT,
    bottom_pct: float = COHORT_BOTTOM_PCT,
    champion_min: float = CHAMPION_NET_MIN,
    failure_max: float = FAILURE_NET_MAX,
) -> dict[str, list[dict[str, Any]]]:
    """Champion（贏家）· failure（輸家）· baseline（中間）cohort split."""
    scored = [(r, float(r["net_pct"])) for r in rows if r.get("net_pct") is not None]
    if not scored:
        return {"champion": [], "failure": [], "baseline": []}
    scored.sort(key=lambda x: x[1])
    n = len(scored)
    k_top = max(1, round(n * top_pct / 100.0))
    k_bot = max(1, round(n * bottom_pct / 100.0))
    top_cut = scored[n - k_top][1]
    bot_cut = scored[k_bot - 1][1]

    champion: list[dict[str, Any]] = []
    failure: list[dict[str, Any]] = []
    baseline: list[dict[str, Any]] = []
    for row, net in scored:
        if net >= top_cut or net >= champion_min:
            champion.append(row)
        elif net <= bot_cut or net <= failure_max:
            failure.append(row)
        else:
            baseline.append(row)
    return {"champion": champion, "failure": failure, "baseline": baseline}


def _first_mv_drop_idx(timeline: list[KinematicSnap], leg: str) -> int | None:
    attr = f"{leg}_mv_decline_streak"
    for i in range(1, len(timeline)):
        if getattr(timeline[i], attr, 0) >= 1:
            return i
    return None


def _first_giveback_idx(
    timeline: list[KinematicSnap],
    *,
    peak_i: int,
    peak_ret: float,
    giveback_pct: float,
) -> int | None:
    threshold = peak_ret - giveback_pct
    for i in range(peak_i, len(timeline)):
        if timeline[i].ret_from_entry_pct <= threshold:
            return i
    return None


def _cascade_order(first_drops: dict[str, int | None]) -> str:
    legs = [(k, v) for k, v in first_drops.items() if v is not None]
    if len(legs) < 2:
        return ""
    legs.sort(key=lambda x: x[1])
    return ">".join(k for k, _ in legs)


def mark_leg_events(timeline: list[KinematicSnap]) -> dict[str, Any]:
    """Event indices on kinematic timeline (price peak/giveback · MV poll drops)."""
    if len(timeline) < 2:
        return {"n_snaps": len(timeline)}

    peak_i = max(range(len(timeline)), key=lambda i: timeline[i].px)
    peak = timeline[peak_i]
    peak_ret = peak.ret_from_entry_pct

    first_drops = {leg: _first_mv_drop_idx(timeline, leg) for leg in MV_LEGS}
    givebacks = {
        f"giveback_{int(g)}pct": _first_giveback_idx(
            timeline, peak_i=peak_i, peak_ret=peak_ret, giveback_pct=g
        )
        for g in GIVEBACK_PCTS
    }

    def _snap_ref(i: int | None) -> dict[str, Any] | None:
        if i is None:
            return None
        s = timeline[i]
        return {
            "frame_idx": s.frame_idx,
            "trade_date": s.trade_date,
            "minute": s.minute,
            "ret_from_entry_pct": round(s.ret_from_entry_pct, 3),
            "px": round(s.px, 4),
        }

    return {
        "n_snaps": len(timeline),
        "T_price_peak": peak_i,
        "T_price_peak_ret_pct": round(peak_ret, 3),
        "T_price_peak_ref": _snap_ref(peak_i),
        **{f"T_{k}": v for k, v in givebacks.items()},
        **{f"T_{leg}_mv_drop": first_drops[leg] for leg in MV_LEGS},
        "mv_drop_cascade": _cascade_order(first_drops),
        "mv_drop_refs": {leg: _snap_ref(first_drops[leg]) for leg in MV_LEGS},
    }


def compute_lead_lag(
    events: dict[str, Any],
    *,
    n_snaps: int,
) -> dict[str, Any]:
    """Lead-lag in poll counts (positive = MV event before price event)."""
    peak_i = events.get("T_price_peak")
    if peak_i is None:
        return {}

    out: dict[str, Any] = {"n_snaps": n_snaps}
    for leg in MV_LEGS:
        drop_i = events.get(f"T_{leg}_mv_drop")
        if drop_i is not None:
            out[f"{leg}_lead_peak_polls"] = peak_i - drop_i
            out[f"{leg}_before_peak"] = drop_i <= peak_i
        for g in GIVEBACK_PCTS:
            gb_key = f"giveback_{int(g)}pct"
            gb_i = events.get(f"T_{gb_key}")
            if drop_i is not None and gb_i is not None:
                out[f"{leg}_lead_{gb_key}_polls"] = gb_i - drop_i
                out[f"{leg}_before_{gb_key}"] = drop_i <= gb_i

    cascade = events.get("mv_drop_cascade") or ""
    out["cascade_is_w3_first"] = cascade.startswith("w3") if cascade else None
    out["cascade_is_w3_w5_w20"] = cascade == "w3>w5>w20"
    return out


def _rate_true(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r.get("lead_lag", {}).get(key) for r in rows]
    flags = [v for v in vals if v is not None]
    if not flags:
        return None
    return round(100.0 * sum(1 for v in flags if v) / len(flags), 1)


def _collect_float(rows: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        v = r.get("lead_lag", {}).get(key)
        if v is not None:
            out.append(float(v))
    return out


def summarize_cohort(rows: list[dict[str, Any]], cohort_id: str) -> dict[str, Any]:
    """Aggregate lead-lag metrics for one cohort."""
    if not rows:
        return {"cohort": cohort_id, "n": 0}

    nets = [float(r["net_pct"]) for r in rows if r.get("net_pct") is not None]
    cascades: dict[str, int] = {}
    for r in rows:
        c = (r.get("events") or {}).get("mv_drop_cascade") or "none"
        if c:
            cascades[c] = cascades.get(c, 0) + 1

    summary: dict[str, Any] = {
        "cohort": cohort_id,
        "n": len(rows),
        "mean_net_pct": round(statistics.mean(nets), 3) if nets else None,
        "median_net_pct": round(statistics.median(nets), 3) if nets else None,
        "pct_w3_before_peak": _rate_true(rows, "w3_before_peak"),
        "pct_w5_before_peak": _rate_true(rows, "w5_before_peak"),
        "pct_w20_before_peak": _rate_true(rows, "w20_before_peak"),
        "mean_w3_lead_peak_polls": _mean(_collect_float(rows, "w3_lead_peak_polls")),
        "median_w3_lead_peak_polls": _median(_collect_float(rows, "w3_lead_peak_polls")),
        "mean_w5_lead_peak_polls": _mean(_collect_float(rows, "w5_lead_peak_polls")),
        "mean_w20_lead_peak_polls": _mean(_collect_float(rows, "w20_lead_peak_polls")),
        "mean_w3_lead_giveback_1pct_polls": _mean(
            _collect_float(rows, "w3_lead_giveback_1pct_polls")
        ),
        "mean_w3_lead_giveback_2pct_polls": _mean(
            _collect_float(rows, "w3_lead_giveback_2pct_polls")
        ),
        "pct_cascade_w3_first": _rate_true(rows, "cascade_is_w3_first"),
        "pct_cascade_w3_w5_w20": _rate_true(rows, "cascade_is_w3_w5_w20"),
        "mv_drop_cascade_top": sorted(
            [{"cascade": k, "n": v, "pct": round(100.0 * v / len(rows), 1)} for k, v in cascades.items()],
            key=lambda x: -x["n"],
        )[:5],
    }
    return summary


def _filter_ledger_by_window(
    ledger: list[dict[str, Any]],
    *,
    date_start: str | None,
    date_end: str | None,
) -> list[dict[str, Any]]:
    out = ledger
    if date_start:
        out = [r for r in out if str(r.get("entry_date") or "") >= date_start]
    if date_end:
        out = [r for r in out if str(r.get("entry_date") or "") <= date_end]
    return out


def run_abc_v3_f1_kinematic_leadlag_study(
    conn: sqlite3.Connection,
    *,
    ledger_json: Path | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    """Main study · ledger JSON + stock_ids fast-path intraday panel."""
    ledger_path = resolve_ledger_json(ledger_json)
    ledger, ledger_meta = load_abc_f1_trade_ledger_from_json(ledger_path)
    ledger = _filter_ledger_by_window(ledger, date_start=date_start, date_end=date_end)
    if not ledger:
        raise ValueError("no ledger rows after date filter")

    cohorts = split_cohorts(ledger)
    cohort_by_leg: dict[str, str] = {}
    for name, rows in cohorts.items():
        for r in rows:
            leg_key = f"{r.get('entry_date')}|{r.get('stock_id')}|{r.get('idx', 0)}"
            cohort_by_leg[leg_key] = name

    periods = [_ledger_row_to_period(r) for r in ledger]
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, full_dates)
    if not annot_dates:
        raise ValueError("no annotation dates from ledger legs")

    print(
        f"lead-lag: {len(ledger)} legs · {len(stock_ids)} stocks · "
        f"{len(annot_dates)} dates · ledger={ledger_path.name}",
        flush=True,
    )

    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    frames, trajectories, traj_meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=annot_dates,
        stock_ids=stock_ids,
        poll_minutes=intraday_poll_minutes(),
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        kbar_bar_minutes=5,
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    leg_rows: list[dict[str, Any]] = []
    for row in ledger:
        period = _ledger_row_to_period(row)
        sid = str(period["stock_id"])
        entry = str(period["entry_date"])
        exit_d = str(period.get("exit_date") or "")
        entry_px = period.get("entry_px")
        if not exit_d or not entry_px or float(entry_px) <= 0:
            continue
        points = stock_maps.get(sid)
        if not points:
            continue
        entry_i = _frame_idx_for_minute(frames, entry, period.get("entry_minute"))
        exit_i = _exit_frame_idx_for_date(frames, exit_d)
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
        lead_lag = compute_lead_lag(events, n_snaps=len(timeline))
        leg_key = f"{entry}|{sid}|{row.get('idx', 0)}"
        cohort = cohort_by_leg.get(leg_key, "baseline")
        leg_rows.append(
            {
                "leg_id": leg_key,
                "cohort": cohort,
                "stock_id": sid,
                "stock_name": period.get("stock_name") or "",
                "entry_date": entry,
                "exit_date": exit_d,
                "entry_minute": period.get("entry_minute"),
                "net_pct": period.get("net_pct"),
                "events": events,
                "lead_lag": lead_lag,
            }
        )

    by_cohort: dict[str, list[dict[str, Any]]] = {
        "champion": [],
        "failure": [],
        "baseline": [],
    }
    for leg in leg_rows:
        by_cohort.setdefault(leg["cohort"], []).append(leg)

    cohort_summaries = [
        summarize_cohort(by_cohort.get("champion") or [], "champion"),
        summarize_cohort(by_cohort.get("failure") or [], "failure"),
        summarize_cohort(by_cohort.get("baseline") or [], "baseline"),
    ]

    def _top_examples(cohort: str, n: int = 5) -> list[dict[str, Any]]:
        rows = sorted(
            by_cohort.get(cohort) or [],
            key=lambda r: abs(float(r.get("net_pct") or 0)),
            reverse=True,
        )[:n]
        return [
            {
                "leg_id": r["leg_id"],
                "stock_id": r["stock_id"],
                "net_pct": r["net_pct"],
                "events": {
                    k: r["events"].get(k)
                    for k in (
                        "T_price_peak_ret_pct",
                        "mv_drop_cascade",
                        "T_w3_mv_drop",
                        "T_w5_mv_drop",
                        "T_w20_mv_drop",
                    )
                },
                "lead_lag": r["lead_lag"],
            }
            for r in rows
        ]

    window_start = min(str(r.get("entry_date") or "") for r in ledger)
    window_end = max(str(r.get("entry_date") or "") for r in ledger)

    return {
        "schema": SCHEMA,
        "ledger_source": str(ledger_path),
        "ledger_meta": ledger_meta,
        "date_start": date_start or window_start,
        "date_end": date_end or window_end,
        "cohort_rules": {
            "champion": f"top {COHORT_TOP_PCT}% or net>={CHAMPION_NET_MIN}%",
            "failure": f"bottom {COHORT_BOTTOM_PCT}% or net<={FAILURE_NET_MAX}%",
            "baseline": "middle remainder",
        },
        "cohort_counts": {k: len(v) for k, v in cohorts.items()},
        "n_legs_analyzed": len(leg_rows),
        "trajectory_meta": traj_meta,
        "poll": "30m",
        "kbar_bar_minutes": 5,
        "giveback_pcts": list(GIVEBACK_PCTS),
        "cohort_summaries": cohort_summaries,
        "examples": {
            "champion": _top_examples("champion"),
            "failure": _top_examples("failure"),
        },
        "leg_rows": leg_rows,
    }


def render_abc_v3_f1_kinematic_leadlag_study_md(payload: dict[str, Any]) -> str:
    """Markdown report · champion vs failure lead-lag."""
    rules = payload.get("cohort_rules") or {}
    summaries = {s["cohort"]: s for s in payload.get("cohort_summaries") or []}
    champ = summaries.get("champion") or {}
    fail = summaries.get("failure") or {}
    base = summaries.get("baseline") or {}

    def _cascade_lines(s: dict[str, Any]) -> str:
        tops = s.get("mv_drop_cascade_top") or []
        if not tops:
            return "—"
        return ", ".join(f"{t['cascade']} ({t['pct']}%)" for t in tops[:3])

    lines = [
        "# ABC v3+f1 · kinematic lead-lag（MV 領先價格）",
        "",
        f"區間：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"ledger `{Path(str(payload.get('ledger_source', ''))).name}` · "
        f"**{payload.get('n_legs_analyzed')}** legs · {payload.get('poll')} poll · "
        f"{payload.get('kbar_bar_minutes')}m kbar",
        "",
        "## Cohort 定義",
        "",
        f"- **Champion（贏家）**：{rules.get('champion', '')}",
        f"- **Failure（輸家）**：{rules.get('failure', '')}",
        f"- **Baseline（中間）**：{rules.get('baseline', '')}",
        "",
        f"Ledger 分組 n：champion={payload.get('cohort_counts', {}).get('champion')} · "
        f"failure={payload.get('cohort_counts', {}).get('failure')} · "
        f"baseline={payload.get('cohort_counts', {}).get('baseline')}",
        "",
        "## Lead-lag 摘要（poll 數 · 正值 = MV 事件在價格事件之前）",
        "",
        "| Cohort | n | mean net% | W3 領先 peak% | W3 mean lead peak | W5 mean lead peak | "
        "W20 mean lead peak | W3→W5→W20 cascade% |",
        "|--------|---|-----------|---------------|-------------------|-------------------|"
        "-------------------|---------------------|",
    ]
    for label, s in (("champion", champ), ("failure", fail), ("baseline", base)):
        lines.append(
            f"| {label} | {s.get('n', 0)} | {s.get('mean_net_pct')}% | "
            f"{s.get('pct_w3_before_peak')}% | {s.get('mean_w3_lead_peak_polls')} | "
            f"{s.get('mean_w5_lead_peak_polls')} | {s.get('mean_w20_lead_peak_polls')} | "
            f"{s.get('pct_cascade_w3_w5_w20')}% |"
        )

    lines.extend(
        [
            "",
            "## MV drop cascade 順序（前 3 名）",
            "",
            f"- **Champion**：{_cascade_lines(champ)}",
            f"- **Failure**：{_cascade_lines(fail)}",
            f"- **Baseline**：{_cascade_lines(base)}",
            "",
            "## Giveback lead（W3 MV drop → 價格回吐 1%/2%）",
            "",
            f"- Champion W3 lead giveback 1%：{champ.get('mean_w3_lead_giveback_1pct_polls')} polls · "
            f"2%：{champ.get('mean_w3_lead_giveback_2pct_polls')} polls",
            f"- Failure W3 lead giveback 1%：{fail.get('mean_w3_lead_giveback_1pct_polls')} polls · "
            f"2%：{fail.get('mean_w3_lead_giveback_2pct_polls')} polls",
            "",
            "## 假設檢驗",
            "",
            "若 **MV decline precedes price decline（動量值下降領先價格回落）**：",
            "",
            f"- Failure cohort 應有較高 **W3 before peak**（目前 champion {champ.get('pct_w3_before_peak')}% vs "
            f"failure {fail.get('pct_w3_before_peak')}%）",
            f"- **w3>w5>w20 cascade** 在 failure 應更常見（champion {champ.get('pct_cascade_w3_w5_w20')}% vs "
            f"failure {fail.get('pct_cascade_w3_w5_w20')}%）",
            "",
            "## 模組",
            "",
            "- `scripts/research/archive/run_abc_v3_f1_kinematic_leadlag_study.py`",
            "- `src/research/backtest/archive/abc_v3_f1_kinematic_leadlag_study.py`",
        ]
    )
    return "\n".join(lines)
