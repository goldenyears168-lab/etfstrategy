"""GeoAlpha Phase 2 · M0–M5 ablation · joint vs single-track entry models."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from report_paths import RESEARCH_RRG
from research.backtest.dual_wma_signal_backtest import (
    ExitRule,
    W20LeadingExitParams,
    _build_stock_frame_maps,
    _exit_frame_idx_w20_leading,
    _summarize_trades,
    _trade_return,
    imp_early_long_champion_entry,
    run_w20_leading_exit_grid,
)
from research.backtest.archive.dual_wma_tactical_slot_backtest import (
    HookEntryParams,
    TacticalExitParams,
    _matches_hook_buy,
    _w20_exit_from_tactical,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.archive.geoalpha_panel import (
    classify_geo_phase,
    compute_s_alpha,
    compute_s_joint,
)
from research.backtest.archive.rrg_drs5d_score_backtest import build_drs5d_panel
from research.backtest.archive.rrg_hybrid_drs5d_ret5d_backtest import (
    attach_daily_dual_wma_spread,
    attach_pit_fundamentals,
    compute_hybrid_structural_score,
)
from rrg_universe_intraday_panel import build_dual_wma_intraday_trajectories

ModelId = Literal["M0", "M1", "M2", "M3", "M4", "M5"]
ALL_MODELS: tuple[ModelId, ...] = ("M0", "M1", "M2", "M3", "M4", "M5")

DEFAULT_STRUCT_PERCENTILE = 70
MIN_OOS_EVENTS = 5
_RANDOM_SEED = 42

IS_DATE_FROM = "2026-04-01"
IS_DATE_TO = "2026-07-03"
OOS_DATE_FROM = "2024-01-01"
OOS_DATE_TO = "2026-03-31"


def geo_hook_champion_entry() -> HookEntryParams:
    """Champion hook filters · improving tier · HOOK_IMP geometry."""
    return HookEntryParams(
        pool_tier="improving",
        w20_rs_min=96.0,
        w20_rs_max=100.0,
        hook_retrace_pct_min=0.15,
        spread_dist_min=1.5,
        first_per_day=True,
    )


def _struct_gate_label(struct_percentile: int) -> str:
    if struct_percentile <= 0:
        return "none"
    return f"P{struct_percentile}"


def _build_daily_struct_lookup(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    struct_percentile: int = DEFAULT_STRUCT_PERCENTILE,
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], dict[str, Any]], dict[str, float]]:
    """Per (sid, date) S_struct + daily row; per-date P* threshold (omega pool)."""
    panel, _ = build_drs5d_panel(conn)
    panel = attach_daily_dual_wma_spread(panel, conn)
    panel = attach_pit_fundamentals(panel, conn)
    date_set = set(dates)
    all_sub = panel[panel["date"].astype(str).isin(date_set)]
    pool_sub = all_sub[all_sub["in_pool_omega_kinematic"]]

    struct_by_key: dict[tuple[str, str], float] = {}
    row_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in all_sub.iterrows():
        key = (str(row["sid"]), str(row["date"]))
        struct_by_key[key] = compute_hybrid_structural_score(row)
        row_by_key[key] = row.to_dict()

    threshold_by_date: dict[str, float] = {}
    for d in dates:
        pool_day = pool_sub[pool_sub["date"].astype(str) == d]
        vals = [
            compute_hybrid_structural_score(row)
            for _, row in pool_day.iterrows()
        ]
        vals = [v for v in vals if np.isfinite(v)]
        if struct_percentile <= 0:
            threshold_by_date[d] = float("-inf")
        elif vals:
            threshold_by_date[d] = float(np.percentile(vals, struct_percentile))
        else:
            threshold_by_date[d] = float("nan")

    return struct_by_key, row_by_key, threshold_by_date


def _passes_struct_gate(
    sid: str,
    d: str,
    struct_by_key: dict[tuple[str, str], float],
    threshold_by_date: dict[str, float],
    *,
    struct_percentile: int = DEFAULT_STRUCT_PERCENTILE,
) -> bool:
    if struct_percentile <= 0:
        return True
    s = struct_by_key.get((sid, d))
    thr = threshold_by_date.get(d, float("nan"))
    if s is None or not np.isfinite(s) or not np.isfinite(thr):
        return False
    return float(s) >= thr


def _score_point(
    p: dict[str, Any],
    row_daily: dict[str, Any],
    s_struct: float,
) -> tuple[str, float, float]:
    phi = classify_geo_phase(p)
    s_alpha = compute_s_alpha(p, row_daily)
    s_joint = compute_s_joint(s_struct, phi, s_alpha) if np.isfinite(s_struct) else float("nan")
    return phi, s_alpha, s_joint


def _collect_geo_candidates(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    model: ModelId,
    *,
    hook_ep: HookEntryParams,
    struct_by_key: dict[tuple[str, str], float],
    row_by_key: dict[tuple[str, str], dict[str, Any]],
    threshold_by_date: dict[str, float],
    rng: random.Random,
    struct_percentile: int = DEFAULT_STRUCT_PERCENTILE,
) -> list[tuple[str, int]]:
    """Collect entry events per ablation model."""
    frame_ids = [f["id"] for f in frames]
    by_stock_day: dict[tuple[str, str], list[tuple[int, str, float, float]]] = {}

    target_phi: str | None = None
    if model in ("M1", "M2", "M4"):
        target_phi = "HOOK_IMP"

    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if not p or not p.get("fresh"):
                continue
            d = frames[i]["date"]
            key = (sid, d)
            row_daily = row_by_key.get(key, {})
            s_struct = struct_by_key.get(key, float("nan"))

            if model == "M3":
                if not _passes_struct_gate(
                    sid, d, struct_by_key, threshold_by_date, struct_percentile=struct_percentile
                ):
                    continue
                by_stock_day.setdefault(key, []).append((i, "ANY", 0.0, 0.0))
                continue

            if model == "M5":
                if not _passes_struct_gate(
                    sid, d, struct_by_key, threshold_by_date, struct_percentile=struct_percentile
                ):
                    continue
                w20_rs = float((p.get("w20") or {}).get("rs_ratio") or 0)
                if w20_rs < hook_ep.w20_rs_min or w20_rs > hook_ep.w20_rs_max:
                    continue
                phi, s_alpha, s_joint = _score_point(p, row_daily, s_struct)
                if phi != "EXT_OPEN":
                    continue
                by_stock_day.setdefault(key, []).append((i, phi, s_alpha, s_joint))
                continue

            if model in ("M1", "M2", "M4"):
                if not _matches_hook_buy(p, hook_ep):
                    continue
                phi, s_alpha, s_joint = _score_point(p, row_daily, s_struct)
                if target_phi and phi != target_phi:
                    continue
                if model == "M4" and not _passes_struct_gate(
                    sid,
                    d,
                    struct_by_key,
                    threshold_by_date,
                    struct_percentile=struct_percentile,
                ):
                    continue
                by_stock_day.setdefault(key, []).append((i, phi, s_alpha, s_joint))

    events: list[tuple[str, int]] = []
    for (sid, d), cands in by_stock_day.items():
        if not cands:
            continue
        if model == "M2":
            events.append((sid, min(cands, key=lambda x: x[0])[0]))
        elif model == "M1":
            events.append((sid, max(cands, key=lambda x: x[2])[0]))
        elif model in ("M4", "M5"):
            events.append((sid, max(cands, key=lambda x: x[3])[0]))
        elif model == "M3":
            pick = rng.choice(cands)
            events.append((sid, pick[0]))

    return events


def _run_geo_model_backtest(
    conn: sqlite3.Connection,
    model: ModelId,
    *,
    cache: dict[str, Any],
    struct_by_key: dict[tuple[str, str], float],
    row_by_key: dict[tuple[str, str], dict[str, Any]],
    threshold_by_date: dict[str, float],
    exit_params: TacticalExitParams,
    rng: random.Random,
    struct_percentile: int = DEFAULT_STRUCT_PERCENTILE,
) -> dict[str, Any]:
    frames = cache["frames"]
    trajectories = cache["trajectories"]
    bench = cache["bench"]
    stock_maps = cache["stock_maps"]
    w20_exit = _w20_exit_from_tactical(exit_params)
    hook_ep = geo_hook_champion_entry()

    if model == "M0":
        imp = run_w20_leading_exit_grid(
            conn,
            dates=cache["dates"],
            entry_params=imp_early_long_champion_entry(),
            exit_params=W20LeadingExitParams(cap_days=exit_params.cap_days),
            etf_codes=cache["etf_codes"],
            _cache=cache,
        )
        return {
            "model": model,
            "description": "imp_early_long baseline · champion entry · tactical exit",
            "summary": {**imp, "entry_signal": "imp_early_long", "model": model},
            "trades": [],
            "events": imp.get("n", 0),
        }

    events = _collect_geo_candidates(
        trajectories,
        frames,
        model,
        hook_ep=hook_ep,
        struct_by_key=struct_by_key,
        row_by_key=row_by_key,
        threshold_by_date=threshold_by_date,
        rng=rng,
        struct_percentile=struct_percentile,
    )

    trades: list[dict[str, Any]] = []
    for sid, ei in events:
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx_w20_leading(ei, frames, pts, w20_exit)
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if tr:
            tr["model"] = model
            trades.append(tr)

    summary = _summarize_trades(
        trades,
        imp_early_long_champion_entry(),
        ExitRule("exit_w20_leading", w20_exit.label()),
    )
    gate_lbl = _struct_gate_label(struct_percentile)
    desc_map = {
        "M1": "S_alpha only · HOOK_IMP · no struct gate · rank s_alpha",
        "M2": "φ=HOOK_IMP only · first poll · no s_alpha rank",
        "M3": f"S_struct {gate_lbl}+ · random intraday poll · no geo gate",
        "M4": f"S_joint · struct {gate_lbl}+ · HOOK_IMP · rank s_joint",
        "M5": f"S_joint · struct {gate_lbl}+ · EXT_OPEN · rank s_joint",
    }
    summary.update(
        {
            "model": model,
            "description": desc_map.get(model, ""),
            "exit_cap_days": exit_params.cap_days,
        }
    )
    return {"model": model, "description": desc_map.get(model, ""), "summary": summary, "trades": trades, "events": len(events)}


def run_geoalpha_ablation(
    conn: sqlite3.Connection,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    models: tuple[ModelId, ...] = ALL_MODELS,
    *,
    exit_params: TacticalExitParams | None = None,
    struct_percentile: int = DEFAULT_STRUCT_PERCENTILE,
    _cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run M0–M5 ablation · shared trajectory cache · tactical exit."""
    xp = exit_params or TacticalExitParams(cap_days=3)
    gate_lbl = _struct_gate_label(struct_percentile)
    print(
        f"GeoAlpha ablation · {dates[0]} → {dates[-1]} · "
        f"models={','.join(models)} · struct={gate_lbl}",
        flush=True,
    )

    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
        meta = _cache.get("panel_meta") or {}
    else:
        print("  build intraday trajectories ...", flush=True)
        frames, trajectories, meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    print("  build daily S_struct lookup ...", flush=True)
    struct_by_key, row_by_key, threshold_by_date = _build_daily_struct_lookup(
        conn, dates, struct_percentile=struct_percentile
    )

    cache: dict[str, Any] = {
        "frames": frames,
        "trajectories": trajectories,
        "bench": bench,
        "stock_maps": stock_maps,
        "dates": dates,
        "etf_codes": etf_codes,
        "panel_meta": meta,
    }
    rng = random.Random(_RANDOM_SEED)

    results: list[dict[str, Any]] = []
    for model in models:
        print(f"  model {model} ...", flush=True)
        results.append(
            _run_geo_model_backtest(
                conn,
                model,
                cache=cache,
                struct_by_key=struct_by_key,
                row_by_key=row_by_key,
                threshold_by_date=threshold_by_date,
                exit_params=xp,
                rng=rng,
                struct_percentile=struct_percentile,
            )
        )

    m0_excess = next(
        (r["summary"].get("mean_excess_pct") for r in results if r["model"] == "M0"),
        None,
    )
    rows: list[dict[str, Any]] = []
    for r in results:
        s = r["summary"]
        ex = s.get("mean_excess_pct")
        delta = round(float(ex) - float(m0_excess), 3) if ex is not None and m0_excess is not None else None
        rows.append(
            {
                "model": r["model"],
                "description": r.get("description", ""),
                "n": s.get("n", 0),
                "mean_excess_pct": ex,
                "win_rate": s.get("win_rate"),
                "excess_win_rate": s.get("excess_win_rate"),
                "mean_hold_polls": s.get("mean_hold_polls"),
                "vs_m0_pp": delta,
                "events": r.get("events", 0),
            }
        )

    verdict = _ablation_verdict(rows)
    return {
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "exit_cap_days": xp.cap_days,
        "exit_label": _w20_exit_from_tactical(xp).label(),
        "struct_gate": gate_lbl,
        "struct_percentile": struct_percentile,
        "panel_meta": meta,
        "models": results,
        "rows": rows,
        "m0_baseline_excess": m0_excess,
        "verdict": verdict,
    }


def _row_by_model(rows: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    return next((r for r in rows if r["model"] == model), None)


def _ablation_verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    m0 = _row_by_model(rows, "M0")
    m1 = _row_by_model(rows, "M1")
    m2 = _row_by_model(rows, "M2")
    m4 = _row_by_model(rows, "M4")
    m5 = _row_by_model(rows, "M5")

    def _ex(r: dict[str, Any] | None) -> float:
        return float(r.get("mean_excess_pct") or -999.0) if r else -999.0

    def _n(r: dict[str, Any] | None) -> int:
        return int(r.get("n") or 0) if r else 0

    m4_n = _n(m4)
    h_m4_gt_m1 = m4_n > 0 and _ex(m4) > _ex(m1)
    h_m4_gt_m2 = m4_n > 0 and _ex(m4) > _ex(m2)
    h_m5_lt_m4 = m4_n > 0 and _ex(m5) < _ex(m4)
    h_m4_gt_m0 = m4_n > 0 and _ex(m4) > _ex(m0)
    m4_zero_sample = m4_n == 0

    best_hook = m2 if _ex(m2) >= _ex(m1) else m1
    hook_beats_m0 = _ex(best_hook) > _ex(m0)

    if m4_zero_sample and hook_beats_m0:
        recommendation = (
            f"struct gate 與 HOOK_IMP champion 零重疊（M4 n=0）；"
            f"首選 {best_hook['model']}（n={best_hook.get('n')} · 超額 {best_hook.get('mean_excess_pct')}%）"
            f" 取代 imp_early_long · 待 OOS"
        )
    elif h_m4_gt_m1 and h_m4_gt_m2 and h_m5_lt_m4 and h_m4_gt_m0:
        recommendation = "採納 M4（S_joint + HOOK_IMP）為戰術槽 champion 候選"
    elif h_m4_gt_m1 and h_m4_gt_m2:
        recommendation = "M4 勝單軌；樣本/OOS 後再對 imp_early_long 決策"
    elif _ex(m0) >= max(_ex(m1), _ex(m2), _ex(m4)):
        recommendation = "維持 imp_early_long（M0）為戰術槽 champion"
    else:
        recommendation = "結果混合；需 OOS holdout 再驗"

    return {
        "h_m4_gt_m1": h_m4_gt_m1,
        "h_m4_gt_m2": h_m4_gt_m2,
        "h_m5_lt_m4": h_m5_lt_m4,
        "h_m4_gt_m0": h_m4_gt_m0,
        "m4_zero_sample": m4_zero_sample,
        "all_expected": h_m4_gt_m1 and h_m4_gt_m2 and h_m5_lt_m4,
        "recommendation": recommendation,
    }


def render_geoalpha_ablation_md(payload: dict[str, Any]) -> str:
    lines = [
        "# GeoAlpha Phase 2 · M0–M5 Ablation",
        "",
        f"期間：**{payload.get('date_from')} → {payload.get('date_to')}** · "
        f"出場 **{payload.get('exit_label')}** · struct gate **{payload.get('struct_gate')}**",
        "",
        "## 模型定義",
        "",
        "| Model | 進場規則 |",
        "|-------|----------|",
        "| M0 | imp_early_long champion · tactical exit |",
        "| M1 | S_alpha · HOOK_IMP · 無 struct gate · rank s_alpha |",
        "| M2 | φ=HOOK_IMP · first poll · 無 s_alpha rank |",
        "| M3 | S_struct top gate · random poll · 無 geo gate |",
        "| M4 | S_joint · struct gate · HOOK_IMP · rank s_joint |",
        "| M5 | 同 M4 · φ=EXT_OPEN（左弧對照） |",
        "",
        "## 結果",
        "",
        "| Model | n | 超額% | 勝率% | 持有(幀) | vs M0 pp |",
        "|-------|---|-------|-------|---------|----------|",
    ]
    for r in payload.get("rows") or []:
        wr = r.get("win_rate")
        wr_s = f"{wr:.1f}" if wr is not None else "—"
        hold = r.get("mean_hold_polls")
        hold_s = f"{hold:.1f}" if hold is not None else "—"
        vs = r.get("vs_m0_pp")
        vs_s = f"{vs:+.3f}" if vs is not None else "—"
        ex = r.get("mean_excess_pct")
        ex_s = f"{ex:.3f}" if ex is not None else "—"
        lines.append(
            f"| {r['model']} | {r.get('n', 0)} | {ex_s} | {wr_s} | {hold_s} | {vs_s} |"
        )

    v = payload.get("verdict") or {}
    lines.extend(
        [
            "",
            "## 假設檢定",
            "",
            f"- H1 M4 > M1：{'✅' if v.get('h_m4_gt_m1') else '❌'}",
            f"- H2 M4 > M2：{'✅' if v.get('h_m4_gt_m2') else '❌'}",
            f"- H3 M5 < M4：{'✅' if v.get('h_m5_lt_m4') else '❌'}"
            f"{'（M4 n=0 · 無法檢定）' if v.get('m4_zero_sample') else ''}",
            f"- H4 M4 > M0：{'✅' if v.get('h_m4_gt_m0') else '❌'}"
            f"{'（M4 n=0）' if v.get('m4_zero_sample') else ''}",
            f"- 全部預期方向：{'✅' if v.get('all_expected') else '❌'}",
            "",
            "## 建議（Research layer · 戰術槽 champion）",
            "",
            f"- {v.get('recommendation', '—')}",
            "",
            "## 解讀備註",
            "",
            "- M0 為 imp_early_long champion + cap3d W20→Leading（hook sweep 對照規格）。",
            "- M1–M5 共用 champion hook 幾何（improving · W20 96–100 · hook_retrace≥0.15 · spread≥1.5）。",
            f"- struct gate = 當日 omega pool S_struct ≥ {payload.get('struct_gate', 'P70')}。",
            "- M3 隨機 poll seed=42 · 可重現。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_geoalpha_ablation_report(
    payload: dict[str, Any],
    *,
    out_dir: Any | None = None,
) -> tuple[Any, Any]:
    out_dir = out_dir or RESEARCH_RRG
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = out_dir / f"geoalpha_ablation_{stamp}.json"
    md_path = out_dir / f"geoalpha_ablation_{stamp}.md"

    serializable = {
        k: v
        for k, v in payload.items()
        if k not in ("models", "panel_meta")
    }
    serializable["rows"] = payload.get("rows")
    serializable["verdict"] = payload.get("verdict")
    serializable["n_panel_frames"] = (payload.get("panel_meta") or {}).get("n_frames")

    json_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_geoalpha_ablation_md(payload), encoding="utf-8")
    return json_path, md_path


def _check_intraday_coverage(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date)
        FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
        """,
        (date_from, date_to),
    ).fetchone()
    min_d, max_d, n_days = row[0], row[1], int(row[2] or 0)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "min_date": min_d,
        "max_date": max_d,
        "n_distinct_dates": n_days,
        "has_coverage": n_days > 0,
    }


def run_m2_geo_event_backtest(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    exit_params: TacticalExitParams | None = None,
    _cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """M2 champion · φ=HOOK_IMP · first poll · geo hook geometry."""
    xp = exit_params or TacticalExitParams(cap_days=3)
    w20_exit = _w20_exit_from_tactical(xp)
    hook_ep = geo_hook_champion_entry()

    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    struct_by_key, row_by_key, threshold_by_date = _build_daily_struct_lookup(
        conn, dates, struct_percentile=0
    )
    rng = random.Random(_RANDOM_SEED)
    events = _collect_geo_candidates(
        trajectories,
        frames,
        "M2",
        hook_ep=hook_ep,
        struct_by_key=struct_by_key,
        row_by_key=row_by_key,
        threshold_by_date=threshold_by_date,
        rng=rng,
        struct_percentile=0,
    )

    trades: list[dict[str, Any]] = []
    for sid, ei in events:
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx_w20_leading(ei, frames, pts, w20_exit)
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if tr:
            tr["entry_signal"] = "geo_m2_hook_imp"
            trades.append(tr)

    summary = _summarize_trades(
        trades,
        imp_early_long_champion_entry(),
        ExitRule("exit_w20_leading", w20_exit.label()),
    )
    summary.update(
        {
            "entry_signal": "geo_m2_hook_imp",
            "model": "M2",
            "pool_tier": hook_ep.pool_tier,
            "hook_retrace_pct_min": hook_ep.hook_retrace_pct_min,
            "spread_dist_min": hook_ep.spread_dist_min,
            "w20_rs_min": hook_ep.w20_rs_min,
            "w20_rs_max": hook_ep.w20_rs_max,
            "exit_cap_days": xp.cap_days,
        }
    )
    return {"trades": trades, "summary": summary, "events": len(events)}


def _run_m2_window(
    conn: sqlite3.Connection,
    *,
    label: str,
    dates: list[str],
    etf_codes: tuple[str, ...],
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    if not dates:
        return {
            "window": label,
            "date_from": None,
            "date_to": None,
            "n": 0,
            "error": "no trading dates",
        }
    coverage = _check_intraday_coverage(conn, dates[0], dates[-1])
    if not coverage["has_coverage"]:
        return {
            "window": label,
            "date_from": dates[0],
            "date_to": dates[-1],
            "n": 0,
            "error": "no intraday 1m coverage",
            "coverage": coverage,
        }
    try:
        result = run_m2_geo_event_backtest(
            conn, dates=dates, etf_codes=etf_codes, exit_params=exit_params
        )
        s = result["summary"]
        return {
            "window": label,
            "date_from": dates[0],
            "date_to": dates[-1],
            "n": s.get("n", 0),
            "events": result.get("events", 0),
            "mean_excess_pct": s.get("mean_excess_pct"),
            "median_excess_pct": s.get("median_excess_pct"),
            "win_rate": s.get("win_rate"),
            "excess_win_rate": s.get("excess_win_rate"),
            "mean_hold_polls": s.get("mean_hold_polls"),
            "coverage": coverage,
        }
    except Exception as exc:
        return {
            "window": label,
            "date_from": dates[0],
            "date_to": dates[-1],
            "n": 0,
            "error": str(exc),
            "coverage": coverage,
        }


def run_m2_oos_holdout(
    conn: sqlite3.Connection,
    *,
    is_dates: list[str],
    oos_dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    """G2 · M2 HOOK_IMP champion · IS vs OOS holdout."""
    xp = exit_params or TacticalExitParams(cap_days=3)
    is_row = _run_m2_window(
        conn, label="IS", dates=is_dates, etf_codes=etf_codes, exit_params=xp
    )
    oos_row = _run_m2_window(
        conn, label="OOS", dates=oos_dates, etf_codes=etf_codes, exit_params=xp
    )

    g2_notes: list[str] = []
    g2_pass = True
    for row in (oos_row,):
        if row.get("error"):
            g2_pass = False
            g2_notes.append(f"{row.get('window')}: {row.get('error')}")
            continue
        n = int(row.get("n") or 0)
        excess = row.get("mean_excess_pct")
        if n < MIN_OOS_EVENTS:
            g2_pass = False
            g2_notes.append(f"{row.get('window')}: n={n} < {MIN_OOS_EVENTS}")
        elif excess is None or excess <= 0:
            g2_pass = False
            g2_notes.append(f"{row.get('window')}: excess={excess}% ≤ 0")

    is_excess = is_row.get("mean_excess_pct")
    oos_excess = oos_row.get("mean_excess_pct")
    if (
        is_excess is not None
        and oos_excess is not None
        and is_excess > 0
        and oos_excess < is_excess * 0.3
        and int(oos_row.get("n") or 0) >= MIN_OOS_EVENTS
    ):
        g2_notes.append(
            f"OOS excess {oos_excess}% << IS {is_excess}% (decay > 70%)"
        )
        g2_pass = False

    oos_coverage = oos_row.get("coverage") or _check_intraday_coverage(
        conn, oos_dates[0] if oos_dates else OOS_DATE_FROM, oos_dates[-1] if oos_dates else OOS_DATE_TO
    )

    return {
        "focus_signal": "geo_m2_hook_imp",
        "entry": geo_hook_champion_entry(),
        "exit_cap_days": xp.cap_days,
        "is": is_row,
        "oos": oos_row,
        "oos_coverage": oos_coverage,
        "g2_oos_holdout": {
            "passed": g2_pass and len(g2_notes) == 0,
            "min_events": MIN_OOS_EVENTS,
            "notes": g2_notes,
        },
    }


def run_geo_m2_tactical_slot_study(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    total_capital: float = 50_000.0,
    c18acc_slots: int = 2,
    tactical_slots: int = 1,
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    """C18acc 2+1 · M2 geo HOOK_IMP tactical leg vs 3-slot baseline."""
    from research.backtest.archive.dual_wma_tactical_slot_backtest import (
        ROUND_TRIP_COST_PCT,
        _evaluate_hypotheses,
        load_c18acc_periods,
        run_partitioned_tactical_combo,
        run_shared_pool_tactical_combo,
        simulate_slot_portfolio,
        trades_to_slot_periods,
    )

    xp = exit_params or TacticalExitParams(cap_days=3)
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError("need ≥2 trade dates in panel")

    cache: dict[str, Any] = {}
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=trade_dates, etf_codes=etf_codes
    )
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    cache.update(
        {
            "frames": frames,
            "trajectories": trajectories,
            "bench": bench,
            "stock_maps": _build_stock_frame_maps(trajectories, frames),
        }
    )

    m2_result = run_m2_geo_event_backtest(
        conn,
        dates=trade_dates,
        etf_codes=etf_codes,
        exit_params=xp,
        _cache=cache,
    )
    m2_periods = trades_to_slot_periods(
        m2_result["trades"],
        source_strategy="geo_m2_hook_imp",
        priority=2,
    )

    c18acc_3, c18_summary_3 = load_c18acc_periods(conn, trade_dates, n_slots=3)
    c18acc_2, _c18_summary_2 = load_c18acc_periods(conn, trade_dates, n_slots=c18acc_slots)

    panel = close.reindex(trade_dates)
    baseline_3 = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        c18acc_3,
        total_capital=total_capital,
        n_slots=3,
        return_daily=True,
    )
    m2_only = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        m2_periods,
        total_capital=total_capital / 3,
        n_slots=1,
        return_daily=True,
    )

    partitioned = run_partitioned_tactical_combo(
        conn,
        trade_dates=trade_dates,
        c18acc_periods=c18acc_2,
        tactical_periods=m2_periods,
        total_capital=total_capital,
        c18acc_slots=c18acc_slots,
        tactical_slots=tactical_slots,
        tactical_pool="geo_m2_hook_imp",
    )
    shared = run_shared_pool_tactical_combo(
        conn,
        trade_dates=trade_dates,
        c18acc_periods=c18acc_3,
        hook_periods=m2_periods,
        total_capital=total_capital,
        n_slots=3,
    )

    overlap = 0
    if m2_periods and c18acc_3:
        c18_keys = {(p["stock_id"], p["entry_date"]) for p in c18acc_3}
        overlap = sum(
            1 for p in m2_periods if (p["stock_id"], p["entry_date"]) in c18_keys
        )

    return {
        "date_from": trade_dates[0],
        "date_to": trade_dates[-1],
        "panel_meta": meta,
        "tactical_signal": "geo_m2_hook_imp",
        "tactical_events": m2_result,
        "c18acc_summary": c18_summary_3,
        "overlap_with_c18acc": overlap,
        "overlap_pct": round(100 * overlap / len(m2_periods), 1) if m2_periods else 0.0,
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "portfolios": {
            "c18acc_3slot_baseline": baseline_3,
            "m2_1slot_third_capital": m2_only,
            "partitioned_2plus1": partitioned,
            "shared_3slot_priority": shared,
        },
        "hypotheses": _evaluate_hypotheses(
            baseline=baseline_3,
            partitioned=partitioned,
            tactical_summary=m2_result["summary"],
        ),
    }


def run_geoalpha_gate_sweep(
    conn: sqlite3.Connection,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    *,
    gate_percentiles: tuple[int, ...] = (70, 50, 0),
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    """M4/M5 struct gate sweep · M1/M2 baseline (no struct gate)."""
    xp = exit_params or TacticalExitParams(cap_days=3)

    print("  build shared intraday cache ...", flush=True)
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    shared_cache = {
        "frames": frames,
        "trajectories": trajectories,
        "bench": bench,
        "stock_maps": stock_maps,
        "dates": dates,
        "etf_codes": etf_codes,
        "panel_meta": meta,
    }

    baseline = run_geoalpha_ablation(
        conn,
        dates,
        etf_codes,
        models=("M1", "M2"),
        exit_params=xp,
        struct_percentile=0,
        _cache=shared_cache,
    )

    gate_rows: list[dict[str, Any]] = []
    for pct in gate_percentiles:
        print(f"  gate sweep P{pct if pct > 0 else 'none'} ...", flush=True)
        payload = run_geoalpha_ablation(
            conn,
            dates,
            etf_codes,
            models=("M4", "M5"),
            exit_params=xp,
            struct_percentile=pct,
            _cache=shared_cache,
        )
        for r in payload.get("rows") or []:
            gate_rows.append({**r, "struct_gate": _struct_gate_label(pct), "struct_percentile": pct})

    return {
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "exit_cap_days": xp.cap_days,
        "baseline_rows": baseline.get("rows") or [],
        "gate_rows": gate_rows,
        "gate_percentiles": list(gate_percentiles),
        "panel_meta": meta,
    }


def render_geoalpha_gate_sweep_md(payload: dict[str, Any]) -> str:
    lines = [
        "# GeoAlpha · M4/M5 struct gate sweep",
        "",
        f"期間：**{payload.get('date_from')} → {payload.get('date_to')}** · "
        f"exit cap **{payload.get('exit_cap_days')}d**",
        "",
        "## M1/M2 baseline（無 struct gate）",
        "",
        "| Model | n | 超額% | 勝率% | vs — |",
        "|-------|---|-------|-------|------|",
    ]
    for r in payload.get("baseline_rows") or []:
        ex = r.get("mean_excess_pct")
        ex_s = f"{ex:.3f}" if ex is not None else "—"
        wr = r.get("win_rate")
        wr_s = f"{wr:.1f}" if wr is not None else "—"
        lines.append(f"| {r['model']} | {r.get('n', 0)} | {ex_s} | {wr_s} | — |")

    lines.extend(
        [
            "",
            "## M4/M5 · struct gate variants",
            "",
            "| Gate | Model | n | 超額% | 勝率% | events |",
            "|------|-------|---|-------|-------|--------|",
        ]
    )
    for r in payload.get("gate_rows") or []:
        ex = r.get("mean_excess_pct")
        ex_s = f"{ex:.3f}" if ex is not None else "—"
        wr = r.get("win_rate")
        wr_s = f"{wr:.1f}" if wr is not None else "—"
        lines.append(
            f"| {r.get('struct_gate')} | {r['model']} | {r.get('n', 0)} | "
            f"{ex_s} | {wr_s} | {r.get('events', '—')} |"
        )

    m2 = next((r for r in (payload.get("baseline_rows") or []) if r["model"] == "M2"), {})
    m2_ex = m2.get("mean_excess_pct")
    lines.extend(["", "## 解讀", ""])
    for gate in payload.get("gate_percentiles") or []:
        lbl = _struct_gate_label(gate)
        m4 = next(
            (r for r in (payload.get("gate_rows") or []) if r["model"] == "M4" and r.get("struct_gate") == lbl),
            {},
        )
        m5 = next(
            (r for r in (payload.get("gate_rows") or []) if r["model"] == "M5" and r.get("struct_gate") == lbl),
            {},
        )
        m4_n = m4.get("n", 0)
        m4_ex = m4.get("mean_excess_pct")
        verdict = "零重疊" if m4_n == 0 else (
            f"M4 超額 {m4_ex}% vs M2 {m2_ex}%"
            if m4_ex is not None and m2_ex is not None
            else "—"
        )
        lines.append(f"- **{lbl}**：M4 n={m4_n} · M5 n={m5.get('n', 0)} · {verdict}")

    lines.extend(
        [
            "",
            "- M1/M2 不受 struct gate 影響；M4/M5 需 HOOK_IMP/EXT_OPEN + S_joint rank。",
            "- P70 若 n=0 表示 champion hook 與 top-struct 池零交集。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_geoalpha_m2_oos_holdout_md(payload: dict[str, Any]) -> str:
    g2 = payload.get("g2_oos_holdout") or {}
    is_row = payload.get("is") or {}
    oos_row = payload.get("oos") or {}
    cov = payload.get("oos_coverage") or {}

    lines = [
        "# GeoAlpha M2 · OOS holdout（G2）",
        "",
        "## 凍結規格",
        "",
        "- 進場：φ=HOOK_IMP · improving · W20 96–100 · hook_retrace≥0.15 · spread≥1.5 · first poll",
        f"- 出場：cap {payload.get('exit_cap_days')}d · W20→Leading",
        "",
        "## IS / OOS",
        "",
        "| 視窗 | 期間 | n | 超額% | 勝率% | 超額勝率% |",
        "|------|------|---|-------|-------|-----------|",
    ]

    def _row(r: dict[str, Any]) -> None:
        if r.get("error"):
            lines.append(
                f"| {r.get('window')} | {r.get('date_from')}→{r.get('date_to')} | "
                f"— | _{r.get('error')}_ | — | — |"
            )
            return
        ex = r.get("mean_excess_pct")
        ex_s = f"{ex:.3f}" if ex is not None else "—"
        wr = r.get("win_rate")
        wr_s = f"{wr:.1f}" if wr is not None else "—"
        ewr = r.get("excess_win_rate")
        ewr_s = f"{ewr:.1f}" if ewr is not None else "—"
        lines.append(
            f"| {r.get('window')} | {r.get('date_from')}→{r.get('date_to')} | "
            f"{r.get('n', 0)} | {ex_s} | {wr_s} | {ewr_s} |"
        )

    _row(is_row)
    _row(oos_row)

    lines.extend(
        [
            "",
            "## OOS 資料覆蓋",
            "",
            f"- stock_kbar_1m：{cov.get('min_date')} → {cov.get('max_date')} · "
            f"{cov.get('n_distinct_dates')} 交易日",
            "",
            "## G2 裁決",
            "",
            f"- **{'PASS' if g2.get('passed') else 'FAIL'}** · min_events={g2.get('min_events')}",
        ]
    )
    for note in g2.get("notes") or []:
        lines.append(f"- {note}")
    if not g2.get("notes"):
        lines.append("- 全部門檻通過")

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- Research layer · 非 Order layer 採納。",
            "- OOS 需 intraday 1m K；無覆蓋時無法檢定 geo phase。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_geoalpha_m2_combo_md(payload: dict[str, Any]) -> str:
    tac = payload["tactical_events"]["summary"]
    pf = payload["portfolios"]
    base = pf["c18acc_3slot_baseline"]
    part = pf["partitioned_2plus1"]
    m2_only = pf["m2_1slot_third_capital"]
    hyp = payload["hypotheses"]

    lines = [
        "# GeoAlpha M2 · C18acc 2+1 戰術槽組合",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}** · "
        f"成本 round-trip **{payload.get('round_trip_cost_pct')}%**",
        "",
        "## M2 事件腿（geo HOOK_IMP）",
        "",
        f"- n={tac.get('n')} · 超額 mean **{tac.get('mean_excess_pct')}%** · "
        f"勝率 {tac.get('win_rate')}%",
        f"- 與 C18acc 重疊 {payload['overlap_with_c18acc']} / "
        f"{tac.get('n') or 0}（{payload['overlap_pct']}%）",
        "",
        "## 組合層級（50k NTD）",
        "",
        "| 模式 | 槽位 | CAGR% | 總報酬% | MaxDD% | n |",
        "|------|------|-------|---------|--------|---|",
    ]

    def _prow(label: str, slots: str, m: dict[str, Any]) -> None:
        lines.append(
            f"| {label} | {slots} | {m.get('cagr_pct', '—')} | "
            f"{m.get('total_return_pct', '—')} | {m.get('max_drawdown_pct', '—')} | "
            f"{m.get('n_trades', m.get('n_periods', '—'))} |"
        )

    _prow("C18acc baseline", "3", base)
    _prow("M2 only", "1（1/3 資金）", m2_only)
    _prow("Partitioned 2+1", "2+1", part)

    if part.get("daily_return_corr") is not None:
        lines.append("")
        lines.append(f"- Partitioned 日報酬 ρ = **{part['daily_return_corr']}**")

    lines.extend(
        [
            "",
            "## 假說（H-C）",
            "",
            f"- H-C1 CAGR↑：{'✅' if hyp.get('H-C1_cagr_up') else '❌' if hyp.get('H-C1_cagr_up') is False else '—'}",
            f"- H-C2 ρ<0.3：{'✅' if hyp.get('H-C2_low_corr') else '❌' if hyp.get('H-C2_low_corr') is False else '—'}",
            "",
            "## 解讀",
            "",
            "- Partitioned 2+1：C18acc 2 槽 + M2 geo 1 槽 · 獨立資金池。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_geoalpha_followup_report(
    payload: dict[str, Any],
    *,
    stem: str,
    render_md: Any,
    out_dir: Any | None = None,
) -> tuple[Any, Any]:
    out_dir = out_dir or RESEARCH_RRG
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = out_dir / f"{stem}_{stamp}.json"
    md_path = out_dir / f"{stem}_{stamp}.md"

    serializable = {
        k: v
        for k, v in payload.items()
        if k not in ("panel_meta", "entry", "tactical_events", "portfolios")
    }
    if "tactical_events" in payload:
        serializable["tactical_summary"] = (payload["tactical_events"] or {}).get("summary")
    if "portfolios" in payload:
        serializable["portfolios"] = {
            k: {kk: vv for kk, vv in v.items() if kk != "daily_nav"}
            for k, v in (payload.get("portfolios") or {}).items()
        }

    json_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_md(payload), encoding="utf-8")
    return json_path, md_path
