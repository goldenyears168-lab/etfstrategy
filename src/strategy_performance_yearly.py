"""Adopted strategy yearly performance · SQLite SSOT + Supabase sync."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from research.backtest.broad_momentum_tv_backtest import run_all_broad_momentum_backtests
from research.backtest.chunge_funnel_backtest import VCP_COIL_CLOSE, VCP_PIVOT_GATE, run_chunge_slot_backtest
from research.backtest.copytrade_backtest import simulate_fixed_slots
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import run_breadth_zone_comparison
from research.backtest.slot_backtest_summary import SlotBacktestConfig
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from stock_db import DEFAULT_DB_PATH, connect, load_copytrade_signal_days_for_run
from supabase_research_sync import _headers, supabase_configured

_TPE = ZoneInfo("Asia/Taipei")
_BENCHMARK = "IX0001"
_TABLE = "strategy_performance_yearly"
_COPYTRADE_2025_START = "2025-05-28"

YEAR_WINDOWS: dict[str, tuple[str, str]] = {
    "2025": ("2025-01-01", "2025-12-31"),
    "2026": ("2026-01-01", "2026-06-18"),
}

_ADOPTED_STRATEGY_IDS = (
    "00981a-l1h9",
    "rrg-mono-hold7",
    "vcp-pivot-gate",
    "vcp-coil-close",
    "minervini-sepa-basket",
)


@dataclass(frozen=True)
class StrategyPerformanceRow:
    strategy_id: str
    year_label: str
    window_start: str
    window_end: str
    capital_ntd: float
    total_return_pct: float
    cagr_pct: float | None
    win_rate_vs_bench_pct: float | None
    sharpe_ratio: float | None
    mean_excess_pct: float | None
    n_periods: int
    n_slots: int | None = None
    hold_days: int | None = None
    benchmark: str = _BENCHMARK
    partial_year: bool = False
    metrics_json: str | None = None
    computed_at: str | None = None


def _round_opt(val: float | None, ndigits: int = 4) -> float | None:
    if val is None:
        return None
    return round(float(val), ndigits)


def _trade_dates(full_dates: list[str], date_start: str, date_end: str) -> list[str]:
    return [d for d in full_dates if date_start <= d <= date_end]


def _portfolio_metrics(
    conn: sqlite3.Connection,
    close,
    full_dates: list[str],
    periods: list[dict],
    *,
    date_start: str,
    date_end: str,
    capital_ntd: float,
    n_slots: int,
) -> dict[str, Any]:
    return portfolio_metrics_for_periods(
        conn,
        periods,
        _trade_dates(full_dates, date_start, date_end),
        total_capital=capital_ntd,
        n_slots=n_slots,
        close=close,
    )


def _copytrade_rows(conn: sqlite3.Connection, full_dates: list[str]) -> list[StrategyPerformanceRow]:
    run = conn.execute(
        """
        SELECT run_id FROM copytrade_runs
        WHERE etf_code = '00981A' AND strategy_id = 'L1H9'
        ORDER BY synced_at DESC LIMIT 1
        """
    ).fetchone()
    if run is None:
        return []

    signal_days = [
        dict(d) for d in load_copytrade_signal_days_for_run(conn, str(run["run_id"]))
    ]
    complete = [d for d in signal_days if str(d.get("status") or "") == "complete"]
    capital = 90_000.0
    rows: list[StrategyPerformanceRow] = []

    for year_label, (ds, de) in YEAR_WINDOWS.items():
        start = _COPYTRADE_2025_START if year_label == "2025" else ds
        sub = [d for d in complete if start <= str(d["signal_date"]) <= de]
        if not sub:
            continue
        beats = sum(
            1
            for d in sub
            if d.get("return_pct") is not None
            and d.get("bench_return_pct") is not None
            and float(d["return_pct"]) > float(d["bench_return_pct"])
        )
        win = round(beats / len(sub) * 100.0, 2)
        excesses = [
            float(d["return_pct"]) - float(d["bench_return_pct"])
            for d in sub
            if d.get("return_pct") is not None and d.get("bench_return_pct") is not None
        ]
        mean_excess = round(sum(excesses) / len(excesses), 4) if excesses else None

        sim = simulate_fixed_slots(conn, sub, n_slots=9, capital_ntd=10_000.0)
        pnl = float(sim.get("recycled_total_pnl_ntd") or 0.0)
        total_ret = round(pnl / capital * 100.0, 4)
        days = len(_trade_dates(full_dates, start, de))
        years = days / 252.0 if days else 0.0
        final = capital + pnl
        cagr = (
            round(((final / capital) ** (1.0 / years) - 1.0) * 100.0, 4)
            if years > 0 and final > 0
            else None
        )
        rows.append(
            StrategyPerformanceRow(
                strategy_id="00981a-l1h9",
                year_label=year_label,
                window_start=start,
                window_end=de,
                capital_ntd=capital,
                n_slots=9,
                hold_days=9,
                total_return_pct=total_ret,
                cagr_pct=cagr,
                win_rate_vs_bench_pct=win,
                sharpe_ratio=None,
                mean_excess_pct=mean_excess,
                n_periods=len(sub),
                partial_year=year_label == "2026",
                metrics_json=json.dumps(
                    {"excess_kind": "per_signal_day_mean", "sharpe_note": "discrete_signal_days"},
                    ensure_ascii=False,
                ),
            )
        )
    return rows


def _slot_strategy_rows(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    capital_ntd: float,
    n_slots: int,
    hold_days: int,
    run_fn,
) -> list[StrategyPerformanceRow]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    rows: list[StrategyPerformanceRow] = []

    for year_label, (ds, de) in YEAR_WINDOWS.items():
        result = run_fn(conn, date_start=ds, date_end=de)
        periods = result["periods"] if isinstance(result, dict) and "periods" in result else result["pooled_all"]["periods"]
        summary = result["summary"] if isinstance(result, dict) and "summary" in result else result["pooled_all"]["summary"]
        pm = _portfolio_metrics(
            conn,
            close,
            full_dates,
            periods,
            date_start=ds,
            date_end=de,
            capital_ntd=capital_ntd,
            n_slots=n_slots,
        )
        rows.append(
            StrategyPerformanceRow(
                strategy_id=strategy_id,
                year_label=year_label,
                window_start=str(summary.get("window_start") or ds),
                window_end=str(summary.get("window_end") or de),
                capital_ntd=capital_ntd,
                n_slots=n_slots,
                hold_days=hold_days,
                total_return_pct=_round_opt(pm.get("total_return_pct")) or 0.0,
                cagr_pct=_round_opt(pm.get("cagr_pct")),
                win_rate_vs_bench_pct=_round_opt(summary.get("win_rate_vs_bench_pct"), 2),
                sharpe_ratio=_round_opt(pm.get("sharpe_ratio"), 2),
                mean_excess_pct=_round_opt(summary.get("mean_excess_pct")),
                n_periods=int(summary.get("n_periods") or pm.get("n_trades") or 0),
                partial_year=year_label == "2026",
                metrics_json=json.dumps({"excess_kind": "per_period_mean"}, ensure_ascii=False),
            )
        )
    return rows


def _vcp_rows(conn: sqlite3.Connection, *, strategy_id: str, cfg_dict: dict) -> list[StrategyPerformanceRow]:
    def _run(c: sqlite3.Connection, *, date_start: str, date_end: str) -> dict:
        cfg = SlotBacktestConfig(
            date_start=date_start,
            date_end=date_end,
            **{k: v for k, v in cfg_dict.items() if k not in ("date_start", "date_end")},
        )
        return run_chunge_slot_backtest(c, config=cfg)

    return _slot_strategy_rows(
        conn,
        strategy_id=strategy_id,
        capital_ntd=50_000.0,
        n_slots=int(cfg_dict.get("n_slots") or 5),
        hold_days=int(cfg_dict.get("hold_days") or 20),
        run_fn=_run,
    )


def _minervini_rows(conn: sqlite3.Connection) -> list[StrategyPerformanceRow]:
    rows: list[StrategyPerformanceRow] = []
    for year_label, (ds, de) in YEAR_WINDOWS.items():
        summary, _, _ = run_all_broad_momentum_backtests(conn, start_date=ds, end_date=de)
        row = summary[summary["strategy"].str.contains("Minervini", na=False)].iloc[0]
        rows.append(
            StrategyPerformanceRow(
                strategy_id="minervini-sepa-basket",
                year_label=year_label,
                window_start=ds,
                window_end=de,
                capital_ntd=0.0,
                n_slots=None,
                hold_days=None,
                total_return_pct=_round_opt(float(row["total_return_pct"])) or 0.0,
                cagr_pct=_round_opt(float(row["cagr_pct"])),
                win_rate_vs_bench_pct=_round_opt(
                    float(row["beat_bench_days"]) / float(row["trading_days"]) * 100.0,
                    2,
                ),
                sharpe_ratio=_round_opt(float(row["sharpe"]), 2),
                mean_excess_pct=_round_opt(float(row["excess_return_pct"])),
                n_periods=int(row["trading_days"]),
                partial_year=year_label == "2026",
                metrics_json=json.dumps(
                    {
                        "excess_kind": "interval",
                        "trading_days": int(row["trading_days"]),
                        "max_drawdown_pct": float(row["max_drawdown_pct"]),
                    },
                    ensure_ascii=False,
                ),
            )
        )
    return rows


def compute_strategy_performance_yearly(
    conn: sqlite3.Connection | None = None,
) -> list[StrategyPerformanceRow]:
    """Recompute adopted-strategy 2025/2026 portfolio metrics from stocks.db."""
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)
    assert conn is not None

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    now = datetime.now(_TPE).isoformat(timespec="seconds")
    rows: list[StrategyPerformanceRow] = []

    rows.extend(_copytrade_rows(conn, full_dates))

    def _rrg_run(c: sqlite3.Connection, *, date_start: str, date_end: str) -> dict:
        r = run_breadth_zone_comparison(c, date_start=date_start, date_end=date_end)
        return {"periods": r["pooled_all"]["periods"], "summary": r["pooled_all"]["summary"]}

    rows.extend(
        _slot_strategy_rows(
            conn,
            strategy_id="rrg-mono-hold7",
            capital_ntd=50_000.0,
            n_slots=3,
            hold_days=7,
            run_fn=_rrg_run,
        )
    )
    rows.extend(_vcp_rows(conn, strategy_id="vcp-pivot-gate", cfg_dict=dict(VCP_PIVOT_GATE)))
    rows.extend(_vcp_rows(conn, strategy_id="vcp-coil-close", cfg_dict=dict(VCP_COIL_CLOSE)))
    rows.extend(_minervini_rows(conn))

    adopted = set(_ADOPTED_STRATEGY_IDS)
    missing = adopted - {r.strategy_id for r in rows}
    if missing:
        raise RuntimeError(f"missing performance rows for: {sorted(missing)}")

    return [StrategyPerformanceRow(**{**asdict(r), "computed_at": now}) for r in rows]


def ensure_strategy_performance_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            strategy_id TEXT NOT NULL,
            year_label TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            capital_ntd REAL NOT NULL,
            n_slots INTEGER,
            hold_days INTEGER,
            total_return_pct REAL NOT NULL,
            cagr_pct REAL,
            win_rate_vs_bench_pct REAL,
            sharpe_ratio REAL,
            mean_excess_pct REAL,
            n_periods INTEGER NOT NULL,
            benchmark TEXT NOT NULL DEFAULT '{_BENCHMARK}',
            partial_year INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, year_label)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_strategy ON {_TABLE} (strategy_id, year_label)"
    )
    conn.commit()


def upsert_strategy_performance(
    conn: sqlite3.Connection,
    rows: list[StrategyPerformanceRow],
) -> int:
    ensure_strategy_performance_table(conn)
    for row in rows:
        conn.execute(
            f"""
            INSERT INTO {_TABLE} (
                strategy_id, year_label, window_start, window_end, capital_ntd,
                n_slots, hold_days, total_return_pct, cagr_pct, win_rate_vs_bench_pct,
                sharpe_ratio, mean_excess_pct, n_periods, benchmark, partial_year,
                metrics_json, computed_at
            ) VALUES (
                :strategy_id, :year_label, :window_start, :window_end, :capital_ntd,
                :n_slots, :hold_days, :total_return_pct, :cagr_pct, :win_rate_vs_bench_pct,
                :sharpe_ratio, :mean_excess_pct, :n_periods, :benchmark, :partial_year,
                :metrics_json, :computed_at
            )
            ON CONFLICT(strategy_id, year_label) DO UPDATE SET
                window_start = excluded.window_start,
                window_end = excluded.window_end,
                capital_ntd = excluded.capital_ntd,
                n_slots = excluded.n_slots,
                hold_days = excluded.hold_days,
                total_return_pct = excluded.total_return_pct,
                cagr_pct = excluded.cagr_pct,
                win_rate_vs_bench_pct = excluded.win_rate_vs_bench_pct,
                sharpe_ratio = excluded.sharpe_ratio,
                mean_excess_pct = excluded.mean_excess_pct,
                n_periods = excluded.n_periods,
                benchmark = excluded.benchmark,
                partial_year = excluded.partial_year,
                metrics_json = excluded.metrics_json,
                computed_at = excluded.computed_at
            """,
            {
                **asdict(row),
                "partial_year": 1 if row.partial_year else 0,
            },
        )
    conn.commit()
    return len(rows)


def load_strategy_performance(
    conn: sqlite3.Connection,
    *,
    strategy_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_strategy_performance_table(conn)
    if strategy_id:
        cur = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE strategy_id = ? ORDER BY year_label",
            (strategy_id,),
        )
    else:
        cur = conn.execute(f"SELECT * FROM {_TABLE} ORDER BY strategy_id, year_label")
    return [dict(r) for r in cur.fetchall()]


def _performance_rest_url() -> str:
    from supabase_research_sync import _supabase_url

    base = _supabase_url().rstrip("/")
    if not base:
        raise RuntimeError("SUPABASE_URL 未設定")
    return f"{base}/rest/v1/{_TABLE}"


def _row_payload(row: StrategyPerformanceRow) -> dict[str, Any]:
    return {
        "strategy_id": row.strategy_id,
        "year_label": row.year_label,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "capital_ntd": row.capital_ntd,
        "n_slots": row.n_slots,
        "hold_days": row.hold_days,
        "total_return_pct": row.total_return_pct,
        "cagr_pct": row.cagr_pct,
        "win_rate_vs_bench_pct": row.win_rate_vs_bench_pct,
        "sharpe_ratio": row.sharpe_ratio,
        "mean_excess_pct": row.mean_excess_pct,
        "n_periods": row.n_periods,
        "benchmark": row.benchmark,
        "partial_year": row.partial_year,
        "metrics_json": json.loads(row.metrics_json) if row.metrics_json else None,
        "computed_at": row.computed_at,
    }


def sync_strategy_performance_to_supabase(rows: list[StrategyPerformanceRow]) -> list[str]:
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")
    uploaded: list[str] = []
    for row in rows:
        resp = requests.post(
            _performance_rest_url(),
            headers=_headers(),
            json=_row_payload(row),
            params={"on_conflict": "strategy_id,year_label"},
            timeout=120,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Supabase {_TABLE} upsert failed ({row.strategy_id}/{row.year_label}): "
                f"{resp.status_code} {resp.text[:500]}"
            )
        uploaded.append(f"{row.strategy_id}:{row.year_label}")
    return uploaded


def refresh_strategy_performance(
    conn: sqlite3.Connection | None = None,
    *,
    sync_supabase: bool = True,
) -> tuple[list[StrategyPerformanceRow], list[str]]:
    """Compute → SQLite upsert → optional Supabase upsert."""
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)
    assert conn is not None
    rows = compute_strategy_performance_yearly(conn)
    upsert_strategy_performance(conn, rows)
    uploaded: list[str] = []
    if sync_supabase:
        uploaded = sync_strategy_performance_to_supabase(rows)
    if own:
        conn.close()
    return rows, uploaded
