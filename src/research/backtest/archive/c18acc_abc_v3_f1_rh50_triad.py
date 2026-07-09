"""Three-way comparison · C18acc vs ABC v3+f1 vs ABC v3+f1+RH50.

Uses existing comparison / RH50 gate JSON · daily RH50 flags (no 1m panel rebuild).
Kbar policy: 5m_aggregated preference · diagnostic path does not scan 1m polls.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from backtest_standard_config import comparison_notional_ntd, load_backtest_standard_config
from research.backtest.archive.abc_v3_slot_sweep import ABC_V3_HOLD_DAYS, ABC_V3_SLOT_CHAMPION
from research.backtest.archive.c18acc_abc_v3_f1_comparison import (
    C18ACC_SLOTS,
    _abc_trade_ledger,
    select_executed_slot_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.archive.rrg_regime_four_axis_phase3 import (
    RH50_THRESHOLD_PCT,
    build_rh50_session_flags,
    build_rotation_health_by_date,
)
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio

ABC_F1_SLOTS = ABC_V3_SLOT_CHAMPION


def _pct_delta(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)


def _pf_block(pf: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "total_return_pct",
        "mean_daily_return_pct",
        "ann_mean_return_pct",
        "cagr_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "avg_utilization_pct",
        "n_trades",
        "avg_hold_days",
    )
    return {k: pf.get(k) for k in keys}


def _leg_stats_from_ledger(
    rows: list[dict[str, Any]],
    *,
    ret_key: str,
) -> dict[str, Any]:
    nets = [float(r[ret_key]) for r in rows if r.get(ret_key) is not None]
    if not nets:
        return {"n": 0}
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(nets),
        "mean_ret_pct": round(sum(nets) / len(nets), 3),
        "win_rate_pct": round(100.0 * wins / len(nets), 2),
    }


def _daily_returns_from_nav(daily_nav: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach day-over-day equity return next to each nav row (first day skip)."""
    out: list[dict[str, Any]] = []
    prev: float | None = None
    for row in daily_nav:
        eq = float(row.get("equity_ntd") or 0)
        ret = None
        if prev is not None and prev > 0:
            ret = (eq / prev - 1.0) * 100.0
        out.append(
            {
                "date": str(row.get("date") or ""),
                "equity_ntd": eq,
                "in_market_flag": int(row.get("in_market_flag") or 0),
                "cash_pct": float(row.get("cash_pct") or 0),
                "daily_return_pct": ret,
            }
        )
        prev = eq
    return out


def _mean_or_none(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 6) if xs else None


def build_layered_metrics(
    daily_nav: list[dict[str, Any]],
    portfolio: dict[str, Any],
    *,
    tail_days: int = 90,
) -> dict[str, Any]:
    """Full NAV · only-invested · util-normalized · last N days.

    only-invested / util-normalized are diagnostic — not CAGR substitutes.
    """
    series = _daily_returns_from_nav(daily_nav)
    all_rets = [float(r["daily_return_pct"]) for r in series if r["daily_return_pct"] is not None]
    invested_rets = [
        float(r["daily_return_pct"])
        for r in series
        if r["daily_return_pct"] is not None and int(r["in_market_flag"]) == 1
    ]
    n_days = len(series)
    n_invested = sum(1 for r in series if int(r["in_market_flag"]) == 1)
    util = portfolio.get("avg_utilization_pct")
    full_daily = portfolio.get("mean_daily_return_pct")
    if full_daily is None:
        full_daily = _mean_or_none(all_rets)
    util_norm = None
    if full_daily is not None and util is not None and float(util) > 0:
        util_norm = round(float(full_daily) / (float(util) / 100.0), 6)

    n_tail = min(tail_days, n_days) if n_days else 0
    tail = series[-n_tail:] if n_tail else []
    tail_rets = [float(r["daily_return_pct"]) for r in tail if r["daily_return_pct"] is not None]
    tail_invested = [
        float(r["daily_return_pct"])
        for r in tail
        if r["daily_return_pct"] is not None and int(r["in_market_flag"]) == 1
    ]
    eq0 = float(tail[0]["equity_ntd"]) if tail else None
    eq1 = float(tail[-1]["equity_ntd"]) if tail else None
    tail_total = round((eq1 / eq0 - 1.0) * 100.0, 4) if eq0 and eq1 and eq0 > 0 else None
    tail_util_proxy = None
    if tail:
        # cash_pct low ⇒ more deployed; util proxy ≈ 100 - mean cash when in market weighted
        # Prefer avg of (100-cash) capped; aligns with idle cash drag narrative.
        dep = [max(0.0, 100.0 - float(r["cash_pct"])) for r in tail]
        tail_util_proxy = round(sum(dep) / len(dep), 2)

    return {
        "full_nav": {
            "n_days": n_days,
            "mean_daily_return_pct": full_daily,
            "ann_mean_return_pct": portfolio.get("ann_mean_return_pct"),
            "total_return_pct": portfolio.get("total_return_pct"),
            "avg_utilization_pct": util,
            "in_market_days": n_invested,
            "in_market_pct": round(100.0 * n_invested / n_days, 2) if n_days else None,
        },
        "only_invested": {
            "n_days": len(invested_rets),
            "mean_daily_return_pct": _mean_or_none(invested_rets),
            "ann_mean_return_pct": round(float(_mean_or_none(invested_rets) or 0) * 252.0, 4)
            if invested_rets
            else None,
            "note": "僅 in_market_flag=1 之日報酬算術均 · 不可直接×252當帳戶年化",
        },
        "util_normalized": {
            "mean_daily_return_pct": util_norm,
            "ann_mean_return_pct": round(float(util_norm) * 252.0, 4) if util_norm is not None else None,
            "formula": "full_mean_daily / (avg_utilization_pct/100)",
            "note": "單位部署效率近似 · 假設 util 可线性外推（反事實）",
        },
        "last_90d": {
            "n_days": n_tail,
            "date_start": tail[0]["date"] if tail else None,
            "date_end": tail[-1]["date"] if tail else None,
            "total_return_pct": tail_total,
            "mean_daily_return_pct": _mean_or_none(tail_rets),
            "only_invested_mean_daily_pct": _mean_or_none(tail_invested),
            "in_market_days": sum(1 for r in tail if int(r["in_market_flag"]) == 1),
            "deployed_proxy_pct": tail_util_proxy,
        },
    }


def build_triad_comparison(
    conn: sqlite3.Connection,
    *,
    comparison_payload: dict[str, Any],
    rh50_gate_payload: dict[str, Any] | None = None,
    rh50_threshold_pct: float = RH50_THRESHOLD_PCT,
    rh_wma_length: int = 20,
) -> dict[str, Any]:
    """C18acc + ABC f1 (from comparison) + ABC f1+RH50 (re-sim under same contract)."""
    t0 = time.perf_counter()
    process_log: list[dict[str, Any]] = []

    def _log(phase: str, detail: str) -> None:
        process_log.append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "elapsed_s": round(time.perf_counter() - t0, 1),
                "phase": phase,
                "detail": detail,
            }
        )

    std = load_backtest_standard_config()
    notional = comparison_notional_ntd()
    cost_model = dict(std.cost_model)
    _log("init", f"notional={notional:,.0f} · cost={cost_model.get('enabled')} · 5m policy / no 1m panel")

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    date_start = str(comparison_payload.get("date_start") or "2024-01-02")
    date_end = str(comparison_payload.get("date_end") or full_dates[-1])
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    _log("window", f"{trade_dates[0]} .. {trade_dates[-1]} · n={len(trade_dates)}")

    c18_pf = ((comparison_payload.get("strategies") or {}).get("rrg-mono-swap-accel") or {}).get(
        "portfolio"
    ) or {}
    abc_pf = ((comparison_payload.get("strategies") or {}).get("abc-v3-f1-pullback") or {}).get(
        "portfolio"
    ) or {}
    c18_ledger = list(comparison_payload.get("c18acc_trade_ledger") or [])
    abc_ledger = list(comparison_payload.get("abc_f1_trade_ledger") or [])
    _log("load_comparison", f"c18_n={len(c18_ledger)} · abc_n={len(abc_ledger)}")

    # RH50 · daily rotation health (day bars / RRG panel · not 1m)
    rh_by_date = build_rotation_health_by_date(conn, length=rh_wma_length)
    flags = build_rh50_session_flags(
        trade_dates,
        full_dates,
        rh_by_date,
        threshold_pct=rh50_threshold_pct,
    )
    pass_days = {d for d, f in flags.items() if f.get("rh50_pass")}
    n_flagged = len(pass_days)
    _log(
        "rh50_flags",
        f"allowed_days={n_flagged}/{len(trade_dates)} "
        f"({round(100.0 * n_flagged / len(trade_dates), 2) if trade_dates else 0}%) · WMA{rh_wma_length}",
    )

    # Candidate set for RH50 refill = ABC executed ledger as periods (no panel rebuild).
    # Official gate used pre-fill RH50 candidates (438→254); here we filter+refill from
    # executed ABC legs that occur on RH50 days (same price path as comparison).
    rh_periods_raw = [
        {
            "entry_date": str(r["entry_date"]),
            "exit_date": str(r["exit_date"]),
            "stock_id": str(r["stock_id"]),
            "entry_px": r.get("entry_px"),
            "exit_px": r.get("exit_px"),
            "net_pct": r.get("net_pct"),
            "excess_pct": r.get("excess_pct"),
            "rank_score": r.get("rank_score"),
            "entry_poll_idx": r.get("entry_poll_idx"),
            "entry_minute": r.get("entry_minute"),
            "stock_name": r.get("stock_name") or "",
            "hold_days": r.get("hold_days") or ABC_V3_HOLD_DAYS,
        }
        for r in abc_ledger
        if str(r.get("entry_date") or "") in pass_days
    ]
    # Order by score / poll like top-K
    rh_periods_raw.sort(
        key=lambda r: (
            str(r["entry_date"]),
            -float(r.get("rank_score") or 0),
            int(r.get("entry_poll_idx") or 0),
        )
    )
    rh_executed = select_executed_slot_periods(
        conn,
        close,
        trade_dates,
        rh_periods_raw,
        total_capital=notional,
        n_slots=ABC_F1_SLOTS,
        cost_model=cost_model,
    )
    rh_sim_periods = [
        {
            "entry_date": str(r["entry_date"]),
            "exit_date": str(r["exit_date"]),
            "stock_id": str(r["stock_id"]),
            "entry_px": r.get("entry_px"),
        }
        for r in rh_executed
    ]
    rh_pf = simulate_slot_portfolio(
        conn,
        close,
        trade_dates,
        rh_sim_periods,
        total_capital=notional,
        n_slots=ABC_F1_SLOTS,
        cost_model=cost_model,
        return_daily=True,
    )
    rh_ledger = _abc_trade_ledger(
        rh_executed, total_capital=notional, slot_cap=notional / ABC_F1_SLOTS
    )
    _log(
        "rh50_portfolio",
        f"filtered_legs={len(rh_periods_raw)} · executed={len(rh_executed)} · "
        f"total_ret={rh_pf.get('total_return_pct')}%",
    )

    gate_full = None
    if rh50_gate_payload:
        gated = (rh50_gate_payload.get("variants") or {}).get("ABC-V3-F1+RH50") or {}
        gate_full = gated.get("FULL")
        _log(
            "rh50_gate_ref",
            f"official FULL n_legs={(gate_full or {}).get('leg_stats', {}).get('n_legs')} · "
            f"daily%={(gate_full or {}).get('portfolio', {}).get('mean_daily_return_pct')} "
            f"(250k/no-cost contract · reference only)",
        )

    rows = {
        "rrg-mono-swap-accel": {
            "label": "C18acc+S2",
            "n_slots": C18ACC_SLOTS,
            "portfolio": _pf_block(c18_pf),
            "leg_stats": _leg_stats_from_ledger(c18_ledger, ret_key="return_pct"),
            "layered": build_layered_metrics(
                list(c18_pf.get("daily_nav") or []), c18_pf
            ),
            "source": "comparison_bundle",
        },
        "abc-v3-f1-pullback": {
            "label": "ABC v3+f1",
            "n_slots": ABC_F1_SLOTS,
            "portfolio": _pf_block(abc_pf),
            "leg_stats": _leg_stats_from_ledger(abc_ledger, ret_key="net_pct"),
            "layered": build_layered_metrics(
                list(abc_pf.get("daily_nav") or []), abc_pf
            ),
            "source": "comparison_bundle",
        },
        "abc-v3-f1-rh50": {
            "label": "ABC v3+f1+RH50",
            "n_slots": ABC_F1_SLOTS,
            "portfolio": _pf_block(rh_pf),
            "leg_stats": _leg_stats_from_ledger(rh_ledger, ret_key="net_pct"),
            "layered": build_layered_metrics(
                list(rh_pf.get("daily_nav") or []), rh_pf
            ),
            "source": "comparison_ledger ∩ RH50 day · refill under same 100k+cost",
            "rh50_filter_n_legs": len(rh_periods_raw),
            "official_gate_full_ref": {
                "leg_stats": (gate_full or {}).get("leg_stats"),
                "portfolio": (gate_full or {}).get("portfolio"),
                "note": (
                    "Official gate: pre-fill RH50 candidates then 5-slot top-K · "
                    "CAPITAL_PER_SLOT 50k×5 · cost off · not same contract as this triad"
                ),
            }
            if gate_full
            else None,
        },
    }

    def _layer_daily(sid: str, block: str) -> float:
        return float(
            (((rows.get(sid) or {}).get("layered") or {}).get(block) or {}).get(
                "mean_daily_return_pct"
            )
            or 0
        )

    c18_d = float(c18_pf.get("mean_daily_return_pct") or 0)
    abc_d = float(abc_pf.get("mean_daily_return_pct") or 0)
    rh_d = float(rh_pf.get("mean_daily_return_pct") or 0)
    ranked = sorted(
        [
            ("rrg-mono-swap-accel", c18_d),
            ("abc-v3-f1-pullback", abc_d),
            ("abc-v3-f1-rh50", rh_d),
        ],
        key=lambda x: -x[1],
    )
    ranked_invested = sorted(
        [
            ("rrg-mono-swap-accel", _layer_daily("rrg-mono-swap-accel", "only_invested")),
            ("abc-v3-f1-pullback", _layer_daily("abc-v3-f1-pullback", "only_invested")),
            ("abc-v3-f1-rh50", _layer_daily("abc-v3-f1-rh50", "only_invested")),
        ],
        key=lambda x: -x[1],
    )
    ranked_util = sorted(
        [
            ("rrg-mono-swap-accel", _layer_daily("rrg-mono-swap-accel", "util_normalized")),
            ("abc-v3-f1-pullback", _layer_daily("abc-v3-f1-pullback", "util_normalized")),
            ("abc-v3-f1-rh50", _layer_daily("abc-v3-f1-rh50", "util_normalized")),
        ],
        key=lambda x: -x[1],
    )

    comparison = {
        "winner_daily": ranked[0][0],
        "winner_only_invested": ranked_invested[0][0],
        "winner_util_normalized": ranked_util[0][0],
        "rank_by_mean_daily": [
            {"strategy_id": sid, "mean_daily_return_pct": d, "rank": i + 1}
            for i, (sid, d) in enumerate(ranked)
        ],
        "rank_by_only_invested": [
            {"strategy_id": sid, "mean_daily_return_pct": d, "rank": i + 1}
            for i, (sid, d) in enumerate(ranked_invested)
        ],
        "rank_by_util_normalized": [
            {"strategy_id": sid, "mean_daily_return_pct": d, "rank": i + 1}
            for i, (sid, d) in enumerate(ranked_util)
        ],
        "delta_abc_vs_c18_daily_pp": _pct_delta(
            c18_pf.get("mean_daily_return_pct"), abc_pf.get("mean_daily_return_pct")
        ),
        "delta_rh50_vs_c18_daily_pp": _pct_delta(
            c18_pf.get("mean_daily_return_pct"), rh_pf.get("mean_daily_return_pct")
        ),
        "delta_rh50_vs_abc_daily_pp": _pct_delta(
            abc_pf.get("mean_daily_return_pct"), rh_pf.get("mean_daily_return_pct")
        ),
        "delta_abc_vs_c18_total_pp": _pct_delta(
            c18_pf.get("total_return_pct"), abc_pf.get("total_return_pct")
        ),
        "delta_rh50_vs_c18_total_pp": _pct_delta(
            c18_pf.get("total_return_pct"), rh_pf.get("total_return_pct")
        ),
        "delta_rh50_vs_abc_total_pp": _pct_delta(
            abc_pf.get("total_return_pct"), rh_pf.get("total_return_pct")
        ),
    }

    _log(
        "done",
        f"total_runtime={time.perf_counter() - t0:.1f}s · "
        f"winner_nav={ranked[0][0]} · winner_invested={ranked_invested[0][0]} · "
        f"winner_util_norm={ranked_util[0][0]}",
    )

    return {
        "schema": "c18acc_abc_v3_f1_rh50_triad-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "kbar_policy": {
            "pricing_preference": "5m_aggregated",
            "rebuild_panel": False,
            "note": (
                "本三方比較不重建 dual-WMA / 不掃 1m。"
                "C18acc·ABC 來自 comparison bundle；RH50 用日線 rotation_health 旗標過濾後同契約重跑 NAV。"
            ),
        },
        "comparison_contract": {
            "notional_ntd": notional,
            "cost_model": cost_model,
            "nav_engine": "simulate_slot_portfolio",
            "benchmark": std.benchmark,
            "doc": "docs/unified-backtest-standard.md",
        },
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_trade_dates": len(trade_dates),
        "rh50_gate": {
            "rule": f"T-1 universe rotation_health_pct(WMA{rh_wma_length}) >= {rh50_threshold_pct}",
            "rh_wma_length": rh_wma_length,
            "threshold_pct": rh50_threshold_pct,
            "flagged_session_days": n_flagged,
            "total_session_days": len(trade_dates),
            "allowed_share_pct": round(100.0 * n_flagged / len(trade_dates), 2)
            if trade_dates
            else None,
            "pit": "regime_t1_as_of = prior TW session",
        },
        "strategies": rows,
        "comparison": comparison,
        "process_log": process_log,
        "total_runtime_s": round(time.perf_counter() - t0, 1),
        "abc_f1_rh50_trade_ledger": rh_ledger,
    }


def render_triad_md(payload: dict[str, Any]) -> str:
    strategies = payload.get("strategies") or {}
    cmp_ = payload.get("comparison") or {}
    contract = payload.get("comparison_contract") or {}
    gate = payload.get("rh50_gate") or {}
    kbar = payload.get("kbar_policy") or {}
    order = (
        "rrg-mono-swap-accel",
        "abc-v3-f1-pullback",
        "abc-v3-f1-rh50",
    )

    def _cell(sid: str, key: str) -> Any:
        return ((strategies.get(sid) or {}).get("portfolio") or {}).get(key)

    def _leg(sid: str, key: str) -> Any:
        return ((strategies.get(sid) or {}).get("leg_stats") or {}).get(key)

    def _ly(sid: str, block: str, key: str) -> Any:
        return (((strategies.get(sid) or {}).get("layered") or {}).get(block) or {}).get(key)

    lines = [
        "# 三者比較 · C18acc vs ABC v3+f1 vs ABC v3+f1+RH50",
        "",
        f"生成：`{payload.get('generated_at')}` · 耗時 **{payload.get('total_runtime_s')}s**",
        "",
        "## 比較契約",
        "",
        f"- 窗口：**{payload.get('date_start')} ~ {payload.get('date_end')}**（{payload.get('n_trade_dates')} 交易日）",
        f"- 本金：**{contract.get('notional_ntd'):,.0f} NTD** · 成本啟用={ (contract.get('cost_model') or {}).get('enabled') }",
        f"- NAV：`simulate_slot_portfolio`",
        f"- K 線政策：**{kbar.get('pricing_preference')}** · rebuild_panel={kbar.get('rebuild_panel')}",
        f"- 說明：{kbar.get('note')}",
        "",
        "## RH50 gate",
        "",
        f"- 規則：{gate.get('rule')}",
        f"- PIT：{gate.get('pit')}",
        f"- 允許進場日：{gate.get('flagged_session_days')}/{gate.get('total_session_days')} "
        f"（{gate.get('allowed_share_pct')}%）",
        "",
        "## 組合績效（同契約 · 可比）",
        "",
        "| 指標 | C18acc（3槽） | ABC v3+f1（5槽） | ABC+RH50（5槽） |",
        "|------|---------------|------------------|-----------------|",
    ]
    metrics = [
        ("區間總報酬 %", "total_return_pct"),
        ("日均報酬 %", "mean_daily_return_pct"),
        ("年化日均 %", "ann_mean_return_pct"),
        ("CAGR %", "cagr_pct"),
        ("Sharpe", "sharpe_ratio"),
        ("MaxDD %", "max_drawdown_pct"),
        ("利用率 %", "avg_utilization_pct"),
        ("成交筆", "n_trades"),
    ]
    for label, key in metrics:
        lines.append(
            f"| {label} | {_cell(order[0], key)} | {_cell(order[1], key)} | {_cell(order[2], key)} |"
        )

    lines.extend(
        [
            "",
            f"**完整 NAV 日均第一**：`{cmp_.get('winner_daily')}`",
            "",
            "| 對照 | Δ daily pp | Δ total pp |",
            "|------|-----------:|-----------:|",
            f"| ABC − C18 | {cmp_.get('delta_abc_vs_c18_daily_pp')} | {cmp_.get('delta_abc_vs_c18_total_pp')} |",
            f"| RH50 − C18 | {cmp_.get('delta_rh50_vs_c18_daily_pp')} | {cmp_.get('delta_rh50_vs_c18_total_pp')} |",
            f"| RH50 − ABC | {cmp_.get('delta_rh50_vs_abc_daily_pp')} | {cmp_.get('delta_rh50_vs_abc_total_pp')} |",
            "",
            "## 分層對照（完整 NAV / 有倉日 / util-normalized / 近 90d）",
            "",
            "> **讀法**：完整 NAV = 真實帳戶；有倉日 = 部署時的邊際表現（不可直接年化當帳戶）；"
            "util-normalized = 反事實單位效率；近 90d = 同長跑切開近期。",
            "",
            "### 日均報酬 %",
            "",
            "| 口徑 | C18acc | ABC v3+f1 | ABC+RH50 | 第一名 |",
            "|------|--------|-----------|----------|--------|",
            f"| 完整 NAV | {_ly(order[0], 'full_nav', 'mean_daily_return_pct')} | "
            f"{_ly(order[1], 'full_nav', 'mean_daily_return_pct')} | "
            f"{_ly(order[2], 'full_nav', 'mean_daily_return_pct')} | `{cmp_.get('winner_daily')}` |",
            f"| 有倉日 only-invested | {_ly(order[0], 'only_invested', 'mean_daily_return_pct')} | "
            f"{_ly(order[1], 'only_invested', 'mean_daily_return_pct')} | "
            f"{_ly(order[2], 'only_invested', 'mean_daily_return_pct')} | "
            f"`{cmp_.get('winner_only_invested')}` |",
            f"| util-normalized | {_ly(order[0], 'util_normalized', 'mean_daily_return_pct')} | "
            f"{_ly(order[1], 'util_normalized', 'mean_daily_return_pct')} | "
            f"{_ly(order[2], 'util_normalized', 'mean_daily_return_pct')} | "
            f"`{cmp_.get('winner_util_normalized')}` |",
            f"| 近 90d NAV 日均 | {_ly(order[0], 'last_90d', 'mean_daily_return_pct')} | "
            f"{_ly(order[1], 'last_90d', 'mean_daily_return_pct')} | "
            f"{_ly(order[2], 'last_90d', 'mean_daily_return_pct')} | — |",
            "",
            "### 部署與樣本",
            "",
            "| 口徑 | C18acc | ABC v3+f1 | ABC+RH50 |",
            "|------|--------|-----------|----------|",
            f"| 利用率 % | {_ly(order[0], 'full_nav', 'avg_utilization_pct')} | "
            f"{_ly(order[1], 'full_nav', 'avg_utilization_pct')} | "
            f"{_ly(order[2], 'full_nav', 'avg_utilization_pct')} |",
            f"| 有倉天數 / 總天數 | {_ly(order[0], 'full_nav', 'in_market_days')}/"
            f"{_ly(order[0], 'full_nav', 'n_days')} "
            f"（{_ly(order[0], 'full_nav', 'in_market_pct')}%） | "
            f"{_ly(order[1], 'full_nav', 'in_market_days')}/"
            f"{_ly(order[1], 'full_nav', 'n_days')} "
            f"（{_ly(order[1], 'full_nav', 'in_market_pct')}%） | "
            f"{_ly(order[2], 'full_nav', 'in_market_days')}/"
            f"{_ly(order[2], 'full_nav', 'n_days')} "
            f"（{_ly(order[2], 'full_nav', 'in_market_pct')}%） |",
            f"| 有倉日樣本 n | {_ly(order[0], 'only_invested', 'n_days')} | "
            f"{_ly(order[1], 'only_invested', 'n_days')} | "
            f"{_ly(order[2], 'only_invested', 'n_days')} |",
            "",
            "### 近 90 天窗口",
            "",
            f"窗口：`{_ly(order[0], 'last_90d', 'date_start')} ~ {_ly(order[0], 'last_90d', 'date_end')}`",
            "",
            "| 指標 | C18acc | ABC v3+f1 | ABC+RH50 |",
            "|------|--------|-----------|----------|",
            f"| 區間報酬 % | {_ly(order[0], 'last_90d', 'total_return_pct')} | "
            f"{_ly(order[1], 'last_90d', 'total_return_pct')} | "
            f"{_ly(order[2], 'last_90d', 'total_return_pct')} |",
            f"| 日均 % | {_ly(order[0], 'last_90d', 'mean_daily_return_pct')} | "
            f"{_ly(order[1], 'last_90d', 'mean_daily_return_pct')} | "
            f"{_ly(order[2], 'last_90d', 'mean_daily_return_pct')} |",
            f"| 有倉日日均 % | {_ly(order[0], 'last_90d', 'only_invested_mean_daily_pct')} | "
            f"{_ly(order[1], 'last_90d', 'only_invested_mean_daily_pct')} | "
            f"{_ly(order[2], 'last_90d', 'only_invested_mean_daily_pct')} |",
            f"| 有倉天數 | {_ly(order[0], 'last_90d', 'in_market_days')} | "
            f"{_ly(order[1], 'last_90d', 'in_market_days')} | "
            f"{_ly(order[2], 'last_90d', 'in_market_days')} |",
            f"| 部署 proxy %（100−現金） | {_ly(order[0], 'last_90d', 'deployed_proxy_pct')} | "
            f"{_ly(order[1], 'last_90d', 'deployed_proxy_pct')} | "
            f"{_ly(order[2], 'last_90d', 'deployed_proxy_pct')} |",
            "",
            "## 事件 / 成交腿",
            "",
            "| | C18acc | ABC v3+f1 | ABC+RH50 |",
            "|--|--------|-----------|----------|",
            f"| n | {_leg(order[0], 'n')} | {_leg(order[1], 'n')} | {_leg(order[2], 'n')} |",
            f"| 每筆均 % | {_leg(order[0], 'mean_ret_pct')} | {_leg(order[1], 'mean_ret_pct')} | {_leg(order[2], 'mean_ret_pct')} |",
            f"| 勝率 % | {_leg(order[0], 'win_rate_pct')} | {_leg(order[1], 'win_rate_pct')} | {_leg(order[2], 'win_rate_pct')} |",
            "",
        ]
    )

    rh_meta = strategies.get("abc-v3-f1-rh50") or {}
    ref = rh_meta.get("official_gate_full_ref") or {}
    if ref:
        ols = ref.get("leg_stats") or {}
        opf = ref.get("portfolio") or {}
        lines.extend(
            [
                "### 官方 RH50 gate FULL（參考 · 不同契約）",
                "",
                f"- n_legs={ols.get('n_legs')} · mean net={ols.get('mean_net_pct')}% · "
                f"excess={ols.get('mean_excess_pct')}% · win={ols.get('win_rate_pct')}%",
                f"- portfolio daily={opf.get('mean_daily_return_pct')}% · "
                f"total={opf.get('total_return_pct')}% · util={opf.get('avg_utilization_pct')}%",
                f"- {ref.get('note')}",
                f"- 本報告同契約成交筆={_leg(order[2], 'n')} "
                f"（自 ABC 成交腿 ∩ RH50 日再 fill；官方預先過濾候選後 fill 可能略多）",
                "",
            ]
        )

    lines.extend(
        [
            "## 解讀",
            "",
            "1. **完整 NAV**：含空手現金 · 長窗通常 **C18acc** 領先（高利用率）。",
            "2. **有倉日 / util-normalized**：回答「有錢在市時誰有效率」· 常讓 ABC 接近或反超；"
            "**不能**當成帳戶 CAGR。",
            "3. **近 90d**：同長跑切開 · ABC 近期 NAV 可能領先（與 teaching deck 對齊）。",
            "4. **RH50**：抬高每筆品質，但再砍可進場日 → 完整 NAV 往往更差。",
            "5. 本三方 **同 10 萬 + 成本**；官方 RH50 gate JSON 為 25 萬無成本，不可直接比 total%。",
            "",
            "## 執行過程",
            "",
            "| 時間 | 秒 | Phase | 說明 |",
            "|------|----|-------|------|",
        ]
    )
    for row in payload.get("process_log") or []:
        lines.append(
            f"| {row.get('ts')} | {row.get('elapsed_s')} | {row.get('phase')} | {row.get('detail')} |"
        )

    lines.extend(
        [
            "",
            "## ABC+RH50 交易紀錄",
            "",
            f"共 **{len(payload.get('abc_f1_rh50_trade_ledger') or [])}** 筆。",
            "",
            "| # | 代碼 | 名稱 | 進場日 | 出場日 | 淨報酬% | 超額% | PnL NTD | 累計% |",
            "|---|------|------|--------|--------|---------|-------|---------|-------|",
        ]
    )
    for r in payload.get("abc_f1_rh50_trade_ledger") or []:
        lines.append(
            f"| {r.get('idx')} | {r.get('stock_id')} | {r.get('stock_name', '')} | "
            f"{r.get('entry_date')} | {r.get('exit_date')} | {r.get('net_pct')} | "
            f"{r.get('excess_pct')} | {r.get('pnl_ntd')} | {r.get('cum_return_pct')} |"
        )

    lines.extend(
        [
            "",
            "## 模組",
            "",
            "- `scripts/run_c18acc_abc_v3_f1_rh50_triad.py`",
            "- `src/research/backtest/c18acc_abc_v3_f1_rh50_triad.py`",
        ]
    )
    return "\n".join(lines)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
