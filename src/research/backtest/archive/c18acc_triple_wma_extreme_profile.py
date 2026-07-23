"""C18acc legs · triple WMA(20/5/3) at entry vs hold peak/trough · decile profile."""

from __future__ import annotations

import sqlite3
import statistics
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    _collect_annot_dates,
    _entry_frame_idx,
    _exit_frame_idx_for_date,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps, _price_at
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c
from research.backtest.w3_rv_hl_winner_profile import (
    _mean,
    _median,
    _quad_rates,
    extract_entry_features,
)
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)

ENTRY_NUMERIC = (
    "w20_rv",
    "w20_mv",
    "w5_rv",
    "w5_mv",
    "w3_rv",
    "w3_mv",
    "w3_dip_depth",
    "w5_w3_rv_spread",
    "w20_w3_rv_spread",
    "w5_w20_rv_spread",
    "spread_dist",
)


def _frame_idx_for_minute(
    frames: list[dict[str, str]],
    trade_date: str,
    minute: str | None,
) -> int:
    if minute:
        for i, f in enumerate(frames):
            if f["date"] == trade_date and f["minute"] == minute:
                return i
        for i, f in enumerate(frames):
            if f["date"] == trade_date and f["minute"] >= minute:
                return i
    return _entry_frame_idx(frames, trade_date)


def _snapshot_from_point(point: dict[str, Any] | None, *, prefix: str) -> dict[str, Any]:
    if not point:
        return {}
    feats = extract_entry_features(point)
    out: dict[str, Any] = {}
    for k, v in feats.items():
        if k.startswith(("w20_", "w5_", "w3_", "w2_", "spread_", "trade_signal", "entry_")):
            out[f"{prefix}_{k}" if not k.startswith(prefix) else k] = v
    # normalize keys with explicit prefix
    renamed: dict[str, Any] = {}
    for k, v in feats.items():
        if k in ("entry_minute", "entry_session"):
            renamed[f"{prefix}_{k}"] = v
        elif k.startswith(("w20_", "w5_", "w3_", "spread_", "trade_signal", "hook_")):
            renamed[f"{prefix}_{k}"] = v
        elif k == "w20_w5_combo":
            renamed[f"{prefix}_w20_w5_combo"] = v
    return renamed


def _scan_hold_extremes(
    conn: sqlite3.Connection,
    bench: pd.Series,
    *,
    stock_id: str,
    entry_i: int,
    exit_i: int,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    entry_px: float | None,
) -> dict[str, Any]:
    peak_px = None
    trough_px = None
    peak_i = entry_i
    trough_i = entry_i
    for j in range(entry_i, min(exit_i + 1, len(frames))):
        px, _ = _price_at(conn, bench, stock_id, frames[j]["date"], frames[j]["minute"])
        if px is None or px <= 0:
            continue
        if peak_px is None or px > peak_px:
            peak_px = px
            peak_i = j
        if trough_px is None or px < trough_px:
            trough_px = px
            trough_i = j
    base = entry_px if entry_px and entry_px > 0 else None
    peak_ret = round((peak_px / base - 1) * 100, 3) if base and peak_px else None
    trough_ret = round((trough_px / base - 1) * 100, 3) if base and trough_px else None
    peak_pt = points_by_idx[peak_i] if peak_i < len(points_by_idx) else None
    trough_pt = points_by_idx[trough_i] if trough_i < len(points_by_idx) else None
    pf = frames[peak_i]
    tf = frames[trough_i]
    out: dict[str, Any] = {
        "peak_date": pf["date"],
        "peak_minute": pf["minute"],
        "peak_px": round(float(peak_px), 4) if peak_px else None,
        "peak_ret_from_entry_pct": peak_ret,
        "trough_date": tf["date"],
        "trough_minute": tf["minute"],
        "trough_px": round(float(trough_px), 4) if trough_px else None,
        "trough_ret_from_entry_pct": trough_ret,
    }
    out.update(_snapshot_from_point(peak_pt, prefix="peak"))
    out.update(_snapshot_from_point(trough_pt, prefix="trough"))
    return out


def annotate_leg_triple_wma(
    period: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    bench: pd.Series,
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    sid = str(period["stock_id"])
    entry = str(period["entry_date"])
    exit_d = str(period.get("exit_date") or "")
    if not exit_d:
        return None
    points_by_idx = stock_maps.get(sid)
    if not points_by_idx:
        return None
    entry_i = _frame_idx_for_minute(frames, entry, period.get("entry_minute"))
    exit_i = _exit_frame_idx_for_date(frames, exit_d)
    if exit_i < entry_i:
        return None
    entry_pt = points_by_idx[entry_i]
    if not entry_pt:
        return None
    entry_feats = extract_entry_features(entry_pt)
    extremes = _scan_hold_extremes(
        conn,
        bench,
        stock_id=sid,
        entry_i=entry_i,
        exit_i=exit_i,
        frames=frames,
        points_by_idx=points_by_idx,
        entry_px=period.get("entry_px"),
    )
    ret = period.get("return_pct")
    if ret is None:
        return None
    return {
        "leg_id": f"{entry}|{sid}|{period.get('slot', 0)}",
        "stock_id": sid,
        "stock_name": period.get("stock_name") or "",
        "entry_date": entry,
        "exit_date": exit_d,
        "entry_minute": period.get("entry_minute"),
        "hold_days": period.get("hold_days"),
        "return_pct": float(ret),
        "excess_pct": period.get("excess_pct"),
        "exit_reason": period.get("exit_reason"),
        **entry_feats,
        **extremes,
    }


def _decile_slice(rows: list[dict[str, Any]], *, top: bool, pct: float = 0.10) -> list[dict[str, Any]]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: float(r["return_pct"]), reverse=True)
    k = max(1, round(len(ordered) * pct))
    return ordered[:k] if top else ordered[-k:]


def _profile_decile(
    rows: list[dict[str, Any]],
    *,
    snapshot_prefix: str = "",
) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    ret_field = "return_pct"
    numeric = list(ENTRY_NUMERIC)
    if snapshot_prefix:
        numeric = [f"{snapshot_prefix}_{f}" for f in ENTRY_NUMERIC if f != "spread_dist"]
        numeric.append(f"{snapshot_prefix}_spread_dist")
    stats: list[dict[str, Any]] = []
    for field in numeric:
        vals = [float(r[field]) for r in rows if r.get(field) is not None]
        if len(vals) < 3:
            continue
        stats.append(
            {
                "field": field,
                "mean": _mean(vals),
                "median": _median(vals),
                "p25": round(statistics.quantiles(vals, n=4)[0], 3) if len(vals) >= 4 else None,
                "p75": round(statistics.quantiles(vals, n=4)[2], 3) if len(vals) >= 4 else None,
            }
        )
    leg_fields = ("w20", "w5", "w3")
    quad: dict[str, Any] = {}
    for leg in leg_fields:
        key = f"{snapshot_prefix}_{leg}_quadrant" if snapshot_prefix else f"{leg}_quadrant"
        quad[leg] = _quad_rates(
            [{f"{leg}_quadrant": r.get(key)} for r in rows],
            leg,
        )
    spread_key = f"{snapshot_prefix}_spread_class" if snapshot_prefix else "spread_class"
    spread = _categorical_rates_simple(rows, spread_key)
    combo_key = (
        f"{snapshot_prefix}_w20_w5_combo" if snapshot_prefix else "w20_w5_combo"
    )
    return {
        "n": len(rows),
        "mean_return_pct": _mean([float(r[ret_field]) for r in rows]),
        "median_return_pct": _median([float(r[ret_field]) for r in rows]),
        "numeric_stats": stats,
        "quadrant_pct": quad,
        "spread_class_pct": spread,
        "w20_w5_combo_pct": _categorical_rates_simple(rows, combo_key),
        "trade_signal_pct": _categorical_rates_simple(
            rows,
            f"{snapshot_prefix}_trade_signal" if snapshot_prefix else "trade_signal",
        ),
    }


def _categorical_rates_simple(rows: list[dict[str, Any]], field: str) -> dict[str, float]:
    n = len(rows)
    if n == 0:
        return {}
    counts: dict[str, int] = {}
    for r in rows:
        key = str(r.get(field) or "none")
        counts[key] = counts.get(key, 0) + 1
    return {k: round(c / n * 100, 1) for k, c in sorted(counts.items())}


def _compare_entry_vs_extreme(
    rows: list[dict[str, Any]],
    *,
    extreme_prefix: str,
) -> dict[str, Any]:
    """Entry vs peak/trough quadrant shift rates for winners/losers."""
    shifts: dict[str, int] = {}
    w20_same = w5_same = w3_same = 0
    leg_n = len(rows)
    for r in rows:
        for leg in ("w20", "w5", "w3"):
            eq = r.get(f"{leg}_quadrant")
            xq = r.get(f"{extreme_prefix}_{leg}_quadrant")
            if eq is None or xq is None:
                continue
            key = f"{leg}:{eq}->{xq}"
            shifts[key] = shifts.get(key, 0) + 1
            if eq == xq:
                if leg == "w20":
                    w20_same += 1
                elif leg == "w5":
                    w5_same += 1
                elif leg == "w3":
                    w3_same += 1
    return {
        "n_legs": leg_n,
        "w20_unchanged_pct": round(100 * w20_same / leg_n, 1) if leg_n else 0,
        "w5_unchanged_pct": round(100 * w5_same / leg_n, 1) if leg_n else 0,
        "w3_unchanged_pct": round(100 * w3_same / leg_n, 1) if leg_n else 0,
        "top_shifts": sorted(shifts.items(), key=lambda x: -x[1])[:12],
    }


def run_c18acc_triple_wma_extreme_profile(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
    decile_pct: float = 0.10,
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
    if not annot_dates:
        raise ValueError("no hold dates for triple-WMA annotation")

    print(
        f"triple-WMA extreme: {len(periods)} legs · {len(stock_ids)} stocks · "
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

    legs: list[dict[str, Any]] = []
    for p in periods:
        row = annotate_leg_triple_wma(
            p,
            conn=conn,
            bench=bench,
            frames=frames,
            stock_maps=stock_maps,
        )
        if row:
            legs.append(row)

    top_win = _decile_slice(legs, top=True, pct=decile_pct)
    bot_loss = _decile_slice(legs, top=False, pct=decile_pct)

    return {
        "schema": "c18acc_triple_wma_extreme_profile-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_slots": n_slots,
        "decile_pct": decile_pct,
        "sim_summary": sim_summary,
        "trajectory_meta": traj_meta,
        "n_legs": len(legs),
        "n_stocks": len(stock_ids),
        "winners_top_decile": {
            "entry": _profile_decile(top_win, snapshot_prefix=""),
            "peak": _profile_decile(top_win, snapshot_prefix="peak"),
            "entry_vs_peak": _compare_entry_vs_extreme(top_win, extreme_prefix="peak"),
            "sample": top_win,
        },
        "losers_bottom_decile": {
            "entry": _profile_decile(bot_loss, snapshot_prefix=""),
            "trough": _profile_decile(bot_loss, snapshot_prefix="trough"),
            "entry_vs_trough": _compare_entry_vs_extreme(bot_loss, extreme_prefix="trough"),
            "sample": bot_loss,
        },
        "all_legs": legs,
    }


def render_c18acc_triple_wma_extreme_profile_md(payload: dict[str, Any]) -> str:
    w = payload["winners_top_decile"]
    l = payload["losers_bottom_decile"]
    we, wp = w["entry"], w["peak"]
    le, lt = l["entry"], l["trough"]
    pct = int(float(payload["decile_pct"]) * 100)

    def _quad_lines(profile: dict[str, Any], title: str) -> list[str]:
        lines = [f"### {title}", ""]
        qd = profile.get("quadrant_pct") or {}
        for leg in ("w20", "w5", "w3"):
            dist = qd.get(leg) or {}
            if not dist:
                continue
            parts = ", ".join(f"{q} {p}%" for q, p in dist.items())
            lines.append(f"- **{leg.upper()}**：{parts}")
        sc = profile.get("spread_class_pct") or {}
        if sc:
            lines.append(
                "- **spread**："
                + ", ".join(f"{k} {v}%" for k, v in sc.items())
            )
        combo = profile.get("w20_w5_combo_pct") or {}
        if combo:
            top3 = sorted(combo.items(), key=lambda x: -x[1])[:5]
            lines.append(
                "- **W20×W5 combo**："
                + ", ".join(f"{k} {v}%" for k, v in top3)
            )
        lines.append("")
        return lines

    def _num_table(profile: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
        stats = {s["field"]: s for s in profile.get("numeric_stats") or []}
        lines = [
            "| 指標 | 均值 | 中位數 |",
            "|------|------|--------|",
        ]
        for f in fields:
            s = stats.get(f)
            if not s:
                continue
            lines.append(f"| {f} | {s.get('mean')} | {s.get('median')} |")
        lines.append("")
        return lines

    lines = [
        "# C18acc · WMA20/5/3 盈虧極值剖面",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽 · **{payload['n_legs']}** legs · 前後 **{pct}%** 分位",
        "",
        f"盤中 30m poll · triple WMA（20/5/3）· {payload['trajectory_meta'].get('n_frames', '—')} 幀",
        "",
        "## 賺錢前 10%（依實際 leg return）",
        "",
        f"- n = **{we.get('n')}** · mean return **{we.get('mean_return_pct')}%** · "
        f"median **{we.get('median_return_pct')}%**",
        "",
        "#### 買入時 WMA 位置",
        "",
    ]
    lines.extend(_quad_lines(we, ""))
    lines.extend(_num_table(we, ENTRY_NUMERIC))
    lines.append("#### 持有期間最高點 WMA 位置")
    lines.append("")
    lines.extend(_quad_lines(wp, ""))
    lines.extend(_num_table(wp, tuple(f"peak_{f}" for f in ENTRY_NUMERIC)))
    evp = w.get("entry_vs_peak") or {}
    lines.extend(
        [
            "#### 買入 → 最高點 象限變化",
            "",
            f"- W20 不變：**{evp.get('w20_unchanged_pct')}%** · "
            f"W5：**{evp.get('w5_unchanged_pct')}%** · "
            f"W3：**{evp.get('w3_unchanged_pct')}%**",
            "",
        ]
    )
    for shift, cnt in evp.get("top_shifts") or []:
        lines.append(f"  - {shift}: {cnt}")
    lines.append("")

    lines.extend(
        [
            "## 賠錢後 10%（依實際 leg return）",
            "",
            f"- n = **{le.get('n')}** · mean return **{le.get('mean_return_pct')}%** · "
            f"median **{le.get('median_return_pct')}%**",
            "",
            "#### 買入時 WMA 位置",
            "",
        ]
    )
    lines.extend(_quad_lines(le, ""))
    lines.extend(_num_table(le, ENTRY_NUMERIC))
    lines.append("#### 持有期間最低點 WMA 位置")
    lines.append("")
    lines.extend(_quad_lines(lt, ""))
    lines.extend(_num_table(lt, tuple(f"trough_{f}" for f in ENTRY_NUMERIC)))
    evt = l.get("entry_vs_trough") or {}
    lines.extend(
        [
            "#### 買入 → 最低點 象限變化",
            "",
            f"- W20 不變：**{evt.get('w20_unchanged_pct')}%** · "
            f"W5：**{evt.get('w5_unchanged_pct')}%** · "
            f"W3：**{evt.get('w3_unchanged_pct')}%**",
            "",
        ]
    )
    for shift, cnt in evt.get("top_shifts") or []:
        lines.append(f"  - {shift}: {cnt}")
    lines.append("")

    lines.extend(
        [
            "## 操作啟示（數據歸納 · 非因果）",
            "",
            _actionable_summary(w, l),
        ]
    )
    return "\n".join(lines)


def _actionable_summary(win: dict[str, Any], loss: dict[str, Any]) -> str:
    we = win["entry"]
    wp = win["peak"]
    le = loss["entry"]
    lt = loss["trough"]

    def _dominant(profile: dict[str, Any], leg: str) -> str | None:
        dist = (profile.get("quadrant_pct") or {}).get(leg) or {}
        if not dist:
            return None
        return max(dist.items(), key=lambda x: x[1])[0]

    bullets: list[str] = []
    for label, prof in (("贏家買入", we), ("贏家高點", wp), ("輸家買入", le), ("輸家低點", lt)):
        w20 = _dominant(prof, "w20")
        w5 = _dominant(prof, "w5")
        w3 = _dominant(prof, "w3")
        if w20 and w5 and w3:
            bullets.append(f"- **{label}** 主導象限：W20={w20} · W5={w5} · W3={w3}")
    return "\n".join(bullets) if bullets else "（樣本不足）"
