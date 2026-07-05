"""FinMind marketplace · 投信燒冷灶（簡化版）· slot backtest."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from analytics.bench import bench_return_entry_to_exit
from backtest_standard_config import load_backtest_standard_config
from flow_returns import (
    return_pct,
    stock_close,
    stock_high,
    stock_low,
    stock_open,
    trading_dates_after,
)
from research.backtest.finmind_it_cold_stove_common import (
    DEFAULT_DATE_START,
    TOPIC_IT_COLD_STOVE,
    ItColdStoveParams,
    ItColdStoveScreenMatrices,
    build_it_cold_stove_screen_matrices,
    count_condition_passes,
)
from research.backtest.finmind_screener_common import load_institutional_long, resolve_universe_stock_ids
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.slot_backtest_summary import SlotBacktestConfig, build_summary_payload
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_N_SLOTS = 5


@dataclass(frozen=True)
class SignalCandidate:
    stock_id: str
    signal_date: str
    investment_trust_net: float


def screen_candidates_on_date(
    signal_date: str,
    stock_ids: list[str],
    matrices: ItColdStoveScreenMatrices,
) -> list[SignalCandidate]:
    e1 = matrices.it_buy.loc[signal_date, stock_ids]
    passing = e1[e1].index.tolist()
    out: list[SignalCandidate] = []
    for sid in passing:
        net = matrices.it_net.at[signal_date, sid]
        out.append(
            SignalCandidate(
                stock_id=str(sid),
                signal_date=signal_date,
                investment_trust_net=float(net or 0),
            )
        )
    out.sort(key=lambda c: -c.investment_trust_net)
    return out


def resolve_sl_tp_exit(
    conn: sqlite3.Connection,
    stock_id: str,
    entry_date: str,
    entry_px: float,
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
    max_hold_days: int,
) -> tuple[str | None, float | None, str]:
    dates = trading_dates_after(conn, entry_date, count=max_hold_days)
    if not dates:
        return None, None, "no_calendar"

    stop_px = entry_px * (1.0 - stop_loss_pct / 100.0)
    tp_px = entry_px * (1.0 + take_profit_pct / 100.0)

    for i, d in enumerate(dates):
        opn = stock_open(conn, stock_id, d)
        low = stock_low(conn, stock_id, d)
        high = stock_high(conn, stock_id, d)

        if opn is not None and opn > 0 and opn <= stop_px:
            return d, float(opn), "stop_loss_gap"
        if low is not None and low <= stop_px:
            return d, float(stop_px), "stop_loss"

        if opn is not None and opn > 0 and opn >= tp_px:
            return d, float(opn), "take_profit_gap"
        if high is not None and high >= tp_px:
            return d, float(tp_px), "take_profit"

        if i == len(dates) - 1:
            close_px = stock_close(conn, stock_id, d)
            if close_px is None or close_px <= 0:
                return None, None, "no_price"
            return d, float(close_px), "hold_expire"

    return None, None, "no_price"


def build_periods(
    conn: sqlite3.Connection,
    *,
    universe: str,
    date_start: str,
    date_end: str,
    n_slots: int,
    params: ItColdStoveParams,
    matrices: ItColdStoveScreenMatrices | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    close, opn, _ = load_price_panels(conn)
    stock_ids = resolve_universe_stock_ids(conn, universe)
    price_ok = [s for s in stock_ids if s in close.columns]
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]

    if matrices is None:
        inst = load_institutional_long(conn)
        inst = inst[inst["stock_id"].isin(stock_ids)]
        matrices = build_it_cold_stove_screen_matrices(
            stock_ids=stock_ids, cal=cal, inst=inst, params=params
        )

    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    signal_days = 0
    exit_reasons: dict[str, int] = {}

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

            entry_px = None
            if cand.stock_id in opn.columns and entry_date in opn.index:
                v = opn.at[entry_date, cand.stock_id]
                if v is not None and float(v) > 0:
                    entry_px = float(v)
            if entry_px is None:
                entry_px = stock_open(conn, cand.stock_id, entry_date)
            if entry_px is None or entry_px <= 0:
                continue

            exit_date, exit_px, reason = resolve_sl_tp_exit(
                conn,
                cand.stock_id,
                entry_date,
                entry_px,
                stop_loss_pct=params.stop_loss_pct,
                take_profit_pct=params.take_profit_pct,
                max_hold_days=params.max_hold_days,
            )
            if exit_date is None or exit_px is None:
                continue

            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
            period = {
                "stock_id": cand.stock_id,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "exit_reason": reason,
                "investment_trust_net": cand.investment_trust_net,
            }
            periods.append(period)
            slots.append(period)
            held.add(cand.stock_id)
            free -= 1

    return periods, {
        "signal_days_with_candidates": signal_days,
        "n_periods": len(periods),
        "exit_reason_counts": exit_reasons,
    }


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


def run_it_cold_stove_backtest(
    *,
    db_path: str | Path | None = None,
    universe: str = "tw100",
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    n_slots: int = DEFAULT_N_SLOTS,
    params: ItColdStoveParams | None = None,
    include_validation: bool = True,
) -> dict[str, Any]:
    p = params or ItColdStoveParams()
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
            hold_days=p.max_hold_days,
            entry_price_mode="open",
            strategy_id=TOPIC_IT_COLD_STOVE,
            variant=f"it_cold_stove_sl{int(p.stop_loss_pct)}_tp{int(p.take_profit_pct)}",
        )
        return build_summary_payload(
            track_id=TOPIC_IT_COLD_STOVE,
            config=cfg,
            summary={**period_summary, **portfolio},
            source_module="finmind_it_cold_stove_backtest",
            extra={
                "research_topic": TOPIC_IT_COLD_STOVE,
                "spec_fidelity": "simplified",
                "universe": universe,
                "params": asdict(p),
                "build_meta": build_meta,
                "validation": validation,
                "by_year": by_year,
                "oos_from_2024": oos_summary,
                "is_before_2024": is_summary,
                "exit_assumption": (
                    f"sl_{p.stop_loss_pct}pct_tp_{p.take_profit_pct}pct"
                    f"_max_{p.max_hold_days}d_ohlc"
                ),
            },
        )
    finally:
        conn.close()


def render_it_cold_stove_markdown(result: dict[str, Any]) -> str:
    v = result.get("validation") or {}
    ch = v.get("condition_hits") or {}
    pm = result.get("portfolio_metrics") or {}
    summary = result.get("summary") or {}
    approx = v.get("approx_notes") or {}
    build_meta = result.get("build_meta") or {}
    exit_counts = build_meta.get("exit_reason_counts") or {}

    lines = [
        "# 投信燒冷灶（簡化版）· 回測報告",
        "",
        f"- **topic**: `{result.get('research_topic', TOPIC_IT_COLD_STOVE)}`",
        f"- **universe**: {result.get('universe', 'tw100')}",
        f"- **窗口**: {result.get('date_start')} ～ {result.get('date_end')}",
        f"- **槽位**: {result.get('n_slots')} 槽",
        f"- **進場**: T+1 open · **出場**: {result.get('exit_assumption', '')}",
        f"- **fidelity**: {result.get('spec_fidelity', 'simplified')}（僅 E1 · E2 blocked）",
        "",
        "## Phase 0 · 資料驗證",
        "",
        f"- universe 檔數: {v.get('universe_size', '—')}",
        f"- 交易日: {v.get('trading_days', '—')}",
        f"- E1 投信買超>300: {ch.get('e1_it_buy', '—')}",
        f"- E2 持股濾網: **blocked**",
        f"- **全條件命中（=E1）**: {ch.get('full_screen', '—')}",
        f"- Phase0 pass (≥50): **{'是' if v.get('phase0_pass') else '否'}**",
        "",
        "### 簡化說明",
        "",
    ]
    for k, note in approx.items():
        lines.append(f"- {k}: {note}")
    if exit_counts:
        lines.extend(["", "### 出場原因分布", ""])
        for reason, n in sorted(exit_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {reason}: {n}")
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
