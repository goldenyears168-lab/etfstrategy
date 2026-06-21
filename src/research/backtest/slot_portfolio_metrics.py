"""Portfolio-level return / volatility for equal-capital slot strategies."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import pandas as pd

from flow_returns import stock_close


@dataclass
class _OpenPos:
    stock_id: str
    entry_date: str
    exit_date: str
    entry_px: float
    allocation: float


def _resolve_entry_px(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    stock_id: str,
    entry_date: str,
    period: dict,
) -> float | None:
    ep = period.get("entry_px")
    if ep is not None and float(ep) > 0:
        return float(ep)
    if stock_id in close.columns and entry_date in close.index:
        px = close.at[entry_date, stock_id]
        if px is not None and float(px) > 0:
            return float(px)
    px = stock_close(conn, stock_id, entry_date)
    return float(px) if px is not None and px > 0 else None


def simulate_slot_portfolio(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    trade_dates: list[str],
    periods: list[dict],
    *,
    total_capital: float = 50_000.0,
    n_slots: int,
) -> dict:
    """
    Equal-capital slot book: deploy total_capital / n_slots per position.
    Daily mark-to-market; enter/exit at close.
    """
    if not trade_dates:
        return _empty_metrics(total_capital)

    slot_cap = total_capital / n_slots
    cash = float(total_capital)
    open_pos: list[_OpenPos] = []

    entries_by_date: dict[str, list[dict]] = {}
    for p in periods:
        ed = str(p.get("entry_date") or "")
        ex = str(p.get("exit_date") or "")
        if not ed or not ex or ed > ex:
            continue
        entries_by_date.setdefault(ed, []).append(p)

    daily_returns: list[float] = []
    equities: list[float] = []
    util_samples: list[float] = []
    prev_equity = total_capital
    deployed_sum = 0.0
    n_entries = 0
    hold_days_sum = 0

    date_idx = {d: i for i, d in enumerate(trade_dates)}

    for d in trade_dates:
        for pos in list(open_pos):
            if pos.exit_date != d:
                continue
            sid = pos.stock_id
            exit_px = None
            if sid in close.columns and d in close.index:
                v = close.at[d, sid]
                if v is not None and float(v) > 0:
                    exit_px = float(v)
            if exit_px is None:
                exit_px = stock_close(conn, sid, d)
            if exit_px is None or exit_px <= 0:
                cash += pos.allocation
            else:
                cash += pos.allocation * (exit_px / pos.entry_px)
            open_pos.remove(pos)

        held_ids = {p.stock_id for p in open_pos}
        for p in entries_by_date.get(d, []):
            if len(open_pos) >= n_slots:
                break
            sid = str(p["stock_id"])
            if sid in held_ids:
                continue
            entry_px = _resolve_entry_px(conn, close, sid, d, p)
            if entry_px is None:
                continue
            alloc = min(slot_cap, cash)
            if alloc <= 0:
                break
            cash -= alloc
            ex = str(p["exit_date"])
            open_pos.append(
                _OpenPos(
                    stock_id=sid,
                    entry_date=d,
                    exit_date=ex,
                    entry_px=entry_px,
                    allocation=alloc,
                )
            )
            held_ids.add(sid)
            deployed_sum += alloc
            n_entries += 1
            ei = date_idx.get(d)
            xi = date_idx.get(ex)
            if ei is not None and xi is not None:
                hold_days_sum += max(0, xi - ei)

        pos_val = 0.0
        for pos in open_pos:
            if d < pos.entry_date:
                continue
            if pos.stock_id in close.columns and d in close.index:
                px = close.at[d, pos.stock_id]
                if px is not None and float(px) > 0:
                    pos_val += pos.allocation * (float(px) / pos.entry_px)

        equity = cash + pos_val
        equities.append(equity)
        deployed = sum(p.allocation for p in open_pos)
        util_samples.append(deployed / total_capital * 100.0 if total_capital > 0 else 0.0)
        if prev_equity > 0:
            daily_returns.append((equity / prev_equity - 1.0) * 100.0)
        prev_equity = equity

    last = trade_dates[-1]
    for pos in list(open_pos):
        sid = pos.stock_id
        exit_px = None
        if sid in close.columns and last in close.index:
            v = close.at[last, sid]
            if v is not None and float(v) > 0:
                exit_px = float(v)
        if exit_px is None:
            exit_px = stock_close(conn, sid, last)
        if exit_px is not None and exit_px > 0:
            cash += pos.allocation * (exit_px / pos.entry_px)
        else:
            cash += pos.allocation
        open_pos.remove(pos)

    final_equity = cash
    return _metrics_from_series(
        total_capital=total_capital,
        final_equity=final_equity,
        daily_returns=daily_returns,
        equities=equities,
        util_samples=util_samples,
        n_slots=n_slots,
        n_entries=n_entries,
        deployed_sum=deployed_sum,
        hold_days_sum=hold_days_sum,
        calendar_days=len(trade_dates),
    )


def _empty_metrics(total_capital: float) -> dict:
    return {
        "total_capital_ntd": total_capital,
        "final_equity_ntd": total_capital,
        "total_return_pct": 0.0,
        "cagr_pct": None,
        "mean_daily_return_pct": None,
        "std_daily_return_pct": None,
        "ann_vol_pct": None,
        "sharpe_ratio": None,
        "avg_utilization_pct": 0.0,
        "turnover_per_year": 0.0,
        "n_trades": 0,
        "avg_hold_days": None,
        "n_periods": 0,
    }


def _metrics_from_series(
    *,
    total_capital: float,
    final_equity: float,
    daily_returns: list[float],
    equities: list[float],
    util_samples: list[float],
    n_slots: int,
    n_entries: int,
    deployed_sum: float,
    hold_days_sum: int,
    calendar_days: int,
) -> dict:
    total_return_pct = (final_equity / total_capital - 1.0) * 100.0 if total_capital > 0 else 0.0
    years = calendar_days / 252.0 if calendar_days > 0 else 0.0
    if years > 0 and final_equity > 0 and total_capital > 0:
        cagr_pct = ((final_equity / total_capital) ** (1.0 / years) - 1.0) * 100.0
    else:
        cagr_pct = None

    n_dr = len(daily_returns)
    if n_dr > 1:
        mean_dr = sum(daily_returns) / n_dr
        var = sum((x - mean_dr) ** 2 for x in daily_returns) / (n_dr - 1)
        std_dr = math.sqrt(var)
        ann_vol = std_dr * math.sqrt(252.0)
        sharpe = (mean_dr / std_dr * math.sqrt(252.0)) if std_dr > 0 else None
    elif n_dr == 1:
        mean_dr = daily_returns[0]
        std_dr = 0.0
        ann_vol = 0.0
        sharpe = None
    else:
        mean_dr = None
        std_dr = None
        ann_vol = None
        sharpe = None

    avg_util = sum(util_samples) / len(util_samples) if util_samples else 0.0

    # Capital turnover: (buy + sell notional) / average equity / years
    avg_equity = sum(equities) / len(equities) if equities else total_capital
    turnover_per_year = (
        (2.0 * deployed_sum) / avg_equity / years if years > 0 and avg_equity > 0 else 0.0
    )

    avg_hold = round(hold_days_sum / n_entries, 1) if n_entries > 0 else None
    ann_mean_return_pct = round(mean_dr * 252.0, 4) if mean_dr is not None else None

    return {
        "total_capital_ntd": round(total_capital, 2),
        "final_equity_ntd": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 4),
        "cagr_pct": round(cagr_pct, 4) if cagr_pct is not None else None,
        "mean_daily_return_pct": round(mean_dr, 6) if mean_dr is not None else None,
        "ann_mean_return_pct": ann_mean_return_pct,
        "std_daily_return_pct": round(std_dr, 6) if std_dr is not None else None,
        "ann_vol_pct": round(ann_vol, 4) if ann_vol is not None else None,
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "avg_utilization_pct": round(avg_util, 2),
        "turnover_per_year": round(turnover_per_year, 4),
        "n_trades": n_entries,
        "avg_hold_days": avg_hold,
        "n_periods": n_entries,
    }


def render_portfolio_breadth_markdown(
    *,
    year_label: str,
    total_capital: float,
    zones: tuple[str, ...],
    zone_zh: dict[str, str],
    metrics_by_strategy: dict[str, dict[str, dict]],
    strategy_labels: dict[str, str],
) -> str:
    """metrics_by_strategy[strategy_key][zone_or_ALL] -> portfolio metrics dict."""
    lines = [
        f"### {year_label} · 組合層級（本金 {total_capital:,.0f} NTD · 日內 mark-to-market）",
        "",
        "每槽部署 = 本金 / 槽數（VCP 5槽=10k/槽 · RRG 3槽≈16.7k/槽）。",
        "區間獨立模擬：僅該 zone 日開新倉。",
        "",
        "| zone | 策略 | 總報酬% | CAGR% | 日報酬均值% | 日報酬標準差% | 年化波動% | Sharpe | 週轉/年 | 資金利用率% | n |",
        "|------|------|--------|-------|------------|--------------|----------|--------|---------|------------|---|",
    ]
    for zone in zones:
        zlabel = zone_zh.get(zone, zone)
        for sk, label in strategy_labels.items():
            m = metrics_by_strategy.get(sk, {}).get(zone) or {}
            lines.append(
                f"| {zlabel} | {label} | "
                f"{m.get('total_return_pct', '—')} | {m.get('cagr_pct', '—')} | "
                f"{m.get('mean_daily_return_pct', '—')} | {m.get('std_daily_return_pct', '—')} | "
                f"{m.get('ann_vol_pct', '—')} | {m.get('sharpe_ratio', '—')} | "
                f"{m.get('turnover_per_year', '—')} | {m.get('avg_utilization_pct', '—')} | "
                f"{m.get('n_trades', 0)} |"
            )
    lines.append("")
    return "\n".join(lines)


def render_portfolio_zone_summary(
    *,
    year_label: str,
    total_capital: float,
    zones: tuple[str, ...],
    zone_zh: dict[str, str],
    metrics_by_strategy: dict[str, dict[str, dict]],
    strategy_labels: dict[str, str],
) -> str:
    """Side-by-side: 期望值（年化算術均值）、波動、Sharpe — 同本金組合層級。"""
    lines = [
        f"#### {year_label} · 同本金風險報酬摘要（組合日報酬 · 非單筆勝率）",
        "",
        f"本金 {total_capital:,.0f} NTD · 週轉 = 年化換手倍數（買賣總額/平均權益）",
        "",
        "| zone | Pivot Gate 總報酬 | 年化期望% | 年化波動% | Sharpe | 週轉/年 | "
        "Coil Close 總報酬 | 年化期望% | 年化波動% | Sharpe | 週轉/年 | "
        "RRG 總報酬 | 年化期望% | 年化波動% | Sharpe | 週轉/年 |",
        "|------|----------------|----------|----------|--------|---------|"
        "----------------|----------|----------|--------|---------|"
        "-------------|----------|----------|--------|---------|",
    ]
    keys = list(strategy_labels.keys())
    for zone in zones:
        zlabel = zone_zh.get(zone, zone)
        cols: list[str] = []
        for sk in keys:
            m = metrics_by_strategy.get(sk, {}).get(zone) or {}
            cols.append(
                f"{m.get('total_return_pct', '—')}% | {m.get('ann_mean_return_pct', '—')} | "
                f"{m.get('ann_vol_pct', '—')} | {m.get('sharpe_ratio', '—')} | "
                f"{m.get('turnover_per_year', '—')}"
            )
        lines.append(f"| {zlabel} | " + " | ".join(cols) + " |")
    lines.append("")
    return "\n".join(lines)


def _fmt_metric(val: object) -> str:
    if val is None:
        return "—"
    return str(val)


def render_zone_centric_portfolio_markdown(
    *,
    portfolio_by_year: dict[str, dict[str, dict[str, dict]]],
    zone_day_counts_by_year: dict[str, dict[str, int]],
    years: list[str],
    total_capital: float,
    strategy_keys: tuple[str, ...],
    strategy_labels: dict[str, str],
    zone_zh: dict[str, str],
    zones: tuple[str, ...] = ("oversold", "weak", "neutral", "strong", "overbought"),
) -> str:
    """
    Zone-first layout: each 200MA zone · independent simulation · 50k portfolio.
    portfolio_by_year[year][strategy_key][zone] -> metrics
    """
    from market_breadth_ma import BREADTH_ZONE_DISPLAY

    lines = [
        "## 依 Breadth zone 獨立測試（組合層級 · 同本金）",
        "",
        "每個 zone **單獨跑一條策略軌**：僅在該 zone 的交易日允許開新倉，",
        "持倉照常持有至出場；本金 **50,000 NTD**，日內 mark-to-market。",
        "（與「全樣本進場再分桶」不同，這裡是 **zone 子策略** 獨立模擬。）",
        "",
    ]
    for zone in zones:
        zlabel = zone_zh.get(zone, zone)
        display = BREADTH_ZONE_DISPLAY.get(zone, zone)
        lines.extend([f"### {zlabel} · `{zone}`", "", f"定義：{display}", "", "| 年份 | 市場日數 | 策略 | 總報酬% | 年化期望% | 年化波動% | Sharpe | 週轉/年 | n |", "|------|---------|------|--------|----------|----------|--------|---------|---|"])
        best_sharpe: tuple[str, str, float] = ("", "", -1e9)
        best_ret: tuple[str, str, float] = ("", "", -1e9)
        for year in years:
            days = zone_day_counts_by_year.get(year, {}).get(zone, 0)
            for sk in strategy_keys:
                m = portfolio_by_year.get(year, {}).get(sk, {}).get(zone) or {}
                label = strategy_labels.get(sk, sk)
                sharpe = m.get("sharpe_ratio")
                ret = m.get("total_return_pct")
                if sharpe is not None and float(sharpe) > best_sharpe[2]:
                    best_sharpe = (year, label, float(sharpe))
                if ret is not None and float(ret) > best_ret[2]:
                    best_ret = (year, label, float(ret))
                lines.append(
                    f"| {year} | {days} | {label} | "
                    f"{_fmt_metric(m.get('total_return_pct'))} | "
                    f"{_fmt_metric(m.get('ann_mean_return_pct'))} | "
                    f"{_fmt_metric(m.get('ann_vol_pct'))} | "
                    f"{_fmt_metric(m.get('sharpe_ratio'))} | "
                    f"{_fmt_metric(m.get('turnover_per_year'))} | "
                    f"{m.get('n_trades', 0)} |"
                )
        if best_sharpe[0]:
            lines.extend(
                [
                    "",
                    f"- **最高 Sharpe**：{best_sharpe[1]}（{best_sharpe[0]} · {best_sharpe[2]:.2f}）",
                    f"- **最高總報酬**：{best_ret[1]}（{best_ret[0]} · {best_ret[2]:.1f}%）",
                    "",
                ]
            )
        else:
            lines.append("")

    lines.extend(
        [
            "### 全樣本對照（不限 zone 開倉）",
            "",
            "| 年份 | 策略 | 總報酬% | 年化期望% | 年化波動% | Sharpe | 週轉/年 | n |",
            "|------|------|--------|----------|----------|--------|---------|---|",
        ]
    )
    for year in years:
        for sk in strategy_keys:
            m = portfolio_by_year.get(year, {}).get(sk, {}).get("ALL") or {}
            lines.append(
                f"| {year} | {strategy_labels.get(sk, sk)} | "
                f"{_fmt_metric(m.get('total_return_pct'))} | "
                f"{_fmt_metric(m.get('ann_mean_return_pct'))} | "
                f"{_fmt_metric(m.get('ann_vol_pct'))} | "
                f"{_fmt_metric(m.get('sharpe_ratio'))} | "
                f"{_fmt_metric(m.get('turnover_per_year'))} | "
                f"{m.get('n_trades', 0)} |"
            )
    lines.append("")
    return "\n".join(lines)


def render_statistical_support_md(
    *,
    portfolio_by_label: dict[str, dict[str, dict[str, dict]]],
    zone_days_by_label: dict[str, dict[str, int]],
    labels: list[str],
    strategy_keys: tuple[str, ...],
    strategy_labels: dict[str, str],
    min_n_zone: int = 10,
    min_n_all: int = 30,
) -> str:
    from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER

    lines = [
        "## 統計可信度（樣本數門檻）",
        "",
        f"- zone 獨立模擬：**n ≥ {min_n_zone}** 筆才標為「可支持」",
        f"- 全樣本合併：**n ≥ {min_n_all}** 筆才標為「可支持」",
        "- 以下為 **zone 獨立模擬** 的成交筆數（非市場日數）",
        "",
        "### 合併區間 · 全樣本 n",
        "",
        "| 區間 | Pivot Gate n | Coil Close n | RRG n | 全策略可信？ |",
        "|------|-----------|------------|-------|------------|",
    ]
    for lbl in labels:
        pg_n = (portfolio_by_label.get(lbl, {}).get("pivot_gate", {}).get("ALL") or {}).get("n_trades", 0)
        cc_n = (portfolio_by_label.get(lbl, {}).get("coil_close", {}).get("ALL") or {}).get("n_trades", 0)
        rr_n = (portfolio_by_label.get(lbl, {}).get("rrg", {}).get("ALL") or {}).get("n_trades", 0)
        ok = "✓" if pg_n >= min_n_all and cc_n >= min_n_all and rr_n >= min_n_all else "△"
        lines.append(f"| `{lbl}` | {pg_n} | {cc_n} | {rr_n} | {ok} |")

    lines.extend(
        [
            "",
            "### 合併區間 · 各 zone 獨立 n（`breadth_valid` 優先參考）",
            "",
            "| zone | 市場日 | PG n | CC n | RRG n | 可信？ |",
            "|------|--------|------|------|-------|--------|",
        ]
    )
    ref_label = "breadth_valid" if "breadth_valid" in labels else labels[-1]
    zone_days = zone_days_by_label.get(ref_label, {})
    port = portfolio_by_label.get(ref_label, {})
    for zone in BREADTH_ZONES_ORDER:
        days = zone_days.get(zone, 0)
        pg_n = (port.get("pivot_gate", {}).get(zone) or {}).get("n_trades", 0)
        cc_n = (port.get("coil_close", {}).get(zone) or {}).get("n_trades", 0)
        rr_n = (port.get("rrg", {}).get(zone) or {}).get("n_trades", 0)
        rr_vals = [x for x in (pg_n, cc_n, rr_n) if x > 0]
        min_trades = min(rr_vals) if rr_vals else 0
        max_trades = max(pg_n, cc_n, rr_n)
        credible = (
            "✓"
            if min_trades >= min_n_zone and days >= 20
            else ("△" if max_trades >= min_n_zone else "✗")
        )
        lines.append(
            f"| {BREADTH_ZONE_ZH[zone]} | {days} | {pg_n} | {cc_n} | {rr_n} | {credible} |"
        )

    lines.extend(
        [
            "",
            "### 合併區間 · 全樣本組合績效（50k · 日內 M2M）",
            "",
            "| 區間 | 策略 | 總報酬% | 年化期望% | 年化波動% | Sharpe | n | 可信？ |",
            "|------|------|--------|----------|----------|--------|---|--------|",
        ]
    )
    for lbl in labels:
        for sk in strategy_keys:
            m = portfolio_by_label.get(lbl, {}).get(sk, {}).get("ALL") or {}
            n = m.get("n_trades", 0)
            cred = "✓" if n >= min_n_all else "△"
            lines.append(
                f"| `{lbl}` | {strategy_labels.get(sk, sk)} | "
                f"{m.get('total_return_pct', '—')} | {m.get('ann_mean_return_pct', '—')} | "
                f"{m.get('ann_vol_pct', '—')} | {m.get('sharpe_ratio', '—')} | {n} | {cred} |"
            )
    lines.append("")
    return "\n".join(lines)


def portfolio_metrics_for_periods(
    conn: sqlite3.Connection,
    periods: list[dict],
    trade_dates: list[str],
    *,
    total_capital: float = 50_000.0,
    n_slots: int,
    close: pd.DataFrame | None = None,
) -> dict:
    if close is None:
        from .finpilot_local_backtest import load_price_panels

        close, _, _ = load_price_panels(conn)
    return simulate_slot_portfolio(
        conn,
        close,
        trade_dates,
        periods,
        total_capital=total_capital,
        n_slots=n_slots,
    )
