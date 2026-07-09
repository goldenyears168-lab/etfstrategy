"""ABC v3 + skip 09:00 · slot portfolio Phase 1 (3/5/7 × FIFO vs top-K)."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime
from typing import Any, Callable, Literal

from market_benchmark import load_benchmark_close
from market_breadth_ma import BREADTH_ZONE_DISPLAY, BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel
from project_config import DEFAULT_ETF_CODES
from research.backtest.dual_wma_signal_backtest import (
    ROUND_TRIP_COST_PCT,
    _build_stock_frame_maps,
    _exit_frame_idx,
    _trade_return,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1
from research.backtest.w3_rv_hl_winner_profile import (
    ABC_V3_ADOPTED_MIN_POLL_IDX,
    _poll_idx_for_frame,
    extract_entry_features,
    backtest_w3_abc_v3_oos,
    matches_w3_rv_hl_abc_v3,
)
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

FillMode = Literal["fifo", "topk_score"]
ABC_V3_SLOT_GRID: tuple[int, ...] = (3, 5, 7)
ABC_V3_HOLD_DAYS = 3
CAPITAL_PER_SLOT_NTD = 50_000.0
ABC_V3_SLOT_CHAMPION = 5
ABC_V3_SCORE_SPEC_IDS: tuple[str, ...] = (
    "v0",
    "spread_only",
    "rv20_only",
    "no_lead",
    "zscore_v1",
)
SCORE_FACTOR_KEYS = ("neg_spread", "neg_rv20_ext", "etf", "lead", "poll_bad")


def _raw_score_factors(features: dict[str, Any]) -> dict[str, float]:
    return {
        "neg_spread": -float(features.get("w20_w3_rv_spread") or 0.0),
        "neg_rv20_ext": -max(0.0, float(features.get("w20_rv") or 100.0) - 100.0),
        "etf": float(features.get("etf_hold_count") or 0.0),
        "lead": 1.0 if features.get("trade_signal") == "lead_pullback_buy" else 0.0,
        "poll_bad": 1.0 if int(features.get("entry_poll_idx") or 0) == 1 else 0.0,
    }


def _score_spec_config(spec_id: str) -> dict[str, Any]:
    specs: dict[str, dict[str, Any]] = {
        "v0": {
            "w": {"neg_spread": 1.0, "neg_rv20_ext": 0.3, "etf": 0.2, "lead": 0.5},
            "poll": 0.5,
            "z": False,
        },
        "spread_only": {"w": {"neg_spread": 1.0}, "poll": 0.0, "z": False},
        "rv20_only": {"w": {"neg_rv20_ext": 1.0}, "poll": 0.0, "z": False},
        "no_lead": {
            "w": {"neg_spread": 1.0, "neg_rv20_ext": 0.3, "etf": 0.2},
            "poll": 0.5,
            "z": False,
        },
        "zscore_v1": {
            "w": {"neg_spread": 1.0, "neg_rv20_ext": 0.3, "etf": 0.2},
            "poll": 0.5,
            "z": True,
        },
    }
    return specs.get(spec_id) or specs["v0"]


def _day_zscore(value: float, mean: float, std: float) -> float:
    if std <= 1e-9:
        return 0.0
    return (value - mean) / std


def _day_factor_stats(rows: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    stats: dict[str, tuple[float, float]] = {}
    for key in SCORE_FACTOR_KEYS:
        vals = [float((r.get("factors") or {}).get(key) or 0.0) for r in rows]
        if not vals:
            stats[key] = (0.0, 0.0)
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        stats[key] = (mean, var**0.5)
    return stats


def compute_abc_v3_rank_score(
    features: dict[str, Any],
    *,
    spec_id: str = "v0",
    day_stats: dict[str, tuple[float, float]] | None = None,
) -> float:
    """PIT-safe rank score · higher = preferred fill."""
    factors = _raw_score_factors(features)
    cfg = _score_spec_config(spec_id)
    weights: dict[str, float] = cfg["w"]
    use_z = bool(cfg.get("z"))
    score = 0.0
    for key, w in weights.items():
        val = factors[key]
        if use_z and day_stats is not None:
            mean, std = day_stats.get(key, (0.0, 0.0))
            val = _day_zscore(val, mean, std)
        score += float(w) * val
    score -= float(cfg.get("poll") or 0.0) * factors["poll_bad"]
    return round(score, 4)


def abc_v3_rank_score_v0(features: dict[str, Any]) -> float:
    return compute_abc_v3_rank_score(features, spec_id="v0")


def apply_score_spec(
    candidates: list[dict[str, Any]],
    *,
    spec_id: str,
) -> list[dict[str, Any]]:
    """Attach rank_score · z-score uses same-day cross-section."""
    enriched: list[dict[str, Any]] = []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        feats = row.get("features") or {}
        item = {**row, "factors": _raw_score_factors(feats)}
        enriched.append(item)
        by_date.setdefault(str(row["entry_date"]), []).append(item)
    day_stats_map = {d: _day_factor_stats(rows) for d, rows in by_date.items()}
    out: list[dict[str, Any]] = []
    for row in enriched:
        feats = row.get("features") or {}
        score = compute_abc_v3_rank_score(
            feats,
            spec_id=spec_id,
            day_stats=day_stats_map.get(str(row["entry_date"])),
        )
        out.append({**row, "rank_score": score, "score_spec": spec_id})
    return out


def collect_abc_v3_skip0900_period_candidates(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    hold_days: int = ABC_V3_HOLD_DAYS,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    matcher: Callable[[dict[str, Any]], bool] | None = None,
    source_strategy: str | None = None,
    frames: list[dict[str, str]] | None = None,
    trajectories: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    stock_maps: dict[str, list[dict[str, Any]]] | None = None,
    bench: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """All ABC v3 skip-09:00 entry candidates with intraday prices · hold Nd."""
    match_fn = matcher or matches_w3_rv_hl_abc_v3
    strat_label = source_strategy or (
        "abc_v3_f1_skip09" if match_fn is matches_w3_rv_hl_abc_v3_f1 else "abc_v3_skip09"
    )
    exit_rule = f"hold_{hold_days}d"
    if frames is None or trajectories is None:
        frames, trajectories, meta = build_dual_wma_intraday_trajectories(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        )
    if bench is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    if stock_maps is None:
        stock_maps = _build_stock_frame_maps(trajectories, frames)
    panel_meta = meta or {}
    frame_ids = [f["id"] for f in frames]
    candidates: list[dict[str, Any]] = []
    seen_day: set[tuple[str, str]] = set()

    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        pts = stock_maps.get(sid)
        if not pts:
            continue
        for i, fid in enumerate(frame_ids):
            poll_idx = _poll_idx_for_frame(frames, i)
            if poll_idx is None or poll_idx < ABC_V3_ADOPTED_MIN_POLL_IDX:
                continue
            p = by_id.get(fid)
            if not p or not match_fn(p):
                continue
            d = frames[i]["date"]
            key = (sid, d)
            if key in seen_day:
                continue
            seen_day.add(key)
            exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
            tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
            if tr is None:
                continue
            feats = extract_entry_features(p, etf_hold_count=int(t.get("etf_hold_count") or 0))
            feats["entry_poll_idx"] = poll_idx
            candidates.append(
                {
                    "stock_id": sid,
                    "stock_name": t.get("stock_name") or "",
                    "entry_date": d,
                    "exit_date": tr["exit_date"],
                    "entry_minute": tr.get("entry_minute") or feats.get("entry_minute"),
                    "entry_px": tr.get("entry_px"),
                    "exit_px": tr.get("exit_px"),
                    "entry_frame_idx": i,
                    "entry_poll_idx": poll_idx,
                    "features": feats,
                    "net_pct": tr.get("net_pct"),
                    "excess_pct": tr.get("excess_pct"),
                    "hold_days": hold_days,
                    "source_strategy": strat_label,
                }
            )

    return candidates, frames, panel_meta


def _build_abc_v3_intraday_panel(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    kbar_bar_minutes: int | None = 5,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any], dict[str, list[dict[str, Any]]], Any]:
    """Build dual-WMA intraday panel once for ABC v3 pipeline reuse."""
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        kbar_bar_minutes=kbar_bar_minutes,
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    return frames, trajectories, meta, stock_maps, bench


def order_periods_for_fill(
    candidates: list[dict[str, Any]],
    *,
    fill_mode: FillMode,
) -> list[dict[str, Any]]:
    """Order slot entry attempts: FIFO (poll time) vs top-K score (per day)."""
    if fill_mode == "fifo":
        return sorted(
            candidates,
            key=lambda r: (str(r["entry_date"]), int(r.get("entry_frame_idx") or 0)),
        )

    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        by_date.setdefault(str(row["entry_date"]), []).append(row)
    ordered: list[dict[str, Any]] = []
    for d in sorted(by_date):
        day_rows = sorted(
            by_date[d],
            key=lambda r: (
                -float(r.get("rank_score") or 0),
                int(r.get("entry_frame_idx") or 0),
            ),
        )
        ordered.extend(day_rows)
    return ordered


def _unconstrained_leg_stats(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {"n": 0}
    nets = [float(r["net_pct"]) for r in candidates if r.get("net_pct") is not None]
    if not nets:
        return {"n": 0}
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(nets),
        "mean_ret_net_pct": round(sum(nets) / len(nets), 3),
        "mean_daily_ret_net_pct": round(sum(nets) / ABC_V3_HOLD_DAYS / len(nets), 4),
        "win_rate_pct": round(100.0 * wins / len(nets), 2),
        "median_ret_net_pct": round(statistics.median(nets), 3),
    }


def backtest_abc_v3_slot_phase1(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    n_slots_grid: tuple[int, ...] = ABC_V3_SLOT_GRID,
    fill_modes: tuple[FillMode, ...] = ("fifo", "topk_score"),
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    matcher: Callable[[dict[str, Any]], bool] | None = None,
    matcher_id: str | None = None,
    frames: list[dict[str, str]] | None = None,
    trajectories: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    stock_maps: dict[str, list[dict[str, Any]]] | None = None,
    bench: Any | None = None,
) -> dict[str, Any]:
    match_fn = matcher or matches_w3_rv_hl_abc_v3
    mid = matcher_id or (
        "w3_rv_hl_abc_v3_f1" if match_fn is matches_w3_rv_hl_abc_v3_f1 else "w3_rv_hl_abc_v3"
    )
    candidates, _frames, panel_meta = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        matcher=match_fn,
        frames=frames,
        trajectories=trajectories,
        meta=meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    candidates = apply_score_spec(candidates, spec_id="v0")
    close, _, _ = load_price_panels(conn)
    trade_dates = [d for d in close.index.astype(str).tolist() if d in set(dates)]
    if not trade_dates:
        trade_dates = list(dates)

    rows: list[dict[str, Any]] = []
    for n_slots in n_slots_grid:
        for fill_mode in fill_modes:
            periods = order_periods_for_fill(candidates, fill_mode=fill_mode)
            total_capital = CAPITAL_PER_SLOT_NTD * n_slots
            sim = simulate_slot_portfolio(
                conn,
                close,
                trade_dates,
                periods,
                total_capital=total_capital,
                n_slots=n_slots,
            )
            n_signals = len(candidates)
            n_trades = int(sim.get("n_trades") or 0)
            rows.append(
                {
                    "variant_id": f"slots{n_slots}_{fill_mode}",
                    "n_slots": n_slots,
                    "fill_mode": fill_mode,
                    "total_capital_ntd": total_capital,
                    "n_signals": n_signals,
                    "n_trades": n_trades,
                    "signal_capture_pct": round(100.0 * n_trades / n_signals, 1) if n_signals else None,
                    **sim,
                }
            )

    ranked = sorted(
        rows,
        key=lambda r: (-(float(r.get("mean_daily_return_pct") or -999)), -(float(r.get("cagr_pct") or -999))),
    )
    return {
        "dates": dates,
        "meta": panel_meta,
        "hold_days": ABC_V3_HOLD_DAYS,
        "execution_gate": f"min_poll_idx>={ABC_V3_ADOPTED_MIN_POLL_IDX}",
        "matcher": mid,
        "n_candidates": len(candidates),
        "unconstrained_legs": _unconstrained_leg_stats(candidates),
        "slot_grid": list(n_slots_grid),
        "fill_modes": list(fill_modes),
        "variants": rows,
        "best": ranked[0] if ranked else None,
    }


def verdict_abc_v3_slot_phase1(bt: dict[str, Any]) -> dict[str, Any]:
    variants = list(bt.get("variants") or [])
    fifo_by_slots = {int(v["n_slots"]): v for v in variants if v.get("fill_mode") == "fifo"}
    topk_by_slots = {int(v["n_slots"]): v for v in variants if v.get("fill_mode") == "topk_score"}
    best = bt.get("best") or {}
    reasons: list[str] = []
    recommend = str(best.get("variant_id") or "slots3_fifo")

    for n in ABC_V3_SLOT_GRID:
        fifo = fifo_by_slots.get(n) or {}
        topk = topk_by_slots.get(n) or {}
        fd = float(fifo.get("mean_daily_return_pct") or 0)
        td = float(topk.get("mean_daily_return_pct") or 0)
        reasons.append(
            f"slots={n} · FIFO daily {fd:.4f}% capture {fifo.get('signal_capture_pct')}% · "
            f"top-K daily {td:.4f}% capture {topk.get('signal_capture_pct')}% · "
            f"util FIFO {fifo.get('avg_utilization_pct')}%"
        )

    uncon = bt.get("unconstrained_legs") or {}
    reasons.append(
        f"unconstrained all signals: n={uncon.get('n')} leg ret={uncon.get('mean_ret_net_pct')}% "
        f"daily={uncon.get('mean_daily_ret_net_pct')}%"
    )
    return {
        "recommend": recommend,
        "best": best,
        "reasons": reasons,
        "summary": (
            f"ABC v3 slot Phase1 · best **{recommend}** · "
            f"daily {best.get('mean_daily_return_pct')}% · "
            f"CAGR {best.get('cagr_pct')}% · "
            f"capture {best.get('signal_capture_pct')}% · "
            f"n_candidates={bt.get('n_candidates')}"
        ),
    }


def run_abc_v3_slot_phase1_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    bt = backtest_abc_v3_slot_phase1(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_abc_v3_slot_phase1(bt)
    return {
        "schema": "abc_v3_slot_phase1-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "backtest": bt,
        "verdict": verdict,
    }


def render_abc_v3_slot_phase1_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    uncon = bt.get("unconstrained_legs") or {}
    lines = [
        "# ABC v3 + skip 09:00 · slot Phase 1",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## 規格",
        "",
        f"- Matcher: **{bt.get('matcher')}** · hold **{bt.get('hold_days')}d**",
        f"- Execution: **{bt.get('execution_gate')}**",
        f"- Slots: **{bt.get('slot_grid')}** · fill: **{bt.get('fill_modes')}**",
        f"- Capital: **{CAPITAL_PER_SLOT_NTD:,.0f}** NTD / slot",
        f"- Candidates: **{bt.get('n_candidates')}** · unconstrained leg ret **{uncon.get('mean_ret_net_pct')}%** "
        f"(daily **{uncon.get('mean_daily_ret_net_pct')}%**)",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** days "
        f"(tail **{payload.get('intraday_tail_days')}**)",
        f"- Portfolio exit: close on `exit_date` · entry: intraday `entry_px`",
        "",
        "## Variants",
        "",
        "| variant | slots | fill | trades | capture% | util% | mean daily% | CAGR% | Sharpe | maxDD% |",
        "|---------|------:|------|-------:|---------:|------:|------------:|------:|-------:|-------:|",
    ]
    for row in sorted(
        bt.get("variants") or [],
        key=lambda r: (int(r.get("n_slots") or 0), str(r.get("fill_mode") or "")),
    ):
        lines.append(
            f"| {row.get('variant_id')} | {row.get('n_slots')} | {row.get('fill_mode')} | "
            f"{row.get('n_trades')} | {row.get('signal_capture_pct')} | {row.get('avg_utilization_pct')} | "
            f"**{row.get('mean_daily_return_pct')}** | {row.get('cagr_pct')} | "
            f"{row.get('sharpe_ratio')} | {row.get('max_drawdown_pct')} |"
        )
    lines.extend(["", "## Verdict", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", f"- **Recommend**: `{v.get('recommend')}`", "", "模組：`scripts/run_abc_v3_slot_sweep.py`"])
    return "\n".join(lines)


def _rank_values(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_ic(candidates: list[dict[str, Any]]) -> float | None:
    rows = [
        r
        for r in candidates
        if r.get("rank_score") is not None and r.get("net_pct") is not None
    ]
    if len(rows) < 5:
        return None
    xs = [float(r["rank_score"]) for r in rows]
    ys = [float(r["net_pct"]) for r in rows]
    rx = _rank_values(xs)
    ry = _rank_values(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = sum((a - mx) ** 2 for a in rx) ** 0.5
    den_y = sum((b - my) ** 2 for b in ry) ** 0.5
    if den_x <= 1e-12 or den_y <= 1e-12:
        return None
    return round(num / (den_x * den_y), 4)


def quintile_forward_table(
    candidates: list[dict[str, Any]],
    *,
    min_bucket_n: int = 3,
) -> dict[str, Any]:
    rows = sorted(candidates, key=lambda r: float(r.get("rank_score") or 0))
    n = len(rows)
    if n < min_bucket_n * 2:
        return {"n": n, "buckets": [], "monotonic": False, "q5_minus_q1": None}
    buckets: list[dict[str, Any]] = []
    for q in range(5):
        start = q * n // 5
        end = (q + 1) * n // 5
        chunk = rows[start:end]
        nets = [float(r["net_pct"]) for r in chunk if r.get("net_pct") is not None]
        if not nets:
            continue
        wins = sum(1 for x in nets if x > 0)
        buckets.append(
            {
                "quintile": q + 1,
                "n": len(nets),
                "mean_ret_net_pct": round(sum(nets) / len(nets), 3),
                "win_rate_pct": round(100.0 * wins / len(nets), 1),
            }
        )
    means = [float(b["mean_ret_net_pct"]) for b in buckets]
    monotonic = all(means[i] <= means[i + 1] for i in range(len(means) - 1)) if len(means) >= 2 else False
    q5_minus_q1 = round(means[-1] - means[0], 3) if len(means) >= 2 else None
    return {
        "n": n,
        "buckets": buckets,
        "monotonic": monotonic,
        "q5_minus_q1": q5_minus_q1,
    }


def split_train_val_dates(
    sample_dates: list[str],
    *,
    train_days: int = 60,
    val_days: int = 30,
) -> tuple[list[str], list[str]]:
    if len(sample_dates) < train_days + val_days:
        cut = max(1, len(sample_dates) // 3)
        return sample_dates[:-cut], sample_dates[-cut:]
    return sample_dates[-(train_days + val_days) : -val_days], sample_dates[-val_days:]


def _filter_candidates_by_dates(
    candidates: list[dict[str, Any]],
    dates: set[str],
) -> list[dict[str, Any]]:
    return [c for c in candidates if str(c.get("entry_date") or "") in dates]


def _score_spec_metrics(
    candidates: list[dict[str, Any]],
    *,
    spec_id: str,
) -> dict[str, Any]:
    scored = apply_score_spec(candidates, spec_id=spec_id)
    quint = quintile_forward_table(scored)
    return {
        "spec_id": spec_id,
        "n": len(scored),
        "spearman_ic": spearman_ic(scored),
        "quintiles": quint,
        "q5_minus_q1": quint.get("q5_minus_q1"),
        "monotonic": quint.get("monotonic"),
    }


def _portfolio_topk_val(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    val_dates: list[str],
    spec_id: str,
    n_slots: int = ABC_V3_SLOT_CHAMPION,
) -> dict[str, Any]:
    val_set = set(val_dates)
    val_cands = _filter_candidates_by_dates(candidates, val_set)
    scored = apply_score_spec(val_cands, spec_id=spec_id)
    periods = order_periods_for_fill(scored, fill_mode="topk_score")
    close, _, _ = load_price_panels(conn)
    trade_dates = [d for d in close.index.astype(str).tolist() if d in val_set]
    if not trade_dates:
        trade_dates = list(val_dates)
    total_capital = CAPITAL_PER_SLOT_NTD * n_slots
    sim = simulate_slot_portfolio(
        conn,
        close,
        trade_dates,
        periods,
        total_capital=total_capital,
        n_slots=n_slots,
    )
    return {
        "spec_id": spec_id,
        "n_slots": n_slots,
        "n_signals": len(val_cands),
        "n_trades": int(sim.get("n_trades") or 0),
        **sim,
    }


def calibrate_abc_v3_score_phase2(
    conn: sqlite3.Connection,
    *,
    sample_dates: list[str],
    train_days: int = 60,
    val_days: int = 30,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    train_dates, val_dates = split_train_val_dates(
        sample_dates, train_days=train_days, val_days=val_days
    )
    all_candidates, _frames, meta = collect_abc_v3_skip0900_period_candidates(
        conn, dates=sample_dates, etf_codes=etf_codes
    )
    train_set = set(train_dates)
    val_set = set(val_dates)
    train_cands = _filter_candidates_by_dates(all_candidates, train_set)
    val_cands = _filter_candidates_by_dates(all_candidates, val_set)

    train_metrics = [
        _score_spec_metrics(train_cands, spec_id=sid) for sid in ABC_V3_SCORE_SPEC_IDS
    ]
    val_metrics = [_score_spec_metrics(val_cands, spec_id=sid) for sid in ABC_V3_SCORE_SPEC_IDS]

    train_ranked = sorted(
        train_metrics,
        key=lambda r: (
            -(float(r.get("q5_minus_q1") or -999)),
            -(float(r.get("spearman_ic") or -999)),
        ),
    )
    champion_spec = str((train_ranked[0] or {}).get("spec_id") or "v0")

    val_portfolios = [
        _portfolio_topk_val(conn, all_candidates, val_dates=val_dates, spec_id=sid)
        for sid in ABC_V3_SCORE_SPEC_IDS
    ]
    v0_daily = float(
        next((p.get("mean_daily_return_pct") for p in val_portfolios if p.get("spec_id") == "v0"), 0)
        or 0
    )

    return {
        "meta": meta,
        "train_dates": train_dates,
        "val_dates": val_dates,
        "n_candidates_total": len(all_candidates),
        "n_train": len(train_cands),
        "n_val": len(val_cands),
        "score_specs": list(ABC_V3_SCORE_SPEC_IDS),
        "train_metrics": train_metrics,
        "val_metrics": val_metrics,
        "train_champion_spec": champion_spec,
        "val_portfolios": val_portfolios,
        "v0_val_daily_pct": v0_daily,
    }


def verdict_abc_v3_score_phase2(cal: dict[str, Any]) -> dict[str, Any]:
    champion = str(cal.get("train_champion_spec") or "v0")
    val_by_spec = {str(v["spec_id"]): v for v in cal.get("val_metrics") or []}
    champ_val = val_by_spec.get(champion) or {}
    v0_val = val_by_spec.get("v0") or {}
    port_by_spec = {str(p["spec_id"]): p for p in cal.get("val_portfolios") or []}
    champ_port = port_by_spec.get(champion) or {}
    v0_port = port_by_spec.get("v0") or {}

    champ_ic = float(champ_val.get("spearman_ic") or 0)
    champ_q = float(champ_val.get("q5_minus_q1") or 0)
    champ_daily = float(champ_port.get("mean_daily_return_pct") or 0)
    v0_daily = float(v0_port.get("mean_daily_return_pct") or 0)

    passed = (
        champ_ic > 0
        and champ_q > 0
        and champ_val.get("monotonic")
        and champ_daily >= v0_daily - 0.02
    )
    recommend = champion if passed else "v0"
    reasons: list[str] = []
    reasons.append(
        f"train champion **{champion}** · Q5−Q1={champ_val.get('q5_minus_q1')}% · "
        f"IC={champ_val.get('spearman_ic')} · monotonic={champ_val.get('monotonic')}"
    )
    reasons.append(
        f"val portfolio 5槽 top-K · {champion} daily {champ_daily:.4f}% vs v0 {v0_daily:.4f}%"
    )
    no_lead = val_by_spec.get("no_lead") or {}
    if float(no_lead.get("q5_minus_q1") or 0) > float(v0_val.get("q5_minus_q1") or 0) + 0.1:
        reasons.append("no_lead beats v0 on val Q5−Q1 · lead term redundant")
    else:
        reasons.append("no_lead does not beat v0 · lead term stays redundant under ABC v3")

    return {
        "passed": passed,
        "recommend_score_spec": recommend,
        "train_champion_spec": champion,
        "reasons": reasons,
        "summary": (
            f"ABC v3 score Phase2 · recommend **{recommend}** · "
            f"val daily {champ_daily:.4f}% (v0 {v0_daily:.4f}%) · "
            f"{'**passed**' if passed else 'hold v0'}"
        ),
    }


def run_abc_v3_score_calibration_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    train_days: int = 60,
    val_days: int = 30,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    cal = calibrate_abc_v3_score_phase2(
        conn,
        sample_dates=sample_dates,
        train_days=train_days,
        val_days=val_days,
        etf_codes=etf_codes,
    )
    verdict = verdict_abc_v3_score_phase2(cal)
    return {
        "schema": "abc_v3_score_calibration-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "train_days": train_days,
        "val_days": val_days,
        "calibration": cal,
        "verdict": verdict,
    }


def render_abc_v3_score_calibration_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    cal = payload.get("calibration") or {}
    lines = [
        "# ABC v3 · score calibration Phase 2",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Split",
        "",
        f"- Train: **{len(cal.get('train_dates') or [])}** days · n={cal.get('n_train')}",
        f"- Val: **{len(cal.get('val_dates') or [])}** days · n={cal.get('n_val')}",
        f"- Train champion (Q5−Q1): **{cal.get('train_champion_spec')}**",
        "",
        "## Train · score specs",
        "",
        "| spec | n | Spearman IC | Q5−Q1 | monotonic | Q1 ret% | Q5 ret% |",
        "|------|--:|----------:|------:|:---------:|--------:|--------:|",
    ]
    for row in cal.get("train_metrics") or []:
        buckets = (row.get("quintiles") or {}).get("buckets") or []
        q1 = buckets[0]["mean_ret_net_pct"] if buckets else "—"
        q5 = buckets[-1]["mean_ret_net_pct"] if buckets else "—"
        lines.append(
            f"| {row.get('spec_id')} | {row.get('n')} | {row.get('spearman_ic')} | "
            f"{row.get('q5_minus_q1')} | {row.get('monotonic')} | {q1} | {q5} |"
        )
    lines.extend(
        [
            "",
            "## Val · score specs",
            "",
            "| spec | n | Spearman IC | Q5−Q1 | monotonic |",
            "|------|--:|----------:|------:|:---------:|",
        ]
    )
    for row in cal.get("val_metrics") or []:
        lines.append(
            f"| {row.get('spec_id')} | {row.get('n')} | {row.get('spearman_ic')} | "
            f"{row.get('q5_minus_q1')} | {row.get('monotonic')} |"
        )
    lines.extend(
        [
            "",
            f"## Val portfolio · {ABC_V3_SLOT_CHAMPION} slots top-K",
            "",
            "| spec | trades | mean daily% | Sharpe | maxDD% |",
            "|------|-------:|------------:|-------:|-------:|",
        ]
    )
    for row in cal.get("val_portfolios") or []:
        lines.append(
            f"| {row.get('spec_id')} | {row.get('n_trades')} | "
            f"**{row.get('mean_daily_return_pct')}** | {row.get('sharpe_ratio')} | "
            f"{row.get('max_drawdown_pct')} |"
        )
    lines.extend(["", "## Verdict", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(
        [
            "",
            f"- **Recommend score spec**: `{v.get('recommend_score_spec')}`",
            "",
            "模組：`scripts/run_abc_v3_score_calibration.py`",
        ]
    )
    return "\n".join(lines)


def _slot_champion_row(bt: dict[str, Any]) -> dict[str, Any]:
    for row in bt.get("variants") or []:
        if int(row.get("n_slots") or 0) == ABC_V3_SLOT_CHAMPION and row.get("fill_mode") == "topk_score":
            return row
    return {}


def run_abc_v3_f1_pipeline_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    train_days: int = 60,
    val_days: int = 30,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """Embed H-ABC-FILTER-1 in matcher · event OOS + 5-slot top-K vs ABC v3."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    train_dates, val_dates = split_train_val_dates(
        sample_dates, train_days=train_days, val_days=val_days
    )
    val_set = set(val_dates)

    frames, trajectories, meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=sample_dates, etf_codes=etf_codes
    )
    panel_kw = {
        "frames": frames,
        "trajectories": trajectories,
        "meta": meta,
        "stock_maps": stock_maps,
        "bench": bench,
    }

    event_oos = backtest_w3_abc_v3_oos(
        conn, dates=sample_dates, etf_codes=etf_codes, **panel_kw
    )
    summaries = event_oos.get("summaries") or {}
    v3_ev = summaries.get("abc_v3_full") or {}
    f1_ev = summaries.get("abc_v3_f1") or {}

    v3_cands, _f, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=sample_dates,
        etf_codes=etf_codes,
        matcher=matches_w3_rv_hl_abc_v3,
        **panel_kw,
    )
    f1_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=sample_dates,
        etf_codes=etf_codes,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        **panel_kw,
    )
    v3_val_legs = _unconstrained_leg_stats(
        [c for c in v3_cands if str(c.get("entry_date")) in val_set]
    )
    f1_val_legs = _unconstrained_leg_stats(
        [c for c in f1_cands if str(c.get("entry_date")) in val_set]
    )
    val_event_passed = float(f1_val_legs.get("mean_ret_net_pct") or 0) > float(
        v3_val_legs.get("mean_ret_net_pct") or 0
    ) and int(f1_val_legs.get("n") or 0) >= 10

    v3_slot = backtest_abc_v3_slot_phase1(
        conn,
        dates=sample_dates,
        n_slots_grid=(ABC_V3_SLOT_CHAMPION,),
        fill_modes=("topk_score",),
        etf_codes=etf_codes,
        matcher=matches_w3_rv_hl_abc_v3,
        **panel_kw,
    )
    f1_slot = backtest_abc_v3_slot_phase1(
        conn,
        dates=sample_dates,
        n_slots_grid=(ABC_V3_SLOT_CHAMPION,),
        fill_modes=("topk_score",),
        etf_codes=etf_codes,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        **panel_kw,
    )
    v3_slot_row = _slot_champion_row(v3_slot)
    f1_slot_row = _slot_champion_row(f1_slot)

    v3_val_port = _portfolio_topk_val(
        conn, v3_cands, val_dates=val_dates, spec_id="v0", n_slots=ABC_V3_SLOT_CHAMPION
    )
    f1_val_port = _portfolio_topk_val(
        conn, f1_cands, val_dates=val_dates, spec_id="v0", n_slots=ABC_V3_SLOT_CHAMPION
    )
    v3_val_daily = float(v3_val_port.get("mean_daily_return_pct") or 0)
    f1_val_daily = float(f1_val_port.get("mean_daily_return_pct") or 0)
    val_port_passed = f1_val_daily > v3_val_daily and int(f1_val_port.get("n_trades") or 0) >= 5

    reasons: list[str] = []
    reasons.append(
        f"event full 90d · v3 n={v3_ev.get('n')} ret={v3_ev.get('mean_ret_3d_net_pct')}% · "
        f"f1 n={f1_ev.get('n')} ret={f1_ev.get('mean_ret_3d_net_pct')}% · "
        f"Δ {event_oos.get('delta_f1_vs_v3')}pp"
    )
    reasons.append(
        f"event val {len(val_dates)}d · v3 ret={v3_val_legs.get('mean_ret_net_pct')}% n={v3_val_legs.get('n')} · "
        f"f1 ret={f1_val_legs.get('mean_ret_net_pct')}% n={f1_val_legs.get('n')} · "
        f"{'**passed**' if val_event_passed else 'failed'}"
    )
    reasons.append(
        f"5-slot top-K full · v3 daily={v3_slot_row.get('mean_daily_return_pct')}% · "
        f"f1 daily={f1_slot_row.get('mean_daily_return_pct')}% · "
        f"capture {v3_slot_row.get('signal_capture_pct')}%→{f1_slot_row.get('signal_capture_pct')}%"
    )
    reasons.append(
        f"5-slot top-K val · v3 daily={v3_val_daily}% · f1 daily={f1_val_daily}% · "
        f"{'**passed**' if val_port_passed else 'failed'}"
    )

    adoptable = val_event_passed and (
        val_port_passed or f1_val_daily >= v3_val_daily - 0.02
    )
    return {
        "schema": "abc_v3_f1_pipeline-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "intraday_tail_days": intraday_tail_days,
        "intraday_sample_dates": sample_dates,
        "train_dates": train_dates,
        "val_dates": val_dates,
        "matcher_v3": "w3_rv_hl_abc_v3",
        "matcher_f1": "w3_rv_hl_abc_v3_f1",
        "execution_gate": f"min_poll_idx>={ABC_V3_ADOPTED_MIN_POLL_IDX}",
        "hold_days": ABC_V3_HOLD_DAYS,
        "meta": meta,
        "event_oos": event_oos,
        "event_val": {
            "v3": v3_val_legs,
            "f1": f1_val_legs,
            "val_passed": val_event_passed,
        },
        "slot_full": {
            "v3": v3_slot_row,
            "f1": f1_slot_row,
        },
        "slot_val": {
            "v3": v3_val_port,
            "f1": f1_val_port,
            "val_passed": val_port_passed,
        },
        "verdict": {
            "adoptable": adoptable,
            "val_event_passed": val_event_passed,
            "val_port_passed": val_port_passed,
            "reasons": reasons,
            "summary": (
                f"ABC v3+f1 pipeline · event val "
                f"{'**passed**' if val_event_passed else 'failed'} · "
                f"5-slot val {'**passed**' if val_port_passed else 'failed'} · "
                f"f1 n={f1_ev.get('n')} ret={f1_ev.get('mean_ret_3d_net_pct')}% vs v3 "
                f"{v3_ev.get('mean_ret_3d_net_pct')}% · "
                f"{'**adoptable**' if adoptable else 'needs review'}"
            ),
        },
    }


def render_abc_v3_f1_pipeline_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    ev = payload.get("event_oos") or {}
    s = ev.get("summaries") or {}
    v3 = s.get("abc_v3_full") or {}
    f1 = s.get("abc_v3_f1") or {}
    ev_val = payload.get("event_val") or {}
    v3_val = ev_val.get("v3") or {}
    f1_val = ev_val.get("f1") or {}
    slot_full = payload.get("slot_full") or {}
    v3_slot = slot_full.get("v3") or {}
    f1_slot = slot_full.get("f1") or {}
    slot_val = payload.get("slot_val") or {}
    v3_vp = slot_val.get("v3") or {}
    f1_vp = slot_val.get("f1") or {}
    spec = ev.get("spec") or {}

    lines = [
        "# ABC v3+f1 · H-ABC-FILTER-1 embedded pipeline",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Matcher",
        "",
        f"- **v3**: A∩B∩C∩D∩E · skip09 · hold {payload.get('hold_days')}d",
        f"- **f1**: v3 + **F** · {spec.get('F')}",
        f"- Execution: **{payload.get('execution_gate')}**",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** days · "
        f"val **{len(payload.get('val_dates') or [])}** days",
        "",
        "## Event-level · full sample",
        "",
        "| matcher | n | mean ret% | win% | median% |",
        "|---------|--:|----------:|-----:|--------:|",
        f"| v3 | {v3.get('n')} | {v3.get('mean_ret_3d_net_pct')} | {v3.get('win_rate_pct')} | "
        f"{v3.get('median_ret_3d_net_pct')} |",
        f"| **f1** | {f1.get('n')} | **{f1.get('mean_ret_3d_net_pct')}** | {f1.get('win_rate_pct')} | "
        f"{f1.get('median_ret_3d_net_pct')} |",
        f"- Δ f1 vs v3: **{ev.get('delta_f1_vs_v3')}** pp",
        "",
        "## Event-level · val hold-out",
        "",
        "| matcher | n | mean ret% | daily% | win% | 判定 |",
        "|---------|--:|----------:|-------:|-----:|:----:|",
        f"| v3 | {v3_val.get('n')} | {v3_val.get('mean_ret_net_pct')} | "
        f"{v3_val.get('mean_daily_ret_net_pct')} | {v3_val.get('win_rate_pct')} | — |",
        f"| **f1** | {f1_val.get('n')} | **{f1_val.get('mean_ret_net_pct')}** | "
        f"{f1_val.get('mean_daily_ret_net_pct')} | {f1_val.get('win_rate_pct')} | "
        f"{'✅ passed' if ev_val.get('val_passed') else '❌ failed'} |",
        "",
        "## Portfolio · 5 slots top-K score v0",
        "",
        "| window | matcher | trades | daily% | Sharpe | maxDD% | capture% |",
        "|--------|---------|-------:|-------:|-------:|-------:|---------:|",
        f"| full | v3 | {v3_slot.get('n_trades')} | {v3_slot.get('mean_daily_return_pct')} | "
        f"{v3_slot.get('sharpe_ratio')} | {v3_slot.get('max_drawdown_pct')} | "
        f"{v3_slot.get('signal_capture_pct')} |",
        f"| full | **f1** | {f1_slot.get('n_trades')} | **{f1_slot.get('mean_daily_return_pct')}** | "
        f"{f1_slot.get('sharpe_ratio')} | {f1_slot.get('max_drawdown_pct')} | "
        f"{f1_slot.get('signal_capture_pct')} |",
        f"| val | v3 | {v3_vp.get('n_trades')} | {v3_vp.get('mean_daily_return_pct')} | "
        f"{v3_vp.get('sharpe_ratio')} | {v3_vp.get('max_drawdown_pct')} | — |",
        f"| val | **f1** | {f1_vp.get('n_trades')} | **{f1_vp.get('mean_daily_return_pct')}** | "
        f"{f1_vp.get('sharpe_ratio')} | {f1_vp.get('max_drawdown_pct')} | "
        f"{'✅ passed' if slot_val.get('val_passed') else '❌ failed'} |",
        "",
        "## Verdict",
        "",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_abc_v3_f1_pipeline.py`"])
    return "\n".join(lines)


def _prior_trade_date(full_dates: list[str], d: str) -> str | None:
    prior = [x for x in full_dates if x < d]
    return prior[-1] if prior else None


def _breadth_zone_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    nets = [float(r["net_pct"]) for r in rows if r.get("net_pct") is not None]
    if not nets:
        return {"n": 0}
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(nets),
        "mean_ret_net_pct": round(sum(nets) / len(nets), 3),
        "win_rate_pct": round(100.0 * wins / len(nets), 2),
        "median_ret_net_pct": round(statistics.median(nets), 3),
    }


def _g3_breadth_verdict(zone_rows: list[dict[str, Any]], *, min_zone_n: int = 8) -> dict[str, Any]:
    """G3: pass if zones with n≥min_zone_n are not all negative mean ret."""
    checked = [z for z in zone_rows if int(z.get("n") or 0) >= min_zone_n]
    if not checked:
        return {
            "passed": True,
            "min_zone_n": min_zone_n,
            "zones_checked": 0,
            "negative_zones": [],
            "reason": "insufficient per-zone n · defer",
        }
    negative = [
        str(z.get("breadth_zone"))
        for z in checked
        if float(z.get("mean_ret_net_pct") or 0) < 0
    ]
    passed = len(negative) < len(checked)
    return {
        "passed": passed,
        "min_zone_n": min_zone_n,
        "zones_checked": len(checked),
        "negative_zones": negative,
        "reason": (
            f"{len(checked) - len(negative)}/{len(checked)} zones non-negative (n≥{min_zone_n})"
            if passed
            else f"all {len(checked)} zones with n≥{min_zone_n} negative"
        ),
    }


def run_abc_v3_f1_breadth_stratification(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    min_zone_n: int = 8,
    frames: list[dict[str, str]] | None = None,
    trajectories: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    stock_maps: dict[str, list[dict[str, Any]]] | None = None,
    bench: Any | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """G3 breadth zone stratification · ABC v3+f1 event trades · T-1 breadth_zone_200 PIT."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    if candidates is None:
        panel_kw: dict[str, Any] = {}
        if frames is not None and trajectories is not None:
            panel_kw = {
                "frames": frames,
                "trajectories": trajectories,
                "meta": meta,
                "stock_maps": stock_maps,
                "bench": bench,
            }
        else:
            frames, trajectories, meta, stock_maps, bench = _build_abc_v3_intraday_panel(
                conn, dates=sample_dates, etf_codes=etf_codes
            )
            panel_kw = {
                "frames": frames,
                "trajectories": trajectories,
                "meta": meta,
                "stock_maps": stock_maps,
                "bench": bench,
            }
        candidates, _, panel_meta = collect_abc_v3_skip0900_period_candidates(
            conn,
            dates=sample_dates,
            etf_codes=etf_codes,
            matcher=matches_w3_rv_hl_abc_v3_f1,
            **panel_kw,
        )
    else:
        panel_meta = meta or {}

    breadth_start = sample_dates[0] if sample_dates else date_start
    panel = build_breadth_panel(conn, date_start=breadth_start, date_end=end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    tagged: list[dict[str, Any]] = []
    for row in candidates:
        entry_d = str(row.get("entry_date") or "")
        prior = _prior_trade_date(full_dates, entry_d)
        zone = zone_by_date.get(prior or entry_d, "unknown")
        tagged.append({**row, "breadth_zone_200": zone, "breadth_as_of": prior or entry_d})

    by_zone: dict[str, list[dict[str, Any]]] = {z: [] for z in (*BREADTH_ZONES_ORDER, "unknown")}
    for row in tagged:
        by_zone.setdefault(str(row.get("breadth_zone_200") or "unknown"), []).append(row)

    zone_rows: list[dict[str, Any]] = []
    for zone in (*BREADTH_ZONES_ORDER, "unknown"):
        stats = _breadth_zone_stats(by_zone.get(zone) or [])
        if int(stats.get("n") or 0) == 0:
            continue
        zone_rows.append(
            {
                "breadth_zone": zone,
                "display": BREADTH_ZONE_DISPLAY.get(zone, zone),
                "zh": BREADTH_ZONE_ZH.get(zone, zone),
                **stats,
            }
        )

    verdict = _g3_breadth_verdict(zone_rows, min_zone_n=min_zone_n)
    return {
        "schema": "abc_v3_f1_breadth_strat-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "intraday_tail_days": intraday_tail_days,
        "sample_dates": sample_dates,
        "matcher": "w3_rv_hl_abc_v3_f1",
        "hold_days": ABC_V3_HOLD_DAYS,
        "execution_gate": f"min_poll_idx>={ABC_V3_ADOPTED_MIN_POLL_IDX}",
        "breadth_axis": "breadth_zone_200",
        "breadth_timing": "T-1 prior trading day close (PIT-safe at intraday entry)",
        "meta": panel_meta,
        "n_trades": len(tagged),
        "by_zone": zone_rows,
        "verdict": verdict,
    }


def render_abc_v3_f1_breadth_strat_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# ABC v3+f1 · Breadth zone（廣度區間）stratification · G3",
        "",
        f"> matcher `{payload.get('matcher')}` · n={payload.get('n_trades')} · "
        f"hold {payload.get('hold_days')}d · {payload.get('breadth_timing')}",
        "",
        f"- **Axis**: `breadth_zone_200` · sample **{len(payload.get('sample_dates') or [])}** days",
        f"- **Execution**: {payload.get('execution_gate')}",
        "",
        "## Per-zone event returns",
        "",
        "| zone | n | mean ret% | win% | median% |",
        "|------|--:|----------:|-----:|--------:|",
    ]
    for row in payload.get("by_zone") or []:
        lines.append(
            f"| {row.get('display')} | {row.get('n')} | {row.get('mean_ret_net_pct')} | "
            f"{row.get('win_rate_pct')} | {row.get('median_ret_net_pct')} |"
        )
    lines.extend(
        [
            "",
            "## G3 verdict",
            "",
            f"- **Passed**: {'✅ yes' if v.get('passed') else '❌ no'} · {v.get('reason')}",
            f"- Zones checked (n≥{v.get('min_zone_n')}): {v.get('zones_checked')}",
        ]
    )
    if v.get("negative_zones"):
        lines.append(f"- Negative zones: {', '.join(v['negative_zones'])}")
    lines.extend(["", "模組：`scripts/run_abc_v3_f1_pipeline.py --breadth-strat`"])
    return "\n".join(lines)


def _candidate_day_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("stock_id") or ""), str(row.get("entry_date") or "")


def _mean_ret_pct(rows: list[dict[str, Any]]) -> float | None:
    nets = [float(r["net_pct"]) for r in rows if r.get("net_pct") is not None]
    if not nets:
        return None
    return round(sum(nets) / len(nets), 3)


def slot_val_overlap_stats(
    v3_candidates: list[dict[str, Any]],
    f1_candidates: list[dict[str, Any]],
    val_dates: list[str],
    *,
    n_slots: int = ABC_V3_SLOT_CHAMPION,
    spec_id: str = "v0",
) -> dict[str, Any]:
    """Pure overlap / per-val-day stats for f1 vs v3 (f1 ⊆ v3)."""
    val_set = set(val_dates)
    v3_val = [c for c in v3_candidates if str(c.get("entry_date") or "") in val_set]
    f1_val = [c for c in f1_candidates if str(c.get("entry_date") or "") in val_set]
    f1_keys = {_candidate_day_key(c) for c in f1_val}
    dropped = [c for c in v3_val if _candidate_day_key(c) not in f1_keys]

    by_v3: dict[str, list[dict[str, Any]]] = {}
    by_f1: dict[str, list[dict[str, Any]]] = {}
    for row in v3_val:
        by_v3.setdefault(str(row["entry_date"]), []).append(row)
    for row in f1_val:
        by_f1.setdefault(str(row["entry_date"]), []).append(row)

    per_day: list[dict[str, Any]] = []
    days_f1_thinner = 0
    days_v3_crowded = 0
    topk_ret_gap_sum = 0.0
    topk_ret_gap_n = 0

    for d in val_dates:
        v3_day = by_v3.get(d, [])
        f1_day = by_f1.get(d, [])
        dropped_day = [c for c in v3_day if _candidate_day_key(c) not in f1_keys]
        v3_scored = apply_score_spec(v3_day, spec_id=spec_id)
        f1_scored = apply_score_spec(f1_day, spec_id=spec_id)
        v3_topk = order_periods_for_fill(v3_scored, fill_mode="topk_score")[:n_slots]
        f1_topk = order_periods_for_fill(f1_scored, fill_mode="topk_score")[:n_slots]
        v3_topk_ret = _mean_ret_pct(v3_topk)
        f1_topk_ret = _mean_ret_pct(f1_topk)
        if len(f1_day) < len(v3_day):
            days_f1_thinner += 1
        if len(v3_day) > n_slots:
            days_v3_crowded += 1
        if v3_topk_ret is not None and f1_topk_ret is not None and f1_day:
            topk_ret_gap_sum += v3_topk_ret - f1_topk_ret
            topk_ret_gap_n += 1
        topk_overlap = len(
            {_candidate_day_key(r) for r in v3_topk} & {_candidate_day_key(r) for r in f1_topk}
        )
        per_day.append(
            {
                "date": d,
                "v3_n": len(v3_day),
                "f1_n": len(f1_day),
                "dropped_n": len(dropped_day),
                "shared_n": len(f1_day),
                "v3_mean_ret_pct": _mean_ret_pct(v3_day),
                "f1_mean_ret_pct": _mean_ret_pct(f1_day),
                "dropped_mean_ret_pct": _mean_ret_pct(dropped_day),
                "kept_mean_ret_pct": _mean_ret_pct(f1_day),
                "v3_topk_mean_ret_pct": v3_topk_ret,
                "f1_topk_mean_ret_pct": f1_topk_ret,
                "topk_overlap_n": topk_overlap,
            }
        )

    dropped_ret = _mean_ret_pct(dropped)
    kept_ret = _mean_ret_pct(f1_val)
    return {
        "val_dates": list(val_dates),
        "n_slots": n_slots,
        "spec_id": spec_id,
        "totals": {
            "v3_signals": len(v3_val),
            "f1_signals": len(f1_val),
            "dropped_by_f1": len(dropped),
            "shared_kept": len(f1_val),
            "dropped_mean_ret_pct": dropped_ret,
            "kept_mean_ret_pct": kept_ret,
            "delta_dropped_minus_kept_pct": (
                round(dropped_ret - kept_ret, 3)
                if dropped_ret is not None and kept_ret is not None
                else None
            ),
        },
        "per_day": per_day,
        "pool_stats": {
            "days_f1_thinner_than_v3": days_f1_thinner,
            "days_v3_more_than_slots": days_v3_crowded,
            "mean_v3_topk_minus_f1_topk_pct": (
                round(topk_ret_gap_sum / topk_ret_gap_n, 3) if topk_ret_gap_n else None
            ),
            "days_with_both_topk": topk_ret_gap_n,
        },
    }


def _slot_val_breadth_val_window(
    conn: sqlite3.Connection,
    *,
    v3_val: list[dict[str, Any]],
    f1_val: list[dict[str, Any]],
    val_dates: list[str],
    date_start: str,
    date_end: str,
) -> list[dict[str, Any]]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    def _tag(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            entry_d = str(row.get("entry_date") or "")
            prior = _prior_trade_date(full_dates, entry_d)
            zone = zone_by_date.get(prior or entry_d, "unknown")
            out.setdefault(zone, []).append(row)
        return out

    v3_by = _tag(v3_val)
    f1_by = _tag(f1_val)
    zones = sorted(set(v3_by) | set(f1_by))
    rows: list[dict[str, Any]] = []
    for zone in zones:
        v3_stats = _breadth_zone_stats(v3_by.get(zone) or [])
        f1_stats = _breadth_zone_stats(f1_by.get(zone) or [])
        if int(v3_stats.get("n") or 0) == 0 and int(f1_stats.get("n") or 0) == 0:
            continue
        rows.append(
            {
                "breadth_zone": zone,
                "display": BREADTH_ZONE_DISPLAY.get(zone, zone),
                "v3_n": v3_stats.get("n"),
                "v3_mean_ret_pct": v3_stats.get("mean_ret_net_pct"),
                "f1_n": f1_stats.get("n"),
                "f1_mean_ret_pct": f1_stats.get("mean_ret_net_pct"),
            }
        )
    return rows


def _slot_val_diagnostic_verdict(
    overlap: dict[str, Any],
    *,
    v3_val_port: dict[str, Any],
    f1_val_port: dict[str, Any],
    breadth_val: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = overlap.get("totals") or {}
    pool = overlap.get("pool_stats") or {}
    dropped_delta = totals.get("delta_dropped_minus_kept_pct")
    topk_gap = pool.get("mean_v3_topk_minus_f1_topk_pct")
    v3_daily = float(v3_val_port.get("mean_daily_return_pct") or 0)
    f1_daily = float(f1_val_port.get("mean_daily_return_pct") or 0)

    hypotheses: list[dict[str, Any]] = []
    # H1: fewer signals
    h1_support = (
        int(pool.get("days_f1_thinner_than_v3") or 0) >= 10
        and topk_gap is not None
        and topk_gap > 0.05
    )
    hypotheses.append(
        {
            "id": "H1_thinner_pool",
            "statement": "Fewer f1 signals worsen top-K selection on val days",
            "supported": h1_support,
            "evidence": (
                f"days f1 thinner={pool.get('days_f1_thinner_than_v3')} · "
                f"mean v3_topk−f1_topk={topk_gap}pp · crowded days={pool.get('days_v3_more_than_slots')}"
            ),
        }
    )
    # H2: score mis-rank — dropped trades outperform kept
    h2_support = dropped_delta is not None and dropped_delta > 0.15
    hypotheses.append(
        {
            "id": "H2_score_v0",
            "statement": "Score v0 ranks f1 pool poorly vs dropped v3-only trades",
            "supported": h2_support,
            "evidence": (
                f"dropped mean={totals.get('dropped_mean_ret_pct')}% · "
                f"kept mean={totals.get('kept_mean_ret_pct')}% · "
                f"Δ dropped−kept={dropped_delta}pp"
            ),
        }
    )
    # H3: val breadth — f1 underperforms v3 in dominant val zones
    val_zones = [z for z in breadth_val if int(z.get("v3_n") or 0) >= 5]
    h3_support = any(
        float(z.get("f1_mean_ret_pct") or 0) < float(z.get("v3_mean_ret_pct") or 0) - 0.3
        for z in val_zones
    )
    hypotheses.append(
        {
            "id": "H3_val_regime",
            "statement": "Val window breadth/regime mismatch vs full-sample G3",
            "supported": h3_support,
            "evidence": "; ".join(
                f"{z.get('breadth_zone')}: v3 {z.get('v3_mean_ret_pct')}% f1 {z.get('f1_mean_ret_pct')}%"
                for z in val_zones
            ),
        }
    )

    supported = [h["id"] for h in hypotheses if h.get("supported")]
    if h2_support:
        recommendation = "fix_score_spec"
        reason = "F gate drops higher-forward-ret names; v0 top-K on val cannot recover event alpha."
    elif h1_support and not h3_support:
        recommendation = "fix_score_spec"
        reason = "Thinner pool + positive top-K gap on val days; recalibrate rank for f1 subset."
    elif h3_support and not h2_support:
        recommendation = "accept_event_only"
        reason = "Event alpha holds; val slot gap aligns with breadth slice — defer slot sizing."
    else:
        recommendation = "accept_event_only"
        reason = (
            f"Event val passed ({f1_daily:.3f}% daily f1 vs {v3_daily:.3f}% v3 failed) · "
            "full 90d slot f1 passed — treat 5-slot val as inconclusive; keep event-only observe."
        )

    return {
        "hypotheses": hypotheses,
        "supported": supported,
        "recommendation": recommendation,
        "reason": reason,
        "v3_val_daily_pct": v3_daily,
        "f1_val_daily_pct": f1_daily,
    }


def run_abc_v3_f1_slot_val_diagnostic(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    train_days: int = 60,
    val_days: int = 30,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    frames: list[dict[str, str]] | None = None,
    trajectories: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    stock_maps: dict[str, list[dict[str, Any]]] | None = None,
    bench: Any | None = None,
) -> dict[str, Any]:
    """Why f1 loses 5-slot val despite event-level val win."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    train_dates, val_dates = split_train_val_dates(
        sample_dates, train_days=train_days, val_days=val_days
    )
    val_set = set(val_dates)

    if frames is None or trajectories is None:
        frames, trajectories, meta, stock_maps, bench = _build_abc_v3_intraday_panel(
            conn, dates=sample_dates, etf_codes=etf_codes
        )
    panel_kw = {
        "frames": frames,
        "trajectories": trajectories,
        "meta": meta or {},
        "stock_maps": stock_maps,
        "bench": bench,
    }

    v3_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn, dates=sample_dates, etf_codes=etf_codes, matcher=matches_w3_rv_hl_abc_v3, **panel_kw
    )
    f1_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=sample_dates,
        etf_codes=etf_codes,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        **panel_kw,
    )
    v3_val = [c for c in v3_cands if str(c.get("entry_date") or "") in val_set]
    f1_val = [c for c in f1_cands if str(c.get("entry_date") or "") in val_set]

    overlap = slot_val_overlap_stats(v3_cands, f1_cands, val_dates)
    v3_val_port = _portfolio_topk_val(
        conn, v3_cands, val_dates=val_dates, spec_id="v0", n_slots=ABC_V3_SLOT_CHAMPION
    )
    f1_val_port = _portfolio_topk_val(
        conn, f1_cands, val_dates=val_dates, spec_id="v0", n_slots=ABC_V3_SLOT_CHAMPION
    )
    breadth_val = _slot_val_breadth_val_window(
        conn,
        v3_val=v3_val,
        f1_val=f1_val,
        val_dates=val_dates,
        date_start=sample_dates[0] if sample_dates else date_start,
        date_end=end,
    )
    verdict = _slot_val_diagnostic_verdict(
        overlap,
        v3_val_port=v3_val_port,
        f1_val_port=f1_val_port,
        breadth_val=breadth_val,
    )

    return {
        "schema": "abc_v3_f1_slot_val_diagnostic-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "intraday_tail_days": intraday_tail_days,
        "train_dates": train_dates,
        "val_dates": val_dates,
        "matcher_v3": "w3_rv_hl_abc_v3",
        "matcher_f1": "w3_rv_hl_abc_v3_f1",
        "execution_gate": f"min_poll_idx>={ABC_V3_ADOPTED_MIN_POLL_IDX}",
        "hold_days": ABC_V3_HOLD_DAYS,
        "meta": panel_kw.get("meta") or {},
        "overlap": overlap,
        "slot_val_port": {"v3": v3_val_port, "f1": f1_val_port},
        "breadth_val_window": breadth_val,
        "verdict": verdict,
    }


def render_abc_v3_f1_slot_val_diagnostic_md(payload: dict[str, Any]) -> str:
    overlap = payload.get("overlap") or {}
    totals = overlap.get("totals") or {}
    pool = overlap.get("pool_stats") or {}
    v = payload.get("verdict") or {}
    v3p = (payload.get("slot_val_port") or {}).get("v3") or {}
    f1p = (payload.get("slot_val_port") or {}).get("f1") or {}

    lines = [
        "# ABC v3+f1 · 5-slot val failure diagnostic",
        "",
        f"> {v.get('reason')}",
        "",
        "## Totals (val hold-out)",
        "",
        "| metric | v3 | f1 |",
        "|--------|---:|---:|",
        f"| signals | {totals.get('v3_signals')} | {totals.get('f1_signals')} |",
        f"| dropped by F gate | {totals.get('dropped_by_f1')} | — |",
        f"| mean ret% (unconstrained) | — | kept {totals.get('kept_mean_ret_pct')}% · dropped {totals.get('dropped_mean_ret_pct')}% |",
        f"| 5-slot daily% | {v3p.get('mean_daily_return_pct')} | {f1p.get('mean_daily_return_pct')} |",
        f"| 5-slot trades | {v3p.get('n_trades')} | {f1p.get('n_trades')} |",
        "",
        "## Pool / top-K",
        "",
        f"- Days f1 thinner than v3: **{pool.get('days_f1_thinner_than_v3')}** / {len(overlap.get('val_dates') or [])}",
        f"- Days v3 signals > 5 slots: **{pool.get('days_v3_more_than_slots')}**",
        f"- Mean v3_topK − f1_topK ret (days with f1 signals): **{pool.get('mean_v3_topk_minus_f1_topk_pct')}** pp",
        "",
        "## Hypotheses",
        "",
        "| id | supported | evidence |",
        "|----|:---------:|----------|",
    ]
    for h in v.get("hypotheses") or []:
        mark = "✅" if h.get("supported") else "—"
        lines.append(f"| {h.get('id')} | {mark} | {h.get('evidence')} |")
    lines.extend(
        [
            "",
            f"**Recommendation**: `{v.get('recommendation')}`",
            "",
            "## Per-val-day signals",
            "",
            "| date | v3_n | f1_n | dropped | v3 mean% | f1 mean% | v3 topK% | f1 topK% |",
            "|------|-----:|-----:|--------:|---------:|---------:|---------:|---------:|",
        ]
    )
    for row in overlap.get("per_day") or []:
        lines.append(
            f"| {row.get('date')} | {row.get('v3_n')} | {row.get('f1_n')} | {row.get('dropped_n')} | "
            f"{row.get('v3_mean_ret_pct')} | {row.get('f1_mean_ret_pct')} | "
            f"{row.get('v3_topk_mean_ret_pct')} | {row.get('f1_topk_mean_ret_pct')} |"
        )
    if payload.get("breadth_val_window"):
        lines.extend(
            [
                "",
                "## Val-window breadth (T-1)",
                "",
                "| zone | v3 n | v3 mean% | f1 n | f1 mean% |",
                "|------|-----:|---------:|-----:|---------:|",
            ]
        )
        for row in payload.get("breadth_val_window") or []:
            lines.append(
                f"| {row.get('display')} | {row.get('v3_n')} | {row.get('v3_mean_ret_pct')} | "
                f"{row.get('f1_n')} | {row.get('f1_mean_ret_pct')} |"
            )
    lines.extend(["", "模組：`scripts/run_abc_v3_f1_pipeline.py --slot-val-diagnostic`"])
    return "\n".join(lines)
