"""FinMind marketplace · 突破月線長紅放量 · slot backtest."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from analytics.bench import bench_return_entry_to_exit
from backtest_standard_config import load_backtest_standard_config
from flow_returns import return_pct, stock_close, stock_open, trading_dates_after
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.finmind_ma20_breakout_common import (
    DEFAULT_DATE_START,
    TOPIC_MA20_BREAKOUT,
    Ma20BreakoutParams,
    Ma20BreakoutScreenMatrices,
    build_ma20_breakout_screen_matrices,
    count_condition_passes,
    load_technical_breakout_long,
    resolve_universe_stock_ids,
)
from research.backtest.slot_backtest_summary import SlotBacktestConfig, build_summary_payload
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_HOLD_DAYS = 10
DEFAULT_N_SLOTS = 5


@dataclass(frozen=True)
class SignalCandidate:
    stock_id: str
    signal_date: str
    return_1d_pct: float


def screen_candidates_on_date(
    signal_date: str,
    stock_ids: list[str],
    matrices: Ma20BreakoutScreenMatrices,
) -> list[SignalCandidate]:
    p1 = matrices.ma20_breakout.loc[signal_date, stock_ids]
    p2 = matrices.return_1d.loc[signal_date, stock_ids]
    i1 = matrices.vol_ratio.loc[signal_date, stock_ids]
    full = p1 & p2 & i1
    passing = full[full].index.tolist()
    out: list[SignalCandidate] = []
    for sid in passing:
        ret = matrices.return_1d_val.at[signal_date, sid]
        out.append(
            SignalCandidate(
                stock_id=str(sid),
                signal_date=signal_date,
                return_1d_pct=float(ret or 0),
            )
        )
    out.sort(key=lambda c: -c.return_1d_pct)
    return out


def build_periods(
    conn: sqlite3.Connection,
    *,
    universe: str,
    date_start: str,
    date_end: str,
    n_slots: int,
    hold_days: int,
    params: Ma20BreakoutParams,
    matrices: Ma20BreakoutScreenMatrices | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    close, opn, _ = load_price_panels(conn)
    stock_ids = resolve_universe_stock_ids(conn, universe)
    price_ok = [s for s in stock_ids if s in close.columns]
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]

    if matrices is None:
        tech = load_technical_breakout_long(conn)
        tech = tech[tech["stock_id"].isin(stock_ids)]
        matrices = build_ma20_breakout_screen_matrices(
            stock_ids=stock_ids,
            cal=cal,
            close=close,
            tech=tech,
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

        cands = screen_candidates_on_date(signal_date, price_ok, matrices)
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

            periods.append(
                {
                    "stock_id": cand.stock_id,
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "entry_px": entry_px,
                    "return_1d_pct": cand.return_1d_pct,
                }
            )
            slots.append(periods[-1])
            held.add(cand.stock_id)
            free -= 1

    return periods, {
        "signal_days_with_candidates": signal_days,
        "n_periods": len(periods),
    }


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


def run_ma20_breakout_backtest(
    *,
    db_path: str | Path | None = None,
    universe: str = "tw100",
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    n_slots: int = DEFAULT_N_SLOTS,
    hold_days: int = DEFAULT_HOLD_DAYS,
    params: Ma20BreakoutParams | None = None,
    include_validation: bool = True,
) -> dict[str, Any]:
    p = params or Ma20BreakoutParams()
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

        cfg = SlotBacktestConfig(
            date_start=date_start,
            date_end=end,
            n_slots=n_slots,
            hold_days=hold_days,
            entry_price_mode="open",
            strategy_id=TOPIC_MA20_BREAKOUT,
            variant=f"ma20_breakout_h{hold_days}_open",
        )
        return build_summary_payload(
            track_id=TOPIC_MA20_BREAKOUT,
            config=cfg,
            summary={**period_summary, **portfolio},
            source_module="finmind_ma20_breakout_backtest",
            extra={
                "research_topic": TOPIC_MA20_BREAKOUT,
                "spec_fidelity": "approx",
                "universe": universe,
                "params": asdict(p),
                "build_meta": build_meta,
                "validation": validation,
                "by_year": by_year,
                "oos_from_2024": oos_summary,
                "is_before_2024": is_summary,
                "exit_assumption": f"hold_{hold_days}d_close_after_t_plus_1_open",
            },
        )
    finally:
        conn.close()


def render_ma20_breakout_markdown(result: dict[str, Any]) -> str:
    v = result.get("validation") or {}
    ch = v.get("condition_hits") or {}
    pm = result.get("portfolio_metrics") or {}
    summary = result.get("summary") or {}
    approx = v.get("approx_notes") or {}
    lines = [
        "# 突破月線長紅放量 · 回測報告",
        "",
        f"- **topic**: `{result.get('research_topic', TOPIC_MA20_BREAKOUT)}`",
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
        f"- P1 MA20 突破: {ch.get('p1_ma20_breakout', '—')}",
        f"- P2 漲幅≥5%: {ch.get('p2_return_5pct', '—')}",
        f"- I1 vol_ratio≥2: {ch.get('i1_vol_ratio_2x', '—')}",
        f"- **全條件命中**: {ch.get('full_screen', '—')}",
        f"- Phase0 pass (≥50): **{'是' if v.get('phase0_pass') else '否'}**",
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
            f"- IS (&lt;2024): n={result.get('is_before_2024', {}).get('n_periods', 0)} · "
            f"mean excess={result.get('is_before_2024', {}).get('mean_excess_pct', '—')}%",
            f"- OOS (2024+): n={result.get('oos_from_2024', {}).get('n_periods', 0)} · "
            f"mean excess={result.get('oos_from_2024', {}).get('mean_excess_pct', '—')}%",
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
