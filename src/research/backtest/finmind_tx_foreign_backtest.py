"""FinMind marketplace · TX 外資淨多 3 日 · single-position timing backtest."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from backtest_standard_config import load_backtest_standard_config
from flow_returns import return_pct, trading_dates_after
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import summarize_periods
from research.backtest.finmind_tx_foreign_common import (
    DEFAULT_DATE_START,
    TOPIC_TX_FOREIGN,
    TxForeignParams,
    bench_close,
    bench_open,
    count_futures_gaps,
    load_foreign_net_oi,
    load_proxy_close_ma,
    streak_signals,
    validate_tx_foreign_data,
)
from research.backtest.slot_backtest_summary import SlotBacktestConfig, build_summary_payload, PORTFOLIO_PCT_METRICS
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from stock_db import DEFAULT_DB_PATH, connect


def build_timing_periods(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    params: TxForeignParams,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bench = load_benchmark_close(conn, code=params.proxy_code)
    cal = [d for d in bench.index.astype(str).tolist() if date_start <= d <= date_end]

    net = load_foreign_net_oi(conn, params.futures_id).reindex(cal)
    signals = streak_signals(net.dropna(), streak_days=params.streak_days).reindex(cal).fillna(False)
    ix_close, ix_ma = load_proxy_close_ma(conn, cal, code=params.proxy_code, ma_period=params.ma_exit_period)

    periods: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    exit_reasons: dict[str, int] = {}

    for i, d in enumerate(cal):
        if position is not None:
            prev = cal[i - 1] if i > 0 else None
            days_in = i - int(position["entry_idx"])
            exit_reason: str | None = None
            if prev and pd.notna(ix_close.get(prev)) and pd.notna(ix_ma.get(prev)):
                if float(ix_close[prev]) < float(ix_ma[prev]):
                    exit_reason = "ma5_break"
            if days_in >= params.max_hold_days:
                exit_reason = exit_reason or "max_hold"

            if exit_reason:
                exit_px = bench_open(conn, d, code=params.proxy_code)
                if exit_px is None or exit_px <= 0:
                    continue
                periods.append(
                    {
                        "stock_id": params.proxy_code,
                        "signal_date": position["signal_date"],
                        "entry_date": position["entry_date"],
                        "exit_date": d,
                        "entry_px": position["entry_px"],
                        "exit_px": exit_px,
                        "exit_reason": exit_reason,
                        "hold_days": days_in,
                    }
                )
                exit_reasons[exit_reason] = exit_reasons.get(exit_reason, 0) + 1
                position = None

        if position is None and bool(signals.get(d, False)):
            entry_dates = trading_dates_after(conn, d, count=1)
            if not entry_dates or entry_dates[0] > date_end:
                continue
            entry_date = entry_dates[0]
            entry_px = bench_open(conn, entry_date, code=params.proxy_code)
            if entry_px is None or entry_px <= 0:
                continue
            try:
                entry_idx = cal.index(entry_date)
            except ValueError:
                continue
            position = {
                "signal_date": d,
                "entry_date": entry_date,
                "entry_px": entry_px,
                "entry_idx": entry_idx,
            }

    if position is not None and cal:
        d = cal[-1]
        exit_px = bench_close(conn, d, code=params.proxy_code)
        if exit_px and exit_px > 0:
            periods.append(
                {
                    "stock_id": params.proxy_code,
                    "signal_date": position["signal_date"],
                    "entry_date": position["entry_date"],
                    "exit_date": d,
                    "entry_px": position["entry_px"],
                    "exit_px": float(exit_px),
                    "exit_reason": "window_end",
                    "hold_days": len(cal) - 1 - int(position["entry_idx"]),
                }
            )
            exit_reasons["window_end"] = exit_reasons.get("window_end", 0) + 1

    meta = {
        "n_periods": len(periods),
        "exit_reason_counts": exit_reasons,
        "signal_days": int(signals.sum()),
    }
    return periods, meta


def enrich_period_returns(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in periods:
        entry_px = float(p["entry_px"])
        exit_px = float(p["exit_px"])
        gross = return_pct(entry_px, exit_px)
        bench = bench_return_entry_to_exit(
            conn, p["entry_date"], p["exit_date"], entry_price_mode="open"
        )
        if bench is None:
            continue
        row = dict(p)
        row.update(
            {
                "return_pct": round(gross, 4),
                "bench_return_pct": round(bench, 4),
                "excess_pct": round(gross - bench, 4),
                "gross_win": gross > 0,
                "beat_bench": gross > bench,
            }
        )
        out.append(row)
    return out


def run_tx_foreign_backtest(
    *,
    db_path: str | Path | None = None,
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    params: TxForeignParams | None = None,
    include_validation: bool = True,
) -> dict[str, Any]:
    p = params or TxForeignParams()
    end = date_end or date.today().isoformat()
    conn = connect(Path(db_path) if db_path is not None else DEFAULT_DB_PATH)
    try:
        validation = (
            validate_tx_foreign_data(
                db_path=Path(db_path) if db_path else DEFAULT_DB_PATH,
                date_start=date_start,
                date_end=end,
                params=p,
            )
            if include_validation
            else None
        )
        periods_raw, build_meta = build_timing_periods(
            conn, date_start=date_start, date_end=end, params=p
        )
        periods = enrich_period_returns(conn, periods_raw)

        bench = load_benchmark_close(conn, code=p.proxy_code)
        cal = [d for d in bench.index.astype(str).tolist() if date_start <= d <= end]
        close_panel = pd.DataFrame({p.proxy_code: bench.reindex(cal).astype(float)})

        std = load_backtest_standard_config()
        portfolio = simulate_slot_portfolio(
            conn,
            close_panel,
            cal,
            periods,
            total_capital=std.comparison_notional_ntd,
            n_slots=1,
            cost_model=std.cost_model if std.cost_model.get("enabled") else None,
        )

        period_summary = summarize_periods(periods)
        if periods:
            period_summary["mean_excess_pct"] = round(
                sum(x["excess_pct"] for x in periods) / len(periods), 4
            )
            period_summary["mean_hold_days"] = round(
                sum(x.get("hold_days", 0) for x in periods) / len(periods), 2
            )
        else:
            period_summary["mean_excess_pct"] = None
            period_summary["mean_hold_days"] = None

        summary = dict(period_summary)
        for key in PORTFOLIO_PCT_METRICS:
            if key in portfolio:
                summary[key] = portfolio[key]

        by_year: dict[str, dict[str, Any]] = {}
        for yr in sorted({x["signal_date"][:4] for x in periods}):
            sub = [x for x in periods if x["signal_date"].startswith(yr)]
            s = summarize_periods(sub)
            if sub:
                s["mean_excess_pct"] = round(
                    sum(x["excess_pct"] for x in sub) / len(sub), 4
                )
            s["n_signals"] = len(sub)
            by_year[yr] = s

        oos = [x for x in periods if x["signal_date"] >= "2024-01-01"]
        is_ = [x for x in periods if x["signal_date"] < "2024-01-01"]
        oos_summary = summarize_periods(oos)
        is_summary = summarize_periods(is_)
        if oos:
            oos_summary["mean_excess_pct"] = round(
                sum(x["excess_pct"] for x in oos) / len(oos), 4
            )
        if is_:
            is_summary["mean_excess_pct"] = round(
                sum(x["excess_pct"] for x in is_) / len(is_), 4
            )

        gaps = count_futures_gaps(conn, p.futures_id, cal)

        cfg = SlotBacktestConfig(
            date_start=date_start,
            date_end=end,
            n_slots=1,
            hold_days=p.max_hold_days,
            entry_price_mode="open",
            strategy_id=TOPIC_TX_FOREIGN,
            variant="tx_foreign_3d_ix_proxy",
        )
        return build_summary_payload(
            track_id=TOPIC_TX_FOREIGN,
            config=cfg,
            summary=summary,
            source_module="finmind_tx_foreign_backtest",
            extra={
                "research_topic": TOPIC_TX_FOREIGN,
                "spec_fidelity": "approx",
                "spec_type": "single_position_timing",
                "engine": "non_slot_timing",
                "proxy_code": p.proxy_code,
                "futures_id": p.futures_id,
                "params": asdict(p),
                "build_meta": build_meta,
                "validation": validation,
                "futures_gap_note": gaps,
                "by_year": by_year,
                "oos_from_2024": oos_summary,
                "is_before_2024": is_summary,
                "exit_assumption": f"{p.proxy_code}_close_lt_ma5_t_plus_1_open_or_max_{p.max_hold_days}d",
            },
        )
    finally:
        conn.close()


def render_tx_foreign_markdown(result: dict[str, Any]) -> str:
    v = result.get("validation") or {}
    gaps = result.get("futures_gap_note") or v.get("futures_coverage") or {}
    ch = v if v.get("e1_streak_3d_signals") is not None else {}
    pm = result.get("portfolio_metrics") or {}
    summary = result.get("summary") or {}
    build_meta = result.get("build_meta") or {}
    approx = v.get("approx_notes") or {}

    lines = [
        "# TX 外資淨多單持續 3 日 · 回測報告",
        "",
        f"- **topic**: `{result.get('research_topic', TOPIC_TX_FOREIGN)}`",
        f"- **引擎**: single-position timing（非槽位）",
        f"- **期貨訊號**: `{result.get('futures_id', 'TX')}` 外資 `net_oi_vol>0` 連 3 日",
        f"- **交易 proxy**: 做多 `{result.get('proxy_code', 'IX0001')}` · T+1 open",
        f"- **窗口**: {result.get('date_start')} ～ {result.get('date_end')}",
        f"- **出場**: {result.get('exit_assumption', '')}",
        f"- **fidelity**: {result.get('spec_fidelity', 'approx')}",
        "",
        "## Phase 0 · 資料驗證",
        "",
        f"- 交易日: {v.get('trading_days', '—')}",
        f"- 期貨 OI 日數: {v.get('foreign_oi_days', '—')}",
        f"- E1 連3日淨多訊號: {v.get('e1_streak_3d_signals', ch.get('e1_streak_3d_signals', '—'))}",
        f"- 期貨缺日: {gaps.get('missing_days', '—')} / {gaps.get('calendar_days_in_range', '—')}",
        f"- Phase0 pass: **{'是' if v.get('phase0_pass') else '否'}**",
        "",
        "### 近似說明",
        "",
    ]
    for k, note in approx.items():
        lines.append(f"- {k}: {note}")
    exit_counts = build_meta.get("exit_reason_counts") or {}
    if exit_counts:
        lines.extend(["", "### 出場原因", ""])
        for reason, n in sorted(exit_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {reason}: {n}")
    lines.extend(
        [
            "",
            "## 組合績效（擇時 vs 全程持有 proxy）",
            "",
            "| 指標 | 值 |",
            "|------|-----|",
            f"| 回合數 | {summary.get('n_periods', 0)} |",
            f"| 均持有日 | {summary.get('mean_hold_days', '—')} |",
            f"| 勝率 vs 台指 | {summary.get('win_rate_vs_bench_pct', '—')}% |",
            f"| 每回合均超額 | {summary.get('mean_excess_pct', '—')}% |",
            f"| 擇時組合總報酬 | {pm.get('total_return_pct', summary.get('total_return_pct', '—'))}% |",
            f"| Sharpe | {pm.get('sharpe_ratio', '—')} |",
            f"| 最大回撤 | {pm.get('max_drawdown_pct', '—')}% |",
            "",
            "## 樣本內外",
            "",
            f"- IS (&lt;2024): n={result.get('is_before_2024', {}).get('n_periods', 0)} · "
            f"mean excess={result.get('is_before_2024', {}).get('mean_excess_pct', '—')}%",
            f"- OOS (2024+): n={result.get('oos_from_2024', {}).get('n_periods', 0)} · "
            f"mean excess={result.get('oos_from_2024', {}).get('mean_excess_pct', '—')}%",
            "",
            "## 年度分解",
            "",
            "| 年 | 回合 | 勝率% | 均超額% |",
            "|----|------|-------|---------|",
        ]
    )
    for yr, row in sorted((result.get("by_year") or {}).items()):
        lines.append(
            f"| {yr} | {row.get('n_signals', 0)} | "
            f"{row.get('win_rate_vs_bench_pct', '—')} | {row.get('mean_excess_pct', '—')} |"
        )
    lines.append("")
    return "\n".join(lines)
