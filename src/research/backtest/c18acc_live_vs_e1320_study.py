"""C18acc · LIVE(E close→E+1 09:30) vs E@13:20 選+買.

Fair pair the user asked for:
- LIVE_NEXT_AM: 昨收 PIT 池 · ≥09:30（= E 收盤名單 → E+1 早盤）
- E_1320: 當日 EOD 池（≈13:20 名單；近三月 top3 異僅 3%）· ≥13:20 成交

Optional SNAP_1320: timing_mode=snapshot_1300 @13:20（真·暫定 RRG；較慢）
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_near_close_timeline_study import apply_fills
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _run_sim,
    _variant_row,
    lag_rows_by_prior_day,
)
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


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


@contextmanager
def _snapshot_minute(minute: str) -> Iterator[None]:
    """Temporarily point snapshot_1300 helpers at another clock minute."""
    import research.backtest.c18acc_snapshot_1300 as snap

    old_m = snap.SNAPSHOT_1300_MINUTE
    old_sql = snap.SNAPSHOT_1300_MINUTE_SQL
    snap.SNAPSHOT_1300_MINUTE = minute
    snap.SNAPSHOT_1300_MINUTE_SQL = f"{minute}:00" if len(minute) == 5 else minute
    snap.clear_snapshot_panel_cache()
    try:
        yield
    finally:
        snap.SNAPSHOT_1300_MINUTE = old_m
        snap.SNAPSHOT_1300_MINUTE_SQL = old_sql
        snap.clear_snapshot_panel_cache()


def run_live_vs_e1320_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    run_snap_1320: bool = True,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    fresh_lag = lag_rows_by_prior_day(
        fresh_by_date, full_dates=full_dates, trade_dates=trade_dates
    )
    mono_lag = lag_rows_by_prior_day(
        mono_by_date, full_dates=full_dates, trade_dates=trade_dates
    )
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
    ctx_eod: dict[str, Any] = {
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
    ctx_live = {
        **ctx_eod,
        "fresh_by_date": fresh_lag,
        "mono_by_date": mono_lag,
        "kbar_cache": {},
    }

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS after {is_end}")

    gate, gate_meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_path,
        live_aligned=True,
        n_slots=n_slots,
    )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    # 1) True live: E close → E+1 09:30
    cfg_live = replace(
        champ,
        variant_id="LIVE_NEXT_AM",
        label="E 收盤名單 → E+1 ≥09:30（真 live）",
        timing_mode="poll_5m",
        no_trade_before="09:30",
    )
    p_live, s_live, _ = _run_sim(
        conn,
        ctx=ctx_live,
        trade_dates=trade_dates,
        cfg=cfg_live,
        confirm_bars=confirm_bars,
        gate=gate,
        label="LIVE_NEXT_AM",
    )

    # 2) E@13:20 proxy: same-day EOD pool (≈13:20 list) + fill ≥13:20
    cfg_e = replace(
        champ,
        variant_id="E_1320",
        label="E 名單(≈13:20) → E ≥13:20 成交",
        timing_mode="poll_5m",
        no_trade_before="13:20",
    )
    p_e, s_e, _ = _run_sim(
        conn,
        ctx=ctx_eod,
        trade_dates=trade_dates,
        cfg=cfg_e,
        confirm_bars=confirm_bars,
        gate=gate,
        label="E_1320",
    )

    live_row = _variant_row(
        variant_id="LIVE_NEXT_AM",
        label="E 收盤名單 → E+1 ≥09:30（真 live）",
        periods=p_live,
        summary=s_live,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
        extra={"pit_clean": True, "live_executable": True},
    )
    lf, li, lo = (
        live_row["slices"]["full"],
        live_row["slices"]["is"],
        live_row["slices"]["oos"],
    )

    # Matched: same LIVE legs, fill at signal-day (prior) 13:20 instead of E+1 09:30
    print("  post-hoc LIVE_MATCHED_1320 …", flush=True)
    p_m, meta_m = apply_fills(
        conn,
        p_live,
        full_dates=full_dates,
        mode="prior",
        fill_minute="13:20",
        kbar_cache=ctx_live["kbar_cache"],
    )
    print(f"    filled={meta_m['n_filled']}/{meta_m['n_baseline']}", flush=True)
    matched_row = _variant_row(
        variant_id="LIVE_MATCHED_1320",
        label="同批 LIVE 腿 · 改在訊號日 E@13:20 成交（配對時點）",
        periods=p_m,
        summary=None,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
        base_full=lf,
        base_is=li,
        base_oos=lo,
        extra={
            "pit_clean": False,
            "live_executable": True,
            "fill_meta": meta_m,
            "note": "answers: same trades, buy E 13:20 vs E+1 09:30",
        },
    )

    e_row = _variant_row(
        variant_id="E_1320",
        label="當日 EOD 池 → E ≥13:20（槽位重跑；交易集合≠LIVE）",
        periods=p_e,
        summary=s_e,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
        base_full=lf,
        base_is=li,
        base_oos=lo,
        extra={
            "pit_clean": False,
            "live_executable": True,
            "note": "n≃B0 · not matched to LIVE lag pool — interpret with caution",
        },
    )

    variants = [live_row, matched_row, e_row]

    snap_meta: dict[str, Any] | None = None
    if run_snap_1320:
        print("  sim SNAP_1320 (full provisional RRG @13:20) …", flush=True)
        cfg_snap = replace(
            champ,
            variant_id="SNAP_1320",
            label="E 13:20 暫定 RRG → E 13:20 成交（PIT）",
            timing_mode="snapshot_1300",
            no_trade_before="13:20",
            snapshot_strict_kbar=False,
        )
        with _snapshot_minute("13:20"):
            p_s, s_s, _ = _run_sim(
                conn,
                ctx={**ctx_eod, "kbar_cache": {}},
                trade_dates=trade_dates,
                cfg=cfg_snap,
                confirm_bars=confirm_bars,
                gate=gate,
                label="SNAP_1320",
            )
        snap_meta = {
            "snapshot_days_skipped": (s_s or {}).get("snapshot_days_skipped"),
            "n_legs": len(p_s),
        }
        variants.append(
            _variant_row(
                variant_id="SNAP_1320",
                label="E 13:20 暫定 RRG + 成交（真 SNAP）",
                periods=p_s,
                summary=s_s,
                date_start=date_start,
                is_end=is_end,
                oos_start=oos_start,
                end=end,
                base_full=lf,
                base_is=li,
                base_oos=lo,
                extra={"pit_clean": True, "live_executable": True, "snap_meta": snap_meta},
            )
        )

    adopt: dict[str, str] = {}
    for v in variants:
        if v["variant_id"] == "LIVE_NEXT_AM":
            continue
        n_oos = int((v["slices"]["oos"] or {}).get("n_legs") or 0)
        # Matched timing uses LIVE OOS n (tiny); still report gate but flag sample
        adopt[v["variant_id"]] = _adopt(
            n_oos=n_oos,
            d_oos=v.get("delta_oos_excess_pp"),
            d_is=v.get("delta_is_excess_pp"),
        )

    m_oos = matched_row.get("delta_oos_excess_pp")
    m_full = matched_row.get("delta_full_excess_pp")
    live_oos_n = int((lo or {}).get("n_legs") or 0)
    prefer = "KEEP_LIVE_NEXT_AM"
    # Primary decision = matched timing (same trades). Require IS ok + FULL edge;
    # if OOS n too thin, mark research-only.
    matched_ok_full = (
        meta_m.get("n_filled", 0) >= 8
        and m_full is not None
        and float(m_full) >= ADOPT_OOS_MIN_DELTA_PP
        and matched_row.get("delta_is_excess_pp") is not None
        and float(matched_row["delta_is_excess_pp"]) >= ADOPT_IS_MIN_DELTA_PP
    )
    if matched_ok_full and adopt.get("LIVE_MATCHED_1320") == "GO":
        prefer = "CONSIDER_BUY_AT_SIGNAL_1320"
    elif matched_ok_full and live_oos_n < ADOPT_OOS_MIN_N:
        prefer = "RESEARCH_ONLY_MATCHED_1320_THIN_OOS"
    if adopt.get("SNAP_1320") == "GO":
        prefer = "CONSIDER_SNAP_1320"

    return {
        "schema": "c18acc_live_vs_e1320-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_meta": gate_meta,
        "snap_meta": snap_meta,
        "matched_fill_meta": meta_m,
        "variants": variants,
        "verdict": {
            "baseline": "LIVE_NEXT_AM",
            "primary_contrast": "LIVE_MATCHED_1320",
            "adopt": adopt,
            "recommendation": prefer,
            "caveat": (
                f"LIVE OOS n={live_oos_n} (thin). E_1320 n={len(p_e)} uses same-day pool "
                f"≠ LIVE lag pool — do not treat E_1320 Δ as pure timing edge. "
                f"Use LIVE_MATCHED_1320 for same-trade timing."
            ),
            "summary": (
                f"MATCHED_1320 vs LIVE FULL Δ={m_full}pp "
                f"filled={meta_m.get('n_filled')}/{meta_m.get('n_baseline')} · "
                f"OOS Δ={m_oos}pp → {adopt.get('LIVE_MATCHED_1320')} · "
                f"E_1320 OOS Δ={e_row.get('delta_oos_excess_pp')}pp "
                f"(unmatched set) → {adopt.get('E_1320')}"
                + (
                    f" · SNAP_1320 Δ={next(x for x in variants if x['variant_id']=='SNAP_1320').get('delta_oos_excess_pp')}pp "
                    f"→ {adopt.get('SNAP_1320')}"
                    if any(x["variant_id"] == "SNAP_1320" for x in variants)
                    else ""
                )
            ),
        },
    }


def render_live_vs_e1320_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · LIVE(E+1 09:30) vs E@13:20",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        "## 對照語意",
        "",
        "| id | 訊號 | 成交 | PIT |",
        "|----|------|------|-----|",
        "| LIVE_NEXT_AM | E 收盤 fresh | E+1 ≥09:30 | 是 |",
        "| LIVE_MATCHED_1320 | 同上（同批腿） | 訊號日 E@13:20 | 配對時點 |",
        "| E_1320 | 當日 EOD 池 | E ≥13:20 | 交易集合≠LIVE |",
        "| SNAP_1320 | E 13:20 暫定 RRG | E 13:20 | 是 |",
        "",
        f"## 結論：{v.get('recommendation')}",
        "",
        f"- {v.get('summary')}",
        f"- 注意：{v.get('caveat')}",
        "",
        "## 結果（Δ vs LIVE_NEXT_AM）",
        "",
        "| variant | window | n | mean_excess% | Δ vs LIVE | adopt |",
        "|---------|--------|--:|-------------:|----------:|-------|",
    ]
    adopt = v.get("adopt") or {}
    for row in payload.get("variants") or []:
        vid = row["variant_id"]
        for win, key, dkey in (
            ("FULL", "full", "delta_full_excess_pp"),
            ("IS", "is", "delta_is_excess_pp"),
            ("OOS", "oos", "delta_oos_excess_pp"),
        ):
            s = (row.get("slices") or {}).get(key) or {}
            d = row.get(dkey)
            d_s = "—" if d is None else f"{d:+.4f}pp"
            a = "—" if win != "OOS" or vid == "LIVE_NEXT_AM" else adopt.get(vid, "—")
            lines.append(
                f"| {vid} | {win} | {s.get('n_legs')} | {s.get('mean_excess_pct')} | {d_s} | {a} |"
            )
    if payload.get("snap_meta"):
        lines.extend(
            [
                "",
                f"SNAP meta: `{payload.get('snap_meta')}`",
            ]
        )
    lines.extend(
        [
            "",
            "門檻：OOS n≥8 · Δ≥+0.5pp · IS Δ≥−0.3pp（相對 LIVE_NEXT_AM）",
            "",
            "模組：`src/research/backtest/c18acc_live_vs_e1320_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
