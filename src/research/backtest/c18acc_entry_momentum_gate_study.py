"""C18acc champion · entry-time W3 MV/RV momentum gate study (5m poll)."""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import replace
from typing import Any, Callable

import pandas as pd

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_triple_wma_extreme_profile import _frame_idx_for_minute
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.triple_wma_pullback_sweep import _mv, _q, _rv
from research.backtest.w3_rv_hl_winner_profile import _mean, extract_entry_features
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)


def _poll_5m_minutes() -> list[str]:
    return intraday_poll_minutes(interval_min=5, start="09:05", end="13:20")


def _prev_same_day_idx(frames: list[dict[str, str]], entry_i: int) -> int | None:
    if entry_i <= 0:
        return None
    day = frames[entry_i]["date"]
    j = entry_i - 1
    while j >= 0 and frames[j]["date"] == day:
        return j
    return None


def _point_momentum(
    entry_pt: dict[str, Any],
    prev_pt: dict[str, Any] | None,
) -> dict[str, Any]:
    feats = extract_entry_features(entry_pt)
    w3_mv = _mv(entry_pt, "w3")
    w3_rv = _rv(entry_pt, "w3")
    w5_mv = _mv(entry_pt, "w5")
    w20_mv = _mv(entry_pt, "w20")
    prev_w3_mv = _mv(prev_pt, "w3") if prev_pt else None
    prev_w3_rv = _rv(prev_pt, "w3") if prev_pt else None
    has_prev = prev_pt is not None
    mv_rising = has_prev and w3_mv is not None and prev_w3_mv is not None and w3_mv > prev_w3_mv
    rv_rising = has_prev and w3_rv is not None and prev_w3_rv is not None and w3_rv > prev_w3_rv
    mv_top = (
        w3_mv is not None
        and w5_mv is not None
        and w20_mv is not None
        and w3_mv >= w5_mv
        and w3_mv >= w20_mv
    )
    return {
        **feats,
        "w3_mv": w3_mv,
        "w3_rv": w3_rv,
        "w5_mv": w5_mv,
        "w20_mv": w20_mv,
        "w3_quadrant": _q(entry_pt, "w3"),
        "has_prev_poll": has_prev,
        "prev_w3_mv": prev_w3_mv,
        "prev_w3_rv": prev_w3_rv,
        "w3_mv_delta_poll": round(w3_mv - prev_w3_mv, 4)
        if has_prev and w3_mv is not None and prev_w3_mv is not None
        else None,
        "w3_rv_delta_poll": round(w3_rv - prev_w3_rv, 4)
        if has_prev and w3_rv is not None and prev_w3_rv is not None
        else None,
        "w3_mv_rising_poll": mv_rising,
        "w3_rv_rising_poll": rv_rising,
        "w3_mv_ge_100": w3_mv is not None and w3_mv >= 100.0,
        "w3_mv_top": mv_top,
        "w3_improving": _q(entry_pt, "w3") == "improving",
        "w3_both_falling_poll": has_prev and not mv_rising and not rv_rising,
        "w3_momentum_ok": (w3_mv is not None and w3_mv >= 100.0) and rv_rising,
    }


def _cohort_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "mean_return_pct": None,
            "mean_excess_pct": None,
            "win_rate_pct": None,
        }
    rets = [float(r["return_pct"]) for r in rows]
    excess = [float(r["excess_pct"]) for r in rows if r.get("excess_pct") is not None]
    return {
        "n": len(rows),
        "mean_return_pct": _mean(rets),
        "mean_excess_pct": _mean(excess) if excess else None,
        "win_rate_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 2),
        "median_return_pct": round(statistics.median(rets), 4),
    }


def _gate_specs() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    return [
        ("baseline", "全樣本", lambda r: True),
        ("w3_mv_ge_100", "W3 MV ≥ 100", lambda r: bool(r.get("w3_mv_ge_100"))),
        ("w3_mv_top", "W3 MV 三腿最高", lambda r: bool(r.get("w3_mv_top"))),
        ("w3_rv_rising", "W3 RV 較上一格上升", lambda r: bool(r.get("w3_rv_rising_poll"))),
        ("w3_mv_rising", "W3 MV 較上一格上升", lambda r: bool(r.get("w3_mv_rising_poll"))),
        ("w3_improving", "W3 Improving 象限", lambda r: bool(r.get("w3_improving"))),
        (
            "combo_mv_high_rv_up",
            "MV≥100 且 RV 較上一格上升",
            lambda r: bool(r.get("w3_momentum_ok")),
        ),
        (
            "combo_not_falling",
            "MV 或 RV 至少一個較上一格上升",
            lambda r: bool(r.get("has_prev_poll"))
            and (bool(r.get("w3_mv_rising_poll")) or bool(r.get("w3_rv_rising_poll"))),
        ),
        (
            "falling_both",
            "MV 與 RV 皆未較上一格上升",
            lambda r: bool(r.get("w3_both_falling_poll")),
        ),
    ]


def collect_c18acc_entry_momentum_legs(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-02",
    date_end: str | None = None,
    confirm_bars: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[tuple[str, str], tuple[list[dict[str, str]], list[dict[str, Any] | None]]]]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    cfg = champion_score_swap_c_config()
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(cfg)
        else None
    )
    base_c = next(c for c in DEFAULT_C_SWEEP if c.variant_id == ("C0" if confirm_bars <= 1 else "C3"))
    entry_c = (
        base_c
        if int(base_c.confirm_bars) == int(confirm_bars)
        else replace(base_c, confirm_bars=int(confirm_bars))
    )
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    periods, sim_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        mono_by_date=mono_by_date,
        kbar_cache=kbar_cache,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        spread_rs_panel=spread_rs_panel,
        entry_c_config=entry_c,
    )
    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    by_date: dict[str, list[str]] = defaultdict(list)
    for p in periods:
        sid = str(p["stock_id"])
        d = str(p["entry_date"])
        if sid not in by_date[d]:
            by_date[d].append(sid)
    poll_5m = _poll_5m_minutes()
    print(
        f"c18acc entry momentum: {len(periods)} legs · {len(stock_ids)} stocks · "
        f"{len(by_date)} entry-days · 5m polls …",
        flush=True,
    )
    day_cache: dict[tuple[str, str], tuple[list[dict[str, str]], list[dict[str, Any] | None]]] = {}
    traj_frames = 0
    for i, d in enumerate(sorted(by_date), start=1):
        sids = by_date[d]
        frames, trajectories, _ = build_dual_wma_intraday_trajectories(
            conn,
            dates=[d],
            stock_ids=sids,
            poll_minutes=poll_5m,
            micro_lengths=(WMA_MICRO_LENGTH,),
            kbar_bar_minutes=5,
        )
        traj_frames += len(frames)
        maps = _build_stock_frame_maps(trajectories, frames)
        for sid in sids:
            pts = maps.get(sid)
            if pts:
                day_cache[(d, sid)] = (frames, pts)
        if i % 20 == 0 or i == len(by_date):
            print(f"  entry-day batch {i}/{len(by_date)} · frames={traj_frames}", flush=True)
    traj_meta = {"n_entry_days": len(by_date), "n_frames": traj_frames}

    legs: list[dict[str, Any]] = []
    missing = 0
    for p in periods:
        sid = str(p["stock_id"])
        d = str(p["entry_date"])
        cached = day_cache.get((d, sid))
        if not cached:
            missing += 1
            continue
        frames, points = cached
        entry_i = _frame_idx_for_minute(
            frames, d, p.get("entry_minute") or p.get("entry_poll_minute")
        )
        entry_pt = points[entry_i] if entry_i < len(points) else None
        if not entry_pt:
            missing += 1
            continue
        prev_i = _prev_same_day_idx(frames, entry_i)
        prev_pt = points[prev_i] if prev_i is not None and prev_i < len(points) else None
        mom = _point_momentum(entry_pt, prev_pt)
        ret = p.get("return_pct")
        if ret is None:
            continue
        legs.append(
            {
                "leg_id": f"{p['entry_date']}|{sid}|{p.get('slot', 0)}",
                "stock_id": sid,
                "stock_name": p.get("stock_name") or "",
                "entry_date": str(p["entry_date"]),
                "entry_poll_minute": p.get("entry_minute") or p.get("entry_poll_minute"),
                "exit_date": p.get("exit_date"),
                "return_pct": float(ret),
                "excess_pct": p.get("excess_pct"),
                "bench_return_pct": p.get("bench_return_pct"),
                "entry_px": p.get("entry_px"),
                "exit_px": p.get("exit_px"),
                "exit_reason": p.get("exit_reason"),
                **mom,
            }
        )
    meta = {
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "confirm_bars": confirm_bars,
        "poll_interval_min": 5,
        "sim_summary": sim_summary,
        "trajectory_meta": traj_meta,
        "n_periods": len(periods),
        "n_legs_annotated": len(legs),
        "n_missing_kinematic": missing,
    }
    return legs, meta, day_cache


def run_entry_momentum_gate_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-02",
    date_end: str | None = None,
    confirm_bars: int = 2,
    is_end: str = "2025-12-31",
) -> dict[str, Any]:
    legs, meta, _day_cache = collect_c18acc_entry_momentum_legs(
        conn,
        date_start=date_start,
        date_end=date_end,
        confirm_bars=confirm_bars,
    )
    baseline = _cohort_summary(legs)
    gates: list[dict[str, Any]] = []
    for gid, label, pred in _gate_specs():
        sub = [r for r in legs if pred(r)]
        stats = _cohort_summary(sub)
        base_ex = baseline.get("mean_excess_pct")
        gate_ex = stats.get("mean_excess_pct")
        lift = (
            round(float(gate_ex) - float(base_ex), 4)
            if gate_ex is not None and base_ex is not None
            else None
        )
        gates.append(
            {
                "gate_id": gid,
                "label": label,
                "share_pct": round(100.0 * stats["n"] / len(legs), 1) if legs else 0.0,
                "delta_vs_baseline_pp": lift,
                **stats,
            }
        )

    oos_start = next((r["entry_date"] for r in legs if r["entry_date"] > is_end), None)
    slices: dict[str, Any] = {}
    for tag, filt in (("full", lambda r: True), ("is", lambda r: r["entry_date"] <= is_end)):
        subset = [r for r in legs if filt(r)]
        if not subset:
            continue
        b = _cohort_summary(subset)
        row = next(g for g in gates if g["gate_id"] == "combo_mv_high_rv_up")
        passed = [r for r in subset if bool(r.get("w3_momentum_ok"))]
        slices[tag] = {
            "baseline": b,
            "combo_mv_high_rv_up": _cohort_summary(passed),
            "falling_both": _cohort_summary([r for r in subset if r.get("w3_both_falling_poll")]),
        }
    if oos_start:
        oos = [r for r in legs if r["entry_date"] >= oos_start]
        slices["oos"] = {
            "baseline": _cohort_summary(oos),
            "combo_mv_high_rv_up": _cohort_summary([r for r in oos if r.get("w3_momentum_ok")]),
            "falling_both": _cohort_summary([r for r in oos if r.get("w3_both_falling_poll")]),
        }

    combo = next(g for g in gates if g["gate_id"] == "combo_mv_high_rv_up")
    falling = next(g for g in gates if g["gate_id"] == "falling_both")
    verdict = {
        "user_hypothesis_supported": bool(
            combo.get("delta_vs_baseline_pp") is not None
            and float(combo.get("delta_vs_baseline_pp") or 0) > 0
            and (falling.get("delta_vs_baseline_pp") or 0) < 0
        ),
        "summary": _verdict_text(combo, falling, baseline, legs),
        "recommend_order_gate": False,
    }
    return {
        "schema": "c18acc_entry_momentum_gate-v1",
        **meta,
        "baseline": baseline,
        "gates": gates,
        "slices": slices,
        "verdict": verdict,
        "sample_legs": sorted(legs, key=lambda r: float(r["return_pct"]), reverse=True)[:10],
    }


def _verdict_text(
    combo: dict[str, Any],
    falling: dict[str, Any],
    baseline: dict[str, Any],
    legs: list[dict[str, Any]],
) -> str:
    n_prev = sum(1 for r in legs if r.get("has_prev_poll"))
    n_fall = int(falling.get("n") or 0)
    return (
        f"baseline n={baseline.get('n')} mean_excess={baseline.get('mean_excess_pct')}% · "
        f"MV≥100∧RV↑ n={combo.get('n')} ({combo.get('share_pct')}%) "
        f"mean_excess={combo.get('mean_excess_pct')}% Δ{combo.get('delta_vs_baseline_pp')}pp · "
        f"雙向未升 n={n_fall} mean_excess={falling.get('mean_excess_pct')}% "
        f"Δ{falling.get('delta_vs_baseline_pp')}pp · "
        f"有上一格樣本 {n_prev}/{len(legs)}"
    )


def render_entry_momentum_gate_md(payload: dict[str, Any]) -> str:
    base = payload.get("baseline") or {}
    lines = [
        "# C18acc · 進場當下 W3 MV / RV 動量 gate 研究",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"champion POOL1 · confirm={payload.get('confirm_bars')} · "
        f"**5m poll** · n={base.get('n')} legs",
        "",
        f"研究問題：下單當下 **W3 MV 偏高** 且 **RV3 較上一格往右（上升）**，"
        f"是否優於全樣本？是否應避開「雙向皆未上升」？",
        "",
        "## 全樣本 baseline",
        "",
        f"- n={base.get('n')} · mean_excess **{base.get('mean_excess_pct')}%** · "
        f"mean_return {base.get('mean_return_pct')}% · win_rate {base.get('win_rate_pct')}%",
        "",
        "## Gate 對照（下單當下 · poll-to-poll）",
        "",
        "| gate | n | share% | mean_excess% | mean_return% | win% | Δ vs baseline |",
        "|------|--:|-------:|-------------:|-------------:|-----:|--------------:|",
    ]
    for g in payload.get("gates") or []:
        if g.get("gate_id") == "baseline":
            continue
        lines.append(
            f"| {g.get('label')} | {g.get('n')} | {g.get('share_pct')} | "
            f"{g.get('mean_excess_pct')} | {g.get('mean_return_pct')} | {g.get('win_rate_pct')} | "
            f"{g.get('delta_vs_baseline_pp')}pp |"
        )
    lines.extend(["", "## 分段（combo · MV≥100 ∧ RV↑）", ""])
    for tag in ("is", "oos", "full"):
        sl = (payload.get("slices") or {}).get(tag)
        if not sl:
            continue
        b = sl.get("baseline") or {}
        c = sl.get("combo_mv_high_rv_up") or {}
        f = sl.get("falling_both") or {}
        lines.append(f"### {tag.upper()}")
        lines.append("")
        lines.append(
            f"- baseline: n={b.get('n')} · mean_excess {b.get('mean_excess_pct')}%"
        )
        lines.append(
            f"- MV≥100∧RV↑: n={c.get('n')} · mean_excess {c.get('mean_excess_pct')}%"
        )
        lines.append(
            f"- 雙向未升: n={f.get('n')} · mean_excess {f.get('mean_excess_pct')}%"
        )
        lines.append("")

    v = payload.get("verdict") or {}
    lines.extend(
        [
            "## 結論",
            "",
            f"- {v.get('summary', '')}",
            f"- 假說方向支持：**{'是' if v.get('user_hypothesis_supported') else '否／不明顯'}**",
            f"- 建議接入 Order gate：**{'是' if v.get('recommend_order_gate') else '否（先觀察）'}**",
            "",
            "模組：`src/research/backtest/c18acc_entry_momentum_gate_study.py`",
        ]
    )
    return "\n".join(lines) + "\n"
