"""C18acc · A vs C @ 13:00 timing study.

Frozen: fresh · confirm_bars=2 · avoid_spread_mixed · G_R5_12 · S2 · n_slots=3.

- B0: poll_5m · no_trade_before=09:30 (live baseline)
- A  PRIOR_1300: same EOD selection · post-hoc fill at prior day 13:00 (look-ahead vs live)
- C  SNAP_1300: timing_mode=snapshot_1300 · 13:00 provisional RRG + 13:00 fill (PIT-clean)
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _entry_c_config,
    _period_stats,
    _run_sim,
    _variant_row,
    apply_prior_day_1300_fills,
)
from research.backtest.c18acc_snapshot_1300 import audit_shortlist_kbar_coverage
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel


def _adopt_label(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_a_vs_c_1300_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
    )
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench) if _poll5m_needs_spread_rs_panel(champ) else None
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

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS dates after is_end={is_end}")

    gate, gate_meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_path,
        live_aligned=True,
        n_slots=n_slots,
    )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    from research.backtest.c18acc_snapshot_1300 import clear_snapshot_panel_cache

    clear_snapshot_panel_cache()

    kbar_cov = audit_shortlist_kbar_coverage(
        conn,
        trade_dates=trade_dates,
        fresh_by_date=fresh_by_date,
        kbar_cache=ctx["kbar_cache"],
    )

    cfg_b0 = replace(
        champ,
        variant_id="B0",
        label="poll_5m · ≥09:30",
        timing_mode="poll_5m",
        no_trade_before="09:30",
    )
    cfg_c = replace(
        champ,
        variant_id="C_SNAP_1300",
        label="snapshot_1300 · 13:00 RRG + fill",
        timing_mode="snapshot_1300",
        no_trade_before="13:00",
        snapshot_strict_kbar=True,
    )

    p_b0, s_b0, _ = _run_sim(
        conn,
        ctx=ctx,
        trade_dates=trade_dates,
        cfg=cfg_b0,
        confirm_bars=confirm_bars,
        gate=gate,
        label="B0 · poll_5m ≥09:30",
    )

    print("  post-hoc A PRIOR_1300 …", flush=True)
    p_a, a_meta = apply_prior_day_1300_fills(
        conn,
        p_b0,
        full_dates=full_dates,
        kbar_cache=ctx["kbar_cache"],
    )
    print(
        f"    filled={a_meta['n_filled']} / {a_meta['n_baseline']} "
        f"skip_no_px={a_meta['n_skipped_no_px']}",
        flush=True,
    )

    # Snapshot path uses C0-style entry helper inside sim; keep confirm=2 entry_c for parity
    # where poll paths still run (swaps). Share kbar_cache + full_rrg cache.
    full_rrg_cache: dict = {}
    print("  sim C SNAP_1300 …", flush=True)
    from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c

    p_c, s_c = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg_c,
        mono_by_date=mono_by_date,
        kbar_cache=ctx["kbar_cache"],
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        spread_rs_panel=spread_rs_panel,
        entry_c_config=_entry_c_config(confirm_bars),
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
        snapshot_full_rrg_cache=full_rrg_cache,
    )
    print(
        f"    n={len(p_c)} excess={_period_stats(p_c, s_c).get('mean_excess_pct')}% "
        f"skip_days={s_c.get('snapshot_days_skipped')}",
        flush=True,
    )

    base_row = _variant_row(
        variant_id="B0",
        label="現行 · poll_5m ≥09:30",
        periods=p_b0,
        summary=s_b0,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
    )
    base_full = base_row["slices"]["full"]
    base_is = base_row["slices"]["is"]
    base_oos = base_row["slices"]["oos"]

    variants = [
        base_row,
        _variant_row(
            variant_id="A_PRIOR_1300",
            label="A · 收盤訊號 + 前一日 13:00 成交（post-hoc · 有前瞻）",
            periods=p_a,
            summary=None,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
            extra={"prior_fill_meta": a_meta, "pit_clean": False},
        ),
        _variant_row(
            variant_id="C_SNAP_1300",
            label="C · 13:00 重算 RRG + 13:00 成交（slot re-sim · PIT）",
            periods=p_c,
            summary=s_c,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
            extra={
                "pit_clean": True,
                "snapshot_days_skipped": s_c.get("snapshot_days_skipped"),
            },
        ),
    ]

    adopt: dict[str, str] = {}
    notes: list[str] = []
    for vid in ("A_PRIOR_1300", "C_SNAP_1300"):
        v = next(x for x in variants if x["variant_id"] == vid)
        oos = (v.get("slices") or {}).get("oos") or {}
        d_oos = v.get("delta_oos_excess_pp")
        d_is = v.get("delta_is_excess_pp")
        n_oos = int(oos.get("n_legs") or 0)
        adopt[vid] = _adopt_label(n_oos=n_oos, d_oos=d_oos, d_is=d_is)
        notes.append(f"{vid}: OOS Δ={d_oos}pp n={n_oos} · IS Δ={d_is}pp → {adopt[vid]}")

    return {
        "schema": "c18acc_a_vs_c_1300-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "candidate_pool": "fresh",
        "frozen": {
            "avoid_spread_mixed": True,
            "entry_prior_ret_max": champ.entry_prior_ret_max,
            "exit": "S2",
        },
        "gate_meta": gate_meta,
        "kbar_coverage_fresh": kbar_cov,
        "variants": variants,
        "verdict": {
            "baseline": "B0",
            "adopt": adopt,
            "thresholds": {
                "oos_min_n": ADOPT_OOS_MIN_N,
                "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
                "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
            },
            "summary": " · ".join(notes),
            "prefer": (
                "C_SNAP_1300"
                if adopt.get("C_SNAP_1300") == "GO"
                else "B0"
            ),
            "note": (
                "A is look-ahead upper bound only (not live-adoptable). "
                "C is PIT-clean; if NO_GO keep B0 poll_5m."
            ),
        },
    }


def render_a_vs_c_1300_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · A vs C @ 13:00",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        "Frozen：`fresh` · `confirm_bars=2` · `avoid_spread_mixed` · `G_R5_12` · S2 · 3 槽",
        "",
        "## 變體",
        "",
        "| id | 意思 | PIT |",
        "|----|------|-----|",
        "| B0 | 現行 poll_5m ≥09:30 | 是（昨收→隔日早） |",
        "| A_PRIOR_1300 | 同一收盤訊號 · 前一日 13:00 成交（post-hoc） | **否（前瞻）** |",
        "| C_SNAP_1300 | 13:00 重算 RRG + 13:00 成交（slot re-sim） | **是** |",
        "",
        "## 結果",
        "",
        "| variant | window | n | mean_ret% | mean_excess% | win% | Δ vs B0 excess |",
        "|---------|--------|--:|----------:|-------------:|-----:|---------------:|",
    ]
    for v in payload.get("variants") or []:
        vid = v.get("variant_id")
        for win, key in (("FULL", "full"), ("IS", "is"), ("OOS", "oos")):
            s = (v.get("slices") or {}).get(key) or {}
            if win == "FULL":
                d = v.get("delta_full_excess_pp")
            elif win == "IS":
                d = v.get("delta_is_excess_pp")
            else:
                d = v.get("delta_oos_excess_pp")
            d_s = "—" if d is None else f"{d:+.4f}pp"
            lines.append(
                f"| {vid} | {win} | {s.get('n_legs')} | {s.get('mean_return_pct')} | "
                f"{s.get('mean_excess_pct')} | {s.get('win_rate_gross_pct')} | {d_s} |"
            )

    cov = payload.get("kbar_coverage_fresh") or {}
    lines.extend(
        [
            "",
            "## Kbar @13:00（fresh shortlist audit）",
            "",
            f"- coverage：**{cov.get('coverage_pct')}%** "
            f"({cov.get('kbar_hits')}/{cov.get('stock_days')} stock-days)",
            "",
            "## Entry minute",
            "",
        ]
    )
    for v in payload.get("variants") or []:
        hist = v.get("entry_minute_hist") or {}
        top = ", ".join(f"{m}:{n}" for m, n in list(hist.items())[:8]) or "—"
        lines.append(f"- **{v.get('variant_id')}**：{top}")

    a = next(
        (x for x in (payload.get("variants") or []) if x.get("variant_id") == "A_PRIOR_1300"),
        None,
    )
    if a and a.get("prior_fill_meta"):
        m = a["prior_fill_meta"]
        lines.extend(
            [
                "",
                "## A fill meta",
                "",
                f"- filled **{m.get('n_filled')}** / {m.get('n_baseline')} · "
                f"skip_no_px **{m.get('n_skipped_no_px')}**",
            ]
        )

    v = payload.get("verdict") or {}
    lines.extend(
        [
            "",
            "## 採納建議",
            "",
            f"- 門檻：OOS n≥{ADOPT_OOS_MIN_N} · Δ≥{ADOPT_OOS_MIN_DELTA_PP}pp · "
            f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp",
            f"- **{v.get('summary')}**",
            f"- prefer：**{v.get('prefer')}** — {v.get('note')}",
            "",
            "模組：`src/research/backtest/c18acc_a_vs_c_1300_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
