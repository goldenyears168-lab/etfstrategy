"""境內基金月前十大持股變化跟單回測（安聯台灣科技等）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .copytrade_backtest import (
    ADD_ACTIONS,
    INITIATION_ACTION,
    CopytradeDayResult,
    CopytradeSignal,
    _strategy_label,
    compute_excess_significance,
    compute_signal_day,
    compute_win_rate_stats,
    group_signals_by_date,
)
from flow_returns import trading_dates_after
from holdings_research import TW_SPOT_CODE
from rank_stats import max_drawdown_pct
from stock_db import (
    load_mutual_fund_holdings,
    list_mutual_fund_snapshot_dates,
    load_stock_beta_map,
)
from sync_mutual_fund_holdings import ALLIANZ_TW_TECH, DISCLOSURE_MONTHLY, FundProfile

ACTION_FILTER_ALL_ADD = "all_add"
ACTION_FILTER_INITIATION = "initiation"
ACTION_FILTER_TOP3_INITIATION = "top3_initiation"

ACTION_FILTERS: dict[str, frozenset[str]] = {
    ACTION_FILTER_ALL_ADD: ADD_ACTIONS,
    ACTION_FILTER_INITIATION: frozenset({INITIATION_ACTION}),
    ACTION_FILTER_TOP3_INITIATION: frozenset({INITIATION_ACTION}),
}

DISCLOSURE_METHODS: dict[str, str] = {
    "m1_d25": "資料月次月 25 日後首個交易日（偏早）",
    "lag28": "資料月末 +28 曆日後首個交易日（預設）",
    "lag33": "資料月末 +33 曆日後首個交易日（偏保守）",
    "m2_d3": "資料月後第 2 個月 3 日後首個交易日（最保守）",
}


@dataclass(frozen=True)
class MutualFundChangeRow:
    stock_id: str
    stock_name: str | None
    action: str
    amount_prev: float
    amount_curr: float
    amount_delta: float
    weight_pct_prev: float | None
    weight_pct_curr: float | None
    weight_delta: float | None
    rank_no_curr: int | None
    snapshot_curr: str
    snapshot_prev: str


@dataclass
class MutualFundCopytradeRunResult:
    fund_code: str
    fund_name: str
    strategy_id: str
    strategy_label: str
    action_filter: str
    disclosure_method: str
    capital_ntd: float
    entry_lag_days: int
    hold_trading_days: int
    cost_bps: float
    window_start: str | None
    window_end: str | None
    signal_days: list[CopytradeDayResult]
    n_signal_days: int
    n_complete_days: int
    n_legs: int
    total_deployed_ntd: float
    total_pnl_ntd: float
    total_return_pct: float | None
    avg_day_return_pct: float | None
    win_rate_gross_pct: float | None
    win_rate_alpha_pct: float | None
    max_drawdown_pct: float | None
    total_alpha_ntd: float
    total_capm_alpha_ntd: float
    mean_excess_pct: float | None
    p_value_ttest: float | None
    p_value_wilcoxon: float | None
    t_stat: float | None


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return year, month


def estimate_disclosure_date(
    conn: sqlite3.Connection,
    snapshot_date: str,
    method: str,
) -> str | None:
    """Estimate publication date (公告日) from data-month snapshot_date."""
    if method not in DISCLOSURE_METHODS:
        raise ValueError(f"unknown disclosure method: {method!r}")
    d = date.fromisoformat(snapshot_date)
    if method == "m1_d25":
        y, m = _add_months(d.year, d.month, 1)
        anchor = date(y, m, 25).isoformat()
    elif method == "lag28":
        anchor = (d + timedelta(days=28)).isoformat()
    elif method == "lag33":
        anchor = (d + timedelta(days=33)).isoformat()
    elif method == "m2_d3":
        y, m = _add_months(d.year, d.month, 2)
        anchor = date(y, m, 3).isoformat()
    else:
        raise ValueError(method)
    dates = trading_dates_after(conn, anchor, count=1, inclusive_anchor=True)
    return dates[0] if dates else None


def compute_mutual_fund_holdings_changes(
    conn: sqlite3.Connection,
    fund_code: str,
    curr_date: str,
    prev_date: str,
    *,
    disclosure_type: str = DISCLOSURE_MONTHLY,
) -> list[MutualFundChangeRow]:
    curr_rows = load_mutual_fund_holdings(
        conn, fund_code, curr_date, disclosure_type=disclosure_type
    )
    prev_rows = load_mutual_fund_holdings(
        conn, fund_code, prev_date, disclosure_type=disclosure_type
    )
    curr = {str(r["stock_id"]): r for r in curr_rows}
    prev = {str(r["stock_id"]): r for r in prev_rows}
    out: list[MutualFundChangeRow] = []
    for stock_id in sorted(set(curr) | set(prev)):
        c = curr.get(stock_id)
        p = prev.get(stock_id)
        amt_c = float(c["amount"] or 0) if c else 0.0
        amt_p = float(p["amount"] or 0) if p else 0.0
        w_c = float(c["weight_pct"]) if c and c["weight_pct"] is not None else None
        w_p = float(p["weight_pct"]) if p and p["weight_pct"] is not None else None
        if p is None:
            action = "新进"
        elif c is None:
            action = "出清"
        elif amt_c > amt_p:
            action = "加码"
        elif amt_c < amt_p:
            action = "减码"
        else:
            action = "不变"
        rank_no = int(c["rank_no"]) if c and c["rank_no"] is not None else None
        out.append(
            MutualFundChangeRow(
                stock_id=stock_id,
                stock_name=(c or p)["stock_name"],
                action=action,
                amount_prev=amt_p,
                amount_curr=amt_c,
                amount_delta=amt_c - amt_p,
                weight_pct_prev=w_p,
                weight_pct_curr=w_c,
                weight_delta=(w_c or 0.0) - (w_p or 0.0)
                if w_c is not None and w_p is not None
                else None,
                rank_no_curr=rank_no,
                snapshot_curr=curr_date,
                snapshot_prev=prev_date,
            )
        )
    return out


def _passes_action_filter(row: MutualFundChangeRow, action_filter: str) -> bool:
    allowed = ACTION_FILTERS.get(action_filter)
    if allowed is None:
        raise ValueError(f"unknown action_filter: {action_filter!r}")
    if row.action not in allowed:
        return False
    if row.action == INITIATION_ACTION and action_filter == ACTION_FILTER_TOP3_INITIATION:
        if row.rank_no_curr is None or row.rank_no_curr > 3:
            return False
    if row.action == "加码" and row.amount_delta <= 0:
        return False
    return True


def iter_mutual_fund_copytrade_signals(
    conn: sqlite3.Connection,
    profile: FundProfile,
    *,
    disclosure_method: str = "lag28",
    action_filter: str = ACTION_FILTER_ALL_ADD,
    disclosure_type: str = DISCLOSURE_MONTHLY,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[CopytradeSignal]:
    dates = sorted(
        list_mutual_fund_snapshot_dates(
            conn, profile.fund_code, disclosure_type=disclosure_type
        )
    )
    out: list[CopytradeSignal] = []
    for i in range(1, len(dates)):
        prev_date, curr_date = dates[i - 1], dates[i]
        pub_date = estimate_disclosure_date(conn, curr_date, disclosure_method)
        if pub_date is None:
            continue
        if window_start and pub_date < window_start:
            continue
        if window_end and pub_date > window_end:
            continue
        for row in compute_mutual_fund_holdings_changes(
            conn,
            profile.fund_code,
            curr_date,
            prev_date,
            disclosure_type=disclosure_type,
        ):
            if not _passes_action_filter(row, action_filter):
                continue
            out.append(
                CopytradeSignal(
                    signal_date=pub_date,
                    stock_id=row.stock_id,
                    stock_name=row.stock_name or "",
                    action=row.action,
                    share_delta=row.amount_delta,
                    weight_delta=row.weight_delta,
                    weight_pct_curr=row.weight_pct_curr,
                )
            )
    return out


def run_mutual_fund_copytrade_backtest(
    conn: sqlite3.Connection,
    profile: FundProfile,
    *,
    disclosure_method: str = "lag28",
    action_filter: str = ACTION_FILTER_ALL_ADD,
    entry_lag_days: int = 0,
    hold_trading_days: int = 20,
    entry_price_mode: str = "open",
    capital_ntd: float = 100_000.0,
    cost_bps: float = 20.0,
    window_start: str | None = None,
    window_end: str | None = None,
) -> MutualFundCopytradeRunResult:
    signals = iter_mutual_fund_copytrade_signals(
        conn,
        profile,
        disclosure_method=disclosure_method,
        action_filter=action_filter,
        window_start=window_start,
        window_end=window_end,
    )
    grouped = group_signals_by_date(signals)
    beta_map, _ = load_stock_beta_map(conn)
    strategy_id = f"L{entry_lag_days + 1}H{hold_trading_days}"
    if action_filter != ACTION_FILTER_ALL_ADD:
        strategy_id = f"{strategy_id}-{action_filter}"
    strategy_label = _strategy_label(
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        entry_price_mode=entry_price_mode,
    )
    day_results: list[CopytradeDayResult] = []
    for signal_date in sorted(grouped):
        day_results.append(
            compute_signal_day(
                conn,
                signal_date,
                grouped[signal_date],
                capital_ntd=capital_ntd,
                entry_lag_days=entry_lag_days,
                hold_trading_days=hold_trading_days,
                cost_bps=cost_bps,
                entry_price_mode=entry_price_mode,
                beta_map=beta_map,
            )
        )
    complete = [d for d in day_results if d.status == "complete"]
    total_deployed = sum(d.deployed_ntd for d in complete)
    total_pnl = sum(d.pnl_ntd for d in complete)
    total_ret = total_pnl / total_deployed * 100.0 if total_deployed > 0 else None
    day_returns = [d.return_pct for d in complete]
    avg_day = sum(day_returns) / len(day_returns) if day_returns else None
    wr = compute_win_rate_stats(day_results)
    mdd = max_drawdown_pct(day_returns)
    sig = compute_excess_significance(day_results)
    w_start = window_start or (min(grouped) if grouped else None)
    w_end = window_end or (max(grouped) if grouped else None)
    return MutualFundCopytradeRunResult(
        fund_code=profile.fund_code,
        fund_name=profile.fund_name,
        strategy_id=strategy_id,
        strategy_label=strategy_label,
        action_filter=action_filter,
        disclosure_method=disclosure_method,
        capital_ntd=capital_ntd,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        signal_days=day_results,
        n_signal_days=len(grouped),
        n_complete_days=len(complete),
        n_legs=sum(d.n_legs for d in complete),
        total_deployed_ntd=total_deployed,
        total_pnl_ntd=total_pnl,
        total_return_pct=total_ret,
        avg_day_return_pct=avg_day,
        win_rate_gross_pct=wr["win_rate_gross_pct"],
        win_rate_alpha_pct=wr["win_rate_alpha_pct"],
        max_drawdown_pct=mdd,
        total_alpha_ntd=sum(d.alpha_ntd for d in complete),
        total_capm_alpha_ntd=sum(d.capm_alpha_ntd for d in complete),
        mean_excess_pct=sig["mean_excess_pct"],
        p_value_ttest=sig["p_value_ttest"],
        p_value_wilcoxon=sig["p_value_wilcoxon"],
        t_stat=sig["t_stat"],
    )


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}" if value is not None else "—"


def _fmt_pnl(value: float) -> str:
    return f"{value:+,.0f}"


def _fmt_p(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


def format_mutual_fund_copytrade_markdown(
    primary: MutualFundCopytradeRunResult,
    *,
    filter_rows: Iterable[MutualFundCopytradeRunResult] | None = None,
    disclosure_rows: Iterable[MutualFundCopytradeRunResult] | None = None,
    horizon_rows: Iterable[MutualFundCopytradeRunResult] | None = None,
) -> str:
    lines = [
        f"# {primary.fund_name}（{primary.fund_code}）跟單回測",
        "",
        "> 境內基金月前十大 · 公告日代理進場 · 資料來源 SITCA",
        "",
        "## 主策略",
        "",
        f"| 項目 | 設定 |",
        f"|------|------|",
        f"| 基金 | {primary.fund_name} `{primary.fund_code}` |",
        f"| 策略 | **{primary.strategy_id}** · {primary.strategy_label} |",
        f"| 訊號 filter | `{primary.action_filter}` |",
        f"| 公告日代理 | `{primary.disclosure_method}` · {DISCLOSURE_METHODS[primary.disclosure_method]} |",
        f"| 每日配置 | {primary.capital_ntd:,.0f} NTD（當日多 leg 等權） |",
        f"| 成本 | {primary.cost_bps:.0f} bps 來回 |",
        f"| 基準 | {TW_SPOT_CODE} |",
        f"| 公告窗口 | {primary.window_start or '—'} .. {primary.window_end or '—'} |",
        "",
        "## 主策略績效",
        "",
        "| 指標 | 數值 |",
        "|------|------|",
        f"| 公告日數 | {primary.n_signal_days}（可交易 {primary.n_complete_days}） |",
        f"| 成交 legs | {primary.n_legs} |",
        f"| 累計損益 | {_fmt_pnl(primary.total_pnl_ntd)} NTD |",
        f"| 累計 α（vs 台指） | {_fmt_pnl(primary.total_alpha_ntd)} NTD |",
        f"| CAPM α | {_fmt_pnl(primary.total_capm_alpha_ntd)} NTD |",
        f"| 日均報酬 | {_fmt_pct(primary.avg_day_return_pct)}% |",
        f"| 勝率（毛利） | {_fmt_pct(primary.win_rate_gross_pct)}% |",
        f"| 勝率（α>0） | {_fmt_pct(primary.win_rate_alpha_pct)}% |",
        f"| MaxDD | {_fmt_pct(primary.max_drawdown_pct)}% |",
        f"| 日均超額 | {_fmt_pct(primary.mean_excess_pct)}% |",
        f"| t-test p | {_fmt_p(primary.p_value_ttest)} |",
        f"| Wilcoxon p | {_fmt_p(primary.p_value_wilcoxon)} |",
        "",
        "### 實務提醒",
        "",
        "- 公告日為**代理估算**（非 SITCA 實際上架日）；實盤應以平台「資料月份」更新當天為 T。",
        "- 月前十大僅 10 檔，訊號頻率低；不適合高頻跟單。",
        "- `top3_initiation`：僅跟**新進前十大且當月排名 ≤3** 的標的。",
        "",
    ]
    if filter_rows:
        lines.extend(
            [
                "## 訊號 filter 對照（同公告日 / 同 H）",
                "",
                "| filter | 說明 | 可交易日 | legs | 累計損益 | 累計 α | 勝率 |",
                "|--------|------|----------|------|----------|--------|------|",
            ]
        )
        labels = {
            ACTION_FILTER_ALL_ADD: "新進 + 加碼",
            ACTION_FILTER_INITIATION: "僅新進前十大",
            ACTION_FILTER_TOP3_INITIATION: "僅新進前三大",
        }
        for row in filter_rows:
            lines.append(
                f"| `{row.action_filter}` | {labels.get(row.action_filter, row.action_filter)} | "
                f"{row.n_complete_days} | {row.n_legs} | {_fmt_pnl(row.total_pnl_ntd)} | "
                f"{_fmt_pnl(row.total_alpha_ntd)} | {_fmt_pct(row.win_rate_gross_pct)}% |"
            )
        lines.append("")
    if disclosure_rows:
        lines.extend(
            [
                "## 公告日敏感性",
                "",
                "| 方法 | 說明 | 可交易日 | 累計損益 | 累計 α | 勝率 |",
                "|------|------|----------|----------|--------|------|",
            ]
        )
        for row in disclosure_rows:
            lines.append(
                f"| `{row.disclosure_method}` | {DISCLOSURE_METHODS[row.disclosure_method]} | "
                f"{row.n_complete_days} | {_fmt_pnl(row.total_pnl_ntd)} | "
                f"{_fmt_pnl(row.total_alpha_ntd)} | {_fmt_pct(row.win_rate_gross_pct)}% |"
            )
        lines.append("")
    if horizon_rows:
        lines.extend(
            [
                "## 持有天數掃描",
                "",
                "| H | 可交易日 | legs | 累計損益 | 累計 α | 日均報酬 | 勝率 |",
                "|---|----------|------|----------|--------|----------|------|",
            ]
        )
        for row in horizon_rows:
            lines.append(
                f"| H{row.hold_trading_days} | {row.n_complete_days} | {row.n_legs} | "
                f"{_fmt_pnl(row.total_pnl_ntd)} | {_fmt_pnl(row.total_alpha_ntd)} | "
                f"{_fmt_pct(row.avg_day_return_pct)}% | {_fmt_pct(row.win_rate_gross_pct)}% |"
            )
        lines.append("")
    skipped = [d for d in primary.signal_days if d.status != "complete"]
    if skipped:
        lines.extend(["## 跳過公告日", ""])
        for day in skipped:
            lines.append(f"- {day.signal_date}: `{day.status}`")
        lines.append("")
    return "\n".join(lines)


def write_mutual_fund_copytrade_report(
    primary: MutualFundCopytradeRunResult,
    *,
    filter_rows: Iterable[MutualFundCopytradeRunResult] | None = None,
    disclosure_rows: Iterable[MutualFundCopytradeRunResult] | None = None,
    horizon_rows: Iterable[MutualFundCopytradeRunResult] | None = None,
    reports_dir: Path | None = None,
) -> Path:
    root = reports_dir or Path(__file__).resolve().parent.parent / "reports"
    root.mkdir(parents=True, exist_ok=True)
    suffix = f"_{primary.fund_code.lower()}_{primary.action_filter}_l{primary.entry_lag_days + 1}h{primary.hold_trading_days}"
    out = root / f"{date.today().strftime('%Y%m%d')}{suffix}_copytrade.md"
    body = format_mutual_fund_copytrade_markdown(
        primary,
        filter_rows=filter_rows,
        disclosure_rows=disclosure_rows,
        horizon_rows=horizon_rows,
    )
    out.write_text(body, encoding="utf-8")
    return out
