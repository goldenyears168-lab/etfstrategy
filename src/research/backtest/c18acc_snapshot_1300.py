"""C18acc snapshot_1300 · 13:00 full_rrg 訊號 + 13:00 成交（research）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from research.backtest.c18acc_weekday_execution_sweep import (
    IS_END_DEFAULT,
    OOS_START_DEFAULT,
    TOTAL_CAPITAL_NTD,
    _delta_pp,
    _finite_mean,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH, rank_shortlist_full_rrg
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    TimingMode,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_mono_daily_brief import ScanRow
from rrg_rotation import compute_rrg_panel
from rrg_universe_snapshot import kbar_close_at_minute
from stock_db.kbar import load_kbar_day_closes

SNAPSHOT_1300_MINUTE = "13:00"
SNAPSHOT_1300_MINUTE_SQL = "13:00:00"
SAMPLE_START_DEFAULT = "2021-01-01"
BENCH_ID = "IX0001"
OOS_PASS_DELTA_PP = -0.3
BYPASS_RETENTION_MIN = 0.7


@dataclass(frozen=True)
class Snapshot1300Panels:
    rs_ratio: pd.DataFrame
    rs_mom: pd.DataFrame
    spread_rs: pd.DataFrame | None


def snapshot_px_strict(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> float | None:
    """13:00 kbar close · 無 fallback（缺資料 → None）。"""
    key = (stock_id, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, stock_id, trade_date)
    if not kbar_cache[key]:
        return None
    px = kbar_close_at_minute(conn, stock_id, trade_date, SNAPSHOT_1300_MINUTE)
    if px is None or px <= 0:
        return None
    return float(px)


def collect_snapshot_stock_ids(
    slots: list[dict[str, Any]],
    *row_lists: list[ScanRow],
) -> set[str]:
    ids: set[str] = {str(p["stock_id"]) for p in slots}
    for rows in row_lists:
        ids.update(r.stock_id for r in rows)
    return ids


def snapshot_day_kbar_ready(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    stock_ids: set[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> bool:
    if not stock_ids:
        return False
    for sid in stock_ids:
        if snapshot_px_strict(conn, sid, trade_date, kbar_cache=kbar_cache) is None:
            return False
    return True


def _provisional_close_bench(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
    stock_ids: set[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[pd.DataFrame, pd.Series] | None:
    if trade_date not in close.index:
        return None
    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    for sid in stock_ids:
        if sid not in prov.columns:
            continue
        px = snapshot_px_strict(conn, sid, trade_date, kbar_cache=kbar_cache)
        if px is None:
            return None
        prov.at[trade_date, sid] = px
    bpx = kbar_close_at_minute(conn, BENCH_ID, trade_date, SNAPSHOT_1300_MINUTE)
    if bpx is None or bpx <= 0:
        if trade_date not in bench_p.index:
            return None
        bpx = float(bench_p.at[trade_date])
    if bpx <= 0:
        return None
    bench_p.at[trade_date] = float(bpx)
    return prov, bench_p


def resolve_snapshot_1300_panels(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    as_of: str,
    stock_ids: set[str],
    rs_ratio: pd.DataFrame | None,
    rs_mom: pd.DataFrame | None,
    spread_rs_panel: pd.DataFrame | None,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> Snapshot1300Panels | None:
    if rs_ratio is None or rs_mom is None or not stock_ids:
        return None
    if not snapshot_day_kbar_ready(
        conn, trade_date=as_of, stock_ids=stock_ids, kbar_cache=kbar_cache
    ):
        return None
    prov_pair = _provisional_close_bench(
        conn,
        close=close,
        bench=bench,
        trade_date=as_of,
        stock_ids=stock_ids,
        kbar_cache=kbar_cache,
    )
    if prov_pair is None:
        return None
    prov_close, prov_bench = prov_pair
    rs_r, rs_m, _ = compute_rrg_panel(prov_close, prov_bench, length=LENGTH)
    spread = build_daily_spread_rs_panel(prov_close, prov_bench) if spread_rs_panel is not None else None
    return Snapshot1300Panels(rs_ratio=rs_r, rs_mom=rs_m, spread_rs=spread)


def rerank_shortlist_snapshot_1300(
    shortlist: list[ScanRow],
    *,
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
) -> list[ScanRow]:
    if not shortlist:
        return []
    return rank_shortlist_full_rrg(
        shortlist,
        conn=conn,
        close=close,
        bench=bench,
        trade_date=trade_date,
        minute=SNAPSHOT_1300_MINUTE_SQL,
        kbar_cache=kbar_cache,
        full_rrg_cache=full_rrg_cache,
    )


def fill_empty_slots_snapshot_1300(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    rows: list[ScanRow],
    slots: list[dict[str, Any]],
    close: pd.DataFrame,
    bench: pd.Series,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    n_slots: int,
    entry_leg: str,
    full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
) -> int:
    """Fill free slots at 13:00 · full_rrg rank · strict kbar px."""
    used = {int(p["slot"]) for p in slots}
    held = {str(p["stock_id"]) for p in slots}
    free = [i for i in range(n_slots) if i not in used]
    if not free or not rows:
        return 0
    ranked = rerank_shortlist_snapshot_1300(
        rows,
        conn=conn,
        close=close,
        bench=bench,
        trade_date=as_of,
        kbar_cache=kbar_cache,
        full_rrg_cache=full_rrg_cache,
    )
    added = 0
    for row in ranked:
        if not free:
            break
        if row.stock_id in held:
            continue
        px = snapshot_px_strict(conn, row.stock_id, as_of, kbar_cache=kbar_cache)
        if px is None:
            continue
        slot = free.pop(0)
        slots.append(
            {
                "slot": slot,
                "stock_id": row.stock_id,
                "stock_name": row.stock_name,
                "signal_date": as_of,
                "entry_date": as_of,
                "entry_px": px,
                "seg_last": round(row.seg_last, 4),
                "disp": round(row.disp, 4),
                "rs_momentum": float(row.rs_momentum),
                "seg_step_delta": row.segs[-1] - row.segs[-2] if len(row.segs) >= 2 else 0.0,
                "entry_minute": SNAPSHOT_1300_MINUTE,
                "entry_leg": entry_leg,
            }
        )
        held.add(row.stock_id)
        added += 1
    return added


def audit_shortlist_kbar_coverage(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    fresh_by_date: dict[str, list[ScanRow]],
    kbar_cache: dict | None = None,
) -> dict[str, Any]:
    cache: dict = kbar_cache if kbar_cache is not None else {}
    checks = 0
    hits = 0
    for d in trade_dates:
        shortlist = fresh_by_date.get(d, [])
        if not shortlist:
            continue
        sids = {r.stock_id for r in shortlist}
        checks += len(sids)
        if snapshot_day_kbar_ready(conn, trade_date=d, stock_ids=sids, kbar_cache=cache):
            hits += len(sids)
        else:
            for sid in sids:
                if snapshot_px_strict(conn, sid, d, kbar_cache=cache) is not None:
                    hits += 1
    return {
        "stock_days": checks,
        "kbar_hits": hits,
        "coverage_pct": round(hits / checks * 100.0, 2) if checks else 0.0,
        "minute": SNAPSHOT_1300_MINUTE,
    }


def build_snapshot_variant_config(
    variant_id: str,
    *,
    timing_mode: TimingMode,
    label: str,
    accel_sell_bypass_on_spread_lost: bool,
    n_slots: int = 3,
) -> ScoreSwapCConfig:
    from dataclasses import replace

    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s2_base = configs["S2"]
    champ = champion_score_swap_c_config()
    return replace(
        s2_base,
        timing_mode=timing_mode,
        candidate_pool="fresh_union_accel",
        variant_id=variant_id,
        label=label,
        n_slots=max(1, int(n_slots)),
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        accel_sell_bypass_on_spread_lost=accel_sell_bypass_on_spread_lost,
        entry_leg=champ.entry_leg,
    )


def preregistered_variants(*, n_slots: int = 3) -> list[ScoreSwapCConfig]:
    champ = champion_score_swap_c_config()
    close_cfg = close_champion_score_swap_c_config()
    return [
        build_snapshot_variant_config(
            "S0",
            timing_mode=champ.timing_mode,
            label="champion · poll_5m",
            accel_sell_bypass_on_spread_lost=True,
            n_slots=n_slots,
        ),
        build_snapshot_variant_config(
            "S1",
            timing_mode=close_cfg.timing_mode,
            label="close · 日 K 訊號+收盤",
            accel_sell_bypass_on_spread_lost=True,
            n_slots=n_slots,
        ),
        build_snapshot_variant_config(
            "S2",
            timing_mode="snapshot_1300",
            label="snapshot_1300 · 13:00 full_rrg A+B",
            accel_sell_bypass_on_spread_lost=True,
            n_slots=n_slots,
        ),
        build_snapshot_variant_config(
            "S2b",
            timing_mode="snapshot_1300",
            label="snapshot_1300 · bypass off",
            accel_sell_bypass_on_spread_lost=False,
            n_slots=n_slots,
        ),
    ]


def _run_variant_full(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    date_start: str,
    date_end: str,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame | None,
    full_dates: list[str],
    fresh_by_date: dict,
    mono_by_date: dict,
    zone_by_date: dict[str, str],
    c0_cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    full_rrg_cache: dict,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 5:
        return {"variant_id": cfg.variant_id, "error": "insufficient_trade_dates"}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        mono_by_date=mono_by_date,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        spread_rs_panel=spread_rs_panel if cfg.accel_sell_bypass_on_spread_lost else None,
        kbar_cache=kbar_cache,
        entry_c_config=c0_cfg,
        snapshot_full_rrg_cache=full_rrg_cache,
    )
    leg_sum = summarize_periods(periods)
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=TOTAL_CAPITAL_NTD,
        n_slots=cfg.n_slots,
        close=close,
    )
    return {
        "variant_id": cfg.variant_id,
        "label": cfg.label,
        "timing_mode": cfg.timing_mode,
        "accel_sell_bypass_on_spread_lost": cfg.accel_sell_bypass_on_spread_lost,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "mean_return_pct": _finite_mean([p.get("return_pct") for p in periods]),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": summary.get("force_exits"),
        "snapshot_days_skipped": summary.get("snapshot_days_skipped"),
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }


def _eval_hypotheses(payload: dict[str, Any]) -> dict[str, Any]:
    by_id = {v["variant_id"]: v for v in payload.get("variants") or []}
    full = payload.get("windows") or {}
    s0_full = (full.get("FULL") or {}).get("S0") or {}
    s0_oos = (full.get("OOS") or {}).get("S0") or {}
    s1_full = (full.get("FULL") or {}).get("S1") or {}
    s2_full = (full.get("FULL") or {}).get("S2") or {}
    s2_oos = (full.get("OOS") or {}).get("S2") or {}
    s2b_full = (full.get("FULL") or {}).get("S2b") or {}

    cov = payload.get("kbar_coverage") or {}
    cov_pct = float(cov.get("coverage_pct") or 0)

    s0_ex = s0_full.get("mean_excess_pct")
    s2_ex = s2_full.get("mean_excess_pct")
    s2_oos_ex = s2_oos.get("mean_excess_pct")
    s1_ex = s1_full.get("mean_excess_pct")

    bypass_s0 = _delta_pp(
        (full.get("FULL") or {}).get("S2b", {}).get("mean_excess_pct"),
        s2_full.get("mean_excess_pct"),
    )
    bypass_s2 = _delta_pp(s2b_full.get("mean_excess_pct"), s2_full.get("mean_excess_pct"))
    bypass_poll = _delta_pp(
        (full.get("FULL") or {}).get("S0", {}).get("mean_excess_pct"),
        (full.get("FULL") or {}).get("S2b", {}).get("mean_excess_pct"),
    )

    retention = None
    if bypass_poll is not None and abs(float(bypass_poll)) > 1e-9 and bypass_s2 is not None:
        retention = round(float(bypass_s2) / float(bypass_poll), 4)

    return {
        "H-S1300-0": {
            "pass": cov_pct >= 85.0,
            "coverage_pct": cov_pct,
            "threshold_pct": 85.0,
        },
        "H-S1300-1": {
            "pass": s2_ex is not None and s0_ex is not None and float(s2_ex) >= float(s0_ex) - 0.3,
            "delta_pp": _delta_pp(s2_ex, s0_ex),
            "threshold_pp": -0.3,
        },
        "H-S1300-2": {
            "pass": s2_oos_ex is not None
            and s0_oos.get("mean_excess_pct") is not None
            and float(s2_oos_ex) >= float(s0_oos["mean_excess_pct"]) - 0.3,
            "delta_pp": _delta_pp(s2_oos_ex, s0_oos.get("mean_excess_pct")),
            "threshold_pp": -0.3,
        },
        "H-S1300-3": {
            "pass": retention is not None and retention >= BYPASS_RETENTION_MIN,
            "retention_ratio": retention,
            "bypass_lift_s2_pp": bypass_s2,
            "bypass_lift_s0_pp": bypass_poll,
        },
        "H-S1300-5": {
            "pass": s2_ex is not None and s1_ex is not None and float(s2_ex) >= float(s1_ex) - 0.2,
            "delta_pp": _delta_pp(s2_ex, s1_ex),
            "threshold_pp": -0.2,
        },
    }


def run_c18acc_snapshot_1300_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = SAMPLE_START_DEFAULT,
    date_end: str,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    n_slots: int = 3,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    kbar_cache: dict = {}
    full_rrg_cache: dict = {}
    kbar_cov = audit_shortlist_kbar_coverage(
        conn, trade_dates=trade_dates, fresh_by_date=fresh_by_date, kbar_cache=kbar_cache
    )

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    variants = preregistered_variants(n_slots=n_slots)

    windows: dict[str, dict[str, Any]] = {}
    for win_label, d0, d1 in (
        ("FULL", date_start, date_end),
        ("IS", date_start, is_end),
        ("OOS", oos_start, date_end),
    ):
        windows[win_label] = {}
        for cfg in variants:
            spread = (
                build_daily_spread_rs_panel(close, bench)
                if cfg.accel_sell_bypass_on_spread_lost
                else None
            )
            windows[win_label][cfg.variant_id] = _run_variant_full(
                conn,
                cfg=cfg,
                date_start=d0,
                date_end=d1,
                close=close,
                bench=bench,
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                spread_rs_panel=spread,
                full_dates=full_dates,
                fresh_by_date=fresh_by_date,
                mono_by_date=mono_by_date,
                zone_by_date=zone_by_date,
                c0_cfg=c0_cfg,
                kbar_cache=kbar_cache,
                full_rrg_cache=full_rrg_cache,
            )

    payload = {
        "schema": "c18acc_snapshot_1300_sweep-v1",
        "topic_id": "c18acc-snapshot-1300",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "snapshot_minute": SNAPSHOT_1300_MINUTE,
        "kbar_coverage": kbar_cov,
        "variants": [v.to_dict() for v in variants],
        "windows": windows,
    }
    payload["hypotheses"] = _eval_hypotheses(payload)
    return payload


def render_c18acc_snapshot_1300_sweep_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · snapshot_1300 sweep",
        "",
        f"- sample: **{payload.get('date_start')}** → **{payload.get('date_end')}**",
        f"- OOS: **{payload.get('oos_start')}** → **{payload.get('date_end')}**",
        f"- snapshot: **{payload.get('snapshot_minute')}** · full_rrg 訊號 + 成交",
        "",
        "## Kbar coverage",
        "",
    ]
    cov = payload.get("kbar_coverage") or {}
    lines.append(
        f"- POOL1 shortlist stock-days: {cov.get('kbar_hits')}/{cov.get('stock_days')} "
        f"({cov.get('coverage_pct')}%)"
    )
    lines.extend(["", "## Variants · FULL", "", "| id | timing | bypass | excess | swaps | skip |", "|---|---|---|---:|---:|---:|"])
    full = (payload.get("windows") or {}).get("FULL") or {}
    for vid in ("S0", "S1", "S2", "S2b"):
        row = full.get(vid) or {}
        lines.append(
            f"| {vid} | {row.get('timing_mode')} | {row.get('accel_sell_bypass_on_spread_lost')} | "
            f"{row.get('mean_excess_pct')} | {row.get('swaps_total')} | {row.get('snapshot_days_skipped')} |"
        )
    lines.extend(["", "## Hypotheses", ""])
    for hid, h in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{hid}**: pass={h.get('pass')} · {h}")
    return "\n".join(lines) + "\n"
