"""C18acc · overnight regime gate · Phase 0 annotation (no rule change)."""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_overnight_regime_panel import (
    DEFAULT_SOX_OPEN_TRAP_PCT,
    DEFAULT_SOX_RISK_OFF_PCT,
    load_overnight_regime_panel,
    mark_open_trap_days,
    regime_row_at,
)
from research.backtest.archive.c18acc_weekday_annot import (
    _finite_floats,
    _load_periods_pool1,
    _periods_in_window,
    _summarize_safe,
)
from research.backtest.finpilot_local_backtest import load_price_panels

IS_START = "2024-01-01"
IS_END = "2025-12-31"
OOS_START = "2026-01-01"
CASE_DATE = "2026-07-07"


def _risk_off_compare(
    legs: list[dict[str, Any]],
    *,
    flag_field: str = "combo_risk_off",
) -> dict[str, Any]:
    on = [lg for lg in legs if lg.get(flag_field)]
    off = [lg for lg in legs if not lg.get(flag_field)]

    def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
        vals = _finite_floats([r.get(key) for r in rows])
        return round(sum(vals) / len(vals), 4) if vals else None

    on_ret = _mean(on, "return_pct")
    off_ret = _mean(off, "return_pct")
    on_ex = _mean(on, "excess_pct")
    off_ex = _mean(off, "excess_pct")
    delta_ret = round(on_ret - off_ret, 4) if on_ret is not None and off_ret is not None else None
    delta_ex = round(on_ex - off_ex, 4) if on_ex is not None and off_ex is not None else None

    total_ret = sum(_finite_floats([lg.get("return_pct") for lg in legs]))
    on_ret_sum = sum(_finite_floats([lg.get("return_pct") for lg in on]))
    share = round(on_ret_sum / total_ret * 100, 2) if total_ret else None

    return {
        "flag_field": flag_field,
        "flagged_n": len(on),
        "unflagged_n": len(off),
        "flagged_mean_return_pct": on_ret,
        "unflagged_mean_return_pct": off_ret,
        "delta_return_pp": delta_ret,
        "flagged_mean_excess_pct": on_ex,
        "unflagged_mean_excess_pct": off_ex,
        "delta_excess_pp": delta_ex,
        "flagged_pnl_share_of_total_return_pct": share,
        "h_or_0_pass": (
            delta_ret is not None
            and len(on) >= 20
            and delta_ret <= -0.2
        ),
    }


def _sox_decile_summary(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vals = [(lg, lg.get("sox_t1_at_entry")) for lg in legs if lg.get("sox_t1_at_entry") is not None]
    if len(vals) < 10:
        return []
    pairs = [(lg, float(v)) for lg, v in vals if v == v]
    if len(pairs) < 10:
        return []
    sox_vals = pd.Series([v for _, v in pairs])
    try:
        labels = pd.qcut(sox_vals, 10, duplicates="drop")
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for label in labels.cat.categories:
        sub = [lg for (lg, _), b in zip(pairs, labels, strict=True) if b == label]
        rets = _finite_floats([lg.get("return_pct") for lg in sub])
        exs = _finite_floats([lg.get("excess_pct") for lg in sub])
        out.append(
            {
                "decile": str(label),
                "n": len(sub),
                "mean_return_pct": round(sum(rets) / len(rets), 4) if rets else None,
                "mean_excess_pct": round(sum(exs) / len(exs), 4) if exs else None,
            }
        )
    return out


def annotate_period_overnight(
    period: dict[str, Any],
    *,
    regime_panel: pd.DataFrame,
    open_trap_by_date: pd.Series,
) -> dict[str, Any]:
    entry = str(period["entry_date"])
    sig = str(period.get("signal_date") or entry)
    reg = regime_row_at(regime_panel, entry) or {}

    return {
        "stock_id": str(period["stock_id"]),
        "stock_name": str(period.get("stock_name") or ""),
        "signal_date": sig,
        "entry_date": entry,
        "exit_date": str(period["exit_date"]),
        "exit_reason": str(period.get("exit_reason") or ""),
        "return_pct": period.get("return_pct"),
        "excess_pct": period.get("excess_pct"),
        "beat_bench": period.get("beat_bench"),
        "sox_t1_at_entry": reg.get("sox_t1_ret_pct"),
        "spy_t1_at_entry": reg.get("spy_t1_ret_pct"),
        "ix_t1_at_entry": reg.get("ix_t1_spread_pct"),
        "tsm_t1_at_entry": reg.get("tsm_t1_ret_pct"),
        "ff_oi_z_at_entry": reg.get("ff_oi_z"),
        "tx_gap_at_entry": reg.get("tx_gap_pct"),
        "us_trade_date": reg.get("us_trade_date"),
        "combo_risk_off": bool(reg.get("combo_risk_off")),
        "risk_off_sox": bool(reg.get("risk_off_sox")),
        "risk_off_spy": bool(reg.get("risk_off_spy")),
        "risk_off_ix": bool(reg.get("risk_off_ix")),
        "ff_oi_extreme_short": bool(reg.get("ff_oi_extreme_short")),
        "open_trap_proxy_sox_up": bool(reg.get("open_trap_proxy_sox_up")),
        "open_trap_day": bool(open_trap_by_date.get(entry, False)),
    }


def _session_day_summary(
    regime_panel: pd.DataFrame,
    ix_daily_ret_pct: pd.Series,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for _, r in regime_panel.iterrows():
        sd = str(r["session_date"])
        ix = ix_daily_ret_pct.get(sd)
        rows.append(
            {
                "session_date": sd,
                "sox_t1_ret_pct": r.get("sox_t1_ret_pct"),
                "ix_day_ret_pct": round(float(ix), 4) if ix is not None and ix == ix else None,
                "combo_risk_off": bool(r.get("combo_risk_off")),
                "open_trap_proxy_sox_up": bool(r.get("open_trap_proxy_sox_up")),
            }
        )
    df = pd.DataFrame(rows)
    trap = df[df["open_trap_proxy_sox_up"] & df["ix_day_ret_pct"].notna() & (df["ix_day_ret_pct"] <= -1.0)]
    return {
        "n_sessions": len(df),
        "n_combo_risk_off": int(df["combo_risk_off"].sum()),
        "n_open_trap_days": int(len(trap)),
        "open_trap_dates": trap["session_date"].tolist()[:20],
    }


def _case_study(
    legs: list[dict[str, Any]],
    case_date: str,
    *,
    regime_panel: pd.DataFrame,
) -> dict[str, Any]:
    day_legs = [lg for lg in legs if lg["entry_date"] == case_date]
    reg = regime_row_at(regime_panel, case_date) or {}
    blocked_e1 = bool(reg.get("risk_off_sox"))
    blocked_e6 = bool(reg.get("combo_risk_off"))
    blocked_e7 = bool(reg.get("open_trap_proxy_sox_up"))
    saved_ret = sum(_finite_floats([lg.get("return_pct") for lg in day_legs]))
    return {
        "case_date": case_date,
        "regime": reg,
        "n_entries_r0": len(day_legs),
        "entries": [
            {
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name"),
                "return_pct": lg.get("return_pct"),
                "excess_pct": lg.get("excess_pct"),
            }
            for lg in day_legs
        ],
        "sum_return_pct_if_entered": round(saved_ret, 4) if day_legs else 0.0,
        "virtual_block_e1_sox": blocked_e1,
        "virtual_block_e6_combo": blocked_e6,
        "virtual_block_e7_sox_up": blocked_e7,
        "note": (
            "E1/E6 block new entry · E7 blocks SOX T-1 > "
            f"{DEFAULT_SOX_OPEN_TRAP_PCT}% · actual IX day return see session summary"
        ),
    }


def _signal_compare(legs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "combo_risk_off": _risk_off_compare(legs, flag_field="combo_risk_off"),
        "risk_off_sox": _risk_off_compare(legs, flag_field="risk_off_sox"),
        "risk_off_spy": _risk_off_compare(legs, flag_field="risk_off_spy"),
        "risk_off_ix": _risk_off_compare(legs, flag_field="risk_off_ix"),
        "open_trap_proxy": _risk_off_compare(legs, flag_field="open_trap_proxy_sox_up"),
        "open_trap_day": _risk_off_compare(legs, flag_field="open_trap_day"),
    }


def run_c18acc_overnight_regime_annot(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2020-01-02",
    date_end: str = "2026-07-07",
    n_slots: int = 3,
    case_date: str = CASE_DATE,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(dates) < 2:
        raise ValueError(f"need ≥2 trade dates in [{date_start}, {date_end}]")

    periods, summary, meta = _load_periods_pool1(conn, dates, n_slots=n_slots)
    regime_panel = load_overnight_regime_panel(conn, dates)
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

    return {
        "schema": "c18acc_overnight_regime_annot-v1",
        "topic": "c18acc-overnight-regime-gate",
        "phase": "phase0_annot",
        "date_start": date_start,
        "date_end": date_end,
        "effective_start_sox": effective_start,
        "n_slots": n_slots,
        "profile": "pool1",
        "meta": meta,
        "thresholds": {
            "sox_risk_off_pct": DEFAULT_SOX_RISK_OFF_PCT,
            "sox_open_trap_pct": DEFAULT_SOX_OPEN_TRAP_PCT,
        },
        "summary": _summarize_safe(periods),
        "n_legs": len(legs),
        "session_summary": _session_day_summary(regime_panel, ix_daily_ret),
        "signal_compare": _signal_compare(legs),
        "sox_deciles_at_entry": _sox_decile_summary(legs),
        "case_study": _case_study(legs, case_date, regime_panel=regime_panel),
        "splits": {
            "FULL": {
                "n_legs": len(legs),
                "summary": _summarize_safe(periods),
                "h_or_0": _risk_off_compare(legs, flag_field="combo_risk_off"),
            },
            "IS": {
                "date_start": IS_START,
                "date_end": IS_END,
                "n_legs": len(is_legs),
                "summary": _summarize_safe(is_periods),
                "h_or_0": _risk_off_compare(is_legs, flag_field="combo_risk_off"),
            },
            "OOS": {
                "date_start": OOS_START,
                "date_end": date_end,
                "n_legs": len(oos_legs),
                "summary": _summarize_safe(oos_periods),
                "h_or_0": _risk_off_compare(oos_legs, flag_field="combo_risk_off"),
            },
        },
        "legs": legs,
    }


def render_c18acc_overnight_regime_annot_md(payload: dict[str, Any]) -> str:
    meta = payload.get("meta") or {}
    h0 = payload["splits"]["FULL"]["h_or_0"]
    sc = payload["signal_compare"]
    case = payload.get("case_study") or {}
    sess = payload.get("session_summary") or {}

    lines = [
        "# C18acc · overnight regime gate · Phase 0 描述統計",
        "",
        f"區間：**{payload['date_start']}** → **{payload['date_end']}** · "
        f"SOX 有效自 **{payload.get('effective_start_sox', '—')}** · "
        f"**{payload['n_legs']}** legs · {payload['n_slots']} 槽",
        "",
        f"- Pool：`{meta.get('candidate_pool', '—')}` · timing：`{meta.get('timing_mode', '—')}`",
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
        f"- risk_off 日 PnL 占總 return：**{h0['flagged_pnl_share_of_total_return_pct']}**%",
        "",
        "## 訊號對照（進場日）",
        "",
        "| 訊號 | flagged n | flagged mean_ret | unflagged mean_ret | Δ return |",
        "|------|-----------|------------------|--------------------|----------|",
    ]
    for key, row in sc.items():
        lines.append(
            f"| {key} | {row['flagged_n']} | {row['flagged_mean_return_pct']} | "
            f"{row['unflagged_mean_return_pct']} | {row['delta_return_pp']} |"
        )

    lines += [
        "",
        "## SOX T−1 十分位（進場日）",
        "",
    ]
    if payload.get("sox_deciles_at_entry"):
        lines += ["| 十分位 | n | mean_return | mean_excess |", "|--------|---|-------------|-------------|"]
        for row in payload["sox_deciles_at_entry"]:
            lines.append(
                f"| {row['decile']} | {row['n']} | {row['mean_return_pct']} | {row['mean_excess_pct']} |"
            )
    else:
        lines.append("_樣本不足或未覆蓋 SOX_")

    reg = case.get("regime") or {}
    lines += [
        "",
        f"## Case study · {case.get('case_date', '—')}",
        "",
        f"- SOX T−1：**{reg.get('sox_t1_ret_pct')}**% · IX T−1：**{reg.get('ix_t1_spread_pct')}**% · "
        f"FF_OI_Z：**{reg.get('ff_oi_z')}**",
        f"- R0 進場數：**{case.get('n_entries_r0', 0)}** · 若全進 sum_return：**{case.get('sum_return_pct_if_entered')}**%",
        f"- 虛擬 E1（SOX gate）擋下：**{case.get('virtual_block_e1_sox')}** · "
        f"E6 combo：**{case.get('virtual_block_e6_combo')}** · "
        f"E7（SOX 大漲擋進）：**{case.get('virtual_block_e7_sox_up')}**",
        "",
        "### 當日進場 leg",
        "",
    ]
    entries = case.get("entries") or []
    if entries:
        lines += ["| 代號 | 名稱 | return | excess |", "|------|------|--------|--------|"]
        for e in entries:
            lines.append(
                f"| {e.get('stock_id')} | {e.get('stock_name', '')} | "
                f"{e.get('return_pct')} | {e.get('excess_pct')} |"
            )
    else:
        lines.append("_當日無新進場 leg（或資料未覆蓋）_")

    lines += [
        "",
        "## Session 統計",
        "",
        f"- 交易日：**{sess.get('n_sessions')}** · combo risk_off 日：**{sess.get('n_combo_risk_off')}**",
        f"- open_trap 日（SOX T−1>阈值 且 當日 IX≤−1%）：**{sess.get('n_open_trap_days')}**",
        "",
        "## 解讀備註",
        "",
        "- 本報告 **不改** POOL1 / S2 規則 · 僅標記隔夜環境與 leg 損益。",
        "- `open_trap_day` 含當日 IX 收盤 · **僅 Phase 0** · 不可直接當可執行 gate。",
        "- Phase 1+ 才引入 overnight regime gate sweep。",
        "",
    ]
    return "\n".join(lines)
