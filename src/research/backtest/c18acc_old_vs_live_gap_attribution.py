"""C18acc · old cinema vs live SSOT · single-factor gap attribution (Phase 0).

Variants (same window · 3 slots · S2 · avoid_mixed):
- B_old: POOL1 · NTB 09:30 · C0×seg_last · confirm=2 · no G_R5_12
- B_live: fresh · NTB 13:00 · avg_accel · confirm=1 · G_R5_12
- A1: B_live · NTB=09:30
- A2: B_live · G_R5_12 off
- A3: B_live · entry=C0×seg_last
- A4: B_old · pool=fresh
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (
    build_avoid_mixed_gate_lookup_live,
    load_gate_cache,
    save_gate_cache,
)
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_pool1_showcase import _live_gate_cache_path
from research.backtest.c18acc_open_timing_study import (
    _delta_pp,
    _entry_minute_hist,
    _period_stats,
    _slice_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import (
    DEFAULT_C_SWEEP,
    CVariantConfig,
    champion_entry_c_config,
)
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel

BIG_WIN_PCT = 20.0
TOPIC_ID = "c18acc-old-vs-live-gap-attribution"
SCHEMA = "c18acc_old_vs_live_gap_attribution-v1"


def _is_cut_date(trade_dates: list[str], *, ratio: float = 0.7) -> str:
    if not trade_dates:
        raise ValueError("empty trade_dates")
    idx = max(0, min(len(trade_dates) - 1, int(len(trade_dates) * ratio) - 1))
    return trade_dates[idx]


def _equal_slot_compound_pct(periods: list[dict[str, Any]], *, capital: float = 10_000.0) -> float | None:
    by: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for p in periods:
        by[p.get("slot")].append(p)
    if not by:
        return None
    comps: list[float] = []
    for xs in by.values():
        xs = sorted(xs, key=lambda r: (str(r.get("entry_date") or ""), str(r.get("entry_minute") or "")))
        eq = float(capital)
        for lg in xs:
            r = lg.get("return_pct")
            if r is None:
                continue
            eq *= 1.0 + float(r) / 100.0
        comps.append((eq / capital - 1.0) * 100.0)
    if not comps:
        return None
    return round(sum(comps) / len(comps), 4)


def _big_win_keys(periods: list[dict[str, Any]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for p in periods:
        try:
            ret = float(p.get("return_pct"))
        except (TypeError, ValueError):
            continue
        if ret >= BIG_WIN_PCT:
            out.add((str(p.get("stock_id") or ""), str(p.get("entry_date") or "")))
    return out


def _entry_cfg(
    *,
    score_mode: str,
    confirm_bars: int,
    no_swap_before: str,
) -> CVariantConfig:
    if score_mode == "avg_accel":
        base = champion_entry_c_config(confirm_bars=int(confirm_bars))
    else:
        # C0 scale · confirm=1 → C0; confirm≥2 → C3
        vid = "C0" if int(confirm_bars) <= 1 else "C3"
        base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == vid)
        if int(base.confirm_bars) != int(confirm_bars):
            base = replace(base, confirm_bars=int(confirm_bars))
    return replace(base, no_swap_before=str(no_swap_before))


def _s2_champion_base(*, n_slots: int) -> ScoreSwapCConfig:
    s2 = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}["S2"]
    champ = champion_score_swap_c_config()
    return replace(
        s2,
        timing_mode=champ.timing_mode,
        poll_interval_min=champ.poll_interval_min,
        entry_leg=champ.entry_leg,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        accel_sell_bypass_on_spread_lost=champ.accel_sell_bypass_on_spread_lost,
        n_slots=max(1, int(n_slots)),
    )


def _variant_specs() -> list[dict[str, Any]]:
    return [
        {
            "variant_id": "B_old",
            "label": "舊站 Champion · POOL1 · 09:30 · C0×seg · c2 · no chase",
            "role": "old_cinema",
            "candidate_pool": "fresh_union_accel",
            "no_trade_before": "09:30",
            "confirm_bars": 2,
            "entry_score_mode": "scale",
            "prior_ret_max": None,
            "factor": None,
        },
        {
            "variant_id": "B_live",
            "label": "現行 live SSOT · fresh · 13:00 · avg_accel · c1 · G_R5_12",
            "role": "live_ssot",
            "candidate_pool": "fresh",
            "no_trade_before": "13:00",
            "confirm_bars": 1,
            "entry_score_mode": "avg_accel",
            "prior_ret_max": 0.12,
            "factor": None,
        },
        {
            "variant_id": "A1",
            "label": "B_live · NTB=09:30 only",
            "role": "ablation",
            "candidate_pool": "fresh",
            "no_trade_before": "09:30",
            "confirm_bars": 1,
            "entry_score_mode": "avg_accel",
            "prior_ret_max": 0.12,
            "factor": "ntb",
        },
        {
            "variant_id": "A2",
            "label": "B_live · G_R5_12 off",
            "role": "ablation",
            "candidate_pool": "fresh",
            "no_trade_before": "13:00",
            "confirm_bars": 1,
            "entry_score_mode": "avg_accel",
            "prior_ret_max": None,
            "factor": "chase_gate",
        },
        {
            "variant_id": "A3",
            "label": "B_live · entry=C0×seg_last",
            "role": "ablation",
            "candidate_pool": "fresh",
            "no_trade_before": "13:00",
            "confirm_bars": 1,
            "entry_score_mode": "scale",
            "prior_ret_max": 0.12,
            "factor": "entry_sort",
        },
        {
            "variant_id": "A4",
            "label": "B_old · pool=fresh (drop accel)",
            "role": "ablation",
            "candidate_pool": "fresh",
            "no_trade_before": "09:30",
            "confirm_bars": 2,
            "entry_score_mode": "scale",
            "prior_ret_max": None,
            "factor": "pool",
        },
    ]


def _resolve_gate(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    ntb: str,
    n_slots: int,
    gate_cache_by_ntb: dict[str, Path | None],
) -> tuple[dict[str, set[str]], dict[str, Any], Path]:
    """Load/build avoid_mixed gate with anchor == NTB (not live-champ default)."""
    path = gate_cache_by_ntb.get(ntb)
    if path is None:
        path = _live_gate_cache_path(trade_dates, gate_anchor_minute=ntb)
    path = Path(path)
    if path.is_file():
        gate, meta, c0, c1 = load_gate_cache(str(path))
        if c0 == trade_dates[0] and c1 == trade_dates[-1]:
            cached_anchor = str((meta or {}).get("gate_anchor_minute") or "")
            if not cached_anchor or cached_anchor == ntb:
                return gate, meta or {}, path
    print(
        f"building avoid_mixed gate @{ntb} · {trade_dates[0]}→{trade_dates[-1]} "
        f"({len(trade_dates)} td) …",
        flush=True,
    )
    ctx = _prepare_c18acc_context(conn, trade_dates)
    cfg = replace(c18acc_live_s2_config(n_slots=n_slots), no_trade_before=ntb)
    gate, meta = build_avoid_mixed_gate_lookup_live(
        conn,
        trade_dates=trade_dates,
        ctx=ctx,
        cfg=cfg,
        no_trade_before=ntb,
        gate_anchor_minute=ntb,
        passthrough_pool=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    save_gate_cache(
        str(path),
        gate=gate,
        meta=meta,
        date_start=trade_dates[0],
        date_end=trade_dates[-1],
    )
    print(f"saved gate → {path}", flush=True)
    return gate, meta, path


def _run_one(
    conn: sqlite3.Connection,
    *,
    ctx: dict[str, Any],
    trade_dates: list[str],
    spec: dict[str, Any],
    gate: dict[str, set[str]] | None,
    n_slots: int,
) -> dict[str, Any]:
    ntb = str(spec["no_trade_before"])
    confirm = int(spec["confirm_bars"])
    cfg = replace(
        _s2_champion_base(n_slots=n_slots),
        variant_id=str(spec["variant_id"]),
        label=str(spec["label"]),
        candidate_pool=str(spec["candidate_pool"]),
        no_trade_before=ntb,
        entry_prior_ret_days=5,
        entry_prior_ret_max=spec["prior_ret_max"],
    )
    entry_c = _entry_cfg(
        score_mode=str(spec["entry_score_mode"]),
        confirm_bars=confirm,
        no_swap_before=ntb,
    )
    print(
        f"  sim {spec['variant_id']} · pool={cfg.candidate_pool} · ntb={ntb} · "
        f"entry={entry_c.variant_id}/{entry_c.score_mode} · c={confirm} · "
        f"chase={cfg.entry_prior_ret_max}",
        flush=True,
    )
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        spread_rs_panel=ctx["spread_rs_panel"],
        entry_c_config=entry_c,
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
    )
    stats = _period_stats(periods, summary)
    compound = _equal_slot_compound_pct(periods)
    print(
        f"    n={stats['n_legs']} excess={stats.get('mean_excess_pct')}% "
        f"compound≈{compound}%",
        flush=True,
    )
    return {
        "spec": spec,
        "periods": periods,
        "summary": summary,
        "stats": stats,
        "compound_slot_pct": compound,
        "entry_minute_hist": _entry_minute_hist(periods),
        "big_win_keys": sorted(_big_win_keys(periods)),
        "entry_variant_id": entry_c.variant_id,
        "entry_score_mode": entry_c.score_mode,
    }


def _slice_bundle(
    run: dict[str, Any],
    *,
    date_start: str,
    is_end: str,
    oos_start: str,
    date_end: str,
) -> dict[str, Any]:
    periods = run["periods"]
    full = run["stats"]
    is_s = _period_stats(_slice_periods(periods, start=date_start, end=is_end))
    oos = _period_stats(_slice_periods(periods, start=oos_start, end=date_end))
    return {
        "full": {
            **full,
            "compound_slot_pct": run["compound_slot_pct"],
            "n_big_wins": len(run["big_win_keys"]),
        },
        "is": {
            **is_s,
            "compound_slot_pct": _equal_slot_compound_pct(
                _slice_periods(periods, start=date_start, end=is_end)
            ),
            "n_big_wins": len(_big_win_keys(_slice_periods(periods, start=date_start, end=is_end))),
        },
        "oos": {
            **oos,
            "compound_slot_pct": _equal_slot_compound_pct(
                _slice_periods(periods, start=oos_start, end=date_end)
            ),
            "n_big_wins": len(_big_win_keys(_slice_periods(periods, start=oos_start, end=date_end))),
        },
    }


def _attribution_verdict(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    b_old = rows["B_old"]["slices"]
    b_live = rows["B_live"]["slices"]
    gap_full = _delta_pp(
        b_old["full"].get("mean_excess_pct"), b_live["full"].get("mean_excess_pct")
    )
    gap_oos = _delta_pp(
        b_old["oos"].get("mean_excess_pct"), b_live["oos"].get("mean_excess_pct")
    )
    gap_compound = _delta_pp(
        b_old["full"].get("compound_slot_pct"), b_live["full"].get("compound_slot_pct")
    )

    # Single-factor recovery vs B_live (positive = closes gap toward B_old)
    factor_rows: list[dict[str, Any]] = []
    for vid in ("A1", "A2", "A3", "A4"):
        r = rows[vid]
        d_full = _delta_pp(
            r["slices"]["full"].get("mean_excess_pct"),
            b_live["full"].get("mean_excess_pct"),
        )
        d_oos = _delta_pp(
            r["slices"]["oos"].get("mean_excess_pct"),
            b_live["oos"].get("mean_excess_pct"),
        )
        d_comp = _delta_pp(
            r["slices"]["full"].get("compound_slot_pct"),
            b_live["full"].get("compound_slot_pct"),
        )
        share = None
        if gap_full not in (None, 0) and d_full is not None:
            share = round(100.0 * float(d_full) / float(gap_full), 2)
        factor_rows.append(
            {
                "variant_id": vid,
                "factor": r["spec"]["factor"],
                "delta_full_excess_pp_vs_live": d_full,
                "delta_oos_excess_pp_vs_live": d_oos,
                "delta_full_compound_pp_vs_live": d_comp,
                "share_of_full_gap_pct": share,
            }
        )

    # Residual = gap - sum of A1/A2/A3 (A4 is pool from old side; keep separate)
    sum_abc = 0.0
    n_abc = 0
    for fr in factor_rows:
        if fr["factor"] in ("ntb", "chase_gate", "entry_sort") and fr[
            "delta_full_excess_pp_vs_live"
        ] is not None:
            sum_abc += float(fr["delta_full_excess_pp_vs_live"])
            n_abc += 1
    residual = None
    if gap_full is not None and n_abc == 3:
        residual = round(float(gap_full) - sum_abc, 4)

    ranked = sorted(
        [fr for fr in factor_rows if fr["delta_full_excess_pp_vs_live"] is not None],
        key=lambda x: float(x["delta_full_excess_pp_vs_live"]),
        reverse=True,
    )
    top = ranked[0] if ranked else None
    primary = str(top["factor"]) if top else "unknown"

    old_big = set(map(tuple, rows["B_old"]["big_win_keys"]))
    live_big = set(map(tuple, rows["B_live"]["big_win_keys"]))
    coverage = {
        "old_n_big_wins": len(old_big),
        "live_n_big_wins": len(live_big),
        "still_in_live": len(old_big & live_big),
        "missing_in_live": len(old_big - live_big),
        "missing_keys": sorted(old_big - live_big)[:20],
    }

    phase1_ntb = "09:30" if primary == "ntb" else str(
        rows["B_live"]["spec"]["no_trade_before"]
    )
    return {
        "gap_old_minus_live_full_excess_pp": gap_full,
        "gap_old_minus_live_oos_excess_pp": gap_oos,
        "gap_old_minus_live_full_compound_pp": gap_compound,
        "factor_effects": factor_rows,
        "residual_full_excess_pp_after_A1_A2_A3": residual,
        "primary_recoverable_factor": primary,
        "phase1_open_window": phase1_ntb if primary == "ntb" else None,
        "phase1_recommendation": (
            "RUN_PROVISIONAL_ALLDAY"
            if primary == "ntb"
            else (
                "TEST_SINGLE_FACTOR_" + primary.upper()
                if primary in ("chase_gate", "entry_sort", "pool")
                else "KEEP_LIVE_DIAGNOSE"
            )
        ),
        "big_win_coverage": coverage,
        "summary": (
            f"FULL gap(old−live) excess={gap_full}pp · compound={gap_compound}pp · "
            f"primary={primary} · Phase1={phase1_ntb if primary == 'ntb' else 'n/a'}"
        ),
    }


def run_old_vs_live_gap_attribution(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str | None = None,
    n_slots: int = 3,
    gate_cache_0930: str | Path | None = None,
    gate_cache_1300: str | Path | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 10:
        raise ValueError(f"need ≥10 trade dates, got {len(trade_dates)}")

    cut = is_end or _is_cut_date(trade_dates, ratio=0.7)
    oos_start = next((d for d in trade_dates if d > cut), None)
    if not oos_start:
        raise ValueError(f"no OOS after is_end={cut}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    base_cfg = _s2_champion_base(n_slots=n_slots)
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(base_cfg)
        else None
    )
    ctx: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": {},
        "spread_rs_panel": spread_rs_panel,
    }

    gate_paths = {
        "09:30": Path(gate_cache_0930) if gate_cache_0930 else None,
        "13:00": Path(gate_cache_1300) if gate_cache_1300 else None,
    }
    gates: dict[str, dict[str, set[str]] | None] = {}
    gate_metas: dict[str, Any] = {}
    for ntb in ("09:30", "13:00"):
        g, gm, path = _resolve_gate(
            conn,
            trade_dates,
            ntb=ntb,
            n_slots=n_slots,
            gate_cache_by_ntb=gate_paths,
        )
        gates[ntb] = g
        gate_metas[ntb] = {**(gm or {}), "cache_path": str(path)}

    rows: dict[str, dict[str, Any]] = {}
    for spec in _variant_specs():
        ntb = str(spec["no_trade_before"])
        run = _run_one(
            conn,
            ctx={**ctx, "kbar_cache": {}},
            trade_dates=trade_dates,
            spec=spec,
            gate=gates[ntb],
            n_slots=n_slots,
        )
        rows[str(spec["variant_id"])] = {
            "spec": spec,
            "slices": _slice_bundle(
                run,
                date_start=date_start,
                is_end=cut,
                oos_start=oos_start,
                date_end=end,
            ),
            "entry_minute_hist": run["entry_minute_hist"],
            "big_win_keys": run["big_win_keys"],
            "entry_variant_id": run["entry_variant_id"],
            "entry_score_mode": run["entry_score_mode"],
            "n_periods_full": len(run["periods"]),
        }

    verdict = _attribution_verdict(rows)
    # Drop heavy period lists from payload rows
    slim_rows = {
        vid: {
            k: v
            for k, v in row.items()
            if k != "periods"
        }
        for vid, row in rows.items()
    }
    return {
        "schema": SCHEMA,
        "topic": TOPIC_ID,
        "date_start": date_start,
        "date_end": end,
        "is_end": cut,
        "oos_start": oos_start,
        "n_trade_dates": len(trade_dates),
        "n_slots": n_slots,
        "split": "default_is_oos_70_30_date",
        "gate_meta": gate_metas,
        "variants": slim_rows,
        "verdict": verdict,
    }


def render_old_vs_live_gap_attribution_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · old cinema vs live SSOT · gap attribution (Phase 0)",
        "",
        f"- Topic：`{payload.get('topic')}`",
        f"- Window：`{payload.get('date_start')}` → `{payload.get('date_end')}` · "
        f"{payload.get('n_trade_dates')} TD",
        f"- Split：IS ≤ `{payload.get('is_end')}` · OOS ≥ `{payload.get('oos_start')}`",
        f"- Verdict：{v.get('summary')}",
        "",
        "## Gap (B_old − B_live)",
        "",
        f"- FULL mean_excess Δ：**{v.get('gap_old_minus_live_full_excess_pp')}** pp",
        f"- OOS mean_excess Δ：**{v.get('gap_old_minus_live_oos_excess_pp')}** pp",
        f"- FULL equal-slot compound Δ：**{v.get('gap_old_minus_live_full_compound_pp')}** pp",
        "",
        "## Variant KPI",
        "",
        "| ID | role | n FULL | excess FULL | compound FULL | excess OOS | n big≥20% |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for vid in ("B_old", "B_live", "A1", "A2", "A3", "A4"):
        row = (payload.get("variants") or {}).get(vid) or {}
        sl = row.get("slices") or {}
        f = sl.get("full") or {}
        o = sl.get("oos") or {}
        lines.append(
            f"| `{vid}` | {row.get('spec', {}).get('role')} | {f.get('n_legs')} | "
            f"{f.get('mean_excess_pct')} | {f.get('compound_slot_pct')} | "
            f"{o.get('mean_excess_pct')} | {f.get('n_big_wins')} |"
        )
    lines += [
        "",
        "## Single-factor effect vs B_live",
        "",
        "| ID | factor | Δexcess FULL | Δexcess OOS | Δcompound FULL | share of gap % |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for fr in v.get("factor_effects") or []:
        lines.append(
            f"| `{fr.get('variant_id')}` | {fr.get('factor')} | "
            f"{fr.get('delta_full_excess_pp_vs_live')} | "
            f"{fr.get('delta_oos_excess_pp_vs_live')} | "
            f"{fr.get('delta_full_compound_pp_vs_live')} | "
            f"{fr.get('share_of_full_gap_pct')} |"
        )
    cov = v.get("big_win_coverage") or {}
    lines += [
        "",
        f"- Residual after A1+A2+A3：**{v.get('residual_full_excess_pp_after_A1_A2_A3')}** pp",
        f"- Primary recoverable factor：**{v.get('primary_recoverable_factor')}**",
        f"- Phase1 recommendation：`{v.get('phase1_recommendation')}` · "
        f"open=`{v.get('phase1_open_window')}`",
        "",
        "## Big-win coverage (≥20%)",
        "",
        f"- Old：{cov.get('old_n_big_wins')} · Live：{cov.get('live_n_big_wins')} · "
        f"still：{cov.get('still_in_live')} · missing：{cov.get('missing_in_live')}",
        f"- Missing sample：`{cov.get('missing_keys')}`",
        "",
        "---",
        "模組：`src/research/backtest/c18acc_old_vs_live_gap_attribution.py`",
        "",
    ]
    return "\n".join(lines)
