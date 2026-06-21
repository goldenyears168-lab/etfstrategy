"""轨 C：事件驱动提前出场（相对固定 H 基准）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from .copytrade_backtest import (
    ADD_ACTIONS,
    DEFAULT_SIGNAL_CAPITAL_NTD,
    _bench_open,
    bench_return_entry_to_exit,
    iter_copytrade_signals,
    resolve_entry_date,
    simulate_fixed_slots,
    count_hold_trading_days,
    snapshot_pairs,
)
from .copytrade_regime_horizon import classify_regime_pit
from flow_returns import exit_close_date_from_entry, return_pct, stock_close, stock_open
from holdings_research import REDUCE_ACTIONS
from stock_db import compute_etf_holdings_changes, list_etf_snapshot_dates

ENTRY_LAG_DAYS = 0
ENTRY_PRICE_MODE = "open"
DEFAULT_BASELINE_H = 20
RESTRICTIVE_EXPOSURE = frozenset({"restrictive", "cash-priority"})

POLICY_SPECS: dict[str, str] = {
    "baseline_h20": "固定 H20 收盘出场（基准）",
    "exit_reduce_clear": "经理减码/出清同股 → 讯号日 T+1 开盘卖",
    "exit_regime_restrictive": "持仓中 regime→restrictive/cash → T+1 开盘卖",
    "exit_reduce_or_regime": "减码/出清 或 regime 转弱（取先到）",
    "readd_extend_h20": "持仓中再次加码 → 自加码日延长 H20",
}


@dataclass(frozen=True)
class HoldingsEvent:
    signal_date: str
    stock_id: str
    action: str
    share_delta: float


@dataclass
class LegExitResult:
    signal_date: str
    stock_id: str
    action: str
    entry_date: str
    planned_exit_date: str
    actual_exit_date: str
    exit_reason: str
    triggered: bool
    hold_days: int
    return_pct: float
    bench_return_pct: float
    excess_pct: float
    alpha_ntd: float
    baseline_alpha_ntd: float
    status: str


def iter_holdings_events(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    actions: frozenset[str] | None = None,
) -> list[HoldingsEvent]:
    dates = list_etf_snapshot_dates(conn, etf_code)
    pairs = snapshot_pairs(dates, backfill=True)
    out: list[HoldingsEvent] = []
    for score_date, outcome_date in pairs:
        if window_start and outcome_date < window_start:
            continue
        if window_end and outcome_date > window_end:
            continue
        for row in compute_etf_holdings_changes(
            conn, etf_code, outcome_date, score_date
        ):
            action = str(row["action"] or "")
            if actions is not None and action not in actions:
                continue
            delta = float(row["share_delta"] or 0)
            if action in ADD_ACTIONS and delta <= 0:
                continue
            if action in REDUCE_ACTIONS and delta >= 0:
                continue
            if action == "不变":
                continue
            out.append(
                HoldingsEvent(
                    signal_date=outcome_date,
                    stock_id=str(row["stock_id"]),
                    action=action,
                    share_delta=delta,
                )
            )
    return out


def _events_by_stock(events: list[HoldingsEvent]) -> dict[str, list[HoldingsEvent]]:
    by: dict[str, list[HoldingsEvent]] = {}
    for ev in events:
        by.setdefault(ev.stock_id, []).append(ev)
    for sid in by:
        by[sid].sort(key=lambda e: e.signal_date)
    return by


def _trading_days_between(
    conn: sqlite3.Connection, start: str, end_inclusive: str
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date > ? AND trade_date <= ?
        ORDER BY d ASC
        """,
        (start, end_inclusive),
    ).fetchall()
    return [str(r["d"]) for r in rows]


def _exit_open_after_signal(
    conn: sqlite3.Connection, signal_date: str
) -> str | None:
    return resolve_entry_date(conn, signal_date, ENTRY_LAG_DAYS)


def _resolve_actual_exit(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    entry_date: str,
    planned_exit_date: str,
    policy_id: str,
    events: list[HoldingsEvent],
) -> tuple[str, str, bool]:
    """返回 (actual_exit_date, exit_reason, triggered)。"""
    if policy_id == "baseline_h20":
        return planned_exit_date, "baseline", False

    actual = planned_exit_date
    reason = "baseline"
    triggered = False

    if policy_id in ("exit_reduce_clear", "exit_reduce_or_regime"):
        for ev in events:
            if ev.signal_date <= entry_date:
                continue
            if ev.signal_date > planned_exit_date:
                break
            if ev.action not in REDUCE_ACTIONS:
                continue
            exit_d = _exit_open_after_signal(conn, ev.signal_date)
            if exit_d is None or exit_d <= entry_date:
                continue
            if exit_d < actual:
                actual = exit_d
                reason = ev.action
                triggered = True
                if policy_id == "exit_reduce_clear":
                    return actual, reason, triggered

    if policy_id in ("exit_regime_restrictive", "exit_reduce_or_regime"):
        for d in _trading_days_between(conn, entry_date, planned_exit_date):
            lab = classify_regime_pit(conn, d)
            if lab is None or lab.exposure_decision not in RESTRICTIVE_EXPOSURE:
                continue
            exit_d = _exit_open_after_signal(conn, d)
            if exit_d is None or exit_d <= entry_date:
                continue
            if exit_d < actual:
                actual = exit_d
                reason = f"regime_{lab.exposure_decision}"
                triggered = True
                if policy_id == "exit_regime_restrictive":
                    return actual, reason, triggered

    if policy_id == "readd_extend_h20":
        baseline_h = _horizon_from_planned(conn, entry_date, planned_exit_date)
        extended = planned_exit_date
        for ev in events:
            if ev.action != "加码":
                continue
            if ev.signal_date <= entry_date or ev.signal_date > planned_exit_date:
                continue
            readd_entry = _exit_open_after_signal(conn, ev.signal_date)
            if readd_entry is None:
                continue
            new_exit = exit_close_date_from_entry(conn, readd_entry, baseline_h)
            if new_exit and new_exit > extended:
                extended = new_exit
                reason = "readd_extend"
                triggered = True
        if extended > planned_exit_date:
            return extended, reason, triggered

    if triggered and policy_id == "exit_reduce_or_regime":
        return actual, reason, triggered
    return planned_exit_date, reason, triggered


def _horizon_from_planned(
    conn: sqlite3.Connection, entry_date: str, planned_exit_date: str
) -> int:
    h = 1
    while h <= 60:
        d = exit_close_date_from_entry(conn, entry_date, h)
        if d is None:
            break
        if d >= planned_exit_date:
            return h
        h += 1
    return DEFAULT_BASELINE_H


def _leg_pnl(
    conn: sqlite3.Connection,
    stock_id: str,
    entry_date: str,
    exit_date: str,
    *,
    leg_capital_ntd: float,
    exit_at_open: bool,
) -> tuple[float, float, float, str]:
    entry_px = stock_open(conn, stock_id, entry_date)
    if entry_px is None or entry_px <= 0:
        return 0.0, 0.0, 0.0, "skip_no_entry_px"
    if exit_at_open:
        exit_px = stock_open(conn, stock_id, exit_date)
        bench = bench_return_entry_to_exit(
            conn, entry_date, exit_date, entry_price_mode=ENTRY_PRICE_MODE
        )
        if exit_px is None:
            return 0.0, 0.0, 0.0, "skip_no_exit_px"
        b0 = _bench_open(conn, entry_date) if ENTRY_PRICE_MODE == "open" else None
        b1 = _bench_open(conn, exit_date)
        if b0 is None or b1 is None:
            return 0.0, 0.0, 0.0, "skip_no_bench"
        bench_ret = return_pct(b0, b1)
    else:
        exit_px = stock_close(conn, stock_id, exit_date)
        bench_ret = bench_return_entry_to_exit(
            conn, entry_date, exit_date, entry_price_mode=ENTRY_PRICE_MODE
        )
        if exit_px is None or bench_ret is None:
            return 0.0, 0.0, 0.0, "skip_no_prices"
        bench = bench_ret
        bench_ret = bench
    leg_ret = return_pct(entry_px, exit_px)
    excess = leg_ret - bench_ret
    alpha = leg_capital_ntd * excess / 100.0
    return leg_ret, bench_ret, alpha, "complete"


def collect_leg_exit_results(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    policy_id: str,
    baseline_h: int = DEFAULT_BASELINE_H,
    leg_capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    window_start: str | None = None,
    window_end: str | None = None,
    baseline_alpha_map: dict[tuple[str, str], float] | None = None,
) -> list[LegExitResult]:
    add_signals = iter_copytrade_signals(
        conn,
        etf_code,
        window_start=window_start,
        window_end=window_end,
        actions=ADD_ACTIONS,
    )
    all_events = iter_holdings_events(
        conn, etf_code, window_start=window_start, window_end=window_end
    )
    by_stock = _events_by_stock(all_events)

    out: list[LegExitResult] = []
    for sig in add_signals:
        entry_date = resolve_entry_date(conn, sig.signal_date, ENTRY_LAG_DAYS)
        if entry_date is None:
            continue
        planned = exit_close_date_from_entry(conn, entry_date, baseline_h)
        if planned is None:
            continue
        stock_events = by_stock.get(sig.stock_id, [])
        actual, reason, triggered = _resolve_actual_exit(
            conn,
            stock_id=sig.stock_id,
            entry_date=entry_date,
            planned_exit_date=planned,
            policy_id=policy_id,
            events=stock_events,
        )
        exit_at_open = triggered and actual < planned
        leg_ret, bench_ret, alpha, status = _leg_pnl(
            conn,
            sig.stock_id,
            entry_date,
            actual,
            leg_capital_ntd=leg_capital_ntd,
            exit_at_open=exit_at_open,
        )
        if policy_id == "baseline_h20":
            b_alpha = alpha
        else:
            b_alpha = (baseline_alpha_map or {}).get(
                (sig.signal_date, sig.stock_id), 0.0
            )
        hold = count_hold_trading_days(conn, entry_date, actual) or 0
        out.append(
            LegExitResult(
                signal_date=sig.signal_date,
                stock_id=sig.stock_id,
                action=sig.action,
                entry_date=entry_date,
                planned_exit_date=planned,
                actual_exit_date=actual,
                exit_reason=reason,
                triggered=triggered,
                hold_days=hold,
                return_pct=leg_ret,
                bench_return_pct=bench_ret,
                excess_pct=leg_ret - bench_ret,
                alpha_ntd=alpha,
                baseline_alpha_ntd=b_alpha,
                status=status,
            )
        )
    return out


def _wilcoxon_paired(a: list[float], b: list[float]) -> float | None:
    if len(a) < 20 or len(a) != len(b):
        return None
    diffs = [x - y for x, y in zip(a, b)]
    nz = [d for d in diffs if abs(d) > 1e-9]
    if len(nz) < 15:
        return None
    try:
        from scipy.stats import wilcoxon

        _, p = wilcoxon(nz)
        return float(p)
    except Exception:
        return None


def summarize_policy(
    results: list[LegExitResult],
    *,
    etf_code: str,
    policy_id: str,
    baseline_h: int,
    conn: sqlite3.Connection,
    rotation_capital_ntd: float | None = None,
    rotation_horizon: int | None = None,
) -> dict:
    complete = [r for r in results if r.status == "complete"]
    n = len(complete)
    alphas = [r.alpha_ntd for r in complete]
    excess = [r.excess_pct for r in complete]
    triggered = [r for r in complete if r.triggered]
    mean_alpha = sum(alphas) / n if n else None
    mean_excess = sum(excess) / n if n else None
    total_alpha = sum(alphas)

    baseline_alphas = [r.baseline_alpha_ntd for r in complete]
    total_baseline = sum(baseline_alphas)
    paired_diff = [a - b for a, b in zip(alphas, baseline_alphas)]
    mean_diff = sum(paired_diff) / n if n else None

    rotation_recycled = None
    rotation_cycles = None
    rotation_capture = None
    if rotation_capital_ntd and rotation_horizon and n:
        deploy_scale = (rotation_capital_ntd / rotation_horizon) / DEFAULT_SIGNAL_CAPITAL_NTD
        signal_days = sorted(
            [
                {
                    "signal_date": r.signal_date,
                    "entry_date": r.entry_date,
                    "exit_date": r.actual_exit_date,
                    "alpha_ntd": r.alpha_ntd,
                    "pnl_ntd": r.alpha_ntd,
                    "status": "complete",
                }
                for r in complete
            ],
            key=lambda d: str(d["signal_date"]),
        )
        sim = simulate_fixed_slots(conn, signal_days, n_slots=rotation_horizon)
        rotation_recycled = round(
            float(sim.get("recycled_total_alpha_ntd") or 0) * deploy_scale, 2
        )
        rotation_cycles = int(sim.get("recycled_n_cycles") or 0)
        rotation_capture = sim.get("signal_capture_pct")

    return {
        "etf_code": etf_code,
        "policy_id": policy_id,
        "policy_label": POLICY_SPECS.get(policy_id, policy_id),
        "baseline_h": baseline_h,
        "n_legs": len(results),
        "n_complete": n,
        "n_triggered": len(triggered),
        "n_early_exit": sum(
            1 for r in triggered if r.actual_exit_date < r.planned_exit_date
        ),
        "mean_alpha_ntd": round(mean_alpha, 2) if mean_alpha is not None else None,
        "mean_excess_pct": round(mean_excess, 4) if mean_excess is not None else None,
        "total_alpha_ntd": round(total_alpha, 2),
        "vs_baseline_alpha_delta": round(total_alpha - total_baseline, 2),
        "mean_paired_alpha_delta": round(mean_diff, 2) if mean_diff is not None else None,
        "p_value_wilcoxon_paired": _wilcoxon_paired(alphas, baseline_alphas),
        "rotation_capital_ntd": rotation_capital_ntd,
        "rotation_recycled_alpha_ntd": rotation_recycled,
        "rotation_n_cycles": rotation_cycles,
        "rotation_capture_pct": rotation_capture,
    }


def format_event_exit_markdown(
    *,
    etf_code: str,
    batch_id: str,
    summaries: list[dict],
    baseline_h: int,
    rotation_capital_ntd: float | None,
) -> str:
    today = date.today().strftime("%Y%m%d")
    baseline = next((s for s in summaries if s["policy_id"] == "baseline_h20"), summaries[0])
    lines = [
        f"# {etf_code} 事件驱动出场（轨 C · L1 · H{baseline_h} 基准）",
        "",
        f"> batch `{batch_id}` · 报告日 {today}",
        "",
        "## 方法",
        "",
        f"- **基准**：L1 进场，固定 **H{baseline_h}** 收盘出场。",
        "- **事件规则**：见下表；触发后 **T+1 开盘** 卖出（regime 为转弱日 T+1 开盘）。",
        "- **再次加码**：自加码进场日重新计 H20（延长持有）。",
        (
            f"- **Rotation 对照**：{rotation_capital_ntd:,.0f} NTD · H{baseline_h} 槽 · "
            f"每日 deploy = capital/H。"
            if rotation_capital_ntd
            else "- **Rotation**：未启用"
        ),
        "",
        "## 政策对照",
        "",
        "| 政策 | 触发数 | 提前出场 | mean α | Δvs基准 | 配对 p(W) | rotation 回收α |",
        "|------|--------|---------|--------|---------|----------|---------------|",
    ]
    for s in summaries:
        p = s.get("p_value_wilcoxon_paired")
        p_s = "—" if s["policy_id"] == "baseline_h20" else (f"{p:.4f}" if p is not None else "—")
        rot = s.get("rotation_recycled_alpha_ntd")
        rot_s = f"{rot:+,.0f}" if rot is not None else "—"
        delta = s.get("vs_baseline_alpha_delta")
        delta_s = "—" if s["policy_id"] == "baseline_h20" else f"{delta:+,.0f}"
        lines.append(
            f"| {s['policy_label']} | {s['n_triggered']} | {s['n_early_exit']} | "
            f"{s['mean_alpha_ntd'] or 0:.0f} | {delta_s} | {p_s} | {rot_s} |"
        )

    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- **Primary**：相对 H20 基准，事件出场是否提升 **total α** 或 **rotation 回收α**。",
            "- 减码/出清为 **同 ETF 持股变动** 讯号，非全市场新闻。",
            "- regime 转弱为 PIT 标签（`classify_regime_pit`），仅持仓期间扫描。",
            "- 若配对 p>0.05 且 Δα≤0 → **维持固定 H20**，事件规则不采纳。",
            "",
            "### 政策说明",
            "",
        ]
    )
    for pid, label in POLICY_SPECS.items():
        lines.append(f"- `{pid}`：{label}")

    lines.append("")
    b_rot = baseline.get("rotation_recycled_alpha_ntd")
    if b_rot is not None:
        best = max(
            summaries,
            key=lambda s: float(s.get("rotation_recycled_alpha_ntd") or -1e18),
        )
        lines.extend(
            [
                "### Rotation 摘要",
                "",
                f"- 基准 H{baseline_h} 回收 α：**{b_rot:+,.0f}** NTD",
                f"- 最高：**{best['policy_label']}** → "
                f"{best.get('rotation_recycled_alpha_ntd'):+,.0f} NTD",
                "",
            ]
        )
    return "\n".join(lines)


def run_event_exit_analysis(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    batch_id: str | None = None,
    baseline_h: int = DEFAULT_BASELINE_H,
    leg_capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    rotation_capital_ntd: float | None = 100_000.0,
    window_start: str | None = None,
    window_end: str | None = None,
    policies: tuple[str, ...] | None = None,
    persist: bool = True,
) -> dict[str, object]:
    from stock_db import persist_copytrade_event_exit

    bid = batch_id or f"{etf_code.lower()}-event-exit-{date.today().strftime('%Y%m%d')}"
    policy_ids = policies or tuple(POLICY_SPECS.keys())
    summaries: list[dict] = []
    all_legs: list[dict] = []

    baseline_results = collect_leg_exit_results(
        conn,
        etf_code,
        policy_id="baseline_h20",
        baseline_h=baseline_h,
        leg_capital_ntd=leg_capital_ntd,
        window_start=window_start,
        window_end=window_end,
    )
    baseline_alpha_map = {
        (r.signal_date, r.stock_id): r.alpha_ntd for r in baseline_results
    }

    for pid in policy_ids:
        results = (
            baseline_results
            if pid == "baseline_h20"
            else collect_leg_exit_results(
                conn,
                etf_code,
                policy_id=pid,
                baseline_h=baseline_h,
                leg_capital_ntd=leg_capital_ntd,
                window_start=window_start,
                window_end=window_end,
                baseline_alpha_map=baseline_alpha_map,
            )
        )
        summary = summarize_policy(
            results,
            etf_code=etf_code,
            policy_id=pid,
            baseline_h=baseline_h,
            conn=conn,
            rotation_capital_ntd=rotation_capital_ntd,
            rotation_horizon=baseline_h,
        )
        summaries.append(summary)
        for r in results:
            all_legs.append(
                {
                    "policy_id": pid,
                    "signal_date": r.signal_date,
                    "stock_id": r.stock_id,
                    "action": r.action,
                    "entry_date": r.entry_date,
                    "planned_exit_date": r.planned_exit_date,
                    "actual_exit_date": r.actual_exit_date,
                    "exit_reason": r.exit_reason,
                    "triggered": int(r.triggered),
                    "hold_days": r.hold_days,
                    "alpha_ntd": round(r.alpha_ntd, 2),
                    "baseline_alpha_ntd": round(r.baseline_alpha_ntd, 2),
                    "status": r.status,
                }
            )

    if persist:
        persist_copytrade_event_exit(conn, bid, summaries, leg_rows=all_legs)

    return {
        "batch_id": bid,
        "summaries": summaries,
        "leg_rows": all_legs,
    }
