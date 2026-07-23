"""C18acc live vs radar-C0 slot-fair compare (Part A).

B0_live: fresh · 13:00 · avg_accel · c1 · G_R5_12
R1_radar_fresh: fresh · 09:30 · C0 · c1 · no chase
R2_radar_union: fresh_union_mono · 09:30 · C0 · c1 · no chase
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.c18acc_old_vs_live_gap_attribution import (
    _big_win_keys,
    _entry_cfg,
    _equal_slot_compound_pct,
    _is_cut_date,
    _resolve_gate,
    _s2_champion_base,
    _slice_bundle,
)
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    _delta_pp,
    _entry_minute_hist,
    _period_stats,
    _slice_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel

TOPIC_ID = "c18acc-vs-radar-c0-slot-fair"
SCHEMA = "c18acc_vs_radar_c0_slot_fair-v1"
CASE_STOCK = "6505"


def _variant_specs() -> list[dict[str, Any]]:
    return [
        {
            "variant_id": "B0_live",
            "label": "現行 live · fresh · 13:00 · avg_accel · c1 · G_R5_12",
            "role": "baseline",
            "candidate_pool": "fresh",
            "no_trade_before": "13:00",
            "confirm_bars": 1,
            "entry_score_mode": "avg_accel",
            "prior_ret_max": 0.12,
        },
        {
            "variant_id": "R1_radar_fresh",
            "label": "radar proxy · fresh · 09:30 · C0 · c1 · no chase",
            "role": "radar",
            "candidate_pool": "fresh",
            "no_trade_before": "09:30",
            "confirm_bars": 1,
            "entry_score_mode": "scale",
            "prior_ret_max": None,
        },
        {
            "variant_id": "R2_radar_union",
            "label": "radar proxy · fresh∪tier2 · 09:30 · C0 · c1 · no chase",
            "role": "radar",
            "candidate_pool": "fresh_union_mono",
            "no_trade_before": "09:30",
            "confirm_bars": 1,
            "entry_score_mode": "scale",
            "prior_ret_max": None,
        },
    ]


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


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
        f"entry={entry_c.variant_id}/{entry_c.score_mode} · chase={cfg.entry_prior_ret_max}",
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
        kbar_cache={},
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
    case_legs = [
        p
        for p in periods
        if str(p.get("stock_id")) == CASE_STOCK
        and "2026-07-01" <= str(p.get("entry_date") or "") <= "2026-07-16"
    ]
    return {
        "spec": spec,
        "periods": periods,
        "summary": summary,
        "stats": stats,
        "compound_slot_pct": compound,
        "entry_minute_hist": _entry_minute_hist(periods),
        "big_win_keys": sorted(_big_win_keys(periods)),
        "case_6505_legs": [
            {
                "entry_date": p.get("entry_date"),
                "entry_minute": p.get("entry_minute"),
                "exit_date": p.get("exit_date"),
                "return_pct": p.get("return_pct"),
                "excess_pct": p.get("excess_pct"),
            }
            for p in case_legs
        ],
    }


def run_c18acc_vs_radar_c0_slot_fair(
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
    cut = is_end or _is_cut_date(trade_dates, ratio=0.7)
    oos_start = next((d for d in trade_dates if d > cut), None)
    if not oos_start:
        raise ValueError(f"no OOS after {cut}")

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
        "spread_rs_panel": spread_rs_panel,
    }

    gate_paths = {
        "09:30": Path(gate_cache_0930) if gate_cache_0930 else None,
        "13:00": Path(gate_cache_1300) if gate_cache_1300 else None,
    }
    gates: dict[str, Any] = {}
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

    rows: list[dict[str, Any]] = []
    base_full = base_is = base_oos = None
    for spec in _variant_specs():
        ntb = str(spec["no_trade_before"])
        run = _run_one(
            conn,
            ctx=ctx,
            trade_dates=trade_dates,
            spec=spec,
            gate=gates[ntb],
            n_slots=n_slots,
        )
        slices = _slice_bundle(
            run,
            date_start=date_start,
            is_end=cut,
            oos_start=oos_start,
            date_end=end,
        )
        row: dict[str, Any] = {
            "variant_id": spec["variant_id"],
            "label": spec["label"],
            "spec": spec,
            "slices": slices,
            "entry_minute_hist": run["entry_minute_hist"],
            "case_6505_legs": run["case_6505_legs"],
            "delta_full_excess_pp": None,
            "delta_is_excess_pp": None,
            "delta_oos_excess_pp": None,
            "adopt": None,
        }
        if spec["variant_id"] == "B0_live":
            base_full = slices["full"]
            base_is = slices["is"]
            base_oos = slices["oos"]
        else:
            row["delta_full_excess_pp"] = _delta_pp(
                slices["full"].get("mean_excess_pct"),
                (base_full or {}).get("mean_excess_pct"),
            )
            row["delta_is_excess_pp"] = _delta_pp(
                slices["is"].get("mean_excess_pct"),
                (base_is or {}).get("mean_excess_pct"),
            )
            row["delta_oos_excess_pp"] = _delta_pp(
                slices["oos"].get("mean_excess_pct"),
                (base_oos or {}).get("mean_excess_pct"),
            )
            row["adopt"] = _adopt(
                n_oos=int(slices["oos"].get("n_legs") or 0),
                d_oos=row["delta_oos_excess_pp"],
                d_is=row["delta_is_excess_pp"],
            )
        rows.append(row)

    by_id = {r["variant_id"]: r for r in rows}
    winners = [vid for vid in ("R1_radar_fresh", "R2_radar_union") if by_id.get(vid, {}).get("adopt") == "GO"]
    winner = None
    if len(winners) == 1:
        winner = winners[0]
    elif len(winners) == 2:
        e1 = float(by_id["R1_radar_fresh"].get("delta_oos_excess_pp") or -999)
        e2 = float(by_id["R2_radar_union"].get("delta_oos_excess_pp") or -999)
        winner = "R1_radar_fresh" if e1 >= e2 else "R2_radar_union"

    go_any = bool(winners)
    verdict = {
        "decision": "GO_RADAR_C0_SLOT" if go_any else "KEEP_LIVE_E1300",
        "winner_variant": winner,
        "order_action": (
            f"Draft strategy/order toward {winner} "
            f"(ntb=09:30 · C0 · pool={by_id[winner]['spec']['candidate_pool']} · no G_R5_12)"
            if winner
            else "No strategy/order change · keep E@13:00 · radar stays advisory"
        ),
        "observe_sleeve": None
        if go_any
        else {
            "topic": "c18acc-early-entry-event-observe",
            "action": "Keep radar advisory · merge overlap event tags into observe sleeve",
        },
        "gates": {
            "oos_min_n": ADOPT_OOS_MIN_N,
            "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
            "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
        },
        "summary": (
            f"{'GO' if go_any else 'NO_GO'} · winner={winner or '—'} · "
            f"R1 oosΔ={by_id.get('R1_radar_fresh', {}).get('delta_oos_excess_pp')} · "
            f"R2 oosΔ={by_id.get('R2_radar_union', {}).get('delta_oos_excess_pp')}"
        ),
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
        "gate_meta": gate_metas,
        "variants": rows,
        "verdict": verdict,
        "note": "6505 case in case_6505_legs is appendix only · not a GO criterion",
    }


def render_c18acc_vs_radar_c0_slot_fair_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc vs radar-C0 slot fair (Part A)",
        "",
        f"- Topic：`{payload.get('topic')}`",
        f"- Window：`{payload.get('date_start')}` → `{payload.get('date_end')}` · "
        f"{payload.get('n_trade_dates')} TD",
        f"- Split：IS ≤ `{payload.get('is_end')}` · OOS ≥ `{payload.get('oos_start')}`",
        f"- Verdict：{v.get('summary')}",
        f"- Decision：`{v.get('decision')}` · winner=`{v.get('winner_variant')}`",
        f"- Order action：{v.get('order_action')}",
        "",
        "## Variants",
        "",
        "| ID | n FULL | excess FULL | compound | excess OOS | ΔOOS | adopt |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in payload.get("variants") or []:
        f = (r.get("slices") or {}).get("full") or {}
        o = (r.get("slices") or {}).get("oos") or {}
        lines.append(
            f"| `{r.get('variant_id')}` | {f.get('n_legs')} | {f.get('mean_excess_pct')} | "
            f"{f.get('compound_slot_pct')} | {o.get('mean_excess_pct')} | "
            f"{r.get('delta_oos_excess_pp')} | {r.get('adopt') or 'baseline'} |"
        )
    lines += [
        "",
        "## Case appendix · 6505（非門檻）",
        "",
    ]
    for r in payload.get("variants") or []:
        legs = r.get("case_6505_legs") or []
        lines.append(f"- `{r.get('variant_id')}`：{legs if legs else '—'}")
    obs = v.get("observe_sleeve") or {}
    lines += [
        "",
        "## GO gates",
        "",
        f"- OOS n≥{ADOPT_OOS_MIN_N} · Δexcess≥{ADOPT_OOS_MIN_DELTA_PP}pp · "
        f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp",
        "",
        "## Decision",
        "",
        f"- `{v.get('decision')}` · {v.get('order_action')}",
    ]
    if obs:
        lines += [
            f"- Observe：`{obs.get('topic')}` · {obs.get('action')}",
        ]
    lines += [
        "",
        "---",
        "模組：`src/research/backtest/c18acc_vs_radar_c0_slot_fair.py`",
        "",
    ]
    return "\n".join(lines)
