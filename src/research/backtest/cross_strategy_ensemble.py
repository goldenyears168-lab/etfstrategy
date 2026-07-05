"""Cross-strategy ensemble · native + FinMind · SSOT: config/backtest_standard.yaml."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_standard_config import (
    BacktestStandardConfig,
    EnsembleTeamSpec,
    load_backtest_standard_config,
)
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.finmind_tx_foreign_common import PROXY_CODE
from research.backtest.finmind_marketplace_comparison import bench_interval_return_pct
from research.backtest.slot_portfolio_metrics import (
    simulate_slot_portfolio,
    simulate_slot_portfolio_yield_new,
)
from research.backtest.unified_nav_adapter import collect_periods
from stock_db import DEFAULT_DB_PATH, connect


def _resolve_window(cfg: BacktestStandardConfig, window_id: str) -> tuple[str, str]:
    win = cfg.windows.get(window_id) or {}
    date_start = str(win.get("date_start") or "2025-05-28")
    date_end = str(win.get("date_end") or date.today().isoformat())
    return date_start, date_end


def _build_close_panel(conn, trade_dates: list[str]) -> pd.DataFrame:
    close, _, _ = load_price_panels(conn)
    panel = close.reindex(trade_dates)
    bench = load_benchmark_close(conn, code=PROXY_CODE)
    if PROXY_CODE not in panel.columns:
        panel[PROXY_CODE] = bench.reindex(trade_dates).astype(float)
    return panel


def _merge_team_periods(
    team: EnsembleTeamSpec,
    period_lists: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    prio = {m.strategy_id: m.priority for m in team.members}
    merged: list[dict[str, Any]] = []
    for member in team.members:
        sid = member.strategy_id
        for p in period_lists.get(sid, []):
            row = dict(p)
            row["source_strategy"] = sid
            row["_priority"] = prio.get(sid, 99)
            merged.append(row)
    merged.sort(
        key=lambda p: (
            str(p.get("entry_date") or ""),
            p["_priority"],
            str(p.get("signal_date") or ""),
            str(p.get("stock_id") or ""),
        )
    )
    return merged


def run_config_ensemble(
    conn,
    team: EnsembleTeamSpec,
    *,
    cfg: BacktestStandardConfig,
    date_start: str,
    date_end: str,
) -> dict[str, Any]:
    period_lists: dict[str, list[dict[str, Any]]] = {}
    for member in team.members:
        sid = member.strategy_id
        adapter = cfg.track_adapters.get(sid)
        if adapter is None:
            raise ValueError(f"missing track_adapter: {sid}")
        start = date_start
        if adapter.earliest_data and start < adapter.earliest_data:
            start = adapter.earliest_data
        period_lists[sid] = collect_periods(
            conn,
            sid,
            date_start=start,
            date_end=date_end,
            adapter=adapter,
        )

    merged = _merge_team_periods(team, period_lists)
    close, _, _ = load_price_panels(conn)
    trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    close_panel = _build_close_panel(conn, trade_dates)

    cost = cfg.cost_model if cfg.cost_model.get("enabled") else None
    notional = cfg.comparison_notional_ntd
    sim_kwargs = dict(
        total_capital=notional,
        n_slots=team.n_slots,
        cost_model=cost,
        return_daily=True,
    )

    if team.pool_mode == "shared_pool_yield_priority":
        portfolio = simulate_slot_portfolio_yield_new(
            conn,
            close_panel,
            trade_dates,
            merged,
            eviction="lowest_priority",
            preempt_only_if_higher_priority=True,
            **sim_kwargs,
        )
    elif team.pool_mode == "shared_pool":
        portfolio = simulate_slot_portfolio(
            conn, close_panel, trade_dates, merged, **sim_kwargs
        )
    else:
        raise ValueError(f"unknown pool_mode: {team.pool_mode}")

    bench_ret = bench_interval_return_pct(conn, date_start, date_end)
    total_ret = portfolio.get("total_return_pct")
    interval_excess = None
    if total_ret is not None and bench_ret is not None:
        interval_excess = round(float(total_ret) - float(bench_ret), 4)

    daily = portfolio.get("daily_nav") or []
    bench = load_benchmark_close(conn).reindex(close.index)
    merged_nav: list[dict[str, Any]] = []
    if daily and not bench.empty:
        b0 = float(bench.loc[trade_dates[0]]) if trade_dates[0] in bench.index else None
        for row in daily:
            d = row["date"]
            beq = None
            if b0 and d in bench.index and b0 > 0:
                beq = round(notional * float(bench.loc[d]) / b0, 2)
            merged_nav.append(
                {
                    "date": d,
                    "strategy_id": team.team_id,
                    "equity_ntd": row["equity_ntd"],
                    "benchmark_equity_ntd": beq,
                    "in_market_flag": row.get("in_market_flag", 0),
                }
            )

    return {
        "team_id": team.team_id,
        "label": team.label,
        "mode": team.pool_mode,
        "window_id": team.window_id,
        "date_start": date_start,
        "date_end": date_end,
        "members": [m.strategy_id for m in team.members],
        "n_candidates": sum(len(v) for v in period_lists.values()),
        "n_fills": portfolio.get("n_periods") or portfolio.get("n_trades"),
        "n_preemptions": portfolio.get("n_preemptions"),
        "avg_hold_days": portfolio.get("avg_hold_days"),
        "total_return_pct": total_ret,
        "bench_interval_return_pct": bench_ret,
        "interval_excess_pct": interval_excess,
        "sharpe_ratio": portfolio.get("sharpe_ratio"),
        "max_drawdown_pct": portfolio.get("max_drawdown_pct"),
        "avg_utilization_pct": portfolio.get("avg_utilization_pct"),
        "beats_bench": interval_excess is not None and interval_excess > 0,
        "daily_nav_merged": merged_nav,
    }


def run_all_ensembles(
    *,
    cfg: BacktestStandardConfig | None = None,
    window_id: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_backtest_standard_config()
    conn = connect(Path(db_path) if db_path else DEFAULT_DB_PATH)
    rows: list[dict[str, Any]] = []
    try:
        for team in cfg.ensemble_teams.values():
            wid = window_id or team.window_id
            date_start, date_end = _resolve_window(cfg, wid)
            rows.append(
                run_config_ensemble(
                    conn, team, cfg=cfg, date_start=date_start, date_end=date_end
                )
            )
    finally:
        conn.close()

    rows.sort(
        key=lambda r: (r.get("interval_excess_pct") is None, -(r.get("interval_excess_pct") or -9999)),
    )
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return {
        "schema_version": "cross_strategy_ensemble-v1",
        "contract_ssot": "config/backtest_standard.yaml",
        "comparison_notional_ntd": cfg.comparison_notional_ntd,
        "benchmark": cfg.benchmark,
        "excess_definition": cfg.excess_definition,
        "rows": rows,
        "generated_at": date.today().isoformat(),
    }


def render_ensemble_league_md(payload: dict[str, Any]) -> str:
    lines = [
        "# 跨策略編隊 · 標準化比較",
        "",
        f"- SSOT: `{payload['contract_ssot']}`",
        f"- notional: {payload['comparison_notional_ntd']:,.0f} NTD",
        f"- benchmark: `{payload['benchmark']}`",
        f"- ranking: `{payload['excess_definition']}`",
        "",
        "| rank | team | window | 總報酬% | 基準% | **區間超額%** | Sharpe | 贏? |",
        "|------|------|--------|---------|-------|---------------|--------|-----|",
    ]
    for row in payload["rows"]:
        beat = "✓" if row.get("beats_bench") else "—"
        lines.append(
            f"| {row['rank']} | {row['label']} | {row['window_id']} | "
            f"{row.get('total_return_pct', '—')} | {row.get('bench_interval_return_pct', '—')} | "
            f"**{row.get('interval_excess_pct', '—')}** | {row.get('sharpe_ratio', '—')} | {beat} |"
        )
    lines.extend(["", "### 成員", ""])
    for row in payload["rows"]:
        lines.append(
            f"- **{row['label']}** (`{row['team_id']}`): "
            f"{', '.join(row.get('members') or [])} · "
            f"成交 {row.get('n_fills')} · 搶槽 {row.get('n_preemptions', '—')} · "
            f"均持倉 {row.get('avg_hold_days', '—')} 日"
        )
    lines.append("")
    return "\n".join(lines)


def run_cross_strategy_ensemble(
    *,
    cfg: BacktestStandardConfig | None = None,
    window_id: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_backtest_standard_config()
    payload = run_all_ensembles(cfg=cfg, window_id=window_id, db_path=db_path)
    out_dir = Path(cfg.output.get("nav_csv_dir") or "reports/research/_unified/")
    out_dir.mkdir(parents=True, exist_ok=True)
    wid = window_id or "all"
    json_path = out_dir / f"ensemble_league_{wid}.json"
    md_path = out_dir / f"ensemble_league_{wid}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_ensemble_league_md(payload), encoding="utf-8")
    return {"json_path": json_path, "md_path": md_path, "payload": payload}
