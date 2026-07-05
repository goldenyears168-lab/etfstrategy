"""FinMind marketplace · ensemble team backtest (shared slot pool)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_standard_config import load_backtest_standard_config
from market_benchmark import load_benchmark_close
from research.backtest.finmind_it_cold_stove_backtest import (
    build_periods as build_it_periods,
    enrich_period_returns as enrich_it,
)
from research.backtest.finmind_it_cold_stove_common import ItColdStoveParams
from research.backtest.finmind_low_rebound_backtest import (
    build_periods as build_lr_periods,
    enrich_period_returns as enrich_lr,
)
from research.backtest.finmind_ma20_breakout_backtest import (
    build_periods as build_ma20_periods,
    enrich_period_returns as enrich_ma20,
)
from research.backtest.finmind_ma20_breakout_common import Ma20BreakoutParams
from research.backtest.finmind_marketplace_comparison import (
    CMP_N_SLOTS_SLOT,
    CMP_UNIVERSE,
    OOS_START,
    WINDOW_FULL_COMMON,
    WINDOW_OOS_2024,
    bench_interval_return_pct,
)
from research.backtest.finmind_ps_growth_backtest import (
    build_periods as build_ps_periods,
    enrich_period_returns as enrich_ps,
)
from research.backtest.finmind_ps_growth_common import PsGrowthParams
from research.backtest.finmind_screener_common import LowReboundParams, load_price_panels
from research.backtest.finmind_tx_foreign_backtest import (
    build_timing_periods,
    enrich_period_returns as enrich_tx,
)
from research.backtest.finmind_tx_foreign_common import PROXY_CODE, TxForeignParams
from research.backtest.slot_portfolio_metrics import (
    simulate_slot_portfolio,
    simulate_slot_portfolio_yield_new,
)
from stock_db import DEFAULT_DB_PATH, connect

# Unlimited per-strategy generation · global pool applies n_slots.
UNLIMITED_SLOTS = 999

SHORT_IDS = {
    "finmind-low-rebound-foreign-it-sync": "S1",
    "finmind-low-ps-growth": "S2",
    "finmind-tx-foreign-net-long-3d": "S3",
    "finmind-ma20-breakout-volume": "S4",
    "finmind-it-cold-stove": "S5",
}


@dataclass(frozen=True)
class EnsembleTeam:
    team_id: str
    label: str
    members: tuple[str, ...]
    priority: tuple[tuple[str, int], ...]


ENSEMBLE_TEAMS: tuple[EnsembleTeam, ...] = (
    EnsembleTeam(
        "team_tri_core",
        "主力三角 · S2+S3+S4",
        (
            "finmind-low-ps-growth",
            "finmind-tx-foreign-net-long-3d",
            "finmind-ma20-breakout-volume",
        ),
        (
            ("finmind-tx-foreign-net-long-3d", 0),
            ("finmind-low-ps-growth", 1),
            ("finmind-ma20-breakout-volume", 2),
        ),
    ),
    EnsembleTeam(
        "team_tri_alt",
        "備選三角 · S1+S2+S3",
        (
            "finmind-low-rebound-foreign-it-sync",
            "finmind-low-ps-growth",
            "finmind-tx-foreign-net-long-3d",
        ),
        (
            ("finmind-tx-foreign-net-long-3d", 0),
            ("finmind-low-ps-growth", 1),
            ("finmind-low-rebound-foreign-it-sync", 2),
        ),
    ),
    EnsembleTeam(
        "team_all5",
        "全員編隊 · S1~S5",
        (
            "finmind-low-rebound-foreign-it-sync",
            "finmind-low-ps-growth",
            "finmind-tx-foreign-net-long-3d",
            "finmind-ma20-breakout-volume",
            "finmind-it-cold-stove",
        ),
        (
            ("finmind-tx-foreign-net-long-3d", 0),
            ("finmind-low-ps-growth", 1),
            ("finmind-low-rebound-foreign-it-sync", 2),
            ("finmind-ma20-breakout-volume", 3),
            ("finmind-it-cold-stove", 4),
        ),
    ),
)


def load_raw_periods(
    conn,
    topic_id: str,
    *,
    date_start: str,
    date_end: str,
) -> list[dict[str, Any]]:
    if topic_id == "finmind-low-rebound-foreign-it-sync":
        raw, _ = build_lr_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=UNLIMITED_SLOTS,
            hold_days=20,
            params=LowReboundParams(),
        )
        return enrich_lr(conn, raw)

    if topic_id == "finmind-low-ps-growth":
        raw, _ = build_ps_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=UNLIMITED_SLOTS,
            hold_days=60,
            params=PsGrowthParams(),
        )
        return enrich_ps(conn, raw)

    if topic_id == "finmind-ma20-breakout-volume":
        raw, _ = build_ma20_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=UNLIMITED_SLOTS,
            hold_days=10,
            params=Ma20BreakoutParams(),
        )
        return enrich_ma20(conn, raw)

    if topic_id == "finmind-it-cold-stove":
        raw, _ = build_it_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=UNLIMITED_SLOTS,
            params=ItColdStoveParams(),
        )
        return enrich_it(conn, raw)

    if topic_id == "finmind-tx-foreign-net-long-3d":
        raw, _ = build_timing_periods(
            conn,
            date_start=date_start,
            date_end=date_end,
            params=TxForeignParams(),
        )
        return enrich_tx(conn, raw)

    raise ValueError(topic_id)


def merge_team_periods(
    team: EnsembleTeam,
    period_lists: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    prio = {tid: p for tid, p in team.priority}
    merged: list[dict[str, Any]] = []
    for topic_id in team.members:
        for p in period_lists.get(topic_id, []):
            row = dict(p)
            row["source_topic"] = topic_id
            row["source_short"] = SHORT_IDS[topic_id]
            row["_priority"] = prio.get(topic_id, 99)
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


def build_close_panel(conn, trade_dates: list[str]) -> pd.DataFrame:
    close, _, _ = load_price_panels(conn)
    cal_close = close.reindex(trade_dates)
    bench = load_benchmark_close(conn, code=PROXY_CODE)
    if PROXY_CODE not in cal_close.columns:
        cal_close[PROXY_CODE] = bench.reindex(trade_dates).astype(float)
    return cal_close


def run_shared_pool_backtest(
    conn,
    team: EnsembleTeam,
    *,
    date_start: str,
    date_end: str,
    n_slots: int = CMP_N_SLOTS_SLOT,
    pool_mode: str = "shared_pool",
    max_hold_days: int | None = None,
) -> dict[str, Any]:
    period_lists = {
        tid: load_raw_periods(conn, tid, date_start=date_start, date_end=date_end)
        for tid in team.members
    }
    merged = merge_team_periods(team, period_lists)
    close, _, _ = load_price_panels(conn)
    trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    close_panel = build_close_panel(conn, trade_dates)

    std = load_backtest_standard_config()
    sim_kwargs = dict(
        total_capital=std.comparison_notional_ntd,
        n_slots=n_slots,
        cost_model=std.cost_model if std.cost_model.get("enabled") else None,
    )
    if pool_mode == "shared_pool_yield_new":
        portfolio = simulate_slot_portfolio_yield_new(
            conn,
            close_panel,
            trade_dates,
            merged,
            max_hold_days=max_hold_days,
            eviction="oldest",
            **sim_kwargs,
        )
    elif pool_mode == "shared_pool_yield_priority":
        portfolio = simulate_slot_portfolio_yield_new(
            conn,
            close_panel,
            trade_dates,
            merged,
            max_hold_days=max_hold_days,
            eviction="lowest_priority",
            preempt_only_if_higher_priority=True,
            **sim_kwargs,
        )
    else:
        portfolio = simulate_slot_portfolio(
            conn,
            close_panel,
            trade_dates,
            merged,
            **sim_kwargs,
        )

    bench_ret = bench_interval_return_pct(conn, date_start, date_end)
    total_ret = portfolio.get("total_return_pct")
    interval_excess = None
    if total_ret is not None and bench_ret is not None:
        interval_excess = round(float(total_ret) - float(bench_ret), 4)

    candidate_counts = {SHORT_IDS[t]: len(period_lists[t]) for t in team.members}

    mode_label = pool_mode
    if pool_mode == "shared_pool_yield_new" and max_hold_days is not None:
        mode_label = f"yield_new_h{max_hold_days}"

    return {
        "team_id": team.team_id,
        "label": team.label,
        "mode": mode_label,
        "members": [SHORT_IDS[t] for t in team.members],
        "n_slots": n_slots,
        "n_candidates": sum(candidate_counts.values()),
        "n_candidates_by_member": candidate_counts,
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
    }


def run_capital_split_backtest(
    conn,
    team: EnsembleTeam,
    *,
    date_start: str,
    date_end: str,
) -> dict[str, Any]:
    """Each member gets equal notional · independent slot books (baseline)."""
    std = load_backtest_standard_config()
    slice_capital = std.comparison_notional_ntd / len(team.members)
    close, _, _ = load_price_panels(conn)
    trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    close_panel = build_close_panel(conn, trade_dates)

    final_equity = 0.0
    n_fills = 0
    for topic_id in team.members:
        periods = load_raw_periods(conn, topic_id, date_start=date_start, date_end=date_end)
        n_slots = 1 if topic_id == "finmind-tx-foreign-net-long-3d" else CMP_N_SLOTS_SLOT
        portfolio = simulate_slot_portfolio(
            conn,
            close_panel,
            trade_dates,
            periods,
            total_capital=slice_capital,
            n_slots=n_slots,
            cost_model=std.cost_model if std.cost_model.get("enabled") else None,
        )
        final_equity += float(portfolio.get("final_equity_ntd") or slice_capital)
        n_fills += int(portfolio.get("n_periods") or portfolio.get("n_trades") or 0)

    total_ret = round((final_equity / std.comparison_notional_ntd - 1.0) * 100.0, 4)
    bench_ret = bench_interval_return_pct(conn, date_start, date_end)
    interval_excess = round(total_ret - float(bench_ret), 4) if bench_ret is not None else None

    return {
        "team_id": team.team_id,
        "label": team.label,
        "mode": "capital_split",
        "members": [SHORT_IDS[t] for t in team.members],
        "n_fills": n_fills,
        "total_return_pct": total_ret,
        "bench_interval_return_pct": bench_ret,
        "interval_excess_pct": interval_excess,
        "beats_bench": interval_excess is not None and interval_excess > 0,
    }


def build_ensemble_report(
    *,
    date_start: str,
    date_end: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    end = date_end or date.today().isoformat()
    conn = connect(Path(db_path) if db_path else DEFAULT_DB_PATH)
    try:
        rows: list[dict[str, Any]] = []
        for team in ENSEMBLE_TEAMS:
            rows.append(
                run_shared_pool_backtest(conn, team, date_start=date_start, date_end=end)
            )
            rows.append(
                run_shared_pool_backtest(
                    conn,
                    team,
                    date_start=date_start,
                    date_end=end,
                    pool_mode="shared_pool_yield_priority",
                )
            )
            if team.team_id == "team_tri_core":
                rows.append(
                    run_shared_pool_backtest(
                        conn,
                        team,
                        date_start=date_start,
                        date_end=end,
                        pool_mode="shared_pool_yield_new",
                    )
                )
            rows.append(
                run_capital_split_backtest(conn, team, date_start=date_start, date_end=end)
            )
    finally:
        conn.close()

    rows.sort(
        key=lambda r: (r.get("interval_excess_pct") is None, -(r.get("interval_excess_pct") or -9999)),
    )
    return {
        "schema_version": "finmind_marketplace_ensemble-v1",
        "window": {"date_start": date_start, "date_end": end},
        "contract": {
            "total_capital_ntd": load_backtest_standard_config().comparison_notional_ntd,
            "n_slots_shared": CMP_N_SLOTS_SLOT,
            "benchmark": "IX0001",
            "shared_pool_rule": "same-day entry priority by team.priority · global 5 slots",
            "yield_new_rule": "slots full → evict oldest position at close · new signal enters",
            "yield_priority_rule": "slots full → evict lowest-priority strategy leg · protect S3/S2",
            "capital_split_rule": "equal notional per member · independent books",
        },
        "rows": rows,
        "generated_at": date.today().isoformat(),
    }


def render_ensemble_markdown(reports: list[dict[str, Any]]) -> str:
    lines = [
        "# FinMind 市集策略 · 編隊合併回測",
        "",
        "合約：100k NTD · IX0001 基準 · 成本模型開啟",
        "",
    ]
    for rep in reports:
        w = rep["window"]
        lines.extend(
            [
                f"## 窗口 · {w['date_start']} ～ {w['date_end']}",
                "",
                "| 編隊 | 模式 | 成交 | 均持倉日 | 搶槽次數 | 總報酬% | 基準% | **區間超額%** | 贏? |",
                "|------|------|------|----------|----------|---------|-------|---------------|-----|",
            ]
        )
        for row in rep["rows"]:
            beat = "✓" if row.get("beats_bench") else "—"
            lines.append(
                f"| {row['label']} | {row['mode']} | {row.get('n_fills', '—')} | "
                f"{row.get('avg_hold_days', '—')} | {row.get('n_preemptions', '—')} | "
                f"{row.get('total_return_pct', '—')} | {row.get('bench_interval_return_pct', '—')} | "
                f"**{row.get('interval_excess_pct', '—')}** | {beat} |"
            )
        lines.append("")

    best = None
    for rep in reports:
        for row in rep["rows"]:
            if row.get("beats_bench"):
                if best is None or (row.get("interval_excess_pct") or 0) > (
                    best.get("interval_excess_pct") or 0
                ):
                    best = row

    lines.extend(["## 結論", ""])
    if best:
        lines.append(
            f"- **有編隊贏基準**：{best['label']}（{best['mode']}）· "
            f"區間超額 **{best['interval_excess_pct']}%**"
        )
    else:
        lines.append("- **尚無編隊贏過 IX0001 區間 buy-and-hold** · 見上表區間超額皆為負或最接近 0 者。")

    tri_modes = [
        r
        for rep in reports
        for r in rep["rows"]
        if r["team_id"] == "team_tri_core"
        and r["mode"] in (
            "shared_pool",
            "shared_pool_yield_new",
            "shared_pool_yield_priority",
        )
    ]
    for tri in tri_modes:
        lines.append(
            f"- 主力三角 `{tri['mode']}`：總報酬 {tri.get('total_return_pct')}% · "
            f"區間超額 {tri.get('interval_excess_pct')}% · "
            f"均持倉 {tri.get('avg_hold_days')} 日 · 搶槽 {tri.get('n_preemptions', '—')} 次"
        )
    lines.append("")
    return "\n".join(lines)


def run_ensemble_analysis(
    *,
    db_path: str | Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    out = out_dir or Path("reports/research/finmind-marketplace")
    out.mkdir(parents=True, exist_ok=True)

    reports = [
        build_ensemble_report(
            date_start=WINDOW_FULL_COMMON[1],
            date_end=WINDOW_FULL_COMMON[2],
            db_path=db_path,
        ),
        build_ensemble_report(
            date_start=WINDOW_OOS_2024[1],
            date_end=WINDOW_OOS_2024[2],
            db_path=db_path,
        ),
    ]
    stamp = date.today().strftime("%Y%m%d")
    json_path = out / f"finmind_marketplace_ensemble_{stamp}.json"
    md_path = out / f"finmind_marketplace_ensemble_{stamp}.md"
    payload = {"schema_version": "finmind_marketplace_ensemble-v1", "windows": reports}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_ensemble_markdown(reports), encoding="utf-8")
    return {"json_path": json_path, "md_path": md_path, "windows": reports}
