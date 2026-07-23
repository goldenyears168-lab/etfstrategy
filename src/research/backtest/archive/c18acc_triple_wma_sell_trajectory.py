"""C18acc · triple WMA hold trajectory · peak visuals · MV-decline sell rules."""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    _collect_annot_dates,
    _exit_frame_idx_for_date,
)
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (
    _frame_idx_for_minute,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps, _price_at
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c
from research.backtest.triple_wma_pullback_sweep import _mv, _q, _rv
from research.backtest.w3_rv_hl_winner_profile import _mean, _median
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)

MV_DROP_EPS = 0.08
PEAK_SPREAD_DIST_MIN = 8.0
TARGET_PCTS = (5.0, 8.0, 10.0, 15.0)


@dataclass(frozen=True)
class FrameSnap:
    frame_idx: int
    trade_date: str
    minute: str
    px: float
    ret_from_entry_pct: float
    w20_rv: float | None
    w20_mv: float | None
    w20_quad: str | None
    w5_rv: float | None
    w5_mv: float | None
    w5_quad: str | None
    w3_rv: float | None
    w3_mv: float | None
    w3_quad: str | None
    spread_class: str | None
    spread_dist: float | None
    w3_mv_drop_poll: bool
    w5_mv_drop_poll: bool
    w3_mv_below_roll_peak: bool
    w5_mv_below_roll_peak: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_idx": self.frame_idx,
            "trade_date": self.trade_date,
            "minute": self.minute,
            "px": round(self.px, 4),
            "ret_from_entry_pct": round(self.ret_from_entry_pct, 3),
            "w20_rv": self.w20_rv,
            "w20_mv": self.w20_mv,
            "w20_quad": self.w20_quad,
            "w5_rv": self.w5_rv,
            "w5_mv": self.w5_mv,
            "w5_quad": self.w5_quad,
            "w3_rv": self.w3_rv,
            "w3_mv": self.w3_mv,
            "w3_quad": self.w3_quad,
            "spread_class": self.spread_class,
            "spread_dist": self.spread_dist,
            "w3_mv_drop_poll": self.w3_mv_drop_poll,
            "w5_mv_drop_poll": self.w5_mv_drop_poll,
            "w3_mv_below_roll_peak": self.w3_mv_below_roll_peak,
            "w5_mv_below_roll_peak": self.w5_mv_below_roll_peak,
        }


def _build_hold_timeline(
    conn: sqlite3.Connection,
    bench: pd.Series,
    *,
    stock_id: str,
    entry_i: int,
    exit_i: int,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    entry_px: float,
) -> list[FrameSnap]:
    if entry_px <= 0:
        return []
    snaps: list[FrameSnap] = []
    w3_peak = w5_peak = None
    prev_w3 = prev_w5 = None
    for j in range(entry_i, min(exit_i + 1, len(frames))):
        px, _ = _price_at(conn, bench, stock_id, frames[j]["date"], frames[j]["minute"])
        if px is None or px <= 0:
            continue
        p = points_by_idx[j]
        if not p:
            continue
        w3mv = _mv(p, "w3")
        w5mv = _mv(p, "w5")
        w3_drop = w3mv is not None and prev_w3 is not None and w3mv < prev_w3 - 1e-9
        w5_drop = w5mv is not None and prev_w5 is not None and w5mv < prev_w5 - 1e-9
        if w3mv is not None:
            w3_peak = w3mv if w3_peak is None else max(w3_peak, w3mv)
        if w5mv is not None:
            w5_peak = w5mv if w5_peak is None else max(w5_peak, w5mv)
        w3_roll = (
            w3mv is not None
            and w3_peak is not None
            and w3mv < w3_peak - MV_DROP_EPS
        )
        w5_roll = (
            w5mv is not None
            and w5_peak is not None
            and w5mv < w5_peak - MV_DROP_EPS
        )
        ret = (px / entry_px - 1.0) * 100.0
        f = frames[j]
        snaps.append(
            FrameSnap(
                frame_idx=j,
                trade_date=f["date"],
                minute=f["minute"],
                px=float(px),
                ret_from_entry_pct=ret,
                w20_rv=_rv(p, "w20"),
                w20_mv=_mv(p, "w20"),
                w20_quad=_q(p, "w20"),
                w5_rv=_rv(p, "w5"),
                w5_mv=w5mv,
                w5_quad=_q(p, "w5"),
                w3_rv=_rv(p, "w3"),
                w3_mv=w3mv,
                w3_quad=_q(p, "w3"),
                spread_class=p.get("spread_class"),
                spread_dist=float(p["spread_dist"]) if p.get("spread_dist") is not None else None,
                w3_mv_drop_poll=w3_drop,
                w5_mv_drop_poll=w5_drop,
                w3_mv_below_roll_peak=w3_roll,
                w5_mv_below_roll_peak=w5_roll,
            )
        )
        if w3mv is not None:
            prev_w3 = w3mv
        if w5mv is not None:
            prev_w5 = w5mv
    return snaps


def _peak_trough_idx(snaps: list[FrameSnap]) -> tuple[int, int]:
    if not snaps:
        return 0, 0
    peak_i = max(range(len(snaps)), key=lambda i: snaps[i].px)
    trough_i = min(range(len(snaps)), key=lambda i: snaps[i].px)
    return peak_i, trough_i


SellPredicate = Callable[[FrameSnap, list[FrameSnap]], bool]


def _pred_w3_mv_poll_drop(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w3_mv_drop_poll


def _pred_w5_mv_poll_drop(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w5_mv_drop_poll


def _pred_w3_or_w5_poll_drop(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w3_mv_drop_poll or s.w5_mv_drop_poll


def _pred_w3_roll_peak_break(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w3_mv_below_roll_peak


def _pred_w5_roll_peak_break(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w5_mv_below_roll_peak


def _pred_spread_pullback(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.spread_class == "pullback"


def _pred_spread_dist_high(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.spread_dist is not None and s.spread_dist >= PEAK_SPREAD_DIST_MIN


def _pred_w5_weakening(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w5_quad == "weakening"


def _pred_w5_lagging(s: FrameSnap, _hist: list[FrameSnap]) -> bool:
    return s.w5_quad == "lagging"


def _target_and(target: float, base: SellPredicate) -> SellPredicate:
    def _fn(s: FrameSnap, hist: list[FrameSnap]) -> bool:
        return s.ret_from_entry_pct >= target and base(s, hist)

    return _fn


def _combo(a: SellPredicate, b: SellPredicate) -> SellPredicate:
    def _fn(s: FrameSnap, hist: list[FrameSnap]) -> bool:
        return a(s, hist) and b(s, hist)

    return _fn


def _first_trigger(
    snaps: list[FrameSnap],
    pred: SellPredicate,
    *,
    skip_first: int = 1,
) -> int | None:
    for i in range(skip_first, len(snaps)):
        if pred(snaps[i], snaps[: i + 1]):
            return i
    return None


def _leg_analysis_row(
    period: dict[str, Any],
    snaps: list[FrameSnap],
    *,
    triggers: dict[str, int | None],
) -> dict[str, Any]:
    if not snaps:
        return {}
    peak_i, trough_i = _peak_trough_idx(snaps)
    peak, trough = snaps[peak_i], snaps[trough_i]
    actual_ret = float(period.get("return_pct") or 0)
    entry = snaps[0]
    prev_peak = snaps[peak_i - 1] if peak_i > 0 else peak
    row: dict[str, Any] = {
        "leg_id": f"{period.get('entry_date')}|{period.get('stock_id')}|{period.get('slot', 0)}",
        "stock_id": period.get("stock_id"),
        "stock_name": period.get("stock_name") or "",
        "return_pct": actual_ret,
        "outcome": "win" if actual_ret > 0 else "loss",
        "peak_ret_pct": round(peak.ret_from_entry_pct, 3),
        "trough_ret_pct": round(trough.ret_from_entry_pct, 3),
        "peak_spread_class": peak.spread_class,
        "peak_spread_dist": peak.spread_dist,
        "peak_w5_quad": peak.w5_quad,
        "peak_w3_quad": peak.w3_quad,
        "peak_w5_mv": peak.w5_mv,
        "peak_w3_mv": peak.w3_mv,
        "peak_w5_mv_delta_poll": (
            round(peak.w5_mv - prev_peak.w5_mv, 3)
            if peak.w5_mv is not None and prev_peak.w5_mv is not None
            else None
        ),
        "peak_w3_mv_delta_poll": (
            round(peak.w3_mv - prev_peak.w3_mv, 3)
            if peak.w3_mv is not None and prev_peak.w3_mv is not None
            else None
        ),
        "peak_w3_mv_drop_poll": peak.w3_mv_drop_poll,
        "peak_w5_mv_drop_poll": peak.w5_mv_drop_poll,
        "entry_w3_mv": entry.w3_mv,
        "entry_w5_mv": entry.w5_mv,
    }
    for rule_id, tri in triggers.items():
        if tri is None:
            row[f"{rule_id}_fired"] = False
            continue
        ts = snaps[tri]
        row[f"{rule_id}_fired"] = True
        row[f"{rule_id}_ret_pct"] = round(ts.ret_from_entry_pct, 3)
        row[f"{rule_id}_vs_peak_pp"] = round(ts.ret_from_entry_pct - peak.ret_from_entry_pct, 3)
        row[f"{rule_id}_vs_actual_pp"] = round(ts.ret_from_entry_pct - actual_ret, 3)
        if peak.ret_from_entry_pct > 0.5:
            row[f"{rule_id}_capture_pct"] = round(
                100.0 * ts.ret_from_entry_pct / peak.ret_from_entry_pct, 1
            )
        row[f"{rule_id}_timing"] = (
            "at_or_before_peak"
            if tri <= peak_i
            else "after_peak"
        )
    return row


def _summarize_rule(rows: list[dict[str, Any]], rule_id: str) -> dict[str, Any]:
    fired = [r for r in rows if r.get(f"{rule_id}_fired")]
    n = len(rows)
    nf = len(fired)
    if nf == 0:
        return {"rule_id": rule_id, "n_legs": n, "n_fired": 0}
    rets = [float(r[f"{rule_id}_ret_pct"]) for r in fired]
    actual = [float(r["return_pct"]) for r in fired]
    peak = [float(r["peak_ret_pct"]) for r in fired]
    vs_peak = [float(r[f"{rule_id}_vs_peak_pp"]) for r in fired if r.get(f"{rule_id}_vs_peak_pp") is not None]
    capture = [
        float(r[f"{rule_id}_capture_pct"])
        for r in fired
        if r.get(f"{rule_id}_capture_pct") is not None
    ]
    before = sum(1 for r in fired if r.get(f"{rule_id}_timing") == "at_or_before_peak")
    wins = [r for r in fired if r.get("outcome") == "win"]
    losses = [r for r in fired if r.get("outcome") == "loss"]
    return {
        "rule_id": rule_id,
        "n_legs": n,
        "n_fired": nf,
        "fire_rate_pct": round(100.0 * nf / n, 1),
        "mean_exit_ret_pct": _mean(rets),
        "median_exit_ret_pct": _median(rets),
        "mean_actual_ret_pct": _mean(actual),
        "mean_peak_ret_pct": _mean(peak),
        "mean_vs_peak_pp": _mean(vs_peak),
        "median_vs_peak_pp": _median(vs_peak),
        "mean_capture_pct_of_peak": _mean(capture),
        "pct_at_or_before_peak": round(100.0 * before / nf, 1),
        "n_win_fired": len(wins),
        "n_loss_fired": len(losses),
        "mean_exit_on_wins": _mean([float(r[f"{rule_id}_ret_pct"]) for r in wins]),
        "mean_exit_on_losses": _mean([float(r[f"{rule_id}_ret_pct"]) for r in losses]),
    }


def _peak_visual_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r for r in rows if r.get("outcome") == "win"]
    losses = [r for r in rows if r.get("outcome") == "loss"]
    top = sorted(rows, key=lambda r: float(r.get("peak_ret_pct") or 0), reverse=True)[
        : max(1, round(len(rows) * 0.25))
    ]

    def _rate(sub: list[dict[str, Any]], key: str) -> float | None:
        if not sub:
            return None
        return round(100.0 * sum(1 for r in sub if r.get(key)) / len(sub), 1)

    def _mean_field(sub: list[dict[str, Any]], key: str) -> float | None:
        vals = [float(r[key]) for r in sub if r.get(key) is not None]
        return _mean(vals)

    def _spread_rates(sub: list[dict[str, Any]]) -> dict[str, float]:
        if not sub:
            return {}
        counts: dict[str, int] = {}
        for r in sub:
            k = str(r.get("peak_spread_class") or "none")
            counts[k] = counts.get(k, 0) + 1
        n = len(sub)
        return {k: round(c / n * 100, 1) for k, c in sorted(counts.items())}

    return {
        "all_wins": {
            "n": len(wins),
            "peak_spread_class_pct": _spread_rates(wins),
            "peak_w3_mv_drop_poll_pct": _rate(wins, "peak_w3_mv_drop_poll"),
            "peak_w5_mv_drop_poll_pct": _rate(wins, "peak_w5_mv_drop_poll"),
            "mean_peak_spread_dist": _mean_field(wins, "peak_spread_dist"),
            "mean_peak_w5_mv_delta": _mean_field(wins, "peak_w5_mv_delta_poll"),
            "mean_peak_w3_mv_delta": _mean_field(wins, "peak_w3_mv_delta_poll"),
        },
        "all_losses": {
            "n": len(losses),
            "peak_spread_class_pct": _spread_rates(losses),
            "peak_w3_mv_drop_poll_pct": _rate(losses, "peak_w3_mv_drop_poll"),
            "peak_w5_mv_drop_poll_pct": _rate(losses, "peak_w5_mv_drop_poll"),
            "mean_peak_spread_dist": _mean_field(losses, "peak_spread_dist"),
        },
        "top_quartile_peak_ret": {
            "n": len(top),
            "peak_spread_class_pct": _spread_rates(top),
            "peak_w3_mv_drop_poll_pct": _rate(top, "peak_w3_mv_drop_poll"),
            "peak_w5_mv_drop_poll_pct": _rate(top, "peak_w5_mv_drop_poll"),
            "mean_peak_spread_dist": _mean_field(top, "peak_spread_dist"),
            "mean_peak_w5_mv_delta": _mean_field(top, "peak_w5_mv_delta_poll"),
            "mean_peak_w3_mv_delta": _mean_field(top, "peak_w3_mv_delta_poll"),
        },
    }


def _sell_rule_catalog() -> list[tuple[str, str, SellPredicate]]:
    rules: list[tuple[str, str, SellPredicate]] = [
        ("w3_mv_poll_drop", "W3 MV 較上一 poll 下降", _pred_w3_mv_poll_drop),
        ("w5_mv_poll_drop", "W5 MV 較上一 poll 下降", _pred_w5_mv_poll_drop),
        ("w3_or_w5_mv_poll_drop", "W3 或 W5 MV poll 下降", _pred_w3_or_w5_poll_drop),
        ("w3_mv_roll_peak_break", f"W3 MV 跌破持有期高點 −{MV_DROP_EPS}", _pred_w3_roll_peak_break),
        ("w5_mv_roll_peak_break", f"W5 MV 跌破持有期高點 −{MV_DROP_EPS}", _pred_w5_roll_peak_break),
        (
            "pullback_and_dist8",
            f"spread=pullback 且 dist≥{PEAK_SPREAD_DIST_MIN}",
            _combo(_pred_spread_pullback, _pred_spread_dist_high),
        ),
        ("spread_pullback", "spread 轉 pullback", _pred_spread_pullback),
        ("w5_weakening", "W5 象限 weakening", _pred_w5_weakening),
        ("w5_lagging", "W5 象限 lagging", _pred_w5_lagging),
    ]
    for t in TARGET_PCTS:
        tid = str(int(t)) if t == int(t) else str(t).replace(".", "p")
        rules.append(
            (
                f"ret{t}_w3_or_w5_mv_drop",
                f"獲利≥{t}% 且 W3/W5 MV poll 下降",
                _target_and(t, _pred_w3_or_w5_poll_drop),
            )
        )
        rules.append(
            (
                f"ret{t}_pullback_dist8",
                f"獲利≥{t}% 且 pullback+dist≥{PEAK_SPREAD_DIST_MIN}",
                _target_and(t, _combo(_pred_spread_pullback, _pred_spread_dist_high)),
            )
        )
    return rules


def run_c18acc_triple_wma_sell_trajectory_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 5:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    ctx = _prepare_c18acc_context(conn, trade_dates)
    cfg = c18acc_live_s2_config(n_slots=n_slots)
    periods, sim_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        mono_up_by_date=ctx["mono_up_by_date"],
        mono_up_fresh_by_date=ctx["mono_up_fresh_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
    )
    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, ctx["full_dates"])
    print(
        f"sell-trajectory: {len(periods)} legs · {len(stock_ids)} stocks · "
        f"{len(annot_dates)} dates …",
        flush=True,
    )
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    frames, trajectories, traj_meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=annot_dates,
        stock_ids=stock_ids,
        poll_minutes=intraday_poll_minutes(),
        micro_lengths=(WMA_MICRO_LENGTH,),
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    rules = _sell_rule_catalog()

    leg_rows: list[dict[str, Any]] = []
    for p in periods:
        sid = str(p["stock_id"])
        entry = str(p["entry_date"])
        exit_d = str(p.get("exit_date") or "")
        entry_px = p.get("entry_px")
        if not exit_d or not entry_px or float(entry_px) <= 0:
            continue
        points = stock_maps.get(sid)
        if not points:
            continue
        entry_i = _frame_idx_for_minute(frames, entry, p.get("entry_minute"))
        exit_i = _exit_frame_idx_for_date(frames, exit_d)
        if exit_i < entry_i:
            continue
        snaps = _build_hold_timeline(
            conn,
            bench,
            stock_id=sid,
            entry_i=entry_i,
            exit_i=exit_i,
            frames=frames,
            points_by_idx=points,
            entry_px=float(entry_px),
        )
        if len(snaps) < 2:
            continue
        triggers = {rid: _first_trigger(snaps, pred) for rid, _, pred in rules}
        row = _leg_analysis_row(p, snaps, triggers=triggers)
        if row:
            leg_rows.append(row)

    rule_summaries = [_summarize_rule(leg_rows, rid) for rid, _, _ in rules]
    rule_summaries.sort(
        key=lambda x: (
            -(float(x.get("mean_capture_pct_of_peak") or 0)),
            float(x.get("mean_vs_peak_pp") or -999),
        ),
    )
    peak_profile = _peak_visual_profile(leg_rows)

    return {
        "schema": "c18acc_triple_wma_sell_trajectory-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_slots": n_slots,
        "sim_summary": sim_summary,
        "trajectory_meta": traj_meta,
        "n_legs": len(leg_rows),
        "mv_drop_eps": MV_DROP_EPS,
        "peak_spread_dist_min": PEAK_SPREAD_DIST_MIN,
        "target_pcts": list(TARGET_PCTS),
        "peak_visual_profile": peak_profile,
        "sell_rule_summaries": rule_summaries,
        "leg_rows": leg_rows,
    }


def render_c18acc_triple_wma_sell_trajectory_md(payload: dict[str, Any]) -> str:
    pv = payload["peak_visual_profile"]
    top = pv.get("top_quartile_peak_ret") or {}
    wins = pv.get("all_wins") or {}
    rules = payload.get("sell_rule_summaries") or []

    def _fmt_spread(d: dict[str, float]) -> str:
        return ", ".join(f"{k} {v}%" for k, v in (d or {}).items()) or "—"

    lines = [
        "# C18acc · WMA 軌跡賣點研究",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽 · **{payload['n_legs']}** legs · 30m poll",
        "",
        "## 圖上「已經很高」長什麼樣（價格高點當下）",
        "",
        "### 全部贏家（n={}）".format(wins.get("n", 0)),
        "",
        f"- **spread 型態**：{_fmt_spread(wins.get('peak_spread_class_pct'))}",
        f"- **spread_dist 均值**：{wins.get('mean_peak_spread_dist')}",
        f"- **高點當根 W3 MV 較前一根下降**：{wins.get('peak_w3_mv_drop_poll_pct')}%",
        f"- **高點當根 W5 MV 較前一根下降**：{wins.get('peak_w5_mv_drop_poll_pct')}%",
        "",
        "### 漲幅前 25% leg 的高點（n={}）".format(top.get("n", 0)),
        "",
        f"- **spread**：{_fmt_spread(top.get('peak_spread_class_pct'))}",
        f"- **spread_dist 均值**：{top.get('mean_peak_spread_dist')}",
        f"- **W5 MV Δ（高點 vs 前 poll）均值**：{top.get('mean_peak_w5_mv_delta')}",
        f"- **W3 MV Δ 均值**：{top.get('mean_peak_w3_mv_delta')}",
        f"- **高點當根 W3/W5 MV 下降比例**：{top.get('peak_w3_mv_drop_poll_pct')}% / "
        f"{top.get('peak_w5_mv_drop_poll_pct')}%",
        "",
        "**圖上辨識（高點區）**：",
        "",
        "1. **W20 遠跑在 W5 前面** → spread 多為 **Pullback**、dist 常 **>8**",
        "2. **W5/W3 仍在 Leading 或轉 Weakening** → 價格可能還在创新高，但短線動能已放缓",
        "3. **W3 或 W5 的 MV 較前一根 poll 下降** → 在漲幅前 25% leg 的高點約 "
        f"**{top.get('peak_w3_mv_drop_poll_pct')}% / {top.get('peak_w5_mv_drop_poll_pct')}%** 出現",
        "",
        "## 軌跡怎麼變 → 考慮賣出（規則回測）",
        "",
        "| 規則 | 觸發率 | 賣出均報酬 | 實際均報酬 | 距高點(pp) | 捕捉高點% | 高點前觸發% |",
        "|------|--------|------------|------------|------------|-----------|-------------|",
    ]
    for r in rules:
        if not r.get("n_fired"):
            continue
        lines.append(
            f"| {r['rule_id']} | {r.get('fire_rate_pct')}% | "
            f"{r.get('mean_exit_ret_pct')}% | {r.get('mean_actual_ret_pct')}% | "
            f"{r.get('mean_vs_peak_pp')} | {r.get('mean_capture_pct_of_peak')}% | "
            f"{r.get('pct_at_or_before_peak')}% |"
        )
    lines.extend(
        [
            "",
            "## 你的假設：超過目標 + W3/W5 MV 下降",
            "",
        ]
    )
    user_rules = [r for r in rules if str(r.get("rule_id", "")).startswith("ret")]
    for r in user_rules:
        if "mv_drop" not in str(r.get("rule_id")):
            continue
        lines.append(
            f"- **{r['rule_id']}**：觸發 {r.get('n_fired')}/{r.get('n_legs')} · "
            f"賣出均 {r.get('mean_exit_ret_pct')}% · 距高點 {r.get('mean_vs_peak_pp')}pp · "
            f"捕捉 {r.get('mean_capture_pct_of_peak')}% · 高點前 {r.get('pct_at_or_before_peak')}%"
        )
    lines.extend(
        [
            "",
            "## 實務觀察清單（盤中）",
            "",
            _practice_checklist(payload),
        ]
    )
    return "\n".join(lines)


def _practice_checklist(payload: dict[str, Any]) -> str:
    rules = {r["rule_id"]: r for r in payload.get("sell_rule_summaries") or []}
    pb = rules.get("pullback_and_dist8") or {}
    mv = rules.get("ret10_w3_or_w5_mv_drop") or rules.get("ret8_w3_or_w5_mv_drop") or {}
    w5lag = rules.get("w5_lagging") or {}
    bullets = [
        "1. **獲利達標後**（建議 ≥8–10%）觀察 W3/W5 MV 是否較前一根 30m poll **轉弱下降**",
        "2. 同時看 **spread 是否轉 Pullback** 且 **dist 拉大（≥8）** — 大贏家高點最常見",
        "3. **W5 跌入 Lagging** → 停損優先（輸家低點常見）",
        "4. 單獨 MV 下降若 **未達獲利目標** 可能過早賣出 · 宜與目標價或 spread 型態併用",
    ]
    if pb.get("n_fired"):
        bullets.append(
            f"5. 回測 **pullback+dist≥8**：觸發率 {pb.get('fire_rate_pct')}% · "
            f"捕捉高點 {pb.get('mean_capture_pct_of_peak')}% · "
            f"距高點 {pb.get('mean_vs_peak_pp')}pp"
        )
    if mv.get("n_fired"):
        bullets.append(
            f"6. 回測 **{mv.get('rule_id')}**：觸發率 {mv.get('fire_rate_pct')}% · "
            f"捕捉 {mv.get('mean_capture_pct_of_peak')}%"
        )
    if w5lag.get("n_fired"):
        bullets.append(
            f"7. **W5 lagging** 觸發率 {w5lag.get('fire_rate_pct')}% · "
            f"虧損 leg 賣出均 {w5lag.get('mean_exit_on_losses')}%"
        )
    return "\n".join(bullets)
