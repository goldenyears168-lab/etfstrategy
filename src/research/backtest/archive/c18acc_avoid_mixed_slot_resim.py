"""C18acc S2 · avoid spread_mixed entry gate · slot re-sim vs post-hoc overlay."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    IS_END_DEFAULT,
    OOS_START_DEFAULT,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_score_swap_c import (
    build_pit_candidate_pool,
    candidate_shortlist_is_passthrough,
    simulate_score_swap_c,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
    spread_class_snapshots_at_minute,
)


DEFAULT_TOTAL_CAPITAL = 100_000.0


def _serialize_gate(gate: dict[str, set[str]]) -> dict[str, list[str]]:
    return {d: sorted(sids) for d, sids in sorted(gate.items())}


def _deserialize_gate(raw: dict[str, list[str]]) -> dict[str, set[str]]:
    return {d: set(sids) for d, sids in raw.items()}


def save_gate_cache(
    path: str,
    *,
    gate: dict[str, set[str]],
    meta: dict[str, Any],
    date_start: str,
    date_end: str,
) -> None:
    import json
    from pathlib import Path

    payload = {
        "schema": "c18acc_avoid_mixed_gate_cache-v1",
        "date_start": date_start,
        "date_end": date_end,
        "gate_meta": meta,
        "gate": _serialize_gate(gate),
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_gate_cache(path: str) -> tuple[dict[str, set[str]], dict[str, Any], str, str]:
    import json
    from pathlib import Path

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return (
        _deserialize_gate(payload["gate"]),
        payload.get("gate_meta") or {},
        str(payload["date_start"]),
        str(payload["date_end"]),
    )


def _portfolio_slice(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    trade_dates: list[str],
    *,
    start: str,
    end: str,
    n_slots: int,
    close,
    total_capital: float = DEFAULT_TOTAL_CAPITAL,
) -> dict[str, Any]:
    sub_dates = [d for d in trade_dates if start <= d <= end]
    if len(sub_dates) < 5:
        return {"n_trade_dates": len(sub_dates)}
    sub_periods = _slice_periods(periods, start=start, end=end)
    port = portfolio_metrics_for_periods(
        conn,
        sub_periods,
        sub_dates,
        total_capital=total_capital,
        n_slots=n_slots,
        close=close,
    )
    return {
        "n_trade_dates": len(sub_dates),
        "n_periods": len(sub_periods),
        "total_return_pct": port.get("total_return_pct"),
        "cagr_pct": port.get("cagr_pct"),
        "ann_mean_return_pct": port.get("ann_mean_return_pct"),
        "sharpe_ratio": port.get("sharpe_ratio"),
        "max_drawdown_pct": port.get("max_drawdown_pct"),
        "avg_utilization_pct": port.get("avg_utilization_pct"),
    }


def _attach_portfolio_slices(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    periods: list[dict[str, Any]],
    trade_dates: list[str],
    *,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str | None,
    n_slots: int,
    close,
    total_capital: float,
) -> None:
    row["portfolio"] = {
        "total_capital_ntd": total_capital,
        "full": _portfolio_slice(
            conn,
            periods,
            trade_dates,
            start=date_start,
            end=date_end,
            n_slots=n_slots,
            close=close,
            total_capital=total_capital,
        ),
        "is": _portfolio_slice(
            conn,
            periods,
            trade_dates,
            start=date_start,
            end=is_end,
            n_slots=n_slots,
            close=close,
            total_capital=total_capital,
        ),
    }
    if oos_start:
        row["portfolio"]["oos"] = _portfolio_slice(
            conn,
            periods,
            trade_dates,
            start=oos_start,
            end=date_end,
            n_slots=n_slots,
            close=close,
            total_capital=total_capital,
        )


def _nav_verdict(variants: list[dict[str, Any]]) -> dict[str, Any]:
    if len(variants) < 2:
        return {"summary": "insufficient variants"}
    base = variants[0]
    gated = variants[1]
    deltas: dict[str, Any] = {}
    for win in ("full", "is", "oos"):
        bp = (base.get("portfolio") or {}).get(win) or {}
        gp = (gated.get("portfolio") or {}).get(win) or {}
        bt, gt = bp.get("total_return_pct"), gp.get("total_return_pct")
        if bt is not None and gt is not None:
            deltas[f"delta_{win}_total_return_pp"] = round(float(gt) - float(bt), 4)
    oos_delta = deltas.get("delta_oos_total_return_pp")
    full_delta = deltas.get("delta_full_total_return_pp")
    adopt_nav = bool(
        oos_delta is not None
        and float(oos_delta) >= 0.0
        and full_delta is not None
        and float(full_delta) >= -2.0
    )
    return {
        **deltas,
        "recommend_adopt_nav": adopt_nav,
        "summary": (
            f"NAV FULL Δtotal={full_delta}pp · OOS Δtotal={oos_delta}pp "
            f"({'pass' if adopt_nav else 'fail'})"
        ),
    }


def _entry_poll_minutes() -> list[str]:
    """30m poll · aligned with c18acc_entry_winloss_profile discovery."""
    return intraday_poll_minutes()


def _candidate_stocks_by_date(
    ctx: dict[str, Any],
    trade_dates: list[str],
    *,
    cfg,
    passthrough_pool: bool = False,
) -> dict[str, set[str]]:
    by_date: dict[str, set[str]] = {}
    for as_of in trade_dates:
        fresh_mono = ctx["fresh_by_date"].get(as_of, [])
        mono_rows = ctx["mono_by_date"].get(as_of, [])
        pool = build_pit_candidate_pool(
            fresh_mono=fresh_mono,
            mono_rows=mono_rows,
            rs_ratio=ctx["rs_ratio"],
            rs_mom=ctx["rs_mom"],
            full_dates=ctx["full_dates"],
            as_of=as_of,
            config=cfg,
        )
        if passthrough_pool or candidate_shortlist_is_passthrough(cfg):
            by_date[as_of] = {row.stock_id for row in pool}
        else:
            top_n = max(1, int(cfg.candidate_top_n))
            by_date[as_of] = {row.stock_id for row in pool[:top_n]}
    return by_date


def _spread_at_first_poll(
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    *,
    trade_date: str,
    no_trade_before: str = "09:30",
) -> str | None:
    for i, f in enumerate(frames):
        if f["date"] != trade_date:
            continue
        if f["minute"] < no_trade_before:
            continue
        pt = points_by_idx[i] if i < len(points_by_idx) else None
        if not pt:
            continue
        return str(pt.get("spread_class") or "") or None
    return None


def build_avoid_mixed_gate_lookup(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    ctx: dict[str, Any],
    cfg,
    no_trade_before: str = "09:30",
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Per-day whitelist: candidates with spread_class != mixed at first entry poll."""
    candidates_by_date = _candidate_stocks_by_date(ctx, trade_dates, cfg=cfg)
    stock_ids = sorted({sid for sids in candidates_by_date.values() for sid in sids})
    if not stock_ids:
        return {}, {"n_stocks": 0, "n_dates": 0}

    poll_minutes = _entry_poll_minutes()
    frames, trajectories, traj_meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=trade_dates,
        stock_ids=stock_ids,
        poll_minutes=poll_minutes,
        micro_lengths=(WMA_MICRO_LENGTH,),
    )
    from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps

    stock_maps = _build_stock_frame_maps(trajectories, frames)

    gate: dict[str, set[str]] = {}
    mixed_counts: dict[str, int] = defaultdict(int)
    total_counts: dict[str, int] = defaultdict(int)
    for d in trade_dates:
        allowed: set[str] = set()
        cands = candidates_by_date.get(d) or set()
        for sid in cands:
            pts = stock_maps.get(sid)
            if not pts:
                allowed.add(sid)
                continue
            sc = _spread_at_first_poll(
                frames,
                pts,
                trade_date=d,
                no_trade_before=no_trade_before,
            )
            total_counts["all"] += 1
            if sc == "mixed":
                mixed_counts["mixed"] += 1
                continue
            allowed.add(sid)
        gate[d] = allowed

    meta = {
        "n_stocks": len(stock_ids),
        "n_dates": len(trade_dates),
        "n_frames": len(frames),
        "poll_interval": "30m",
        "no_trade_before": no_trade_before,
        "candidate_mixed_pct": round(
            100.0 * mixed_counts["mixed"] / max(total_counts["all"], 1),
            2,
        ),
        "trajectory_meta": traj_meta,
    }
    return gate, meta


def build_avoid_mixed_gate_lookup_live(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    ctx: dict[str, Any],
    cfg,
    no_trade_before: str = "09:30",
    gate_anchor_minute: str = "09:30",
    passthrough_pool: bool = True,
    progress_every: int = 25,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Order layer aligned gate · poll_5m sim · full POOL1 passthrough · W5/W20 @ anchor."""
    candidates_by_date = _candidate_stocks_by_date(
        ctx,
        trade_dates,
        cfg=cfg,
        passthrough_pool=passthrough_pool,
    )
    close = ctx["close"]
    bench = ctx["bench"]
    gate: dict[str, set[str]] = {}
    mixed_counts: dict[str, int] = defaultdict(int)
    total_counts: dict[str, int] = defaultdict(int)
    n_days = len(trade_dates)
    for i, d in enumerate(trade_dates):
        if i == 0 or (i + 1) % progress_every == 0 or i + 1 == n_days:
            print(f"  live spread gate {i + 1}/{n_days} · {d}", flush=True)
        cands = sorted(candidates_by_date.get(d) or [])
        if not cands:
            gate[d] = set()
            continue
        snaps = spread_class_snapshots_at_minute(
            conn,
            close=close,
            bench=bench,
            trade_date=d,
            minute=gate_anchor_minute,
            stock_ids=cands,
        )
        allowed: set[str] = set()
        for sid in cands:
            total_counts["all"] += 1
            sc = snaps.get(sid)
            if sc is None:
                allowed.add(sid)
                continue
            if sc == "mixed":
                mixed_counts["mixed"] += 1
                continue
            allowed.add(sid)
        gate[d] = allowed

    meta = {
        "gate_mode": "live_order_layer",
        "n_stocks_union": len({sid for sids in candidates_by_date.values() for sid in sids}),
        "n_dates": len(trade_dates),
        "poll_interval": "5m_sim",
        "gate_anchor_minute": gate_anchor_minute,
        "no_trade_before": no_trade_before,
        "passthrough_pool": passthrough_pool,
        "candidate_mixed_pct": round(
            100.0 * mixed_counts["mixed"] / max(total_counts["all"], 1),
            2,
        ),
    }
    return gate, meta


def _slice_periods(
    periods: list[dict[str, Any]],
    *,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    return [
        p
        for p in periods
        if start <= str(p.get("entry_date") or "") <= end
    ]


def _window_stats(periods: list[dict[str, Any]]) -> dict[str, Any]:
    s = summarize_periods(periods)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    s["n_periods"] = len(periods)
    s["mean_excess_pct"] = (
        round(float(mr) - float(mb), 4) if mr is not None and mb is not None else None
    )
    return s


def _run_sim_variant(
    conn: sqlite3.Connection,
    *,
    ctx: dict[str, Any],
    trade_dates: list[str],
    cfg,
    entry_gate_by_date: dict[str, set[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return simulate_score_swap_c(
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
        entry_gate_by_date=entry_gate_by_date,
        swap_gate_by_date=entry_gate_by_date,
    )


def run_c18acc_avoid_mixed_slot_resim(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    total_capital: float = DEFAULT_TOTAL_CAPITAL,
    gate_cache_path: str | None = None,
    save_gate_cache_path: str | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    ctx = _prepare_c18acc_context(conn, trade_dates)

    cfg = c18acc_live_s2_config(n_slots=n_slots)
    effective_oos = next((d for d in trade_dates if d >= oos_start), None)

    gate_lookup: dict[str, set[str]] | None = None
    gate_meta: dict[str, Any] = {}
    if gate_cache_path:
        from pathlib import Path

        if Path(gate_cache_path).is_file():
            print(f"loading gate cache {gate_cache_path} …", flush=True)
            gate_lookup, gate_meta, c0, c1 = load_gate_cache(gate_cache_path)
            if c0 != trade_dates[0] or c1 != trade_dates[-1]:
                raise ValueError(
                    f"gate cache window {c0}..{c1} != requested {trade_dates[0]}..{trade_dates[-1]}"
                )
        elif gate_cache_path:
            print(f"gate cache missing · will build and save to {gate_cache_path}", flush=True)

    if gate_lookup is None:
        print(
            f"avoid_mixed slot re-sim: {len(trade_dates)} td · building 30m spread gate …",
            flush=True,
        )
        gate_lookup, gate_meta = build_avoid_mixed_gate_lookup(
            conn,
            trade_dates=trade_dates,
            ctx=ctx,
            cfg=cfg,
            no_trade_before=str(cfg.no_trade_before or "09:30"),
        )
        out_cache = save_gate_cache_path or gate_cache_path
        if out_cache:
            save_gate_cache(
                out_cache,
                gate=gate_lookup,
                meta=gate_meta,
                date_start=trade_dates[0],
                date_end=trade_dates[-1],
            )
            print(f"saved gate cache → {out_cache}", flush=True)

    variants: list[dict[str, Any]] = []
    for vid, label, gate in (
        ("baseline", "baseline · C18acc S2", None),
        ("avoid_mixed", "avoid spread_mixed · slot re-sim", gate_lookup),
    ):
        print(f"  sim {vid} …", flush=True)
        periods, summary = _run_sim_variant(
            conn,
            ctx=ctx,
            trade_dates=trade_dates,
            cfg=cfg,
            entry_gate_by_date=gate,
        )
        row: dict[str, Any] = {
            "variant_id": vid,
            "label": label,
            "sim_summary": summary,
            "slices": {
                "full": _window_stats(periods),
                "is": _window_stats(_slice_periods(periods, start=date_start, end=is_end)),
            },
        }
        if effective_oos:
            row["slices"]["oos"] = _window_stats(
                _slice_periods(periods, start=effective_oos, end=end)
            )
        else:
            row["slices"]["oos"] = {"n_periods": 0, "mean_excess_pct": None}
        _attach_portfolio_slices(
            conn,
            row,
            periods,
            trade_dates,
            date_start=date_start,
            date_end=end,
            is_end=is_end,
            oos_start=effective_oos,
            n_slots=n_slots,
            close=close,
            total_capital=total_capital,
        )
        variants.append(row)

    base = variants[0]
    base_oos_ex = (base["slices"].get("oos") or {}).get("mean_excess_pct")
    base_is_ex = (base["slices"].get("is") or {}).get("mean_excess_pct")
    for v in variants[1:]:
        oos_ex = (v["slices"].get("oos") or {}).get("mean_excess_pct")
        is_ex = (v["slices"].get("is") or {}).get("mean_excess_pct")
        v["delta_oos_excess_pp"] = (
            round(float(oos_ex or 0) - float(base_oos_ex or 0), 4)
            if oos_ex is not None and base_oos_ex is not None
            else None
        )
        v["delta_is_excess_pp"] = (
            round(float(is_ex or 0) - float(base_is_ex or 0), 4)
            if is_ex is not None and base_is_ex is not None
            else None
        )

    adopt = variants[1]
    oos_n = int((adopt["slices"].get("oos") or {}).get("n_periods") or 0)
    oos_delta = adopt.get("delta_oos_excess_pp")
    is_delta = adopt.get("delta_is_excess_pp")
    passes = bool(
        oos_n >= 8
        and oos_delta is not None
        and float(oos_delta) >= 0.5
        and (is_delta is None or float(is_delta) >= -0.3)
    )

    nav = _nav_verdict(variants)

    return {
        "schema": "c18acc_avoid_mixed_slot_resim-v2",
        "topic": "c18acc-avoid-mixed-slot-resim",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": effective_oos,
        "n_slots": n_slots,
        "total_capital_ntd": total_capital,
        "gate_meta": gate_meta,
        "variants": variants,
        "verdict": {
            "slot_resim": True,
            "post_hoc_reference": "20260711_c18acc_entry_winloss_oos_overlay",
            "recommend_adopt_legs": passes,
            "recommend_adopt_nav": nav.get("recommend_adopt_nav"),
            "leg_summary": (
                f"slot re-sim avoid_mixed OOS Δexcess={oos_delta}pp n={oos_n} "
                f"({'pass' if passes else 'fail'})"
            ),
            "nav_summary": nav.get("summary"),
            "nav_deltas": {
                k: v for k, v in nav.items() if k.startswith("delta_")
            },
        },
        "note": (
            "30m poll spread_class at first poll ≥ no_trade_before · "
            "entry+swap gate · C18acc S2 live-aligned config"
        ),
    }


def render_c18acc_avoid_mixed_slot_resim_md(payload: dict[str, Any]) -> str:
    gm = payload.get("gate_meta") or {}
    vdict = payload.get("verdict") or {}
    variants = payload.get("variants") or []
    base_ports = (variants[0].get("portfolio") if variants else {}) or {}
    lines = [
        "# C18acc · avoid spread_mixed · slot re-sim",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload.get('n_slots')} 槽 · **slot re-sim**（非 post-hoc）",
        "",
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start') or '—'}**",
        "",
        payload.get("note", ""),
        "",
        f"Gate 建置：30m poll · 候選池 top-{10} · "
        f"候選 mixed **{gm.get('candidate_mixed_pct')}%** · "
        f"{gm.get('n_stocks')} stocks · {gm.get('n_frames')} frames",
        "",
        "## 變體 × 窗口（simulate_score_swap_c）",
        "",
        "| variant | window | n legs | mean_ret% | mean_excess% | win% | slot_util% | Δ OOS excess |",
        "|---------|--------|-------:|----------:|-------------:|-----:|-----------:|-------------:|",
    ]
    for v in payload.get("variants") or []:
        delta = v.get("delta_oos_excess_pp")
        delta_s = f"{delta}pp" if v["variant_id"] != "baseline" and delta is not None else "—"
        sim = v.get("sim_summary") or {}
        util = sim.get("slot_util_pct")
        for win in ("full", "is", "oos"):
            sm = (v.get("slices") or {}).get(win) or {}
            lines.append(
                f"| {v.get('label')} | {win.upper()} | {sm.get('n_periods')} | "
                f"{sm.get('mean_return_pct')} | {sm.get('mean_excess_pct')} | "
                f"{sm.get('win_rate_gross_pct')} | {util if win == 'full' else '—'} | {delta_s} |"
            )

    lines.extend(
        [
            "",
            f"## Portfolio NAV（`simulate_slot_portfolio` · {payload.get('total_capital_ntd', DEFAULT_TOTAL_CAPITAL):,.0f} NTD · 無成本）",
            "",
            "| variant | window | total_ret% | CAGR% | Sharpe | MaxDD% | avg_util% | Δ total (vs base) |",
            "|---------|--------|----------:|------:|-------:|-------:|----------:|------------------:|",
        ]
    )
    base_ports = (variants[0].get("portfolio") if variants else {}) or {}
    for v in payload.get("variants") or []:
        for win in ("full", "is", "oos"):
            pf = (v.get("portfolio") or {}).get(win) or {}
            if not pf.get("total_return_pct") and pf.get("n_trade_dates", 0) < 5:
                continue
            bp = (base_ports.get(win) or {}).get("total_return_pct")
            tp = pf.get("total_return_pct")
            delta = (
                round(float(tp) - float(bp), 4)
                if v["variant_id"] != "baseline" and tp is not None and bp is not None
                else None
            )
            delta_s = f"{delta}pp" if delta is not None else "—"
            lines.append(
                f"| {v.get('label')} | {win.upper()} | {tp} | {pf.get('cagr_pct')} | "
                f"{pf.get('sharpe_ratio')} | {pf.get('max_drawdown_pct')} | "
                f"{pf.get('avg_utilization_pct')} | {delta_s} |"
            )

    lines.extend(
        [
            "",
            "## vs post-hoc overlay（參考）",
            "",
            "| 方法 | OOS Δ excess | 說明 |",
            "|------|-------------:|------|",
            "| post-hoc leg filter | +0.96pp | 不重跑 slot · 可能高估 |",
            f"| **slot re-sim** | **{(payload['variants'][1].get('delta_oos_excess_pp') if len(payload.get('variants') or []) > 1 else '—')}pp** | 略過 mixed 後重跑 fill/swap |",
            "",
            "## 採納建議",
            "",
            f"- Leg gate：OOS n≥8 · Δexcess≥0.5pp · IS 不逆勢 → "
            f"{'是' if vdict.get('recommend_adopt_legs') else '否'}",
            f"- **NAV gate**：OOS total_ret ≥ baseline · FULL 跌幅 ≤2pp → "
            f"{'是' if vdict.get('recommend_adopt_nav') else '否'}",
            f"- Leg：{vdict.get('leg_summary', '')}",
            f"- NAV：{vdict.get('nav_summary', '')}",
            "",
            "模組：`src/research/backtest/archive/c18acc_avoid_mixed_slot_resim.py`",
        ]
    )
    return "\n".join(lines)
