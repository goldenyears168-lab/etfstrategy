"""C18acc vs ABC v3+f1 · unified comparison bundle (2024+ · trade ledger)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any

from backtest_standard_config import comparison_notional_ntd, load_backtest_standard_config
from flow_returns import stock_close
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    ABC_V3_SLOT_CHAMPION,
    _build_abc_v3_intraday_panel,
    _unconstrained_leg_stats,
    apply_score_spec,
    collect_abc_v3_skip0900_period_candidates,
    order_periods_for_fill,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import build_c18acc_s2_executed_legs_for_timeline
from research.backtest.slot_portfolio_metrics import (
    _OpenPos,
    _apply_exit_proceeds,
    _entry_cost,
    _resolve_entry_px,
    simulate_slot_portfolio,
)
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1


C18ACC_SLOTS = 3
ABC_F1_SLOTS = ABC_V3_SLOT_CHAMPION

# SSOT · event-level champion · `config/strategy.yaml` abc-v3-f1-pullback
ABC_F1_PIPELINE_CHAMPION = {
    "label": "90d OOS pipeline",
    "window": "intraday tail 90d · train 60d / val 30d",
    "n": 170,
    "mean_ret_3d_net_pct": 2.845,
    "win_rate_pct": 54.71,
    "median_ret_3d_net_pct": 0.561,
    "val_mean_ret_3d_net_pct": 1.826,
    "source": "reports/research/rrg/20260708_abc_v3_f1_pipeline_90d.json",
}


def _event_stats_from_pct_values(
    nets: list[float],
    *,
    hold_days: int | None = None,
) -> dict[str, Any]:
    if not nets:
        return {"n": 0}
    wins = sum(1 for x in nets if x > 0)
    stats: dict[str, Any] = {
        "n": len(nets),
        "mean_ret_net_pct": round(sum(nets) / len(nets), 3),
        "win_rate_pct": round(100.0 * wins / len(nets), 2),
    }
    if hold_days and hold_days > 0:
        stats["mean_daily_ret_net_pct"] = round(
            sum(nets) / hold_days / len(nets), 4
        )
    return stats


def _c18_event_stats(legs: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [float(lg["return_pct"]) for lg in legs if lg.get("return_pct") is not None]
    stats = _event_stats_from_pct_values(nets)
    if stats.get("n"):
        stats["hold_note"] = "5–10d + rotation/S2 · not 3d event"
    return stats


def enrich_event_level(payload: dict[str, Any]) -> dict[str, Any]:
    """Backfill event-level block from ledgers when absent (render-only path)."""
    if payload.get("event_level"):
        return payload
    abc_rows = payload.get("abc_f1_trade_ledger") or []
    c18_rows = payload.get("c18acc_trade_ledger") or []
    abc_nets = [float(r["net_pct"]) for r in abc_rows if r.get("net_pct") is not None]
    abc_strat = (payload.get("strategies") or {}).get("abc-v3-f1-pullback") or {}
    n_candidates = int(abc_strat.get("n_candidates") or 0)
    payload = dict(payload)
    all_candidates: dict[str, Any] = {"n": 0, "note": "re-run bundle to populate unconstrained stats"}
    if n_candidates:
        all_candidates["n_candidates_reported"] = n_candidates
    payload["event_level"] = {
        "champion_pipeline": dict(ABC_F1_PIPELINE_CHAMPION),
        "abc_f1": {
            "all_candidates": all_candidates,
            "executed_topk": _event_stats_from_pct_values(
                abc_nets, hold_days=ABC_V3_HOLD_DAYS
            ),
            "hold_days": ABC_V3_HOLD_DAYS,
        },
        "c18acc": _c18_event_stats(c18_rows),
    }
    return payload


def _periods_from_legs(legs: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "entry_date": str(lg["entry_date"]),
            "exit_date": str(lg["exit_date"]),
            "stock_id": str(lg["stock_id"]),
        }
        for lg in legs
    ]


def _pct_delta(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)


def select_executed_slot_periods(
    conn: sqlite3.Connection,
    close,
    trade_dates: list[str],
    periods: list[dict[str, Any]],
    *,
    total_capital: float,
    n_slots: int,
    cost_model: dict | None = None,
) -> list[dict[str, Any]]:
    """Replay slot fill logic · return periods actually entered (same rules as simulate_slot_portfolio)."""
    if not trade_dates:
        return []

    slot_cap = total_capital / n_slots
    cash = float(total_capital)
    open_pos: list[_OpenPos] = []
    executed: list[dict[str, Any]] = []

    entries_by_date: dict[str, list[dict[str, Any]]] = {}
    for p in periods:
        ed = str(p.get("entry_date") or "")
        ex = str(p.get("exit_date") or "")
        if not ed or not ex or ed > ex:
            continue
        entries_by_date.setdefault(ed, []).append(p)

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
                cash += _apply_exit_proceeds(
                    pos.allocation, pos.entry_px, exit_px, cost_model=cost_model
                )
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
            entry_fee = _entry_cost(slot_cap, cost_model=cost_model)
            alloc = min(slot_cap, cash - entry_fee)
            if alloc <= 0:
                break
            cash -= alloc + entry_fee
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
            executed.append({**p, "slot_allocation_ntd": round(alloc, 2)})

    return executed


def _c18acc_trade_ledger(
    legs: list[dict[str, Any]],
    *,
    total_capital: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cum_pnl = 0.0
    for i, lg in enumerate(
        sorted(legs, key=lambda x: (x.get("entry_date", ""), x.get("stock_id", ""))),
        1,
    ):
        pnl = float(lg.get("pnl_ntd") or 0)
        cum_pnl += pnl
        ret = lg.get("return_pct")
        bench = lg.get("bench_return_pct")
        excess = (float(ret) - float(bench)) if ret is not None and bench is not None else None
        rows.append(
            {
                "idx": i,
                "stock_id": lg.get("stock_id"),
                "stock_name": lg.get("stock_name", ""),
                "entry_date": lg.get("entry_date"),
                "exit_date": lg.get("exit_date"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "return_pct": ret,
                "bench_return_pct": bench,
                "excess_pct": round(excess, 3) if excess is not None else None,
                "pnl_ntd": round(pnl, 2),
                "cum_return_pct": round(cum_pnl / total_capital * 100.0, 3) if total_capital else None,
                "slot_id": lg.get("slot_id"),
                "exit_reason": lg.get("exit_reason"),
            }
        )
    return rows


def _abc_trade_ledger(
    executed: list[dict[str, Any]],
    *,
    total_capital: float,
    slot_cap: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cum_pnl = 0.0
    for i, row in enumerate(
        sorted(executed, key=lambda x: (x.get("entry_date", ""), x.get("stock_id", ""))),
        1,
    ):
        net = row.get("net_pct")
        alloc = float(row.get("slot_allocation_ntd") or slot_cap)
        pnl = alloc * float(net) / 100.0 if net is not None else 0.0
        cum_pnl += pnl
        rows.append(
            {
                "idx": i,
                "stock_id": row.get("stock_id"),
                "stock_name": row.get("stock_name", ""),
                "entry_date": row.get("entry_date"),
                "exit_date": row.get("exit_date"),
                "entry_minute": row.get("entry_minute"),
                "entry_px": row.get("entry_px"),
                "exit_px": row.get("exit_px"),
                "net_pct": net,
                "excess_pct": row.get("excess_pct"),
                "rank_score": row.get("rank_score"),
                "entry_poll_idx": row.get("entry_poll_idx"),
                "pnl_ntd": round(pnl, 2),
                "cum_return_pct": round(cum_pnl / total_capital * 100.0, 3) if total_capital else None,
                "hold_days": row.get("hold_days"),
            }
        )
    return rows


def _portfolio_block(
    conn: sqlite3.Connection,
    close,
    trade_dates: list[str],
    periods: list[dict[str, Any]],
    *,
    total_capital: float,
    n_slots: int,
    cost_model: dict | None,
) -> dict[str, Any]:
    return simulate_slot_portfolio(
        conn,
        close,
        trade_dates,
        periods,
        total_capital=total_capital,
        n_slots=n_slots,
        cost_model=cost_model,
        return_daily=True,
    )


def build_c18acc_abc_v3_f1_comparison_bundle(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
) -> dict[str, Any]:
    """Run C18acc (3-slot) vs ABC v3+f1 (5-slot top-K) · comparison layer contract."""
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
    c18_total = notional
    abc_total = notional
    c18_slot_cap = c18_total / C18ACC_SLOTS
    abc_slot_cap = abc_total / ABC_F1_SLOTS

    _log("init", f"notional={notional:,.0f} NTD · cost_enabled={cost_model.get('enabled')}")

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 5:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    _log("window", f"{trade_dates[0]} .. {trade_dates[-1]} · n={len(trade_dates)} td")

    t_c18 = time.perf_counter()
    c18_legs, _, _, c18_meta = build_c18acc_s2_executed_legs_for_timeline(
        conn,
        trade_dates,
        n_slots=C18ACC_SLOTS,
        capital_ntd=c18_slot_cap,
        use_close_timing=True,
    )
    _log(
        "c18acc",
        f"legs={len(c18_legs)} · runtime={time.perf_counter() - t_c18:.1f}s",
    )

    c18_periods = _periods_from_legs(c18_legs)
    t_pf = time.perf_counter()
    c18_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        c18_periods,
        total_capital=c18_total,
        n_slots=C18ACC_SLOTS,
        cost_model=cost_model,
    )
    _log("c18acc_portfolio", f"n_trades={c18_pf.get('n_trades')} · {time.perf_counter() - t_pf:.1f}s")

    t_panel = time.perf_counter()
    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=trade_dates
    )
    _log(
        "abc_panel",
        f"frames={len(frames)} · trajectories={len(trajectories)} · "
        f"runtime={time.perf_counter() - t_panel:.1f}s",
    )

    t_abc = time.perf_counter()
    f1_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=trade_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        frames=frames,
        trajectories=trajectories,
        meta=panel_meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    f1_scored = apply_score_spec(f1_cands, spec_id="v0")
    f1_ordered = order_periods_for_fill(f1_scored, fill_mode="topk_score")
    f1_event_all = _unconstrained_leg_stats(f1_cands)
    f1_executed = select_executed_slot_periods(
        conn,
        close,
        trade_dates,
        f1_ordered,
        total_capital=abc_total,
        n_slots=ABC_F1_SLOTS,
        cost_model=cost_model,
    )
    f1_periods = [
        {
            "entry_date": str(r["entry_date"]),
            "exit_date": str(r["exit_date"]),
            "stock_id": str(r["stock_id"]),
            "entry_px": r.get("entry_px"),
        }
        for r in f1_executed
    ]
    abc_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        f1_periods,
        total_capital=abc_total,
        n_slots=ABC_F1_SLOTS,
        cost_model=cost_model,
    )
    f1_event_executed = _unconstrained_leg_stats(f1_executed)
    c18_event = _c18_event_stats(c18_legs)
    _log(
        "abc_f1",
        f"candidates={len(f1_cands)} · executed={len(f1_executed)} · "
        f"event_all={f1_event_all.get('mean_ret_net_pct')}% · "
        f"event_exec={f1_event_executed.get('mean_ret_net_pct')}% · "
        f"runtime={time.perf_counter() - t_abc:.1f}s",
    )

    comparison = {
        "c18acc_total_return_pct": c18_pf.get("total_return_pct"),
        "abc_f1_total_return_pct": abc_pf.get("total_return_pct"),
        "delta_total_return_pp": _pct_delta(c18_pf.get("total_return_pct"), abc_pf.get("total_return_pct")),
        "c18acc_mean_daily_pct": c18_pf.get("mean_daily_return_pct"),
        "abc_f1_mean_daily_pct": abc_pf.get("mean_daily_return_pct"),
        "delta_mean_daily_pp": _pct_delta(
            c18_pf.get("mean_daily_return_pct"), abc_pf.get("mean_daily_return_pct")
        ),
        "c18acc_ann_mean_pct": c18_pf.get("ann_mean_return_pct"),
        "abc_f1_ann_mean_pct": abc_pf.get("ann_mean_return_pct"),
        "delta_ann_mean_pp": _pct_delta(
            c18_pf.get("ann_mean_return_pct"), abc_pf.get("ann_mean_return_pct")
        ),
        "c18acc_cagr_pct": c18_pf.get("cagr_pct"),
        "abc_f1_cagr_pct": abc_pf.get("cagr_pct"),
        "c18acc_sharpe": c18_pf.get("sharpe_ratio"),
        "abc_f1_sharpe": abc_pf.get("sharpe_ratio"),
        "c18acc_n_trades": c18_pf.get("n_trades"),
        "abc_f1_n_trades": abc_pf.get("n_trades"),
        "abc_f1_signal_capture_pct": round(
            100.0 * len(f1_executed) / len(f1_cands), 1
        )
        if f1_cands
        else None,
        "abc_f1_event_all_mean_ret_pct": f1_event_all.get("mean_ret_net_pct"),
        "abc_f1_event_executed_mean_ret_pct": f1_event_executed.get("mean_ret_net_pct"),
        "abc_f1_champion_mean_ret_pct": ABC_F1_PIPELINE_CHAMPION["mean_ret_3d_net_pct"],
        "c18acc_event_mean_ret_pct": c18_event.get("mean_ret_net_pct"),
        "winner_daily": (
            "abc-v3-f1-pullback"
            if float(abc_pf.get("mean_daily_return_pct") or 0)
            > float(c18_pf.get("mean_daily_return_pct") or 0)
            else "rrg-mono-swap-accel"
        ),
        "winner_ann_mean": (
            "abc-v3-f1-pullback"
            if float(abc_pf.get("ann_mean_return_pct") or 0)
            > float(c18_pf.get("ann_mean_return_pct") or 0)
            else "rrg-mono-swap-accel"
        ),
    }

    _log("done", f"total_runtime={time.perf_counter() - t0:.1f}s")

    return {
        "schema": "c18acc_abc_v3_f1_comparison-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
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
        "strategies": {
            "rrg-mono-swap-accel": {
                "variant": "C18acc+S2",
                "n_slots": C18ACC_SLOTS,
                "slot_cap_ntd": c18_slot_cap,
                "total_capital_ntd": c18_total,
                "hold_days": "5-10d + rotation/S2",
                "portfolio": c18_pf,
                "meta": c18_meta,
            },
            "abc-v3-f1-pullback": {
                "matcher": "w3_rv_hl_abc_v3_f1",
                "n_slots": ABC_F1_SLOTS,
                "slot_cap_ntd": abc_slot_cap,
                "total_capital_ntd": abc_total,
                "hold_days": ABC_V3_HOLD_DAYS,
                "fill_mode": "topk_score",
                "score_spec": "v0",
                "n_candidates": len(f1_cands),
                "portfolio": abc_pf,
                "portfolio_validated": False,
            },
        },
        "comparison": comparison,
        "event_level": {
            "champion_pipeline": dict(ABC_F1_PIPELINE_CHAMPION),
            "abc_f1": {
                "all_candidates": f1_event_all,
                "executed_topk": f1_event_executed,
                "hold_days": ABC_V3_HOLD_DAYS,
            },
            "c18acc": c18_event,
        },
        "process_log": process_log,
        "total_runtime_s": round(time.perf_counter() - t0, 1),
        "c18acc_trade_ledger": _c18acc_trade_ledger(c18_legs, total_capital=c18_total),
        "abc_f1_trade_ledger": _abc_trade_ledger(
            f1_executed, total_capital=abc_total, slot_cap=abc_slot_cap
        ),
        "abc_f1_candidate_periods": [
            {
                "stock_id": str(r.get("stock_id") or ""),
                "entry_date": str(r.get("entry_date") or ""),
                "exit_date": str(r.get("exit_date") or ""),
                "entry_px": r.get("entry_px"),
                "entry_frame_idx": r.get("entry_frame_idx"),
                "entry_poll_idx": r.get("entry_poll_idx"),
                "net_pct": r.get("net_pct"),
                "hold_days": r.get("hold_days"),
            }
            for r in f1_cands
        ],
    }


def render_c18acc_abc_v3_f1_comparison_md(payload: dict[str, Any]) -> str:
    payload = enrich_event_level(payload)
    cmp_ = payload.get("comparison") or {}
    contract = payload.get("comparison_contract") or {}
    c18 = (payload.get("strategies") or {}).get("rrg-mono-swap-accel") or {}
    abc = (payload.get("strategies") or {}).get("abc-v3-f1-pullback") or {}
    c18_pf = c18.get("portfolio") or {}
    abc_pf = abc.get("portfolio") or {}
    ev = payload.get("event_level") or {}
    champ = ev.get("champion_pipeline") or {}
    abc_ev = ev.get("abc_f1") or {}
    abc_all = abc_ev.get("all_candidates") or {}
    abc_exec = abc_ev.get("executed_topk") or {}
    c18_ev = ev.get("c18acc") or {}
    hold_days = abc_ev.get("hold_days") or ABC_V3_HOLD_DAYS
    abc_all_n = abc_all.get("n") or abc_all.get("n_candidates_reported") or abc.get("n_candidates") or "—"
    abc_all_ret = abc_all.get("mean_ret_net_pct")
    abc_all_ret_disp = abc_all_ret if abc_all_ret is not None else "—"
    abc_all_note = abc_all.get("note") or "688 信號全計 · 無槽位限制"
    if abc_all_ret is None and abc_all_n not in ("—", 0):
        abc_all_note = f"n={abc_all_n} · 待 bundle 重跑填入 unconstrained 均值"

    lines = [
        "# C18acc vs ABC v3+f1 · 2024 起正式對照",
        "",
        f"生成：`{payload.get('generated_at')}` · 總耗時 **{payload.get('total_runtime_s')}s**",
        "",
        "## 比較契約",
        "",
        f"- 窗口：**{payload.get('date_start')} ~ {payload.get('date_end')}**（{payload.get('n_trade_dates')} 交易日）",
        f"- 本金：**{contract.get('notional_ntd'):,.0f} NTD**（比較層 SSOT · `config/backtest_standard.yaml`）",
        f"- NAV 引擎：`simulate_slot_portfolio`（no-compound · 日內 M2M）",
        f"- 成本：{'已啟用' if (contract.get('cost_model') or {}).get('enabled') else '未啟用'}",
        f"- C18acc：**{c18.get('n_slots')} 槽** · C18acc+S2 · 轮动换仓",
        f"- ABC v3+f1：**{abc.get('n_slots')} 槽** · top-K score · hold {abc.get('hold_days')}d",
        "",
        "> **兩層邏輯不可混讀**：Teaching deck / pipeline 驗證的是 **Event-level（事件層）** 每筆 3 天 α；"
        "下方「組合績效」是 **Portfolio NAV（組合層）** 資金受限模擬。",
        "> ABC v3+f1 **5-slot portfolio val 未過** · event-level 已採納 · 本報告為研究對照。",
        "",
        f"## 事件層績效（ABC hold {hold_days}d · validated edge）",
        "",
        "Teaching deck 引用的 **+2.85% / 勝率 54.7%** 來自 pipeline OOS（90d · n=170），"
        "不是下方組合 NAV 的區間總報酬。",
        "",
        "### ABC v3+f1 · 3 天 event-level",
        "",
        "| 口徑 | n | 每筆均報酬 % | 日均 % | 勝率 % | 中位數 % | 備註 |",
        "|------|---|-------------|--------|--------|---------|------|",
        f"| **Champion · {champ.get('label', 'pipeline OOS')}** | **{champ.get('n')}** | "
        f"**{champ.get('mean_ret_3d_net_pct')}** | "
        f"{round(float(champ.get('mean_ret_3d_net_pct') or 0) / hold_days, 4)} | "
        f"**{champ.get('win_rate_pct')}** | {champ.get('median_ret_3d_net_pct')} | "
        f"`{champ.get('source')}` · val {champ.get('val_mean_ret_3d_net_pct')}% |",
        f"| 本窗口 · 全候選 unconstrained | {abc_all_n} | "
        f"**{abc_all_ret_disp}** | "
        f"{abc_all.get('mean_daily_ret_net_pct', '—')} | "
        f"{abc_all.get('win_rate_pct', '—')} | {abc_all.get('median_ret_net_pct', '—')} | "
        f"{abc_all_note} |",
        f"| 本窗口 · 5 槽 top-K 成交 | {abc_exec.get('n', abc_pf.get('n_trades', '—'))} | "
        f"{abc_exec.get('mean_ret_net_pct', '—')} | "
        f"{abc_exec.get('mean_daily_ret_net_pct', '—')} | "
        f"{abc_exec.get('win_rate_pct', '—')} | {abc_exec.get('median_ret_net_pct', '—')} | "
        f"capture {cmp_.get('abc_f1_signal_capture_pct')}% |",
        "",
        "### C18acc · 參考（非 3 天口徑）",
        "",
        "| 口徑 | n | 每筆均報酬 % | 勝率 % | 備註 |",
        "|------|---|-------------|--------|------|",
        f"| 3 槽實際成交 | {c18_ev.get('n', c18_pf.get('n_trades', '—'))} | "
        f"{c18_ev.get('mean_ret_net_pct', '—')} | {c18_ev.get('win_rate_pct', '—')} | "
        f"{c18_ev.get('hold_note', '5–10d + rotation/S2')} |",
        "",
        "**解讀**：ABC 事件層在本窗口全候選 "
        f"**{abc_all_ret_disp}%**（n={abc_all_n}）"
        " 才是與 teaching deck 同類的「每筆 3 天 α」。"
        "Champion pipeline（90d OOS）已驗證 **+2.845%**。"
        f"5 槽 NAV 僅成交 {cmp_.get('abc_f1_signal_capture_pct')}% 信號、"
        f"資金利用率 {abc_pf.get('avg_utilization_pct')}%，"
        "故組合總報酬遠低於事件層實力。",
        "",
        "## 組合績效（100k NAV · 資金受限 · 可比）",
        "",
        "| 指標 | C18acc（3 槽） | ABC v3+f1（5 槽） | Δ (ABC−C18) |",
        "|------|---------------|------------------|-------------|",
        f"| 區間總報酬 % | {c18_pf.get('total_return_pct')} | {abc_pf.get('total_return_pct')} | {cmp_.get('delta_total_return_pp')} |",
        f"| 日均報酬 % | {c18_pf.get('mean_daily_return_pct')} | {abc_pf.get('mean_daily_return_pct')} | {cmp_.get('delta_mean_daily_pp')} |",
        f"| 年化日均 % (×252) | {c18_pf.get('ann_mean_return_pct')} | {abc_pf.get('ann_mean_return_pct')} | {cmp_.get('delta_ann_mean_pp')} |",
        f"| CAGR % | {c18_pf.get('cagr_pct')} | {abc_pf.get('cagr_pct')} | — |",
        f"| Sharpe | {c18_pf.get('sharpe_ratio')} | {abc_pf.get('sharpe_ratio')} | — |",
        f"| MaxDD % | {c18_pf.get('max_drawdown_pct')} | {abc_pf.get('max_drawdown_pct')} | — |",
        f"| 成交筆數 | {c18_pf.get('n_trades')} | {abc_pf.get('n_trades')} | — |",
        f"| 資金利用率 % | {c18_pf.get('avg_utilization_pct')} | {abc_pf.get('avg_utilization_pct')} | — |",
        "",
        f"**組合層日均較高**：`{cmp_.get('winner_daily')}` · "
        f"**年化日均較高**：`{cmp_.get('winner_ann_mean')}`",
        "",
        f"ABC 信號捕獲率：{cmp_.get('abc_f1_signal_capture_pct')}% "
        f"（候選 {abc.get('n_candidates')} → 成交 {abc_pf.get('n_trades')}）",
        "",
        "## 執行過程紀錄",
        "",
        "| 時間 | 累計秒 | Phase | 說明 |",
        "|------|--------|-------|------|",
    ]
    for row in payload.get("process_log") or []:
        lines.append(
            f"| {row.get('ts')} | {row.get('elapsed_s')} | {row.get('phase')} | {row.get('detail')} |"
        )

    lines.extend(
        [
            "",
            "## C18acc 交易紀錄",
            "",
            f"共 **{len(payload.get('c18acc_trade_ledger') or [])}** 筆（slot 模擬後與 legs 一致）。",
            "",
            "| # | 代碼 | 名稱 | 進場日 | 出場日 | 進場價 | 出場價 | 報酬% | 超額% | PnL NTD | 累計% | 出場原因 |",
            "|---|------|------|--------|--------|--------|--------|-------|-------|---------|-------|----------|",
        ]
    )
    for r in payload.get("c18acc_trade_ledger") or []:
        lines.append(
            f"| {r.get('idx')} | {r.get('stock_id')} | {r.get('stock_name', '')} | "
            f"{r.get('entry_date')} | {r.get('exit_date')} | {r.get('entry_px')} | {r.get('exit_px')} | "
            f"{r.get('return_pct')} | {r.get('excess_pct')} | {r.get('pnl_ntd')} | "
            f"{r.get('cum_return_pct')} | {r.get('exit_reason', '')} |"
        )

    lines.extend(
        [
            "",
            "## ABC v3+f1 交易紀錄（5 槽 top-K 實際成交）",
            "",
            f"共 **{len(payload.get('abc_f1_trade_ledger') or [])}** 筆。",
            "",
            "| # | 代碼 | 名稱 | 進場日 | 出場日 | poll | 進場價 | 淨報酬% | 超額% | score | PnL NTD | 累計% |",
            "|---|------|------|--------|--------|------|--------|---------|-------|-------|---------|-------|",
        ]
    )
    for r in payload.get("abc_f1_trade_ledger") or []:
        lines.append(
            f"| {r.get('idx')} | {r.get('stock_id')} | {r.get('stock_name', '')} | "
            f"{r.get('entry_date')} | {r.get('exit_date')} | {r.get('entry_poll_idx')} | "
            f"{r.get('entry_px')} | {r.get('net_pct')} | {r.get('excess_pct')} | "
            f"{r.get('rank_score')} | {r.get('pnl_ntd')} | {r.get('cum_return_pct')} |"
        )

    lines.extend(
        [
            "",
            "## 模組",
            "",
            "- `scripts/run_c18acc_abc_v3_f1_comparison.py`",
            "- `src/research/backtest/c18acc_abc_v3_f1_comparison.py`",
            "",
            "## 但書",
            "",
            "- **Event-level vs Portfolio**：ABC 採納依據是 event-level OOS（+2.85% / 3d），不是 5 槽 NAV。",
            "- 槽位數不同（3 vs 5）· 依比較層 `notional / n_slots` 等比例部署。",
            "- ABC 5-slot val 未過 · 資金利用率低 · 不可當 slot 策略直接對標 C18acc NAV。",
            "- C18acc 已採納凍結 · `enabled: true` · 有 slot 狀態機 · hold 5–10d + rotation。",
            "- `ann_mean_return_pct` = 日均 × 252（算術年化）· 與 CAGR（幾何）不同。",
        ]
    )
    return "\n".join(lines)
