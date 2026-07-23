"""C18acc executed legs · full-population entry WMA20/5/3 win vs loss profile."""

from __future__ import annotations

import sqlite3
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import _collect_annot_dates
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (
    annotate_leg_triple_wma,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c
from research.backtest.w3_rv_hl_winner_profile import (
    SPREAD_CLASSES,
    _mean,
    _median,
    _numeric_lifts,
    profile_winners_losers,
)
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)

NUMERIC_FIELDS = (
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
    "w5_w3_mv_spread",
    "w20_w3_mv_spread",
    "w5_w20_mv_spread",
    "spread_dist",
    "spread_rs",
    "spread_mom",
    "entry_poll_idx",
)


def _c18acc_factor_specs() -> list[tuple[str, str, Any]]:
    from research.backtest.w3_rv_hl_winner_profile import _factor_specs as _base_specs

    def _quad(leg: str, q: str):
        return lambda r: r.get(f"{leg}_quadrant") == q

    def _spread(sc: str):
        return lambda r: r.get("spread_class") == sc

    extra: list[tuple[str, str, Any]] = [
        ("w3_leading", "W3 Leading", _quad("w3", "leading")),
        ("w3_weakening", "W3 Weakening", _quad("w3", "weakening")),
        ("w3_improving", "W3 Improving", _quad("w3", "improving")),
        ("w3_lagging", "W3 Lagging", _quad("w3", "lagging")),
        ("spread_mixed", "spread=mixed", _spread("mixed")),
        ("spread_aligned", "spread=aligned", _spread("aligned")),
        ("spread_pullback", "spread=pullback", _spread("pullback")),
        ("spread_extension", "spread=extension", _spread("extension")),
        ("w3_weakening_or_lagging", "W3 Weakening or Lagging", lambda r: r.get("w3_quadrant") in ("weakening", "lagging")),
    ]
    return _base_specs() + extra


def _scan_c18acc_discriminators(
    trades: list[dict[str, Any]],
    *,
    min_pos: int = 12,
    min_neg: int = 12,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fid, label, pred in _c18acc_factor_specs():
        pos = [t for t in trades if pred(t)]
        neg = [t for t in trades if not pred(t)]
        if len(pos) < min_pos or len(neg) < min_neg:
            continue
        pos_rets = [float(t["net_pct"]) for t in pos]
        neg_rets = [float(t["net_pct"]) for t in neg]
        mp, mn = _mean(pos_rets), _mean(neg_rets)
        wp = round(100.0 * sum(1 for x in pos_rets if x > 0) / len(pos_rets), 1)
        wn = round(100.0 * sum(1 for x in neg_rets if x > 0) / len(neg_rets), 1)
        rows.append(
            {
                "id": fid,
                "label": label,
                "n_pos": len(pos),
                "n_neg": len(neg),
                "mean_ret_pos": mp,
                "mean_ret_neg": mn,
                "lift": round((mp or 0) - (mn or 0), 3),
                "win_rate_pos": wp,
                "win_rate_neg": wn,
            }
        )
    rows.sort(key=lambda x: abs(float(x.get("lift") or 0)), reverse=True)
    return rows


def _profile_c18acc_winloss(trades: list[dict[str, Any]]) -> dict[str, Any]:
    prof = profile_winners_losers(trades)
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    prof["numeric_lifts"] = _numeric_lifts(wins, losses, NUMERIC_FIELDS)
    prof["factor_discriminators"] = _scan_c18acc_discriminators(trades)
    return prof


def _tertile_slices(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda t: float(t["net_pct"]), reverse=True)
    n = len(ordered)
    if n < 3:
        return {}
    k = n // 3
    top = ordered[:k]
    bot = ordered[-k:]
    return {
        "top_third": {
            "n": len(top),
            "mean_ret_pct": _mean([float(t["net_pct"]) for t in top]),
        },
        "bottom_third": {
            "n": len(bot),
            "mean_ret_pct": _mean([float(t["net_pct"]) for t in bot]),
        },
        "top_sample": top[:15],
        "bottom_sample": bot[:15],
    }


def collect_c18acc_wma_legs(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        f"c18acc entry win/loss: {len(periods)} legs · {len(stock_ids)} stocks · "
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
    meta = {
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_slots": n_slots,
        "sim_summary": sim_summary,
        "trajectory_meta": traj_meta,
        "n_legs": len(legs),
        "n_stocks": len(stock_ids),
    }
    return legs, meta


def _as_profile_trades(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for leg in legs:
        t = dict(leg)
        ret = float(leg["return_pct"])
        t["net_pct"] = ret
        t["outcome"] = "win" if ret > 0 else "loss"
        out.append(t)
    return out


def run_c18acc_entry_winloss_profile(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
) -> dict[str, Any]:
    legs, meta = collect_c18acc_wma_legs(
        conn,
        date_start=date_start,
        date_end=date_end,
        n_slots=n_slots,
    )
    trades = _as_profile_trades(legs)
    profile = _profile_c18acc_winloss(trades)
    tertiles = _tertile_slices(trades)
    return {
        "schema": "c18acc_entry_winloss_profile-v1",
        **meta,
        "profile": profile,
        "tertiles": tertiles,
        "all_legs": legs,
    }


def render_c18acc_entry_winloss_profile_md(payload: dict[str, Any]) -> str:
    prof = payload.get("profile") or {}
    tert = payload.get("tertiles") or {}
    tm = payload.get("trajectory_meta") or {}

    lines = [
        "# C18acc · 全母體進場 WMA20/5/3 · 贏家 vs 輸家",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽 · **{payload['n_legs']}** executed legs",
        "",
        f"盤中 30m poll · triple WMA（20/5/3）· {tm.get('n_frames', '—')} 幀",
        "",
        "## 樣本",
        "",
        f"- 成交筆：**{prof.get('n')}** · 贏 **{prof.get('n_win')}** / 輸 **{prof.get('n_loss')}**",
        f"- 勝率：**{prof.get('win_rate_pct')}%**",
        f"- 每筆均：贏 **{prof.get('mean_ret_win')}%** · 輸 **{prof.get('mean_ret_loss')}%**",
        "",
        "## 贏家 vs 輸家 · 數值特徵（下單當下）",
        "",
        "| 特徵 | mean 贏 | mean 輸 | lift | median 贏 | median 輸 |",
        "|------|--------:|--------:|-----:|----------:|----------:|",
    ]
    for row in prof.get("numeric_lifts") or []:
        lines.append(
            f"| {row.get('field')} | {row.get('mean_win')} | {row.get('mean_loss')} | "
            f"{row.get('lift')} | {row.get('median_win')} | {row.get('median_loss')} |"
        )

    lines.extend(
        [
            "",
            "## W20 / W5 / W3 象限分布 %（下單當下）",
            "",
            "| 腿 | 象限 | 贏家% | 輸家% | Δpp |",
            "|----|------|------:|------:|----:|",
        ]
    )
    for leg in ("w20", "w5", "w3"):
        qd = prof.get(f"{leg}_quadrant") or {}
        wq, lq = qd.get("win_pct") or {}, qd.get("loss_pct") or {}
        for q in sorted(set(wq) | set(lq)):
            wp, lp = wq.get(q, 0), lq.get(q, 0)
            lines.append(f"| {leg} | {q} | {wp} | {lp} | {round(wp - lp, 1)} |")

    lines.extend(
        [
            "",
            "## W20×W5 象限組合 %",
            "",
            "| 組合 | 贏家% | 輸家% | Δpp |",
            "|------|------:|------:|----:|",
        ]
    )
    for row in (prof.get("w20_w5_combo") or {}).get("rows") or []:
        lines.append(
            f"| {row.get('combo')} | {row.get('win_pct')} | {row.get('loss_pct')} | "
            f"{row.get('delta_pp')} |"
        )

    sp = prof.get("spread_class") or {}
    lines.extend(["", "## Spread class %", "", "| class | 贏家% | 輸家% | Δpp |", "|-------|------:|------:|----:|"])
    for q in SPREAD_CLASSES:
        wp = (sp.get("win_pct") or {}).get(q, 0)
        lp = (sp.get("loss_pct") or {}).get(q, 0)
        lines.append(f"| {q} | {wp} | {lp} | {round(wp - lp, 1)} |")

    lines.extend(
        [
            "",
            "## 進場時段 / trade_signal %",
            "",
            "| 類別 | 值 | 贏家% | 輸家% |",
            "|------|-----|------:|------:|",
        ]
    )
    for field, title in (("trade_signal", "signal"), ("entry_session", "session")):
        cat = prof.get(field) or {}
        wq, lq = cat.get("win_pct") or {}, cat.get("loss_pct") or {}
        for q in sorted(set(wq) | set(lq)):
            lines.append(f"| {title} | {q} | {wq.get(q, 0)} | {lq.get(q, 0)} |")

    lines.extend(
        [
            "",
            "## 因子鑑別掃描（二元 · 依 mean ret lift 排序）",
            "",
            "| 因子 | n+ | mean+ | win+ | n− | mean− | win− | lift |",
            "|------|---:|------:|-----:|---:|------:|-----:|-----:|",
        ]
    )
    for row in (prof.get("factor_discriminators") or [])[:25]:
        lines.append(
            f"| {row.get('label')} | {row.get('n_pos')} | {row.get('mean_ret_pos')} | "
            f"{row.get('win_rate_pos')} | {row.get('n_neg')} | {row.get('mean_ret_neg')} | "
            f"{row.get('win_rate_neg')} | {row.get('lift')} |"
        )

    top_n = tert.get("top_third") or {}
    bot_n = tert.get("bottom_third") or {}
    lines.extend(
        [
            "",
            "## 三分位（依 leg return）",
            "",
            f"- 上三分之一：n={top_n.get('n')} · mean **{top_n.get('mean_ret_pct')}%**",
            f"- 下三分之一：n={bot_n.get('n')} · mean **{bot_n.get('mean_ret_pct')}%**",
            "",
            "### 上三分之一樣本（top 15）",
            "",
            "| stock | entry | minute | ret% | W20 Q | W5 Q | W3 Q | spread | w20_rv | w3_rv |",
            "|-------|-------|--------|-----:|-------|------|------|--------|-------:|------:|",
        ]
    )
    for r in tert.get("top_sample") or []:
        lines.append(
            f"| {r.get('stock_id')} | {r.get('entry_date')} | {r.get('entry_minute')} | "
            f"**{round(float(r.get('net_pct') or 0), 3)}** | {r.get('w20_quadrant')} | "
            f"{r.get('w5_quadrant')} | {r.get('w3_quadrant')} | {r.get('spread_class')} | "
            f"{r.get('w20_rv')} | {r.get('w3_rv')} |"
        )

    lines.extend(
        [
            "",
            "### 下三分之一樣本（bottom 15）",
            "",
            "| stock | entry | minute | ret% | W20 Q | W5 Q | W3 Q | spread | w20_rv | w3_rv |",
            "|-------|-------|--------|-----:|-------|------|------|--------|-------:|------:|",
        ]
    )
    for r in tert.get("bottom_sample") or []:
        lines.append(
            f"| {r.get('stock_id')} | {r.get('entry_date')} | {r.get('entry_minute')} | "
            f"**{round(float(r.get('net_pct') or 0), 3)}** | {r.get('w20_quadrant')} | "
            f"{r.get('w5_quadrant')} | {r.get('w3_quadrant')} | {r.get('spread_class')} | "
            f"{r.get('w20_rv')} | {r.get('w3_rv')} |"
        )

    lines.extend(
        [
            "",
            "## 解讀（數據歸納 · 非因果）",
            "",
            _interpretation(prof),
            "",
            "模組：`src/research/backtest/archive/c18acc_entry_winloss_profile.py`",
        ]
    )
    return "\n".join(lines)


def _interpretation(prof: dict[str, Any]) -> str:
    bullets: list[str] = []
    lifts = prof.get("numeric_lifts") or []
    if lifts:
        top = lifts[0]
        bullets.append(
            f"- 數值 lift 最大：**{top.get('field')}** "
            f"（贏 {top.get('mean_win')} vs 輸 {top.get('mean_loss')} · Δ {top.get('lift')}）"
        )

    sp = prof.get("spread_class") or {}
    wsp, lsp = sp.get("win_pct") or {}, sp.get("loss_pct") or {}
    mixed_delta = round((wsp.get("mixed") or 0) - (lsp.get("mixed") or 0), 1)
    if mixed_delta:
        bullets.append(
            f"- **spread mixed**：贏家 {wsp.get('mixed', 0)}% vs 輸家 {lsp.get('mixed', 0)}% "
            f"（Δ {mixed_delta}pp）"
        )

    w3q = prof.get("w3_quadrant") or {}
    w3w = (w3q.get("win_pct") or {}).get("weakening", 0)
    w3l = (w3q.get("loss_pct") or {}).get("weakening", 0)
    if w3w or w3l:
        bullets.append(
            f"- **W3 weakening 進場**：贏家 {w3w}% vs 輸家 {w3l}% "
            f"（Δ {round(w3w - w3l, 1)}pp）"
        )

    disc = prof.get("factor_discriminators") or []
    if disc:
        best_pos = max(disc, key=lambda x: float(x.get("lift") or 0))
        best_neg = min(disc, key=lambda x: float(x.get("lift") or 0))
        bullets.append(
            f"- 鑑別最強正向：**{best_pos.get('label')}** lift {best_pos.get('lift')}% "
            f"（n+={best_pos.get('n_pos')}）"
        )
        bullets.append(
            f"- 鑑別最強負向：**{best_neg.get('label')}** lift {best_neg.get('lift')}% "
            f"（n+={best_neg.get('n_pos')}）"
        )

    w20 = prof.get("w20_quadrant") or {}
    w20_lead_delta = round(
        (w20.get("win_pct") or {}).get("leading", 0)
        - (w20.get("loss_pct") or {}).get("leading", 0),
        1,
    )
    bullets.append(
        f"- **W20 leading 進場**：贏/輸 Δ {w20_lead_delta}pp — "
        "若接近 0，表示三線 leading 為共同背景而非贏家專屬"
    )
    return "\n".join(bullets) if bullets else "（樣本不足）"
