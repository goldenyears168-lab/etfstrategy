"""FinMind marketplace · 低檔回升且外資投信同步買超 · slot backtest."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_standard_config import load_backtest_standard_config
from flow_returns import return_pct, stock_close, stock_open, trading_dates_after
from analytics.bench import bench_return_entry_to_exit
from research.backtest.finpilot_local_backtest import (
    load_financial_history,
    load_fundamental_snapshot,
    load_price_panels,
    summarize_periods,
)
from research.backtest.finmind_screener_common import (
    TOPIC_LOW_REBOUND,
    LowReboundParams,
    LowReboundScreenMatrices,
    build_low_rebound_screen_matrices,
    count_condition_passes,
    load_dividend_long,
    load_institutional_long,
    load_technical_long,
    resolve_universe_stock_ids,
)
from research.backtest.slot_backtest_summary import (
    SlotBacktestConfig,
    build_summary_payload,
)
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_HOLD_DAYS = 20
DEFAULT_N_SLOTS = 5
DEFAULT_VARIANT = "low_rebound_h20_open"
DEFAULT_STOP_LOSS_PCT = 8.0
DEFAULT_TAKE_PROFIT_PCT = 15.0


@dataclass(frozen=True)
class LowReboundExitRule:
    hold_days: int = DEFAULT_HOLD_DAYS
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None

    @property
    def variant_id(self) -> str:
        if self.stop_loss_pct is not None and self.take_profit_pct is not None:
            return (
                f"h{self.hold_days}_sl{int(self.stop_loss_pct)}"
                f"_tp{int(self.take_profit_pct)}"
            )
        return f"h{self.hold_days}_close"


@dataclass(frozen=True)
class SignalCandidate:
    stock_id: str
    signal_date: str
    three_institution_net: float
    return_5d_pct: float


def screen_candidates_on_date(
    signal_date: str,
    stock_ids: list[str],
    matrices: LowReboundScreenMatrices,
) -> list[SignalCandidate]:
    e1 = matrices.foreign.loc[signal_date, stock_ids]
    e2 = matrices.investment_trust.loc[signal_date, stock_ids]
    e3 = matrices.dividend.loc[signal_date, stock_ids]
    e4 = matrices.roe.loc[signal_date, stock_ids]
    e5 = matrices.kd.loc[signal_date, stock_ids]
    full = e1 & e2 & e3 & e4 & e5
    passing = full[full].index.tolist()
    out: list[SignalCandidate] = []
    for sid in passing:
        tin = matrices.three_institution_net.at[signal_date, sid]
        ret5 = matrices.return_5d_pct.at[signal_date, sid]
        out.append(
            SignalCandidate(
                stock_id=str(sid),
                signal_date=signal_date,
                three_institution_net=float(tin or 0),
                return_5d_pct=float(ret5 or 0),
            )
        )
    out.sort(key=lambda c: (-c.three_institution_net, c.return_5d_pct))
    return out


def resolve_exit_for_period(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    stock_id: str,
    entry_date: str,
    entry_px: float,
    rule: LowReboundExitRule,
) -> tuple[str | None, float | None, str]:
    dates = trading_dates_after(conn, entry_date, count=rule.hold_days)
    if not dates:
        return None, None, "no_calendar"
    for i, d in enumerate(dates):
        exit_px: float | None = None
        if stock_id in close.columns and d in close.index:
            v = close.at[d, stock_id]
            if v is not None and float(v) > 0:
                exit_px = float(v)
        if exit_px is None:
            exit_px = stock_close(conn, stock_id, d)
        if exit_px is None or exit_px <= 0:
            continue
        ret = return_pct(entry_px, exit_px)
        if rule.stop_loss_pct is not None and ret <= -rule.stop_loss_pct:
            return d, exit_px, "stop_loss"
        if rule.take_profit_pct is not None and ret >= rule.take_profit_pct:
            return d, exit_px, "take_profit"
        if i == len(dates) - 1:
            return d, exit_px, "hold_expire"
    return None, None, "no_price"


def _exit_date_from_entry(
    conn: sqlite3.Connection,
    entry_date: str,
    hold_days: int,
) -> str | None:
    after = trading_dates_after(conn, entry_date, count=hold_days)
    if len(after) < hold_days:
        return None
    return after[hold_days - 1]


def build_periods(
    conn: sqlite3.Connection,
    *,
    universe: str,
    date_start: str,
    date_end: str,
    n_slots: int,
    hold_days: int,
    exit_rule: LowReboundExitRule | None = None,
    params: LowReboundParams,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    close, opn, _ = load_price_panels(conn)
    stock_ids = resolve_universe_stock_ids(conn, universe)
    price_ok = [s for s in stock_ids if s in close.columns]
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    inst = load_institutional_long(conn)
    tech = load_technical_long(conn)
    div = load_dividend_long(conn)
    fund = load_fundamental_snapshot(conn)
    fin_hist = load_financial_history(conn)

    inst = inst[inst["stock_id"].isin(stock_ids)]
    tech = tech[tech["stock_id"].isin(stock_ids)]
    div = div[div["stock_id"].isin(stock_ids)]

    matrices = build_low_rebound_screen_matrices(
        stock_ids=stock_ids,
        cal=cal,
        inst=inst,
        tech=tech,
        div=div,
        fund=fund,
        fin_hist=fin_hist,
        params=params,
    )

    rule = exit_rule or LowReboundExitRule(hold_days=hold_days)

    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    signal_days = 0

    for signal_date in cal:
        # expire
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

            exit_date, exit_px, exit_reason = resolve_exit_for_period(
                conn, close, cand.stock_id, entry_date, entry_px, rule
            )
            if exit_date is None or exit_px is None:
                continue

            period = {
                "stock_id": cand.stock_id,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "exit_reason": exit_reason,
                "three_institution_net": cand.three_institution_net,
                "return_5d_pct": cand.return_5d_pct,
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
        exit_px = p.get("exit_px")
        if exit_px is not None:
            exit_px = float(exit_px)
        else:
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


def run_low_rebound_backtest(
    *,
    db_path: str | Path | None = None,
    universe: str = "tw100",
    date_start: str = "2015-01-01",
    date_end: str | None = None,
    n_slots: int = DEFAULT_N_SLOTS,
    hold_days: int = DEFAULT_HOLD_DAYS,
    exit_rule: LowReboundExitRule | None = None,
    params: LowReboundParams | None = None,
    include_validation: bool = True,
) -> dict[str, Any]:
    p = params or LowReboundParams()
    rule = exit_rule or LowReboundExitRule(hold_days=hold_days)
    end = date_end or date.today().isoformat()
    from pathlib import Path

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
            hold_days=rule.hold_days,
            exit_rule=rule,
            params=p,
        )
        periods = enrich_period_returns(conn, periods_raw)
        close, _, _ = load_price_panels(conn)
        trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= end]

        std = load_backtest_standard_config()
        total_capital = std.comparison_notional_ntd
        portfolio = simulate_slot_portfolio(
            conn,
            close,
            trade_dates,
            periods,
            total_capital=total_capital,
            n_slots=n_slots,
            cost_model=std.cost_model if std.cost_model.get("enabled") else None,
        )

        period_summary = summarize_periods(periods)
        if periods:
            period_summary["mean_excess_pct"] = round(
                sum(p["excess_pct"] for p in periods) / len(periods), 4
            )
        else:
            period_summary["mean_excess_pct"] = None

        by_year: dict[str, dict[str, Any]] = {}
        for yr in sorted({p["signal_date"][:4] for p in periods}):
            sub = [p for p in periods if p["signal_date"].startswith(yr)]
            s = summarize_periods(sub)
            if sub:
                s["mean_excess_pct"] = round(
                    sum(x["excess_pct"] for x in sub) / len(sub), 4
                )
            s["n_signals"] = len(sub)
            by_year[yr] = s

        oos = [p for p in periods if p["signal_date"] >= "2024-01-01"]
        is_ = [p for p in periods if p["signal_date"] < "2024-01-01"]
        oos_summary = summarize_periods(oos)
        is_summary = summarize_periods(is_)
        if oos:
            oos_summary["mean_excess_pct"] = round(
                sum(p["excess_pct"] for p in oos) / len(oos), 4
            )
        if is_:
            is_summary["mean_excess_pct"] = round(
                sum(p["excess_pct"] for p in is_) / len(is_), 4
            )

        cfg = SlotBacktestConfig(
            date_start=date_start,
            date_end=end,
            n_slots=n_slots,
            hold_days=rule.hold_days,
            entry_price_mode="open",
            strategy_id=TOPIC_LOW_REBOUND,
            variant=rule.variant_id,
        )
        summary = {**period_summary, **portfolio}
        payload = build_summary_payload(
            track_id=TOPIC_LOW_REBOUND,
            config=cfg,
            summary=summary,
            source_module="finmind_low_rebound_backtest",
            extra={
                "research_topic": TOPIC_LOW_REBOUND,
                "spec_fidelity": "faithful",
                "universe": universe,
                "params": asdict(p),
                "build_meta": build_meta,
                "validation": validation,
                "by_year": by_year,
                "oos_from_2024": oos_summary,
                "is_before_2024": is_summary,
                "exit_assumption": _exit_assumption_label(rule),
            },
        )
        return payload
    finally:
        conn.close()


def _exit_assumption_label(rule: LowReboundExitRule) -> str:
    if rule.stop_loss_pct is not None and rule.take_profit_pct is not None:
        return (
            f"sl_{rule.stop_loss_pct}pct_tp_{rule.take_profit_pct}pct"
            f"_or_h{rule.hold_days}d_close"
        )
    return f"hold_{rule.hold_days}d_close_after_t_plus_1_open"


def run_low_rebound_sweep_cell(
    *,
    db_path: str | Path | None = None,
    universe: str = "tw100",
    date_start: str = "2015-01-01",
    date_end: str | None = None,
    n_slots: int = DEFAULT_N_SLOTS,
    exit_rule: LowReboundExitRule,
) -> dict[str, Any]:
    """Single sensitivity cell · skips Phase 0 validation."""
    result = run_low_rebound_backtest(
        db_path=db_path,
        universe=universe,
        date_start=date_start,
        date_end=date_end,
        n_slots=n_slots,
        exit_rule=exit_rule,
        include_validation=False,
    )
    summary = result.get("summary") or {}
    pm = result.get("portfolio_metrics") or {}
    oos = result.get("oos_from_2024") or {}
    return {
        "variant": exit_rule.variant_id,
        "universe": universe,
        "date_start": result.get("date_start"),
        "date_end": result.get("date_end"),
        "exit_assumption": result.get("exit_assumption"),
        "n_periods": summary.get("n_periods", 0),
        "win_rate_vs_bench_pct": summary.get("win_rate_vs_bench_pct"),
        "mean_return_pct": summary.get("mean_return_pct"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "total_return_pct": pm.get("total_return_pct", summary.get("total_return_pct")),
        "interval_excess_pct": pm.get("interval_excess_pct"),
        "sharpe_ratio": pm.get("sharpe_ratio"),
        "max_drawdown_pct": pm.get("max_drawdown_pct"),
        "oos_n_periods": oos.get("n_periods", 0),
        "oos_mean_excess_pct": oos.get("mean_excess_pct"),
        "oos_win_rate_vs_bench_pct": oos.get("win_rate_vs_bench_pct"),
        "g2_oos_pass": (
            (oos.get("n_periods") or 0) >= 30
            and (oos.get("mean_excess_pct") or -999) > 0
        ),
    }


PHASE2_EXIT_GRID: tuple[LowReboundExitRule, ...] = (
    LowReboundExitRule(hold_days=10),
    LowReboundExitRule(hold_days=20),
    LowReboundExitRule(hold_days=40),
    LowReboundExitRule(
        hold_days=20,
        stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
        take_profit_pct=DEFAULT_TAKE_PROFIT_PCT,
    ),
)


def render_sweep_markdown(
    cells: list[dict[str, Any]],
    *,
    baseline_variant: str = "h20_close",
) -> str:
    lines = [
        "# 低檔回升 · Phase 2 sensitivity sweep",
        "",
        "| variant | n | 均超額% | 組合總報酬% | Sharpe | OOS n | OOS 均超額% | G2 |",
        "|---------|---|---------|-------------|--------|-------|-------------|-----|",
    ]
    best_oos = max(cells, key=lambda c: c.get("oos_mean_excess_pct") or -999)
    for c in cells:
        g2 = "✓" if c.get("g2_oos_pass") else "—"
        mark = "**" if c.get("variant") == best_oos.get("variant") else ""
        end = "**" if c.get("variant") == best_oos.get("variant") else ""
        lines.append(
            f"| {mark}{c.get('variant')}{end} | {c.get('n_periods', 0)} | "
            f"{c.get('mean_excess_pct', '—')} | {c.get('total_return_pct', '—')} | "
            f"{c.get('sharpe_ratio', '—')} | {c.get('oos_n_periods', 0)} | "
            f"{c.get('oos_mean_excess_pct', '—')} | {g2} |"
        )
    lines.extend(
        [
            "",
            f"- **最佳 OOS 均超額**: `{best_oos.get('variant')}` · "
            f"{best_oos.get('oos_mean_excess_pct')}% (n={best_oos.get('oos_n_periods')})",
            f"- **G2 門檻**: OOS n≥30 且 mean excess>0",
            "",
        ]
    )
    return "\n".join(lines)


def render_low_rebound_markdown(result: dict[str, Any]) -> str:
    v = result.get("validation") or {}
    ch = v.get("condition_hits") or {}
    pm = result.get("portfolio_metrics") or {}
    summary = result.get("summary") or {}
    lines = [
        "# 低檔回升且外資投信同步買超 · 回測報告",
        "",
        f"- **topic**: `{result.get('research_topic', TOPIC_LOW_REBOUND)}`",
        f"- **universe**: {result.get('universe', 'tw100')}",
        f"- **窗口**: {result.get('date_start')} ～ {result.get('date_end')}",
        f"- **槽位 / 持有**: {result.get('n_slots')} 槽 · {result.get('hold_days')} 交易日",
        f"- **進場**: T+1 open · **出場**: {result.get('exit_assumption', '')}",
        f"- **fidelity**: {result.get('spec_fidelity', 'faithful')}（出場為研究假設）",
        "",
        "## Phase 0 · 資料驗證",
        "",
        f"- universe 檔數: {v.get('universe_size', '—')}",
        f"- 交易日: {v.get('trading_days', '—')}",
        f"- E1 外資買超命中: {ch.get('e1_foreign_buy', '—')}",
        f"- E2 投信買超命中: {ch.get('e2_it_buy', '—')}",
        f"- E3 股利 5 年命中: {ch.get('e3_dividend_5y', '—')}",
        f"- E4 ROE 命中: {ch.get('e4_roe', '—')}",
        f"- E5 KD 穿越命中: {ch.get('e5_kd_cross', '—')}",
        f"- **全條件命中（stock-day）**: {ch.get('full_screen', '—')}",
        f"- Phase0 pass (≥50): **{'是' if v.get('phase0_pass') else '否'}**",
        "",
        "## 組合績效",
        "",
        f"| 指標 | 值 |",
        f"|------|-----|",
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
