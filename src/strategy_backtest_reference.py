"""Frozen backtest reference metrics for strategy daily screens (Supabase snapshot_json)."""

from __future__ import annotations

import sqlite3
from typing import Any

from research.backtest.slot_backtest_summary import (
    SlotBacktestConfig,
    load_slot_backtest_summary,
    metrics_from_summary_payload,
    resolve_summary_path,
)
from stock_db.copytrade import load_copytrade_signal_days_for_run
from strategy_config import load_strategy_config

def _copytrade_reference_summary(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    strategy_code: str,
    cfg: SlotBacktestConfig,
) -> dict[str, Any] | None:
    """Latest copytrade run for adopted strategy (reference banner · not per-signal)."""
    row = conn.execute(
        """
        SELECT * FROM copytrade_runs
        WHERE etf_code = ? AND strategy_id = ?
        ORDER BY synced_at DESC
        LIMIT 1
        """,
        (etf_code, strategy_code),
    ).fetchone()
    if row is None:
        return None

    rid = str(row["run_id"])
    signal_days = load_copytrade_signal_days_for_run(conn, rid)
    complete = [d for d in signal_days if str(d["status"] or "") == "complete"]
    if cfg.date_start or cfg.date_end:
        complete = [
            d
            for d in complete
            if (not cfg.date_start or str(d["signal_date"]) >= cfg.date_start)
            and (not cfg.date_end or str(d["signal_date"]) <= cfg.date_end)
        ]
    win_vs_bench: float | None = None
    if complete:
        beats = sum(
            1
            for d in complete
            if d["return_pct"] is not None
            and d["bench_return_pct"] is not None
            and float(d["return_pct"]) > float(d["bench_return_pct"])
        )
        win_vs_bench = round(beats / len(complete) * 100.0, 2)

    mean_excess = row["mean_excess_pct"]
    avg_ret = row["avg_day_return_pct"]
    return {
        "n_periods": len(complete) or int(row["n_complete_days"] or 0),
        "n_signal_days": len(complete) or int(row["n_signal_days"] or 0),
        "win_rate_vs_bench_pct": win_vs_bench,
        "mean_excess_pct": round(float(mean_excess), 4)
        if mean_excess is not None
        else None,
        "mean_return_pct": round(float(avg_ret), 4) if avg_ret is not None else None,
        "window_start": cfg.date_start,
        "window_end": cfg.date_end,
        "run_id": rid,
    }


def build_backtest_reference(
    strategy_id: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Strategy-level reference stats from strategy.yaml + backtest JSON / SQLite."""
    spec = load_strategy_config().get(strategy_id)
    if spec is None or spec.backtest is None:
        return None

    bt = spec.backtest
    cfg = SlotBacktestConfig.from_yaml_dict(
        {
            "date_start": bt.params.get("date_start", "2026-01-01"),
            "date_end": bt.params.get("date_end", "2026-12-31"),
            "n_slots": bt.params.get("n_slots") or spec.n_slots or 3,
            "hold_days": bt.params.get("hold_days") or spec.hold_days or 7,
            "capital_ntd": bt.params.get("capital_ntd", 10_000.0),
            "entry_price_mode": bt.params.get("entry_price_mode", "close"),
            "source_summary": bt.params.get("source_summary"),
            "strategy_id": bt.params.get("strategy_id") or spec.strategy_code,
            "copytrade_batch_id": bt.params.get("copytrade_batch_id"),
            "model_id": bt.params.get("model_id"),
            "min_composite": bt.params.get("min_composite", 45.0),
            "execution_states": bt.params.get("execution_states") or (),
            "top_n": bt.params.get("top_n", 15),
            "entry_ready_only": bt.params.get("entry_ready_only", False),
            "variant": bt.params.get("variant", "hold7"),
            "max_entry_wait_days": bt.params.get("max_entry_wait_days", 10),
            "stop_lookback_days": bt.params.get("stop_lookback_days", 20),
            "require_pivot": bt.params.get("require_pivot", False),
            "min_dist_pivot_pct": bt.params.get("min_dist_pivot_pct"),
            "max_dist_pivot_pct": bt.params.get("max_dist_pivot_pct"),
        }
    )
    if cfg is None:
        return None

    summary: dict[str, Any] | None = None
    source: str | None = None

    path = resolve_summary_path(
        bt.params.get("source_summary") or bt.source_report or bt.fallback_report
    )
    if path is not None:
        payload = load_slot_backtest_summary(path)
        if payload is not None:
            summary = metrics_from_summary_payload(payload)
            try:
                from stock_db import PROJECT_ROOT

                source = str(path.relative_to(PROJECT_ROOT))
            except ValueError:
                source = str(path)

    if summary is None and conn is not None and strategy_id == "00981a-l1h9":
        etf = spec.etf_code or "00981A"
        code = str(cfg.strategy_id or spec.strategy_code or "L1H9")
        summary = _copytrade_reference_summary(
            conn, etf_code=etf, strategy_code=code, cfg=cfg
        )
        if summary is not None:
            source = "copytrade_runs"

    if not summary:
        return None

    ref: dict[str, Any] = {
        "spec_type": bt.spec_type,
        "window": {"start": cfg.date_start, "end": cfg.date_end},
        "hold_days": cfg.hold_days,
        "n_slots": cfg.n_slots,
    }
    if source:
        ref["source"] = source
    if bt.notes:
        ref["notes"] = bt.notes

    for key in bt.metrics:
        if key in summary and summary[key] is not None:
            ref[key] = summary[key]

    # Frontend-friendly aliases
    if ref.get("mean_excess_pct") is not None:
        ref["expected_excess_pct"] = ref["mean_excess_pct"]
    if ref.get("mean_return_pct") is not None:
        ref["expected_return_pct"] = ref["mean_return_pct"]
    if ref.get("win_rate_vs_bench_pct") is not None:
        ref["historical_win_rate_vs_bench_pct"] = ref["win_rate_vs_bench_pct"]

    return ref
