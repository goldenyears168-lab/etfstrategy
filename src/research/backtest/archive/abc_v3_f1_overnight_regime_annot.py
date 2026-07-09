"""ABC v3+f1 · overnight regime gate · Phase 0 annotation (SOX/SPY · no rule change)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_SLOT_CHAMPION,
    CAPITAL_PER_SLOT_NTD,
    _build_abc_v3_intraday_panel,
    apply_score_spec,
    collect_abc_v3_skip0900_period_candidates,
    order_periods_for_fill,
)
from research.backtest.archive.c18acc_abc_v3_f1_comparison import select_executed_slot_periods
from research.backtest.archive.c18acc_overnight_regime_annot import (
    CASE_DATE,
    IS_END,
    IS_START,
    OOS_START,
    _case_study,
    _risk_off_compare,
    _session_day_summary,
    _signal_compare,
    _sox_decile_summary,
    annotate_period_overnight,
)
from research.backtest.archive.c18acc_overnight_regime_panel import (
    DEFAULT_SOX_OPEN_TRAP_PCT,
    DEFAULT_SOX_RISK_OFF_PCT,
    load_overnight_regime_panel,
    mark_open_trap_days,
)
from research.backtest.archive.c18acc_weekday_annot import _finite_floats, _periods_in_window
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1

C18ACC_OVERNIGHT_BASELINE = {
    "source": "reports/research/rrg/20260707_c18acc_overnight_regime_annot.json",
    "profile": "pool1 · 3-slot",
    "n_legs": 459,
    "signal_compare": {
        "risk_off_sox": {
            "flagged_n": 110,
            "delta_return_pp": -1.2701,
            "delta_excess_pp": -1.8307,
            "h_or_0_pass": True,
        },
        "risk_off_spy": {
            "flagged_n": 58,
            "delta_return_pp": -2.6758,
            "delta_excess_pp": -3.9417,
            "h_or_0_pass": True,
        },
        "combo_risk_off": {
            "flagged_n": 136,
            "delta_return_pp": -0.1159,
            "delta_excess_pp": -0.1546,
            "h_or_0_pass": False,
        },
    },
}


def _ledger_row_to_period(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_id": str(row.get("stock_id") or ""),
        "stock_name": str(row.get("stock_name") or ""),
        "entry_date": str(row.get("entry_date") or ""),
        "exit_date": str(row.get("exit_date") or ""),
        "return_pct": row.get("net_pct"),
        "excess_pct": row.get("excess_pct"),
        "entry_minute": row.get("entry_minute"),
        "entry_px": row.get("entry_px"),
        "exit_px": row.get("exit_px"),
        "hold_days": row.get("hold_days"),
        "rank_score": row.get("rank_score"),
    }


def load_abc_f1_trade_ledger_from_json(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ledger = payload.get("abc_f1_trade_ledger") or []
    if not ledger:
        raise ValueError(f"no abc_f1_trade_ledger in {path}")
    meta = {
        "ledger_source": str(path),
        "date_start": payload.get("date_start"),
        "date_end": payload.get("date_end"),
        "n_trade_dates": payload.get("n_trade_dates"),
        "n_candidates": (payload.get("strategies") or {}).get("abc-v3-f1-pullback", {}).get(
            "n_candidates"
        ),
        "matcher": "w3_rv_hl_abc_v3_f1",
        "n_slots": ABC_V3_SLOT_CHAMPION,
        "hold_days": 3,
        "fill_mode": "topk_score",
    }
    return ledger, meta


def _load_abc_f1_executed_legs_from_db(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    n_slots: int = ABC_V3_SLOT_CHAMPION,
    score_spec: str = "v0",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    dates = [d for d in full_dates if date_start <= d <= date_end]
    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=dates
    )
    candidates, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        frames=frames,
        trajectories=trajectories,
        meta=panel_meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    candidates = apply_score_spec(candidates, spec_id=score_spec)
    ordered = order_periods_for_fill(candidates, fill_mode="topk_score")
    total_capital = CAPITAL_PER_SLOT_NTD * n_slots
    executed = select_executed_slot_periods(
        conn, close, dates, ordered, total_capital=total_capital, n_slots=n_slots
    )
    ledger = [
        {
            "stock_id": r.get("stock_id"),
            "stock_name": r.get("stock_name", ""),
            "entry_date": r.get("entry_date"),
            "exit_date": r.get("exit_date"),
            "entry_minute": r.get("entry_minute"),
            "entry_px": r.get("entry_px"),
            "exit_px": r.get("exit_px"),
            "net_pct": r.get("net_pct"),
            "excess_pct": r.get("excess_pct"),
            "rank_score": r.get("rank_score"),
            "entry_poll_idx": r.get("entry_poll_idx"),
            "hold_days": r.get("hold_days"),
        }
        for r in executed
    ]
    meta = {
        "ledger_source": "db_rebuild",
        "date_start": date_start,
        "date_end": date_end,
        "n_candidates": len(candidates),
        "matcher": "w3_rv_hl_abc_v3_f1",
        "n_slots": n_slots,
        "hold_days": 3,
        "fill_mode": "topk_score",
        "panel_meta": panel_meta,
    }
    return ledger, meta


def _summarize_abc_legs(periods: list[dict[str, Any]]) -> dict[str, Any]:
    rets = _finite_floats([p.get("return_pct") for p in periods])
    exs = _finite_floats([p.get("excess_pct") for p in periods])
    n = len(periods)
    return {
        "n_periods": n,
        "mean_return_pct": round(sum(rets) / len(rets), 4) if rets else None,
        "mean_excess_pct": round(sum(exs) / len(exs), 4) if exs else None,
        "win_rate_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 2) if rets else None,
        "total_return_pct": round(sum(rets), 4) if rets else None,
        "total_excess_pct": round(sum(exs), 4) if exs else None,
    }


def run_abc_v3_f1_overnight_regime_annot(
    conn: sqlite3.Connection,
    *,
    date_start: str = IS_START,
    date_end: str = "2026-07-07",
    ledger: list[dict[str, Any]] | None = None,
    ledger_meta: dict[str, Any] | None = None,
    n_slots: int = ABC_V3_SLOT_CHAMPION,
    case_date: str = CASE_DATE,
    rebuild_panel: bool = False,
) -> dict[str, Any]:
    """Phase 0 · annotate ABC v3+f1 slot-executed legs × SOX/SPY overnight regime."""
    if ledger is None:
        if rebuild_panel:
            ledger, ledger_meta = _load_abc_f1_executed_legs_from_db(
                conn, date_start=date_start, date_end=date_end, n_slots=n_slots
            )
        else:
            raise ValueError("ledger required unless rebuild_panel=True")

    periods = [_ledger_row_to_period(row) for row in ledger]
    if not periods:
        raise ValueError("empty ABC v3+f1 ledger")

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    entry_dates = sorted({str(p["entry_date"]) for p in periods})
    panel_start = min(entry_dates[0], date_start)
    panel_end = max(entry_dates[-1], date_end)
    panel_dates = [d for d in full_dates if panel_start <= d <= panel_end]

    regime_panel = load_overnight_regime_panel(conn, panel_dates)
    ix_bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    ix_daily_ret = ix_bench.pct_change(fill_method=None) * 100.0
    ix_daily_ret.index = ix_daily_ret.index.astype(str)
    open_trap_by_date = mark_open_trap_days(regime_panel, ix_daily_ret)

    legs = [
        annotate_period_overnight(
            p,
            regime_panel=regime_panel,
            open_trap_by_date=open_trap_by_date,
        )
        for p in periods
    ]

    is_periods = _periods_in_window(periods, IS_START, IS_END)
    oos_periods = [p for p in periods if str(p["entry_date"]) >= OOS_START]
    is_legs = _periods_in_window(legs, IS_START, IS_END)
    oos_legs = [lg for lg in legs if lg["entry_date"] >= OOS_START]

    effective_start = None
    if not regime_panel.empty:
        covered = regime_panel["sox_t1_ret_pct"].notna()
        if covered.any():
            effective_start = str(regime_panel.loc[covered, "session_date"].iloc[0])

    meta = dict(ledger_meta or {})
    meta.setdefault("profile", "abc_v3_f1_slot_executed")
    meta.setdefault("timing_mode", "skip09_topk")

    return {
        "schema": "abc_v3_f1_overnight_regime_annot-v1",
        "topic": "abc-v3-f1-overnight-regime-gate",
        "phase": "phase0_annot",
        "date_start": date_start,
        "date_end": date_end,
        "effective_start_sox": effective_start,
        "n_slots": n_slots,
        "matcher": "w3_rv_hl_abc_v3_f1",
        "hold_days": 3,
        "meta": meta,
        "thresholds": {
            "sox_risk_off_pct": DEFAULT_SOX_RISK_OFF_PCT,
            "sox_open_trap_pct": DEFAULT_SOX_OPEN_TRAP_PCT,
        },
        "summary": _summarize_abc_legs(periods),
        "n_legs": len(legs),
        "session_summary": _session_day_summary(regime_panel, ix_daily_ret),
        "signal_compare": _signal_compare(legs),
        "sox_deciles_at_entry": _sox_decile_summary(legs),
        "case_study": _case_study(legs, case_date, regime_panel=regime_panel),
        "c18acc_baseline": C18ACC_OVERNIGHT_BASELINE,
        "splits": {
            "FULL": {
                "n_legs": len(legs),
                "summary": _summarize_abc_legs(periods),
                "h_or_0": _risk_off_compare(legs, flag_field="combo_risk_off"),
                "signal_compare": _signal_compare(legs),
            },
            "IS": {
                "date_start": IS_START,
                "date_end": IS_END,
                "n_legs": len(is_legs),
                "summary": _summarize_abc_legs(is_periods),
                "h_or_0": _risk_off_compare(is_legs, flag_field="combo_risk_off"),
                "signal_compare": _signal_compare(is_legs),
            },
            "OOS": {
                "date_start": OOS_START,
                "date_end": date_end,
                "n_legs": len(oos_legs),
                "summary": _summarize_abc_legs(oos_periods),
                "h_or_0": _risk_off_compare(oos_legs, flag_field="combo_risk_off"),
                "signal_compare": _signal_compare(oos_legs),
            },
        },
        "legs": legs,
    }


def render_abc_v3_f1_overnight_regime_annot_md(payload: dict[str, Any]) -> str:
    meta = payload.get("meta") or {}
    h0 = payload["splits"]["FULL"]["h_or_0"]
    sc = payload["signal_compare"]
    case = payload.get("case_study") or {}
    sess = payload.get("session_summary") or {}
    baseline = payload.get("c18acc_baseline") or {}
    bl_sc = baseline.get("signal_compare") or {}

    lines = [
        "# ABC v3+f1 · overnight regime gate · Phase 0 描述統計",
        "",
        f"區間：**{payload['date_start']}** → **{payload['date_end']}** · "
        f"SOX 有效自 **{payload.get('effective_start_sox', '—')}** · "
        f"**{payload['n_legs']}** legs · {payload['n_slots']} 槽 · hold {payload.get('hold_days')}d",
        "",
        f"- Matcher：`{payload.get('matcher')}` · fill：top-K score · skip 09:00",
        f"- Ledger：`{meta.get('ledger_source', '—')}`",
        f"- mean excess（FULL）：**{payload['summary'].get('mean_excess_pct', '—')}%**",
        f"- RRG 對照基準：**IX0001**（不變）",
        "",
        "## H-OR-0 · combo risk_off 日 vs 非 risk_off（進場日）",
        "",
        f"| 子樣本 | n | mean_return | mean_excess |",
        f"|--------|---|-------------|-------------|",
        f"| risk_off（SOX<{payload['thresholds']['sox_risk_off_pct']}% ∨ FF_OI_Z 極端） | "
        f"{h0['flagged_n']} | {h0['flagged_mean_return_pct']} | {h0['flagged_mean_excess_pct']} |",
        f"| 非 risk_off | {h0['unflagged_n']} | {h0['unflagged_mean_return_pct']} | "
        f"{h0['unflagged_mean_excess_pct']} |",
        "",
        f"**Δ return（risk_off − non）**：{h0['delta_return_pp']} pp · "
        f"**Δ excess**：{h0['delta_excess_pp']} pp · "
        f"H-OR-0 pass：**{h0['h_or_0_pass']}**",
        "",
        "## 訊號對照（進場日 · ABC v3+f1）",
        "",
        "| 訊號 | flagged n | flagged mean_ret | unflagged mean_ret | Δ return | Δ excess | h_or_0 |",
        "|------|-----------|------------------|--------------------|----------|----------|--------|",
    ]
    for key, row in sc.items():
        lines.append(
            f"| {key} | {row['flagged_n']} | {row['flagged_mean_return_pct']} | "
            f"{row['unflagged_mean_return_pct']} | {row['delta_return_pp']} | "
            f"{row['delta_excess_pp']} | {row['h_or_0_pass']} |"
        )

    lines += [
        "",
        "## 對照 · C18acc POOL1 overnight Phase 0（20260707）",
        "",
        f"基線：**{baseline.get('profile', '—')}** · n={baseline.get('n_legs', '—')} · "
        f"source `{baseline.get('source', '—')}`",
        "",
        "| 訊號 | C18acc Δ return | ABC f1 Δ return | C18acc pass | ABC f1 pass |",
        "|------|----------------|-----------------|-------------|-------------|",
    ]
    for key in ("risk_off_sox", "risk_off_spy", "combo_risk_off"):
        bl = bl_sc.get(key) or {}
        ab = sc.get(key) or {}
        lines.append(
            f"| {key} | {bl.get('delta_return_pp', '—')} | {ab.get('delta_return_pp', '—')} | "
            f"{bl.get('h_or_0_pass', '—')} | {ab.get('h_or_0_pass', '—')} |"
        )

    lines += [
        "",
        "## IS / OOS 分層 · risk_off_sox",
        "",
        "| window | n | flagged n | flagged mean_ret | unflagged mean_ret | Δ return |",
        "|--------|---|-----------|------------------|--------------------|----------|",
    ]
    for win in ("IS", "OOS", "FULL"):
        split = payload["splits"][win]
        row = (split.get("signal_compare") or {}).get("risk_off_sox") or {}
        lines.append(
            f"| {win} | {split.get('n_legs')} | {row.get('flagged_n', '—')} | "
            f"{row.get('flagged_mean_return_pct', '—')} | {row.get('unflagged_mean_return_pct', '—')} | "
            f"{row.get('delta_return_pp', '—')} |"
        )

    lines += ["", "## SOX T−1 十分位（進場日）", ""]
    if payload.get("sox_deciles_at_entry"):
        lines += ["| 十分位 | n | mean_return | mean_excess |", "|--------|---|-------------|-------------|"]
        for row in payload["sox_deciles_at_entry"]:
            lines.append(
                f"| {row['decile']} | {row['n']} | {row['mean_return_pct']} | {row['mean_excess_pct']} |"
            )
    else:
        lines.append("_樣本不足_")

    reg = case.get("regime") or {}
    lines += [
        "",
        f"## Case study · {case.get('case_date', '—')}",
        "",
        f"- SOX T−1：**{reg.get('sox_t1_ret_pct')}**% · SPY T−1：**{reg.get('spy_t1_ret_pct')}**% · "
        f"IX T−1：**{reg.get('ix_t1_spread_pct')}**%",
        f"- R0 進場數：**{case.get('n_entries_r0', 0)}** · sum_return：**{case.get('sum_return_pct_if_entered')}**%",
        f"- 虛擬 E1（SOX gate）擋下：**{case.get('virtual_block_e1_sox')}** · "
        f"E7（SOX 大漲擋進）：**{case.get('virtual_block_e7_sox_up')}**",
        "",
        "## Session 統計",
        "",
        f"- 交易日：**{sess.get('n_sessions')}** · combo risk_off 日：**{sess.get('n_combo_risk_off')}**",
        f"- open_trap 日：**{sess.get('n_open_trap_days')}**",
        "",
        "## 解讀備註",
        "",
        "- Phase 0 僅標記 · 不改 ABC v3+f1 規則。",
        "- Legs = 5 槽 top-K 實際成交（與 RH50 gate / comparison 同口徑）。",
        "- `open_trap_day` 含當日 IX 收盤 · 不可直接當可執行 gate。",
        "",
    ]
    return "\n".join(lines)
