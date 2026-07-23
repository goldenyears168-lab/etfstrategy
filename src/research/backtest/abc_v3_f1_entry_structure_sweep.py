"""ABC v3+F1 · entry structure sweep · MV gap sweet-band + W3 RV trough rebound.

Hypothesis: entry quality can be designed directly from entry-side structure
(how aligned W20/W5/W3 MV legs are, and whether W3 RV has just bounced off its
intraday trough) rather than by inverting exit-side kinematic rules (gaps
opening, RV declining from peak — see c18acc_kinematic_gap_rv3_study.py).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.dual_wma_signal_backtest import (
    _build_stock_frame_maps,
    _exit_frame_idx,
    _trade_return,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import (
    _mv,
    _rv,
    _summarize_trades,
    matches_w3_rv_hl_abc_v3_f1,
)
from research.backtest.w3_rv_hl_winner_profile import _poll_idx_for_frame
from research_config import load_research_splits
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

Rv3ReboundMode = Literal["rebound_only", "rebound_and_poll_rise"]


def _safe_gap(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(a - b, 4)


@dataclass(frozen=True)
class GapSweetBand:
    """W20-W5 and W5-W3 MV gap bounds — aligned-but-not-overextended band."""

    gap_w20_w5_min: float
    gap_w20_w5_max: float
    gap_w5_w3_min: float
    gap_w5_w3_max: float

    def ok(self, p: dict[str, Any]) -> bool:
        gap_w20_w5 = _safe_gap(_mv(p, "w20"), _mv(p, "w5"))
        gap_w5_w3 = _safe_gap(_mv(p, "w5"), _mv(p, "w3"))
        if gap_w20_w5 is None or gap_w5_w3 is None:
            return False
        return (
            self.gap_w20_w5_min <= gap_w20_w5 <= self.gap_w20_w5_max
            and self.gap_w5_w3_min <= gap_w5_w3 <= self.gap_w5_w3_max
        )

    def label(self) -> str:
        return (
            f"gap20-5[{self.gap_w20_w5_min:g},{self.gap_w20_w5_max:g}]·"
            f"gap5-3[{self.gap_w5_w3_min:g},{self.gap_w5_w3_max:g}]"
        )


DEFAULT_GAP_SWEET_BAND = GapSweetBand(
    gap_w20_w5_min=2.0,
    gap_w20_w5_max=7.0,
    gap_w5_w3_min=-0.5,
    gap_w5_w3_max=1.5,
)


@dataclass
class Rv3TroughState:
    """Per-stock-day running trough tracker for W3 RV. Reset at poll_idx==0 (09:00)."""

    trough: float | None = None
    prev_rv3: float | None = None

    def reset(self) -> None:
        self.trough = None
        self.prev_rv3 = None

    def update(self, rv3: float) -> tuple[float, bool]:
        """Returns (rebound = rv3 - trough_so_far, poll_rise = rv3 > prev_rv3)."""
        if self.trough is None or rv3 < self.trough:
            self.trough = rv3
        rebound = round(rv3 - self.trough, 4)
        poll_rise = self.prev_rv3 is not None and rv3 > self.prev_rv3
        self.prev_rv3 = rv3
        return rebound, poll_rise


def _rebound_ok(
    rebound: float,
    poll_rise: bool,
    *,
    rebound_min: float,
    mode: Rv3ReboundMode,
) -> bool:
    if rebound < rebound_min - 1e-9:
        return False
    if mode == "rebound_and_poll_rise":
        return poll_rise
    return True


@dataclass(frozen=True)
class EntryStructureParams:
    id: str
    label: str
    gap_band: GapSweetBand
    rebound_min: float
    rebound_mode: Rv3ReboundMode
    min_poll_idx: int = 0


def build_entry_structure_grid(
    *,
    gap_bands: tuple[GapSweetBand, ...] = (DEFAULT_GAP_SWEET_BAND,),
    rebound_mins: tuple[float, ...] = (0.2, 0.3, 0.5, 0.8),
    rebound_modes: tuple[Rv3ReboundMode, ...] = ("rebound_only", "rebound_and_poll_rise"),
    min_poll_idxs: tuple[int, ...] = (0, 1),
) -> tuple[EntryStructureParams, ...]:
    cells: list[EntryStructureParams] = []
    for gb in gap_bands:
        for rmin in rebound_mins:
            for mode in rebound_modes:
                for mp in min_poll_idxs:
                    cid = (
                        f"gap{gb.gap_w20_w5_min:g}_{gb.gap_w20_w5_max:g}_"
                        f"r{rmin:g}_{mode}_mp{mp}"
                    )
                    label = f"{gb.label()} · rebound≥{rmin:g}({mode}) · poll≥{mp}"
                    cells.append(
                        EntryStructureParams(
                            id=cid,
                            label=label,
                            gap_band=gb,
                            rebound_min=rmin,
                            rebound_mode=mode,
                            min_poll_idx=mp,
                        )
                    )
    return tuple(cells)


def _spec_dict(cell: EntryStructureParams) -> dict[str, Any]:
    return {
        "id": cell.id,
        "label": cell.label,
        "gap_w20_w5_min": cell.gap_band.gap_w20_w5_min,
        "gap_w20_w5_max": cell.gap_band.gap_w20_w5_max,
        "gap_w5_w3_min": cell.gap_band.gap_w5_w3_min,
        "gap_w5_w3_max": cell.gap_band.gap_w5_w3_max,
        "rebound_min": cell.rebound_min,
        "rebound_mode": cell.rebound_mode,
        "min_poll_idx": cell.min_poll_idx,
    }


def backtest_abc_v3_f1_entry_structure_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    grid: tuple[EntryStructureParams, ...] | None = None,
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """ABC v3+F1 baseline vs. MV-gap-sweet-band + W3-RV-trough-rebound grid.

    Trough tracker updates on every poll (not just F1-matching ones) so the
    rebound signal reflects the true intraday RV path, independent of when F1
    happens to fire.
    """
    grid = grid or build_entry_structure_grid()
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    frame_ids = [f["id"] for f in frames]

    baseline_rows: list[dict[str, Any]] = []
    seen_baseline: set[tuple[str, str]] = set()
    cell_rows: dict[str, list[dict[str, Any]]] = {c.id: [] for c in grid}
    seen_by_cell: dict[str, set[tuple[str, str]]] = {c.id: set() for c in grid}

    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        pts = stock_maps.get(sid)
        if not pts:
            continue
        trough = Rv3TroughState()
        for i, fid in enumerate(frame_ids):
            poll_idx = _poll_idx_for_frame(frames, i)
            if poll_idx is None:
                continue
            p = by_id.get(fid)
            if not p:
                continue
            if poll_idx == 0:
                trough.reset()
            rv3 = _rv(p, "w3")
            rebound: float | None = None
            poll_rise = False
            if rv3 is not None:
                rebound, poll_rise = trough.update(rv3)
            if not matches_w3_rv_hl_abc_v3_f1(p):
                continue
            d = frames[i]["date"]
            key = (sid, d)
            if key not in seen_baseline:
                seen_baseline.add(key)
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is not None:
                    baseline_rows.append(
                        {**tr, "stock_id": sid, "entry_date": d, "entry_poll_idx": poll_idx}
                    )
            if rebound is None:
                continue
            for cell in grid:
                if poll_idx < cell.min_poll_idx:
                    continue
                if key in seen_by_cell[cell.id]:
                    continue
                if not cell.gap_band.ok(p):
                    continue
                if not _rebound_ok(
                    rebound, poll_rise, rebound_min=cell.rebound_min, mode=cell.rebound_mode
                ):
                    continue
                seen_by_cell[cell.id].add(key)
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is None:
                    continue
                cell_rows[cell.id].append(
                    {**tr, "stock_id": sid, "entry_date": d, "entry_poll_idx": poll_idx}
                )

    summaries = {"baseline_f1": _summarize_trades(baseline_rows)}
    summaries.update({c.id: _summarize_trades(cell_rows[c.id]) for c in grid})

    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "matcher": "w3_rv_hl_abc_v3_f1",
        "grid": [_spec_dict(c) for c in grid],
        "summaries": summaries,
    }


def verdict_abc_v3_f1_entry_structure_sweep(
    bt: dict[str, Any],
    *,
    min_n: int = 15,
) -> dict[str, Any]:
    summaries = bt.get("summaries") or {}
    baseline = summaries.get("baseline_f1") or {}
    base_ret = float(baseline.get("mean_ret_3d_net_pct") or 0)
    base_n = int(baseline.get("n") or 0)

    cells: list[dict[str, Any]] = []
    for spec in bt.get("grid") or []:
        cid = str(spec.get("id"))
        row = summaries.get(cid) or {}
        n = int(row.get("n") or 0)
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        cells.append(
            {
                **spec,
                "gap_band_label": (
                    f"gap20-5[{spec.get('gap_w20_w5_min'):g},{spec.get('gap_w20_w5_max'):g}]·"
                    f"gap5-3[{spec.get('gap_w5_w3_min'):g},{spec.get('gap_w5_w3_max'):g}]"
                ),
                **row,
                "delta_vs_baseline": round(ret - base_ret, 3),
                "fill_rate_pct": round(100.0 * n / base_n, 1) if base_n else None,
            }
        )
    cells.sort(key=lambda c: float(c.get("mean_ret_3d_net_pct") or -999), reverse=True)

    eligible = [c for c in cells if int(c.get("n") or 0) >= min_n]
    best = eligible[0] if eligible else None

    reasons: list[str] = []
    passed = False
    recommend = "baseline_f1"
    if best and float(best.get("mean_ret_3d_net_pct") or 0) > base_ret + 0.05:
        passed = True
        recommend = str(best.get("id"))
        reasons.append(
            f"Cell {best.get('label')} beats F1 baseline "
            f"({best.get('mean_ret_3d_net_pct')}% vs {base_ret}%) · "
            f"n {base_n}→{best.get('n')} · fill {best.get('fill_rate_pct')}%"
        )
    elif best:
        reasons.append(
            f"Best cell {best.get('label')} does not beat F1 baseline "
            f"({best.get('mean_ret_3d_net_pct')}% vs {base_ret}%)"
        )
    else:
        reasons.append(f"No grid cell reached min_n={min_n}")

    return {
        "passed": passed,
        "recommend": recommend,
        "baseline": baseline,
        "best_cell": best,
        "cells": cells,
        "reasons": reasons,
        "summary": (
            f"Entry structure sweep · baseline n={base_n} ret={base_ret}% · "
            f"best={'**' + str((best or {}).get('id')) + '**' if best else '—'} · "
            f"{'**passed**' if passed else 'hold F1 baseline'}"
        ),
    }


def _split_dates_is_oos(dates: list[str], *, ratio: float) -> tuple[list[str], list[str]]:
    """Date-ordered IS/OOS split — no leakage (OOS strictly after IS)."""
    n = len(dates)
    if n == 0:
        return [], []
    n_is = max(0, min(n, int(round(n * ratio))))
    if n > 1:
        n_is = min(n_is, n - 1)
        n_is = max(n_is, 1)
    return dates[:n_is], dates[n_is:]


def collect_same_day_counterfactual_events(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    winner: EntryStructureParams,
    delay_polls: tuple[int, ...] = (1, 2),
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """For stock-days where F1 fires on >=2 polls: first F1 trigger vs. waiting
    for the winning cell to also fire vs. delaying further after that."""
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    frame_ids = [f["id"] for f in frames]

    rows: dict[str, list[dict[str, Any]]] = {"first_f1": [], "winner_cell": []}
    for dp in delay_polls:
        rows[f"delay_{dp}"] = []

    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        pts = stock_maps.get(sid)
        if not pts:
            continue
        day_frame_idxs: dict[str, list[int]] = {}
        for i, fid in enumerate(frame_ids):
            if by_id.get(fid) is None:
                continue
            day_frame_idxs.setdefault(frames[i]["date"], []).append(i)

        trough = Rv3TroughState()
        f1_idx_by_day: dict[str, int] = {}
        n_f1_by_day: dict[str, int] = {}
        winner_idx_by_day: dict[str, int] = {}
        for i, fid in enumerate(frame_ids):
            poll_idx = _poll_idx_for_frame(frames, i)
            if poll_idx is None:
                continue
            p = by_id.get(fid)
            if not p:
                continue
            if poll_idx == 0:
                trough.reset()
            rv3 = _rv(p, "w3")
            rebound: float | None = None
            poll_rise = False
            if rv3 is not None:
                rebound, poll_rise = trough.update(rv3)
            if not matches_w3_rv_hl_abc_v3_f1(p):
                continue
            d = frames[i]["date"]
            n_f1_by_day[d] = n_f1_by_day.get(d, 0) + 1
            if d not in f1_idx_by_day:
                f1_idx_by_day[d] = i
            if (
                d not in winner_idx_by_day
                and rebound is not None
                and poll_idx >= winner.min_poll_idx
                and winner.gap_band.ok(p)
                and _rebound_ok(
                    rebound, poll_rise, rebound_min=winner.rebound_min, mode=winner.rebound_mode
                )
            ):
                winner_idx_by_day[d] = i

        for d, n_f1 in n_f1_by_day.items():
            if n_f1 < 2:
                continue
            first_i = f1_idx_by_day[d]
            exit_i = _exit_frame_idx(exit_rule, first_i, frames, pts)
            tr = _trade_return(conn, bench, stock_maps, sid, first_i, exit_i, frames)
            if tr is not None:
                rows["first_f1"].append({**tr, "stock_id": sid, "entry_date": d})

            w_i = winner_idx_by_day.get(d)
            if w_i is None:
                continue
            exit_i = _exit_frame_idx(exit_rule, w_i, frames, pts)
            tr = _trade_return(conn, bench, stock_maps, sid, w_i, exit_i, frames)
            if tr is not None:
                rows["winner_cell"].append({**tr, "stock_id": sid, "entry_date": d})

            day_polls = day_frame_idxs.get(d) or []
            last_i = day_polls[-1] if day_polls else w_i
            for dp in delay_polls:
                delayed_i = min(w_i + dp, last_i)
                exit_i2 = _exit_frame_idx(exit_rule, delayed_i, frames, pts)
                tr2 = _trade_return(conn, bench, stock_maps, sid, delayed_i, exit_i2, frames)
                if tr2 is not None:
                    rows[f"delay_{dp}"].append({**tr2, "stock_id": sid, "entry_date": d})

    summaries = {k: _summarize_trades(v) for k, v in rows.items()}
    return {
        "dates": dates,
        "winner_id": winner.id,
        "winner_label": winner.label,
        "delay_polls": list(delay_polls),
        "summaries": summaries,
    }


def run_abc_v3_f1_entry_structure_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    min_n: int = 15,
    grid: tuple[EntryStructureParams, ...] | None = None,
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    grid = grid or build_entry_structure_grid()
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )

    split = load_research_splits().get("intraday_is_oos_70_30")
    ratio = float(split.ratio) if split and split.ratio is not None else 0.7
    min_oos_n = int(split.min_oos_n) if split and split.min_oos_n is not None else 80
    is_dates, oos_dates = _split_dates_is_oos(sample_dates, ratio=ratio)

    is_bt = backtest_abc_v3_f1_entry_structure_sweep(
        conn, dates=is_dates, grid=grid, exit_rule=exit_rule, etf_codes=etf_codes
    )
    is_verdict = verdict_abc_v3_f1_entry_structure_sweep(is_bt, min_n=min_n)
    winner_id = str(is_verdict.get("recommend") or "baseline_f1")
    winner_cell = next((c for c in grid if c.id == winner_id), None)

    oos_payload: dict[str, Any] = {}
    if winner_cell is not None and oos_dates:
        oos_bt = backtest_abc_v3_f1_entry_structure_sweep(
            conn, dates=oos_dates, grid=(winner_cell,), exit_rule=exit_rule, etf_codes=etf_codes
        )
        oos_summaries = oos_bt.get("summaries") or {}
        oos_summary = oos_summaries.get(winner_cell.id) or {}
        oos_baseline = oos_summaries.get("baseline_f1") or {}
        oos_n = int(oos_summary.get("n") or 0)
        oos_payload = {
            "winner_id": winner_cell.id,
            "winner_label": winner_cell.label,
            "oos_summary": oos_summary,
            "oos_baseline": oos_baseline,
            "oos_n": oos_n,
            "meets_min_oos_n": oos_n >= min_oos_n,
        }

    counterfactual: dict[str, Any] = {}
    if winner_cell is not None:
        counterfactual = collect_same_day_counterfactual_events(
            conn,
            dates=sample_dates,
            winner=winner_cell,
            exit_rule=exit_rule,
            etf_codes=etf_codes,
        )

    return {
        "schema": "abc_v3_f1_entry_structure_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "min_n": min_n,
        "split_spec": "intraday_is_oos_70_30",
        "is_dates": is_dates,
        "oos_dates": oos_dates,
        "min_oos_n": min_oos_n,
        "is_backtest": is_bt,
        "is_verdict": is_verdict,
        "oos": oos_payload,
        "counterfactual": counterfactual,
    }


def render_abc_v3_f1_entry_structure_md(payload: dict[str, Any]) -> str:
    v = payload.get("is_verdict") or {}
    oos = payload.get("oos") or {}
    cf = payload.get("counterfactual") or {}
    lines = [
        "# ABC v3+F1 · entry structure sweep（MV gap sweet-band + W3 RV 谷底反彈）",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## 機制",
        "",
        "- Baseline：**ABC v3+F1**（`matches_w3_rv_hl_abc_v3_f1`）當日第一次觸發",
        "- 新增兩個進場閘門（皆疊加在 baseline 之上）：",
        "  - **MV gap sweet-band**：W20−W5、W5−W3 MV 差距落在區間內（對齊但未過度延伸，"
        "與出場端「gap 打開」相反）",
        "  - **W3 RV 谷底反彈**：09:00 起 W3 RV 的 running trough，反彈幅度 ≥ δ"
        "（可選同時要求該 poll 較前一 poll 上升）",
        "",
        f"- IS 樣本：**{len(payload.get('is_dates') or [])}** 天 · "
        f"OOS 樣本：**{len(payload.get('oos_dates') or [])}** 天"
        f"（`{payload.get('split_spec')}`）",
        "- Exit: **hold_3d**",
        "",
        "## IS grid",
        "",
        "| Cell | gap band | rebound≥ | mode | min poll | n | fill% | mean ret% | win% | Δ vs baseline |",
        "|------|----------|---------:|------|---------:|--:|------:|----------:|-----:|---------------:|",
    ]
    for c in v.get("cells") or []:
        lines.append(
            f"| {c.get('label')} | {c.get('gap_band_label', '—')} | {c.get('rebound_min')} | "
            f"{c.get('rebound_mode')} | {c.get('min_poll_idx')} | {c.get('n', 0)} | "
            f"{c.get('fill_rate_pct', '—')} | {c.get('mean_ret_3d_net_pct', '—')} | "
            f"{c.get('win_rate_pct', '—')} | {c.get('delta_vs_baseline', '—')} |"
        )

    lines.extend(["", "## OOS 驗證（IS 選出的 winner cell）", ""])
    if oos:
        oos_summary = oos.get("oos_summary") or {}
        oos_baseline = oos.get("oos_baseline") or {}
        lines.extend(
            [
                f"- Winner: **{oos.get('winner_label')}**",
                f"- OOS n={oos.get('oos_n')}（min_oos_n={payload.get('min_oos_n')}）· "
                f"{'✓ 達標' if oos.get('meets_min_oos_n') else '⚠ 樣本不足'}",
                f"- OOS mean ret%：winner **{oos_summary.get('mean_ret_3d_net_pct')}** "
                f"vs baseline **{oos_baseline.get('mean_ret_3d_net_pct')}**",
            ]
        )
    else:
        lines.append("- （無 winner cell 或 OOS 樣本為空）")

    lines.extend(["", "## 同日 counterfactual（多次觸發 stock-day）", ""])
    cs = cf.get("summaries") or {}
    if cs:
        lines.extend(["| 情境 | n | mean ret% | win% |", "|------|--:|----------:|-----:|"])
        labels = {"first_f1": "第一次 F1 觸發", "winner_cell": "等待 winner cell 確認"}
        for key in sorted(k for k in cs if k.startswith("delay_")):
            labels[key] = f"winner 後延遲 {key.split('_')[1]} poll"
        for key, label in labels.items():
            row = cs.get(key) or {}
            lines.append(
                f"| {label} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
                f"{row.get('win_rate_pct', '—')} |"
            )
    else:
        lines.append("- （無多次觸發樣本）")

    lines.extend(["", "## Verdict", "", f"- **Recommend**: {v.get('recommend')}", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/research/run_abc_v3_f1_entry_structure_sweep.py`"])
    return "\n".join(lines)
