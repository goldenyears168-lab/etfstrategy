"""Slot strategy backtest summary JSON — shared by research backtest scripts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT, load_copytrade_signal_days_for_run


@dataclass(frozen=True)
class SlotBacktestConfig:
    date_start: str
    date_end: str
    n_slots: int = 3
    hold_days: int = 7
    capital_ntd: float = 10_000.0
    entry_price_mode: str = "close"
    source_summary: str | None = None
    strategy_id: str | None = None
    copytrade_batch_id: str | None = None
    model_id: str | None = None
    min_composite: float = 45.0
    execution_states: tuple[str, ...] = ()
    top_n: int = 15
    entry_ready_only: bool = False
    variant: str = "hold7"
    max_entry_wait_days: int = 10
    stop_lookback_days: int = 20
    require_pivot: bool = False
    min_dist_pivot_pct: float | None = None
    max_dist_pivot_pct: float | None = None

    @classmethod
    def from_yaml_dict(cls, raw: dict[str, Any] | None) -> SlotBacktestConfig | None:
        if not raw or not isinstance(raw, dict):
            return None
        states = raw.get("execution_states") or ()
        return cls(
            date_start=str(raw.get("date_start") or "2026-01-01"),
            date_end=str(raw.get("date_end") or "2026-12-31"),
            n_slots=int(raw.get("n_slots") or 3),
            hold_days=int(raw.get("hold_days") or 7),
            capital_ntd=float(raw.get("capital_ntd") or 10_000.0),
            entry_price_mode=str(raw.get("entry_price_mode") or "close"),
            source_summary=str(raw["source_summary"]) if raw.get("source_summary") else None,
            strategy_id=str(raw["strategy_id"]) if raw.get("strategy_id") else None,
            copytrade_batch_id=str(raw["copytrade_batch_id"]) if raw.get("copytrade_batch_id") else None,
            model_id=str(raw["model_id"]) if raw.get("model_id") else None,
            min_composite=float(raw.get("min_composite") or 45.0),
            execution_states=tuple(str(s) for s in states),
            top_n=int(raw.get("top_n") or 15),
            entry_ready_only=bool(raw.get("entry_ready_only", False)),
            variant=str(raw.get("variant") or "hold7"),
            max_entry_wait_days=int(raw.get("max_entry_wait_days") or 10),
            stop_lookback_days=int(raw.get("stop_lookback_days") or 20),
            require_pivot=bool(raw.get("require_pivot", False)),
            min_dist_pivot_pct=float(raw["min_dist_pivot_pct"])
            if raw.get("min_dist_pivot_pct") is not None
            else None,
            max_dist_pivot_pct=float(raw["max_dist_pivot_pct"])
            if raw.get("max_dist_pivot_pct") is not None
            else None,
        )


def resolve_summary_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / path_str


def load_slot_backtest_summary(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def build_summary_payload(
    *,
    track_id: str,
    config: SlotBacktestConfig,
    summary: dict[str, Any],
    source_module: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "track_id": track_id,
        "spec_type": "slot_strategy_backtest",
        "date_start": config.date_start,
        "date_end": config.date_end,
        "n_slots": config.n_slots,
        "hold_days": config.hold_days,
        "capital_ntd": config.capital_ntd,
        "entry_price_mode": config.entry_price_mode,
        "generated_at": date.today().isoformat(),
        "source_module": source_module,
        "summary": summary,
        "entry_ready_only": config.entry_ready_only,
        "variant": config.variant,
    }
    if extra:
        payload.update(extra)
    return payload


def write_slot_backtest_summary(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def compute_copytrade_slot_summary(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    cfg: SlotBacktestConfig,
) -> dict[str, Any] | None:
    etf = etf_code or "00981A"
    strategy_id = cfg.strategy_id or "L1H9"
    batch_id = cfg.copytrade_batch_id

    sql = """
        SELECT * FROM copytrade_runs
        WHERE etf_code = ? AND strategy_id = ?
    """
    params: list[object] = [etf, strategy_id]
    if batch_id:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    if cfg.date_start:
        sql += " AND (window_start IS NULL OR window_start >= ?)"
        params.append(cfg.date_start)
    if cfg.date_end:
        sql += " AND (window_end IS NULL OR window_end <= ?)"
        params.append(cfg.date_end)
    sql += " ORDER BY synced_at DESC LIMIT 1"
    run = conn.execute(sql, params).fetchone()
    if run is None:
        return None

    n = int(run["n_complete_days"] or 0)
    mean_excess = run["mean_excess_pct"]
    avg_ret = run["avg_day_return_pct"]
    win_vs_bench: float | None = None
    rid = str(run["run_id"])
    signal_days = load_copytrade_signal_days_for_run(conn, rid)
    complete = [d for d in signal_days if str(d["status"] or "") == "complete"]
    if complete:
        beats = sum(
            1
            for d in complete
            if d["return_pct"] is not None
            and d["bench_return_pct"] is not None
            and float(d["return_pct"]) > float(d["bench_return_pct"])
        )
        win_vs_bench = round(beats / len(complete) * 100.0, 2)

    return {
        "n_periods": n,
        "n_signal_days": int(run["n_signal_days"] or 0),
        "win_rate_vs_bench_pct": win_vs_bench,
        "mean_excess_pct": round(float(mean_excess), 4) if mean_excess is not None else None,
        "mean_return_pct": round(float(avg_ret), 4) if avg_ret is not None else None,
        "window_start": run["window_start"],
        "window_end": run["window_end"],
        "run_id": rid,
    }


def metrics_from_summary_payload(data: dict[str, Any]) -> dict[str, Any]:
    s = data.get("summary") or {}
    out: dict[str, Any] = {}
    for key in (
        "n_periods",
        "win_rate_vs_bench_pct",
        "mean_excess_pct",
        "mean_return_pct",
        "total_excess_pct",
        "n_signal_days",
        "screen_coverage_pct",
    ):
        if key in s and s[key] is not None:
            out[key] = s[key]
    return out
