"""C18acc POOL1+S2 · weekday execution gate sweep (Phase 1–4)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_rotation_force_exit_sweep import _cohort_stats, _rotation_cohort_periods
from research.backtest.c18acc_rotation_selective_exit_sweep import rotation_selective_exit_sweep_configs
from research.backtest.c18acc_weekday_gate import WeekdayGateConfig, leg_fingerprint, trading_days_between
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel

IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-01"
TOTAL_CAPITAL_NTD = 30_000.0

ALL_VARIANTS: tuple[str, ...] = (
    "W0",
    "E0",
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
    "S0",
    "S1",
    "S2",
    "S3",
    "X0",
    "X1",
    "X2",
    "X3",
    "H1",
    "F1",
    "HF1",
)

PHASE2_VARIANTS: tuple[str, ...] = ("W0", "H1", "F1", "HF1")
FRIDAY_SWAP_MARGIN_DEFAULT = 0.03


def _gate_for_variant(vid: str) -> WeekdayGateConfig:
    if vid == "W0":
        return WeekdayGateConfig(variant_id="W0")
    if vid == "H1":
        return WeekdayGateConfig(variant_id="H1", max_hold_exit_align="friday")
    if vid == "F1":
        return WeekdayGateConfig(variant_id="F1", friday_swap_margin=FRIDAY_SWAP_MARGIN_DEFAULT)
    if vid == "HF1":
        return WeekdayGateConfig(
            variant_id="HF1",
            max_hold_exit_align="friday",
            friday_swap_margin=FRIDAY_SWAP_MARGIN_DEFAULT,
        )
    if vid.startswith("E"):
        return WeekdayGateConfig(variant_id=vid, entry_mode=vid)  # type: ignore[arg-type]
    if vid.startswith("S"):
        return WeekdayGateConfig(variant_id=vid, swap_mode=vid)  # type: ignore[arg-type]
    if vid.startswith("X"):
        return WeekdayGateConfig(variant_id=vid, exit_mode=vid)  # type: ignore[arg-type]
    if vid.startswith("C"):
        raise ValueError(f"combo {vid} must be built via combo_gates()")
    raise ValueError(f"unknown variant {vid}")


def combo_gates(best_entry: str, best_swap: str, best_exit: str) -> list[WeekdayGateConfig]:
    entry = best_entry if best_entry.startswith("E") else "E0"
    swap = best_swap if best_swap.startswith("S") else "S0"
    exit_m = best_exit if best_exit.startswith("X") else "X0"
    return [
        WeekdayGateConfig(variant_id="C1", entry_mode=entry, swap_mode="S0", exit_mode="X0"),  # type: ignore[arg-type]
        WeekdayGateConfig(variant_id="C2", entry_mode="E0", swap_mode=swap, exit_mode="X0"),  # type: ignore[arg-type]
        WeekdayGateConfig(
            variant_id="C3",
            entry_mode=entry,  # type: ignore[arg-type]
            swap_mode=swap,  # type: ignore[arg-type]
            exit_mode="X0",
        ),
        WeekdayGateConfig(
            variant_id="C4",
            entry_mode=entry,  # type: ignore[arg-type]
            swap_mode=swap,  # type: ignore[arg-type]
            exit_mode=exit_m,  # type: ignore[arg-type]
        ),
    ]


def weekday_execution_gates(*, variants: tuple[str, ...]) -> list[WeekdayGateConfig]:
    return [_gate_for_variant(v) for v in variants if not v.startswith("C")]


def build_pool1_s2_config(*, n_slots: int = 3) -> ScoreSwapCConfig:
    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s2_base = configs["S2"]
    champ = champion_score_swap_c_config()
    return replace(
        s2_base,
        timing_mode=champ.timing_mode,
        candidate_pool="fresh_union_accel",
        variant_id="POOL1-P5M",
        n_slots=max(1, int(n_slots)),
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        accel_sell_bypass_on_spread_lost=champ.accel_sell_bypass_on_spread_lost,
    )


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _finite_mean(values: list[Any]) -> float | None:
    vals: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:
            vals.append(f)
    return round(sum(vals) / len(vals), 4) if vals else None


def _delta_pp(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def _mean_signal_to_entry_lag(periods: list[dict[str, Any]], full_dates: list[str]) -> float | None:
    lags = [
        trading_days_between(full_dates, str(p.get("signal_date") or p["entry_date"]), str(p["entry_date"]))
        for p in periods
    ]
    return round(sum(lags) / len(lags), 3) if lags else None


def _run_variant_window(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    gate: WeekdayGateConfig,
    date_start: str,
    date_end: str,
    label: str,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame,
    full_dates: list[str],
    fresh_by_date: dict[str, list],
    mono_by_date: dict[str, list],
    zone_by_date: dict[str, str],
    c0_cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    collect_fingerprints: bool = False,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 5:
        return {
            "label": label,
            "error": "insufficient_trade_dates",
            "gate_variant_id": gate.variant_id,
            "n_trade_dates": len(trade_dates),
        }

    cfg_run = replace(cfg, variant_id=f"{cfg.variant_id}+{gate.variant_id}")
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg_run,
        mono_by_date=mono_by_date,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        spread_rs_panel=spread_rs_panel,
        kbar_cache=kbar_cache,
        entry_c_config=c0_cfg,
        weekday_gate=gate,
    )
    leg_sum = summarize_periods(periods)
    leg_sum["mean_return_pct"] = _finite_mean([p.get("return_pct") for p in periods])
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=TOTAL_CAPITAL_NTD,
        n_slots=cfg.n_slots,
        close=close,
    )
    rot = _cohort_stats(_rotation_cohort_periods(periods, rs_ratio, rs_mom, full_dates))

    row: dict[str, Any] = {
        "label": label,
        "gate_variant_id": gate.variant_id,
        "gate": gate.to_dict(),
        "config_variant_id": cfg_run.variant_id,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "calendar_years": round(_calendar_years(date_start, date_end), 3),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "mean_return_pct": leg_sum.get("mean_return_pct"),
        "win_rate_vs_bench_pct": leg_sum.get("win_rate_vs_bench_pct"),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": summary.get("force_exits"),
        "max_hold_exits": summary.get("max_hold_exits"),
        "signals_deferred": summary.get("signals_deferred"),
        "signals_skipped_expired": summary.get("signals_skipped_expired"),
        "swaps_deferred": summary.get("swaps_deferred"),
        "swaps_lost": summary.get("swaps_lost"),
        "slot_util_pct": summary.get("slot_util_pct"),
        "mean_signal_to_entry_lag": _mean_signal_to_entry_lag(periods, full_dates),
        "total_entries": summary.get("total_entries"),
        "rotation_cohort": rot,
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "sharpe_ratio": port.get("sharpe_ratio"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }
    if collect_fingerprints:
        row["_fingerprints"] = [leg_fingerprint(p) for p in periods]
    return row


def _best_phase_variant(
    by_gate: dict[str, dict[str, Any]],
    *,
    prefix: str,
    baseline: str,
    window: str = "OOS",
    min_delta: float = -0.2,
) -> str | None:
    base_ex = float((by_gate.get(baseline, {}).get(window) or {}).get("mean_excess_pct") or 0)
    best: str | None = None
    best_ex = base_ex + min_delta - 1
    for vid, wins in by_gate.items():
        if not vid.startswith(prefix) or vid == baseline:
            continue
        ex = (wins.get(window) or {}).get("mean_excess_pct")
        if ex is None:
            continue
        if float(ex) >= base_ex + min_delta and float(ex) > best_ex:
            best_ex = float(ex)
            best = vid
    return best


def _eval_hypotheses(
    by_gate: dict[str, dict[str, Any]],
    *,
    w0_n_full: int,
) -> dict[str, Any]:
    w0 = by_gate.get("W0", {})
    w0_full = w0.get("FULL") or {}
    w0_oos = w0.get("OOS") or {}
    w0_rot = w0_full.get("rotation_cohort") or {}

    def _g(vid: str, win: str) -> dict[str, Any]:
        return by_gate.get(vid, {}).get(win) or {}

    e1_full = _g("E1", "FULL")
    e3_oos = _g("E3", "OOS")
    e4_full = _g("E4", "FULL")
    s1_full = _g("S1", "FULL")
    x3_full = _g("X3", "FULL")

    e1_cap = round(100.0 * int(e1_full.get("n_periods") or 0) / w0_n_full, 2) if w0_n_full else None

    h1_full = _g("H1", "FULL")
    h1_oos = _g("H1", "OOS")
    f1_full = _g("F1", "FULL")
    f1_oos = _g("F1", "OOS")
    hf1_full = _g("HF1", "FULL")
    hf1_oos = _g("HF1", "OOS")

    def _phase2_pass(full_row: dict[str, Any], oos_row: dict[str, Any]) -> bool:
        d_full = _delta_pp(full_row.get("mean_excess_pct"), w0_full.get("mean_excess_pct"))
        d_oos = _delta_pp(oos_row.get("mean_excess_pct"), w0_oos.get("mean_excess_pct"))
        return (
            d_full is not None
            and d_oos is not None
            and float(d_full) >= -0.2
            and float(d_oos) >= -0.2
        )

    h: dict[str, Any] = {
        "H-WD-E1": {
            "full_delta_excess_pp": _delta_pp(e1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "oos_delta_excess_pp": _delta_pp(_g("E1", "OOS").get("mean_excess_pct"), w0_oos.get("mean_excess_pct")),
            "capture_rate_pct": e1_cap,
            "pass": (
                _delta_pp(e1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) is not None
                and float(_delta_pp(e1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) or -999) >= 0.2
                and e1_cap is not None
                and e1_cap >= 85.0
            ),
        },
        "H-WD-E3": {
            "oos_delta_excess_pp": _delta_pp(e3_oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct")),
            "pass": (
                _delta_pp(e3_oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct")) is not None
                and float(_delta_pp(e3_oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct")) or -999) >= -0.2
            ),
        },
        "H-WD-S1": {
            "full_delta_excess_pp": _delta_pp(s1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "pass": (
                _delta_pp(s1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) is not None
                and float(_delta_pp(s1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) or -999) >= -0.2
                and float((s1_full.get("rotation_cohort") or {}).get("mean_return_pct") or -999)
                >= float(w0_rot.get("mean_return_pct") or -999)
            ),
        },
        "H-WD-X3": {
            "full_delta_excess_pp": _delta_pp(x3_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "mdd_delta_pp": _delta_pp(
                (x3_full.get("portfolio") or {}).get("max_drawdown_pct"),
                (w0_full.get("portfolio") or {}).get("max_drawdown_pct"),
            ),
            "pass": (
                _delta_pp(x3_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) is not None
                and float(_delta_pp(x3_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) or -999) >= -0.2
                and float((x3_full.get("portfolio") or {}).get("max_drawdown_pct") or -999)
                >= float((w0_full.get("portfolio") or {}).get("max_drawdown_pct") or -999)
            ),
        },
        "H-WD-NEG": {
            "full_delta_excess_pp": _delta_pp(e4_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "pass": (
                _delta_pp(e4_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) is not None
                and float(_delta_pp(e4_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) or 999) <= -0.3
            ),
        },
        "H-WD-H1": {
            "full_delta_excess_pp": _delta_pp(h1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "oos_delta_excess_pp": _delta_pp(h1_oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct")),
            "max_hold_exits": h1_full.get("max_hold_exits"),
            "pass": _phase2_pass(h1_full, h1_oos),
        },
        "H-WD-F1": {
            "full_delta_excess_pp": _delta_pp(f1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "oos_delta_excess_pp": _delta_pp(f1_oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct")),
            "swaps_total": f1_full.get("swaps_total"),
            "pass": _phase2_pass(f1_full, f1_oos),
        },
        "H-WD-HF1": {
            "full_delta_excess_pp": _delta_pp(hf1_full.get("mean_excess_pct"), w0_full.get("mean_excess_pct")),
            "oos_delta_excess_pp": _delta_pp(hf1_oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct")),
            "pass": _phase2_pass(hf1_full, hf1_oos),
        },
    }

    combo_best: dict[str, Any] | None = None
    for vid in ("C1", "C2", "C3", "C4"):
        oos = _g(vid, "OOS")
        if not oos:
            continue
        d = _delta_pp(oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct"))
        cap = round(100.0 * int(oos.get("n_periods") or 0) / int(w0_oos.get("n_periods") or 1), 2)
        if combo_best is None or float(oos.get("mean_excess_pct") or -999) > float(combo_best.get("mean_excess") or -999):
            combo_best = {"variant_id": vid, "delta_oos_pp": d, "capture_rate_pct": cap, "mean_excess": oos.get("mean_excess_pct")}

    if combo_best:
        h["H-WD-C"] = {
            "best_variant": combo_best["variant_id"],
            "oos_delta_excess_pp": combo_best["delta_oos_pp"],
            "capture_rate_pct": combo_best["capture_rate_pct"],
            "pass": (
                combo_best["delta_oos_pp"] is not None
                and float(combo_best["delta_oos_pp"]) >= 0.2
                and float(combo_best["capture_rate_pct"] or 0) >= 80.0
            ),
        }
    else:
        h["H-WD-C"] = {"pass": False, "best_variant": None}

    return h


def _overall_verdict(hyp: dict[str, Any]) -> str:
    if hyp.get("H-WD-C", {}).get("pass"):
        return "ADOPT"
    if any(
        hyp.get(k, {}).get("pass")
        for k in ("H-WD-H1", "H-WD-F1", "H-WD-HF1", "H-WD-E1", "H-WD-E3", "H-WD-S1", "H-WD-X3")
    ):
        return "EXEC_NOTE_ONLY"
    return "NO_GO"


def run_c18acc_weekday_execution_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2020-01-02",
    date_end: str = "2026-07-03",
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    n_slots: int = 3,
    variants: tuple[str, ...] = ALL_VARIANTS,
    include_combos: bool = True,
) -> dict[str, Any]:
    cfg = build_pool1_s2_config(n_slots=n_slots)
    gates = weekday_execution_gates(variants=variants)
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict = {}

    windows = [
        ("IS", "2024-01-01", is_end),
        ("OOS", oos_start, date_end),
        ("FULL", date_start, date_end),
    ]

    rows: list[dict[str, Any]] = []
    for gate in gates:
        fp = gate.variant_id in ("W0", "E0", "S0", "X0")
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            rows.append(
                _run_variant_window(
                    conn,
                    cfg=cfg,
                    gate=gate,
                    date_start=w_start,
                    date_end=w_end,
                    label=win_label,
                    close=close,
                    bench=bench,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    spread_rs_panel=spread_rs_panel,
                    full_dates=full_dates,
                    fresh_by_date=fresh_by_date,
                    mono_by_date=mono_by_date,
                    zone_by_date=zone_by_date,
                    c0_cfg=c0_cfg,
                    kbar_cache=kbar_cache,
                    collect_fingerprints=fp and win_label == "FULL",
                )
            )

    by_gate: dict[str, dict[str, Any]] = {}
    for r in rows:
        gid = str(r.get("gate_variant_id", ""))
        by_gate.setdefault(gid, {})[str(r.get("label", ""))] = r

    w0_full_n = int((by_gate.get("W0", {}).get("FULL") or {}).get("n_periods") or 0)
    w0_fp = set((by_gate.get("W0", {}).get("FULL") or {}).get("_fingerprints") or [])
    regressions: list[dict[str, Any]] = []
    for vid in ("W0", "E0", "S0", "X0"):
        fp = set((by_gate.get(vid, {}).get("FULL") or {}).get("_fingerprints") or [])
        regressions.append(
            {
                "variant_id": vid,
                "n_legs": len(fp),
                "match_w0_pct": 100.0 if fp == w0_fp else round(len(fp & w0_fp) / max(len(w0_fp), 1) * 100, 2),
            }
        )

    best_e = _best_phase_variant(by_gate, prefix="E", baseline="E0")
    best_s = _best_phase_variant(by_gate, prefix="S", baseline="S0")
    best_x = _best_phase_variant(by_gate, prefix="X", baseline="X0")

    combo_ids: list[str] = []
    if include_combos and best_e and best_s and best_x:
        for cg in combo_gates(best_e, best_s, best_x):
            combo_ids.append(cg.variant_id)
            for win_label, w_start, w_end in windows:
                if w_start > w_end:
                    continue
                rows.append(
                    _run_variant_window(
                        conn,
                        cfg=cfg,
                        gate=cg,
                        date_start=w_start,
                        date_end=w_end,
                        label=win_label,
                        close=close,
                        bench=bench,
                        rs_ratio=rs_ratio,
                        rs_mom=rs_mom,
                        spread_rs_panel=spread_rs_panel,
                        full_dates=full_dates,
                        fresh_by_date=fresh_by_date,
                        mono_by_date=mono_by_date,
                        zone_by_date=zone_by_date,
                        c0_cfg=c0_cfg,
                        kbar_cache=kbar_cache,
                    )
                )
                by_gate.setdefault(cg.variant_id, {})[win_label] = rows[-1]

    for r in rows:
        r.pop("_fingerprints", None)
        if r.get("label") == "FULL" and w0_full_n and r.get("gate_variant_id") != "W0":
            r["capture_rate_pct"] = round(int(r.get("n_periods") or 0) / w0_full_n * 100, 2)

    comparisons: list[dict[str, Any]] = []
    for r in rows:
        gid = str(r.get("gate_variant_id", ""))
        if gid == "W0":
            continue
        win = str(r.get("label", ""))
        base = by_gate.get("W0", {}).get(win, {})
        comparisons.append(
            {
                "gate": gid,
                "window": win,
                "n_periods": r.get("n_periods"),
                "mean_excess_pct": r.get("mean_excess_pct"),
                "delta_excess_vs_w0_pp": _delta_pp(r.get("mean_excess_pct"), base.get("mean_excess_pct")),
                "capture_rate_pct": r.get("capture_rate_pct") if win == "FULL" else None,
                "signals_skipped_expired": r.get("signals_skipped_expired"),
                "signals_deferred": r.get("signals_deferred"),
                "swaps_deferred": r.get("swaps_deferred"),
                "swaps_lost": r.get("swaps_lost"),
            }
        )

    hypotheses = _eval_hypotheses(by_gate, w0_n_full=w0_full_n)
    verdict = _overall_verdict(hypotheses)

    w0_full = by_gate.get("W0", {}).get("FULL") or {}
    best_row = max(
        (r for r in rows if r.get("label") == "FULL" and r.get("gate_variant_id") not in ("W0", "E0", "S0", "X0")),
        key=lambda r: float(r.get("mean_excess_pct") or -999),
        default=None,
    )

    return {
        "schema": "c18acc_weekday_execution_sweep-v1",
        "topic": "c18acc-weekday-execution",
        "phase": "phase1_4_full",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_slots": n_slots,
        "baseline": "W0",
        "pool": "fresh_union_accel",
        "timing_mode": cfg.timing_mode,
        "variants_requested": list(variants) + combo_ids,
        "best_phase_winners": {"entry": best_e, "swap": best_s, "exit": best_x},
        "regressions": regressions,
        "rows": rows,
        "by_gate": by_gate,
        "comparisons": comparisons,
        "hypotheses": hypotheses,
        "verdict": verdict,
        "w0_baseline": {
            "FULL": {
                "n_periods": w0_full.get("n_periods"),
                "mean_excess_pct": w0_full.get("mean_excess_pct"),
                "swaps_total": w0_full.get("swaps_total"),
            }
        },
        "best_variant_full": best_row.get("gate_variant_id") if best_row else None,
        "best_delta_excess_full_pp": (
            _delta_pp(best_row.get("mean_excess_pct"), w0_full.get("mean_excess_pct")) if best_row else None
        ),
    }


def render_c18acc_weekday_execution_sweep_md(payload: dict[str, Any]) -> str:
    hyp = payload.get("hypotheses") or {}
    w0 = (payload.get("w0_baseline") or {}).get("FULL") or {}
    w0_ex = float(w0.get("mean_excess_pct") or 0)
    lines = [
        "# C18acc POOL1+S2 · weekday execution gate sweep",
        "",
        f"區間：**{payload['date_start']}** → **{payload['date_end']}** · "
        f"IS ≤ {payload['is_end']} · OOS ≥ {payload['oos_start']}",
        "",
        f"- Pool：**{payload.get('pool')}** · timing：**{payload.get('timing_mode')}** · "
        f"{payload.get('n_slots')} 槽",
        f"- **Verdict：{payload.get('verdict')}**",
        "",
        f"W0 FULL：n={w0.get('n_periods')} · excess={w0.get('mean_excess_pct')}% · swaps={w0.get('swaps_total')}",
        "",
        "## Hypotheses",
        "",
        "| ID | pass | detail |",
        "|----|------|--------|",
    ]
    for hid in (
        "H-WD-H1",
        "H-WD-F1",
        "H-WD-HF1",
        "H-WD-E1",
        "H-WD-E3",
        "H-WD-S1",
        "H-WD-X3",
        "H-WD-C",
        "H-WD-NEG",
    ):
        h = hyp.get(hid) or {}
        detail = ", ".join(f"{k}={v}" for k, v in h.items() if k != "pass")
        lines.append(f"| {hid} | {h.get('pass')} | {detail} |")

    lines += ["", "## Phase 2 · max_hold Friday + F1 margin (FULL)", "", "| gate | n | excess | Δ vs W0 | max_hold | swaps |", "|------|---|--------|----------|----------|-------|"]
    for vid in ("H1", "F1", "HF1"):
        r = (payload.get("by_gate") or {}).get(vid, {}).get("FULL") or {}
        if not r:
            continue
        ex = r.get("mean_excess_pct")
        lines.append(
            f"| {vid} | {r.get('n_periods')} | {ex} | {_delta_pp(ex, w0.get('mean_excess_pct'))} | "
            f"{r.get('max_hold_exits')} | {r.get('swaps_total')} |"
        )

    lines += ["", "## Phase 1 · Entry (FULL)", "", "| gate | n | excess | Δ vs W0 | capture% | expired |", "|------|---|--------|----------|----------|---------|"]
    for vid in ("E0", "E1", "E2", "E3", "E4", "E5"):
        r = (payload.get("by_gate") or {}).get(vid, {}).get("FULL") or {}
        if not r:
            continue
        ex = r.get("mean_excess_pct")
        delta = _delta_pp(ex, w0.get("mean_excess_pct"))
        lines.append(
            f"| {vid} | {r.get('n_periods')} | {ex} | {delta} | {r.get('capture_rate_pct')} | {r.get('signals_skipped_expired')} |"
        )

    lines += ["", "## Phase 2 · Swap (FULL)", "", "| gate | swaps | Δ excess | rot mean_ret |", "|------|-------|----------|--------------|"]
    for vid in ("S0", "S1", "S2", "S3"):
        r = (payload.get("by_gate") or {}).get(vid, {}).get("FULL") or {}
        if not r:
            continue
        lines.append(
            f"| {vid} | {r.get('swaps_total')} | {_delta_pp(r.get('mean_excess_pct'), w0_ex)} | "
            f"{(r.get('rotation_cohort') or {}).get('mean_return_pct')} |"
        )

    lines += ["", "## Phase 3 · Exit (FULL)", "", "| gate | force_exits | Δ excess | MDD% |", "|------|-------------|----------|------|"]
    for vid in ("X0", "X1", "X2", "X3"):
        r = (payload.get("by_gate") or {}).get(vid, {}).get("FULL") or {}
        if not r:
            continue
        lines.append(
            f"| {vid} | {r.get('force_exits')} | {_delta_pp(r.get('mean_excess_pct'), w0_ex)} | "
            f"{(r.get('portfolio') or {}).get('max_drawdown_pct')} |"
        )

    bp = payload.get("best_phase_winners") or {}
    lines += [
        "",
        f"Phase 4 combos：{', '.join(v for v in payload.get('variants_requested', []) if str(v).startswith('C')) or '—'}",
        f"Best phase winners：E={bp.get('entry')} S={bp.get('swap')} X={bp.get('exit')}",
        f"Best FULL variant：**{payload.get('best_variant_full')}** · Δ **{payload.get('best_delta_excess_full_pp')}** pp",
        "",
        "## W0 regression",
        "",
    ]
    for reg in payload.get("regressions") or []:
        lines.append(f"- {reg.get('variant_id')}: match W0 **{reg.get('match_w0_pct')}%** ({reg.get('n_legs')} legs)")

    lines += ["", "---", "模組：`scripts/run_c18acc_weekday_execution_sweep.py`", ""]
    return "\n".join(lines)
