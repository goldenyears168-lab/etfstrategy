"""ABC v3+F1 · pullback morphology (path into entry).

Prior methods were misaligned with ABC's structural pullback:
  - W20 T-2→T-1→T0 overnight deltas measure slow leg drift, not pullback shape
  - Absolute spread_dist terciles ignore *how* the pullback formed

This study scores morphology of the path **into** the entry poll:

  Reference snap = previous same-day poll if any, else T-1 13:30 EOD
  (critical: ~55% of ABC entries are 09:30 — no prior intraday poll).

  fresh_onset   — entry is the first ``spread_class=pullback`` poll of T0
  deepening     — spread_dist rose vs reference
  lead_firm     — W20 MV did not fall vs reference (trend still held)
  short_soft    — W5 or W3 MV fell vs reference (short legs eased)
  px_dip        — entry price below the day's open (price soft into signal)

Classic morph = fresh_onset ∧ deepening ∧ lead_firm ∧ short_soft
Active dip    = deepening ∧ px_dip ∧ lead_firm ∧ short_soft
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from market_benchmark import load_benchmark_close, previous_trading_date
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import CHAMPION_COMBO
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import evaluate_leg_tp_sl
from research.backtest.archive.c18acc_triple_wma_extreme_profile import _frame_idx_for_minute
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps, _price_at
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import _mv, _rv
from research.backtest.w3_rv_hl_winner_profile import INTRADAY_POLL_MINUTES
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

SCHEMA = "abc_v3_f1_pullback_morphology-v2"

SHAPE_SPECS: tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...] = (
    ("fresh_onset", "當日首次 pullback（新鮮回踩）", lambda r: bool(r.get("fresh_onset"))),
    ("deepening", "價差加深（vs 前 poll／T−1）", lambda r: bool(r.get("deepening"))),
    ("lead_firm", "長線穩（W20 MV 不降）", lambda r: bool(r.get("lead_firm"))),
    ("short_soft", "短線鬆（W5/W3 MV 降）", lambda r: bool(r.get("short_soft"))),
    ("px_dip", "價回檔（進場 < 開盤）", lambda r: bool(r.get("px_dip"))),
    (
        "classic_morph",
        "經典形貌：新鮮∧加深∧長穩∧短鬆",
        lambda r: bool(r.get("classic_morph")),
    ),
    (
        "active_dip",
        "主動回檔：加深∧價回∧長穩∧短鬆",
        lambda r: bool(r.get("active_dip")),
    ),
    (
        "asymmetric",
        "非對稱回踩：長穩∧短鬆（核心）",
        lambda r: bool(r.get("lead_firm")) and bool(r.get("short_soft")),
    ),
    (
        "stale_flat",
        "陳舊／平坦：非新鮮且未加深",
        lambda r: (not r.get("fresh_onset")) and (not r.get("deepening")),
    ),
)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return round(s[mid], 3)
    return round((s[mid - 1] + s[mid]) / 2.0, 3)


def _win_rate(xs: list[float]) -> float | None:
    if not xs:
        return None
    return round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1)


def _summarize(rets: list[float], *, baseline: float | None) -> dict[str, Any]:
    mean = _mean(rets)
    return {
        "n": len(rets),
        "mean_pct": mean,
        "median_pct": _median(rets),
        "win_rate_pct": _win_rate(rets),
        "delta_vs_baseline_pp": (
            round(mean - baseline, 3) if mean is not None and baseline is not None else None
        ),
    }


def _poll_points_to_entry(
    frames: list[dict[str, str]],
    points: list[dict[str, Any] | None],
    *,
    entry_date: str,
    entry_minute: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, fr in enumerate(frames):
        if fr.get("date") != entry_date:
            continue
        minute = str(fr.get("minute") or "")
        if minute not in INTRADAY_POLL_MINUTES:
            continue
        if entry_minute and minute > entry_minute:
            break
        p = points[i] if i < len(points) else None
        if not p:
            continue
        out.append(p)
        if entry_minute and minute == entry_minute:
            break
    return out


def _eod_point(
    frames: list[dict[str, str]],
    points: list[dict[str, Any] | None],
    *,
    trade_date: str,
) -> dict[str, Any] | None:
    idx = _frame_idx_for_minute(frames, trade_date, "13:30")
    if idx is None or idx >= len(points):
        return None
    return points[idx]


def score_pullback_morphology(
    day_points: list[dict[str, Any]],
    *,
    prior_ref: dict[str, Any] | None = None,
    ref_source: str = "none",
    entry_px: float | None = None,
    open_px: float | None = None,
) -> dict[str, Any] | None:
    """Score morphology; ``prior_ref`` = prev poll or T-1 EOD."""
    if len(day_points) < 1:
        return None
    entry = day_points[-1]
    dist_e = entry.get("spread_dist")
    if dist_e is None:
        return None
    dist_e = float(dist_e)

    prior_pb = [
        p for p in day_points[:-1] if str(p.get("spread_class") or "") == "pullback"
    ]
    fresh_onset = len(prior_pb) == 0 and str(entry.get("spread_class") or "") == "pullback"

    prev = day_points[-2] if len(day_points) >= 2 else prior_ref
    used_ref = "prev_poll" if len(day_points) >= 2 else ref_source

    deepening = False
    lead_firm = False
    short_soft = False
    d_spread = d_w20 = d_w5 = d_w3 = None
    if prev is not None:
        dist_p = prev.get("spread_dist")
        if dist_p is not None:
            d_spread = round(dist_e - float(dist_p), 4)
            deepening = dist_e > float(dist_p)
        mv20_e, mv20_p = _mv(entry, "w20"), _mv(prev, "w20")
        mv5_e, mv5_p = _mv(entry, "w5"), _mv(prev, "w5")
        mv3_e, mv3_p = _mv(entry, "w3"), _mv(prev, "w3")
        if mv20_e is not None and mv20_p is not None:
            d_w20 = round(float(mv20_e) - float(mv20_p), 4)
            lead_firm = float(mv20_e) >= float(mv20_p) - 1e-9
        soft = False
        if mv5_e is not None and mv5_p is not None:
            d_w5 = round(float(mv5_e) - float(mv5_p), 4)
            soft = soft or float(mv5_e) < float(mv5_p)
        if mv3_e is not None and mv3_p is not None:
            d_w3 = round(float(mv3_e) - float(mv3_p), 4)
            soft = soft or float(mv3_e) < float(mv3_p)
        short_soft = soft

    px_dip = bool(
        entry_px is not None and open_px is not None and open_px > 0 and entry_px < open_px - 1e-9
    )
    px_from_open_pct = (
        round((entry_px / open_px - 1.0) * 100.0, 4)
        if entry_px is not None and open_px not in (None, 0)
        else None
    )

    classic = bool(fresh_onset and deepening and lead_firm and short_soft)
    active = bool(deepening and px_dip and lead_firm and short_soft)

    lead_rise = bool(d_w20 is not None and d_w20 > 0)
    w20_gt_w5 = bool(d_w20 is not None and d_w5 is not None and d_w20 > d_w5)
    w20_ge_w5 = bool(d_w20 is not None and d_w5 is not None and d_w20 >= d_w5)
    w20_gt_w3 = bool(d_w20 is not None and d_w3 is not None and d_w20 > d_w3)
    w20_gt_both = bool(w20_gt_w5 and w20_gt_w3)
    # Relative cushion: W20 lost less than W5 even if both down.
    w20_rel_cushion = w20_gt_w5
    lead_firm_px = bool(lead_firm and px_dip)
    lead_firm_not_deep = bool(lead_firm and not deepening)

    return {
        "n_polls_to_entry": len(day_points),
        "entry_minute": str(entry.get("minute") or ""),
        "ref_source": used_ref,
        "spread_dist": dist_e,
        "spread_class": entry.get("spread_class"),
        "d_spread": d_spread,
        "d_w20_mv": d_w20,
        "d_w5_mv": d_w5,
        "d_w3_mv": d_w3,
        "fresh_onset": fresh_onset,
        "deepening": deepening,
        "lead_firm": lead_firm,
        "lead_rise": lead_rise,
        "short_soft": short_soft,
        "px_dip": px_dip,
        "px_from_open_pct": px_from_open_pct,
        "w20_gt_w5": w20_gt_w5,
        "w20_ge_w5": w20_ge_w5,
        "w20_gt_w3": w20_gt_w3,
        "w20_gt_both": w20_gt_both,
        "w20_rel_cushion": w20_rel_cushion,
        "lead_firm_px": lead_firm_px,
        "lead_firm_not_deep": lead_firm_not_deep,
        "classic_morph": classic,
        "active_dip": active,
        "w20_rv": _rv(entry, "w20"),
        "w5_rv": _rv(entry, "w5"),
        "w3_rv": _rv(entry, "w3"),
    }


def annotate_legs_pullback_morphology(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    *,
    chunk_days: int = 15,
) -> list[dict[str, Any]]:
    """Attach morphology vs prev-poll/T-1 EOD + champion TP-only return."""
    close, opn, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        d = str(leg.get("entry_date") or "")
        if d:
            by_date[d].append(leg)

    dates = sorted(by_date)
    out: list[dict[str, Any]] = []
    n_chunks = (len(dates) + chunk_days - 1) // chunk_days
    for ci in range(n_chunks):
        chunk = dates[ci * chunk_days : (ci + 1) * chunk_days]
        prev_dates = {
            previous_trading_date(conn, d) for d in chunk if previous_trading_date(conn, d)
        }
        build_dates = sorted(set(chunk) | prev_dates)
        sids = sorted({str(leg["stock_id"]) for d in chunk for leg in by_date[d]})
        print(
            f"  morph chunk {ci + 1}/{n_chunks}: entry_dates={len(chunk)} "
            f"build_dates={len(build_dates)} stocks={len(sids)}",
            flush=True,
        )
        frames, trajectories, _ = build_dual_wma_intraday_trajectories(
            conn,
            dates=build_dates,
            stock_ids=sids,
            micro_lengths=(WMA_MICRO_LENGTH, WMA_ULTRA_LENGTH),
        )
        stock_maps = _build_stock_frame_maps(trajectories, frames)
        for d in chunk:
            t1 = previous_trading_date(conn, d)
            for leg in by_date[d]:
                sid = str(leg["stock_id"])
                points = stock_maps.get(sid)
                if not points:
                    continue
                t0 = leg.get("entry_t0") or {}
                entry_minute = str(leg.get("entry_minute") or t0.get("minute") or "")
                if not entry_minute:
                    fi = t0.get("frame_idx")
                    if fi is not None and 0 <= int(fi) < len(frames):
                        entry_minute = str(frames[int(fi)].get("minute") or "")
                if not entry_minute or _frame_idx_for_minute(frames, d, entry_minute) is None:
                    continue
                day_pts = _poll_points_to_entry(
                    frames, points, entry_date=d, entry_minute=entry_minute
                )
                prior_ref = None
                ref_source = "none"
                if len(day_pts) < 2 and t1:
                    prior_ref = _eod_point(frames, points, trade_date=t1)
                    ref_source = "t_minus_1_eod"
                entry_px, _ = _price_at(conn, bench, sid, d, entry_minute)
                open_px = None
                try:
                    if sid in opn.columns and d in opn.index:
                        v = opn.at[d, sid]
                        open_px = float(v) if v == v else None
                except Exception:
                    open_px = None
                morph = score_pullback_morphology(
                    day_pts,
                    prior_ref=prior_ref,
                    ref_source=ref_source,
                    entry_px=entry_px,
                    open_px=open_px,
                )
                if not morph:
                    continue
                tp = evaluate_leg_tp_sl(leg, CHAMPION_COMBO)
                out.append(
                    {
                        "stock_id": sid,
                        "entry_date": d,
                        "tp_only_ret_pct": float(tp["exit_ret_pct"]),
                        "hold_ret_pct": float(leg.get("return_pct") or 0),
                        **morph,
                    }
                )
    return out


def run_pullback_morphology_study(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    *,
    legs_source: str = "",
    min_bucket_n: int = 30,
    lift_pp_gate: float = 2.0,
) -> dict[str, Any]:
    annotated = annotate_legs_pullback_morphology(conn, legs)
    rets = [float(r["tp_only_ret_pct"]) for r in annotated]
    baseline = _mean(rets)
    shapes: list[dict[str, Any]] = []
    for sid, label, pred in SHAPE_SPECS:
        sub = [float(r["tp_only_ret_pct"]) for r in annotated if pred(r)]
        comp = [float(r["tp_only_ret_pct"]) for r in annotated if not pred(r)]
        mean = _mean(sub)
        shapes.append(
            {
                "id": sid,
                "label": label,
                **_summarize(sub, baseline=baseline),
                "complement_n": len(comp),
                "complement_mean_pct": _mean(comp),
                "passes_gate": bool(
                    len(sub) >= min_bucket_n
                    and mean is not None
                    and baseline is not None
                    and (mean - baseline) >= lift_pp_gate
                ),
            }
        )

    dates = sorted({str(r["entry_date"]) for r in annotated})
    composites = [s for s in shapes if s["id"] in ("classic_morph", "active_dip", "asymmetric")]
    best = max(
        composites,
        key=lambda s: (
            s.get("delta_vs_baseline_pp") is not None,
            s.get("delta_vs_baseline_pp") or -999,
        ),
        default=None,
    )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legs_source": legs_source,
        "n_input_legs": len(legs),
        "n_annotated": len(annotated),
        "n_stocks": len({str(r["stock_id"]) for r in annotated}),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "exit": "champion TP-only · NEVER_SL · hold_5d cap",
        "method_note": (
            "Path into entry: prev same-day poll, else T-1 13:30 EOD. "
            "px_dip = entry < day open. Formation, not absolute depth."
        ),
        "baseline_mean_tp_pct": baseline,
        "min_bucket_n": min_bucket_n,
        "lift_pp_gate": lift_pp_gate,
        "shapes": shapes,
        "best_composite": best,
        "any_shape_passes_gate": any(bool(s.get("passes_gate")) for s in shapes),
        "ref_sources": {
            "prev_poll": sum(1 for r in annotated if r.get("ref_source") == "prev_poll"),
            "t_minus_1_eod": sum(1 for r in annotated if r.get("ref_source") == "t_minus_1_eod"),
            "none": sum(1 for r in annotated if r.get("ref_source") in (None, "none")),
        },
        "rates": {
            "fresh_onset_pct": round(
                100.0 * sum(1 for r in annotated if r.get("fresh_onset")) / len(annotated), 1
            )
            if annotated
            else None,
            "deepening_pct": round(
                100.0 * sum(1 for r in annotated if r.get("deepening")) / len(annotated), 1
            )
            if annotated
            else None,
            "classic_morph_pct": round(
                100.0 * sum(1 for r in annotated if r.get("classic_morph")) / len(annotated), 1
            )
            if annotated
            else None,
            "asymmetric_pct": round(
                100.0
                * sum(1 for r in annotated if r.get("lead_firm") and r.get("short_soft"))
                / len(annotated),
                1,
            )
            if annotated
            else None,
        },
    }


def render_pullback_morphology_md(payload: dict[str, Any]) -> str:
    lines = [
        "# ABC v3+F1 · 回踩形貌強化（進場路徑 · v2）",
        "",
        f"> legs **{payload.get('n_annotated')}** / input **{payload.get('n_input_legs')}** · "
        f"股票 **{payload.get('n_stocks')}** · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"> 出場：{payload.get('exit')} · 來源：`{payload.get('legs_source')}`",
        "",
        "## 方法修正",
        "",
        "- **不做**：跨日 W20 單腿差額當主軸、單純 `spread_dist` 絕對深度",
        "- **改做**：進場當下相對「前一根參考」的**形成過程**",
        "- 參考點：同日上一 poll；若無（常見 09:30）→ **T−1 13:30 EOD**",
        "- 價回檔：進場價 < 當日開盤（軌跡點無 px，改用 kbar／日線開盤）",
        f"- {payload.get('method_note')}",
        f"- 參考來源計數：{payload.get('ref_sources')}",
        "",
        "## 形貌定義",
        "",
        "| ID | 條件 |",
        "|----|------|",
    ]
    for sid, label, _ in SHAPE_SPECS:
        lines.append(f"| `{sid}` | {label} |")
    rates = payload.get("rates") or {}
    lines.extend(
        [
            "",
            f"## 結果 · baseline TP-only **{payload.get('baseline_mean_tp_pct')}%**",
            "",
            f"- 新鮮 {rates.get('fresh_onset_pct')}% · 加深 {rates.get('deepening_pct')}% · "
            f"非對稱 {rates.get('asymmetric_pct')}% · 經典 {rates.get('classic_morph_pct')}%",
            f"- 任一族過門檻（n≥{payload.get('min_bucket_n')} · lift≥{payload.get('lift_pp_gate')}pp）："
            f"**{payload.get('any_shape_passes_gate')}**",
            "",
            "| 形貌 | n | 均% | 中位% | 勝率% | Δ pp | 補集均% | 過門檻 |",
            "|------|--:|----:|------:|------:|-----:|--------:|:------:|",
        ]
    )
    for row in payload.get("shapes") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | "
            f"{row.get('median_pct')} | {row.get('win_rate_pct')} | "
            f"{row.get('delta_vs_baseline_pp')} | {row.get('complement_mean_pct')} | "
            f"{'✔' if row.get('passes_gate') else '—'} |"
        )
    best = payload.get("best_composite") or {}
    lines.extend(
        [
            "",
            "## 解讀",
            "",
            f"- 複合最佳：`{best.get('id')}` · n={best.get('n')} · "
            f"Δ={best.get('delta_vs_baseline_pp')}pp · passes={best.get('passes_gate')}",
            "- 核心假說是「非對稱回踩」（長線穩、短線鬆），不是「愈新鮮愈好」",
            "",
            "模組：`src/research/backtest/abc_v3_f1_pullback_morphology_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
