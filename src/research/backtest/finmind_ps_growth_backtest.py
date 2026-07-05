"""FinMind marketplace · 低市銷率成長股 · slot backtest."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from backtest_standard_config import load_backtest_standard_config
from flow_returns import return_pct, stock_close, stock_open, trading_dates_after
from research.backtest.finpilot_local_backtest import (
    load_financial_history,
    load_fundamental_snapshot,
    load_price_panels,
    summarize_periods,
)
from research.backtest.finmind_ps_growth_common import (
    DEFAULT_DATE_START,
    TOPIC_PS_GROWTH,
    PsGrowthParams,
    PsGrowthScreenMatrices,
    build_ps_growth_screen_matrices,
    count_condition_passes,
    load_market_value_long,
    load_shareholding_long,
    resolve_universe_stock_ids,
)
from research.backtest.slot_backtest_summary import SlotBacktestConfig, build_summary_payload
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_HOLD_DAYS = 60
DEFAULT_N_SLOTS = 5


@dataclass(frozen=True)
class SignalCandidate:
    stock_id: str
    signal_date: str
    revenue_yoy_pct: float
    ps_ratio: float


def screen_candidates_on_date(
    signal_date: str,
    stock_ids: list[str],
    matrices: PsGrowthScreenMatrices,
    *,
    require_market: bool,
) -> list[SignalCandidate]:
    if require_market and not bool(matrices.market_ma5.loc[signal_date]):
        return []
    h1 = matrices.ps_low.loc[signal_date, stock_ids]
    h2 = matrices.rev_yoy.loc[signal_date, stock_ids]
    h3 = matrices.margin_2q.loc[signal_date, stock_ids]
    h4 = matrices.capital.loc[signal_date, stock_ids]
    h5 = matrices.turnover.loc[signal_date, stock_ids]
    full = h1 & h2 & h3 & h4 & h5
    passing = full[full].index.tolist()
    out: list[SignalCandidate] = []
    for sid in passing:
        yoy = matrices.rev_yoy_val.at[signal_date, sid]
        ps = matrices.ps_ratio.at[signal_date, sid]
        out.append(
            SignalCandidate(
                stock_id=str(sid),
                signal_date=signal_date,
                revenue_yoy_pct=float(yoy or 0),
                ps_ratio=float(ps if ps == ps else 999.0),
            )
        )
    out.sort(key=lambda c: (-c.revenue_yoy_pct, c.ps_ratio))
    return out


def build_periods(
    conn: sqlite3.Connection,
    *,
    universe: str,
    date_start: str,
    date_end: str,
    n_slots: int,
    hold_days: int,
    params: PsGrowthParams,
    matrices: PsGrowthScreenMatrices | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    close, opn, vol = load_price_panels(conn)
    stock_ids = resolve_universe_stock_ids(conn, universe)
    price_ok = [s for s in stock_ids if s in close.columns]
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]

    if matrices is None:
        mv = load_market_value_long(conn)
        sh = load_shareholding_long(conn)
        fund = load_fundamental_snapshot(conn)
        fin_hist = load_financial_history(conn)
        matrices = build_ps_growth_screen_matrices(
            conn=conn,
            stock_ids=stock_ids,
            cal=cal,
            close=close,
            vol=vol,
            mv=mv,
            sh=sh,
            fund=fund,
            fin_hist=fin_hist,
            params=params,
        )

    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    signal_days = 0

    for signal_date in cal:
        slots = [s for s in slots if s["exit_date"] >= signal_date]
        held = {s["stock_id"] for s in slots}
        free = n_slots - len(slots)
        if free <= 0:
            continue

        cands = screen_candidates_on_date(
            signal_date,
            price_ok,
            matrices,
            require_market=params.require_market_ma5,
        )
        if not cands:
            continue
        signal_days += 1

        entry_candidates = trading_dates_after(conn, signal_date, count=1)
        if not entry_candidates:
            continue
        entry_date = entry_candidates[0]
        if entry_date > date_end:
            continue

        for cand in cands:
            if free <= 0:
                break
            if cand.stock_id in held:
                continue
            after = trading_dates_after(conn, entry_date, count=hold_days)
            if len(after) < hold_days:
                continue
            exit_date = after[hold_days - 1]

            entry_px = None
            if cand.stock_id in opn.columns and entry_date in opn.index:
                v = opn.at[entry_date, cand.stock_id]
                if v is not None and float(v) > 0:
                    entry_px = float(v)
            if entry_px is None:
                entry_px = stock_open(conn, cand.stock_id, entry_date)
            if entry_px is None or entry_px <= 0:
                continue

            period = {
                "stock_id": cand.stock_id,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_px": entry_px,
                "revenue_yoy_pct": cand.revenue_yoy_pct,
                "ps_ratio": cand.ps_ratio,
            }
            periods.append(period)
            slots.append(period)
            held.add(cand.stock_id)
            free -= 1

    meta = {
        "signal_days_with_candidates": signal_days,
        "n_periods": len(periods),
    }
    return periods, meta


def enrich_period_returns(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in periods:
        sid = p["stock_id"]
        entry = p["entry_date"]
        exit_d = p["exit_date"]
        entry_px = float(p["entry_px"])
        exit_px = stock_close(conn, sid, exit_d)
        if exit_px is None or exit_px <= 0:
            continue
        gross = return_pct(entry_px, float(exit_px))
        bench = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode="open")
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


def run_ps_growth_backtest(
    *,
    db_path: str | Path | None = None,
    universe: str = "tw100",
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    n_slots: int = DEFAULT_N_SLOTS,
    hold_days: int = DEFAULT_HOLD_DAYS,
    params: PsGrowthParams | None = None,
    include_validation: bool = True,
) -> dict[str, Any]:
    p = params or PsGrowthParams()
    end = date_end or date.today().isoformat()
    conn = connect(Path(db_path) if db_path is not None else DEFAULT_DB_PATH)
    try:
        validation = (
            count_condition_passes(conn, universe, date_start, end, params=p)
            if include_validation
            else None
        )
        periods_raw, build_meta = build_periods(
            conn,
            universe=universe,
            date_start=date_start,
            date_end=end,
            n_slots=n_slots,
            hold_days=hold_days,
            params=p,
        )
        periods = enrich_period_returns(conn, periods_raw)
        close, _, _ = load_price_panels(conn)
        trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= end]

        std = load_backtest_standard_config()
        portfolio = simulate_slot_portfolio(
            conn,
            close,
            trade_dates,
            periods,
            total_capital=std.comparison_notional_ntd,
            n_slots=n_slots,
            cost_model=std.cost_model if std.cost_model.get("enabled") else None,
        )

        period_summary = summarize_periods(periods)
        if periods:
            period_summary["mean_excess_pct"] = round(
                sum(x["excess_pct"] for x in periods) / len(periods), 4
            )
        else:
            period_summary["mean_excess_pct"] = None

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

        above_ma = [x for x in periods if x.get("market_above_ma5", True)]
        below_ma: list[dict[str, Any]] = []

        cfg = SlotBacktestConfig(
            date_start=date_start,
            date_end=end,
            n_slots=n_slots,
            hold_days=hold_days,
            entry_price_mode="open",
            strategy_id=TOPIC_PS_GROWTH,
            variant=f"ps_growth_h{hold_days}_open",
        )
        summary = {**period_summary, **portfolio}
        return build_summary_payload(
            track_id=TOPIC_PS_GROWTH,
            config=cfg,
            summary=summary,
            source_module="finmind_ps_growth_backtest",
            extra={
                "research_topic": TOPIC_PS_GROWTH,
                "spec_fidelity": "approx",
                "universe": universe,
                "params": asdict(p),
                "build_meta": build_meta,
                "validation": validation,
                "by_year": by_year,
                "oos_from_2024": oos_summary,
                "is_before_2024": is_summary,
                "exit_assumption": f"hold_{hold_days}d_close_after_t_plus_1_open",
                "regime_stratify_ix_ma5": {
                    "above_ma5_n": len(above_ma),
                    "below_ma5_n": len(below_ma),
                },
            },
        )
    finally:
        conn.close()


def render_ps_growth_markdown(result: dict[str, Any]) -> str:
    v = result.get("validation") or {}
    ch = v.get("condition_hits") or {}
    pm = result.get("portfolio_metrics") or {}
    summary = result.get("summary") or {}
    approx = v.get("approx_notes") or {}
    lines = [
        "# 低市銷率成長股 · 回測報告",
        "",
        f"- **topic**: `{result.get('research_topic', TOPIC_PS_GROWTH)}`",
        f"- **universe**: {result.get('universe', 'tw100')}",
        f"- **窗口**: {result.get('date_start')} ～ {result.get('date_end')}",
        f"- **槽位 / 持有**: {result.get('n_slots')} 槽 · {result.get('hold_days')} 交易日",
        f"- **進場**: T+1 open · **出場**: {result.get('exit_assumption', '')}",
        f"- **fidelity**: {result.get('spec_fidelity', 'approx')}",
        "",
        "## Phase 0 · 資料驗證",
        "",
        f"- universe 檔數: {v.get('universe_size', '—')}",
        f"- 交易日: {v.get('trading_days', '—')}",
        f"- H1 P/S<5: {ch.get('h1_ps_low', '—')}",
        f"- H2 營收 YoY≥50%: {ch.get('h2_rev_yoy', '—')}",
        f"- H3 連續2季 margin≥5%: {ch.get('h3_margin_2q', '—')}",
        f"- H4 股本≥10億: {ch.get('h4_capital', '—')}",
        f"- H5 5日成交值≥0.2億: {ch.get('h5_turnover', '—')}",
        f"- M1 台指>MA5 日數: {ch.get('m1_ix_ma5', '—')}",
        f"- **全條件命中**: {ch.get('full_screen', '—')}",
        f"- Phase0 pass (≥30): **{'是' if v.get('phase0_pass') else '否'}**",
        "",
        "### 近似說明",
        "",
    ]
    for k, note in approx.items():
        lines.append(f"- {k}: {note}")
    lines.extend(
        [
            "",
            "## 組合績效",
            "",
            "| 指標 | 值 |",
            "|------|-----|",
            f"| 成交筆數 | {summary.get('n_periods', 0)} |",
            f"| 勝率 vs 台指 | {summary.get('win_rate_vs_bench_pct', '—')}% |",
            f"| 每筆均報酬 | {summary.get('mean_return_pct', '—')}% |",
            f"| 每筆均超額 | {summary.get('mean_excess_pct', '—')}% |",
            f"| 組合總報酬 | {pm.get('total_return_pct', summary.get('total_return_pct', '—'))}% |",
            f"| Sharpe | {pm.get('sharpe_ratio', '—')} |",
            f"| 最大回撤 | {pm.get('max_drawdown_pct', '—')}% |",
            "",
            "## 樣本內外",
            "",
        ]
    )
    oos = result.get("oos_from_2024") or {}
    is_ = result.get("is_before_2024") or {}
    lines.extend(
        [
            f"- IS (&lt;2024): n={is_.get('n_periods', 0)} · mean excess={is_.get('mean_excess_pct', '—')}%",
            f"- OOS (2024+): n={oos.get('n_periods', 0)} · mean excess={oos.get('mean_excess_pct', '—')}%",
            "",
            "## 年度分解",
            "",
            "| 年 | 筆數 | 勝率% | 均超額% |",
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
