"""C18acc+S2 · weekday execution annotation (Phase 0 · no rule change)."""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls
from typing import Any, Literal

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import WEEKDAY_ZH, _summarize
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c
from rrg_rotation import compute_rrg_panel

TimingProfile = Literal["timeline", "pool1"]
IS_START = "2024-01-01"
IS_END = "2025-12-31"
OOS_START = "2026-01-01"


def _iso_weekday(iso: str) -> int:
    return date_cls.fromisoformat(iso).weekday()


def _finite_floats(values: list[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:
            out.append(f)
    return out


def _summarize_safe(periods: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _summarize(periods)
    ex = _finite_floats([p.get("excess_pct") for p in periods])
    rets = _finite_floats([p.get("return_pct") for p in periods])
    summary["mean_excess_pct"] = round(sum(ex) / len(ex), 4) if ex else None
    summary["mean_return_pct"] = round(sum(rets) / len(rets), 4) if rets else None
    summary["total_return_pct"] = round(sum(rets), 4) if rets else None
    summary["total_excess_pct"] = round(sum(ex), 4) if ex else None
    return summary


def _periods_in_window(periods: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    return [p for p in periods if start <= str(p["entry_date"]) <= end]


def _compute_weekend_gap_pct(
    *,
    stock_id: str,
    entry_date: str,
    exit_date: str,
    full_dates: list[str],
    close,
    opn,
) -> tuple[bool, float | None, int]:
    """Return held_over_weekend, sum gap ret %, n_weekends."""
    idx = {d: i for i, d in enumerate(full_dates)}
    if entry_date not in idx or exit_date not in idx:
        return False, None, 0
    i0, i1 = idx[entry_date], idx[exit_date]
    if i1 <= i0:
        return False, 0.0, 0

    gap_sum = 0.0
    n_weekends = 0
    held = False
    for i in range(i0, i1):
        fri = full_dates[i]
        if _iso_weekday(fri) != 4:
            continue
        if i + 1 >= len(full_dates):
            continue
        mon = full_dates[i + 1]
        if mon > exit_date:
            continue
        held = True
        n_weekends += 1
        try:
            fri_close = float(close.at[fri, stock_id])
            mon_open = float(opn.at[mon, stock_id])
        except (KeyError, TypeError, ValueError):
            continue
        if fri_close <= 0 or mon_open <= 0:
            continue
        gap_sum += (mon_open / fri_close - 1.0) * 100.0

    if not held:
        return False, None, 0
    return True, round(gap_sum, 4), n_weekends


def annotate_period_weekday(
    period: dict[str, Any],
    *,
    full_dates: list[str],
    close,
    opn,
) -> dict[str, Any]:
    sig = str(period.get("signal_date") or period["entry_date"])
    entry = str(period["entry_date"])
    exit_d = str(period["exit_date"])
    exit_reason = str(period.get("exit_reason") or "")
    is_swap = exit_reason == "score_swap"

    held, gap_pct, n_wk = _compute_weekend_gap_pct(
        stock_id=str(period["stock_id"]),
        entry_date=entry,
        exit_date=exit_d,
        full_dates=full_dates,
        close=close,
        opn=opn,
    )
    lag = _trading_days_between(full_dates, sig, entry)

    row = {
        "stock_id": str(period["stock_id"]),
        "stock_name": str(period.get("stock_name") or ""),
        "signal_date": sig,
        "entry_date": entry,
        "exit_date": exit_d,
        "exit_reason": exit_reason,
        "is_swap_exit": is_swap,
        "return_pct": period.get("return_pct"),
        "excess_pct": period.get("excess_pct"),
        "beat_bench": period.get("beat_bench"),
        "gross_win": period.get("gross_win"),
        "bench_return_pct": period.get("bench_return_pct"),
        "signal_to_entry_lag_days": lag,
        "held_over_weekend": held,
        "weekend_gap_ret_pct": gap_pct,
        "n_weekends_spanned": n_wk,
        "signal_weekday": _iso_weekday(sig),
        "signal_weekday_zh": WEEKDAY_ZH[_iso_weekday(sig)],
        "entry_weekday": _iso_weekday(entry),
        "entry_weekday_zh": WEEKDAY_ZH[_iso_weekday(entry)],
        "exit_weekday": _iso_weekday(exit_d),
        "exit_weekday_zh": WEEKDAY_ZH[_iso_weekday(exit_d)],
        "swap_weekday": _iso_weekday(exit_d) if is_swap else None,
        "swap_weekday_zh": WEEKDAY_ZH[_iso_weekday(exit_d)] if is_swap else None,
    }
    return row


def _weekday_bucket_summary(
    legs: list[dict[str, Any]],
    field: str,
) -> dict[str, dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {i: [] for i in range(5)}
    for lg in legs:
        wd = lg.get(field)
        if isinstance(wd, int) and wd in buckets:
            buckets[wd].append(lg)
    out: dict[str, dict[str, Any]] = {}
    for wd in range(5):
        sub = buckets[wd]
        s = _summarize_safe(sub)
        out[WEEKDAY_ZH[wd]] = {
            "weekday": wd,
            "weekday_zh": WEEKDAY_ZH[wd],
            **s,
        }
    return out


def _slot_utilization_by_weekday(
    legs: list[dict[str, Any]],
    trade_dates: list[str],
) -> dict[str, dict[str, Any]]:
    by_wd: dict[int, list[float]] = {i: [] for i in range(5)}
    for d in trade_dates:
        wd = _iso_weekday(d)
        n = sum(1 for lg in legs if lg["entry_date"] <= d <= lg["exit_date"])
        by_wd[wd].append(float(n))
    out: dict[str, dict[str, Any]] = {}
    for wd in range(5):
        vals = by_wd[wd]
        out[WEEKDAY_ZH[wd]] = {
            "weekday": wd,
            "weekday_zh": WEEKDAY_ZH[wd],
            "n_trade_days": len(vals),
            "mean_slots_used": round(sum(vals) / len(vals), 3) if vals else None,
            "max_slots_used": max(vals) if vals else None,
        }
    return out


def _friday_vs_non_friday(periods: list[dict[str, Any]], field: str = "entry_weekday") -> dict[str, Any]:
    fri = [p for p in periods if p.get(field) == 4]
    other = [p for p in periods if p.get(field) != 4]

    def _mean_excess(rows: list[dict[str, Any]]) -> float | None:
        vals = _finite_floats([p.get("excess_pct") for p in rows])
        return round(sum(vals) / len(vals), 4) if vals else None

    fri_ex = _mean_excess(fri)
    other_ex = _mean_excess(other)
    delta = round(fri_ex - other_ex, 4) if fri_ex is not None and other_ex is not None else None
    return {
        "field": field,
        "friday_n": len(fri),
        "non_friday_n": len(other),
        "friday_mean_excess_pct": fri_ex,
        "non_friday_mean_excess_pct": other_ex,
        "delta_excess_pp": delta,
        "h_wd_0_pass": (
            delta is not None
            and len(fri) >= 30
            and delta >= 0.2
        ),
    }


def _weekend_exposure_summary(legs: list[dict[str, Any]]) -> dict[str, Any]:
    held = [lg for lg in legs if lg.get("held_over_weekend")]
    gaps = [lg["weekend_gap_ret_pct"] for lg in held if lg.get("weekend_gap_ret_pct") is not None]
    ex_vals = _finite_floats([lg.get("excess_pct") for lg in legs])
    total_excess = sum(ex_vals) if ex_vals else 0.0
    gap_total = sum(gaps) if gaps else 0.0
    share = round(gap_total / total_excess * 100, 2) if total_excess else None
    return {
        "n_legs_held_over_weekend": len(held),
        "pct_legs_held_over_weekend": round(len(held) / len(legs) * 100, 2) if legs else None,
        "mean_weekend_gap_ret_pct": round(sum(gaps) / len(gaps), 4) if gaps else None,
        "sum_weekend_gap_ret_pct": round(gap_total, 4) if gaps else None,
        "weekend_gap_share_of_total_excess_pct": share,
    }


def _load_periods_timeline(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int,
    use_close_timing: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    from dataclasses import replace

    from research.backtest.c18acc_rotation_selective_exit_sweep import (
        rotation_selective_exit_sweep_configs,
    )
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
    from research.backtest.rrg_mono_score_swap_c import (
        champion_score_swap_c_config,
        close_champion_score_swap_c_config,
    )

    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s2_base = configs["S2"]
    if not use_close_timing:
        base = champion_score_swap_c_config()
        s2_base = replace(
            s2_base,
            timing_mode=base.timing_mode,
            entry_price_mode=base.entry_price_mode,
        )
    cfg = replace(s2_base, n_slots=max(1, int(n_slots)))

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
    )
    meta = {
        "profile": "timeline",
        "candidate_pool": cfg.candidate_pool,
        "timing_mode": cfg.timing_mode,
        "variant_id": cfg.variant_id,
        "n_slots": cfg.n_slots,
        "parent_variant_id": close_champion_score_swap_c_config().variant_id,
    }
    return [dict(p) for p in periods], summary, meta


def _load_periods_pool1(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    from dataclasses import replace

    from research.backtest.c18acc_rotation_selective_exit_sweep import (
        rotation_selective_exit_sweep_configs,
    )
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
    from research.backtest.rrg_mono_score_swap_c import (
        champion_score_swap_c_config,
        build_daily_spread_rs_panel,
    )
    from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar

    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s2_base = configs["S2"]
    champ = champion_score_swap_c_config()
    cfg = replace(
        s2_base,
        timing_mode=champ.timing_mode,
        candidate_pool="fresh_union_accel",
        variant_id="POOL1-P5M",
        n_slots=max(1, int(n_slots)),
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        accel_sell_bypass_on_spread_lost=champ.accel_sell_bypass_on_spread_lost,
    )
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        mono_by_date=mono_by_date,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        spread_rs_panel=spread_rs_panel,
        entry_c_config=c0_cfg,
    )
    meta = {
        "profile": "pool1",
        "candidate_pool": cfg.candidate_pool,
        "timing_mode": cfg.timing_mode,
        "variant_id": cfg.variant_id,
        "n_slots": cfg.n_slots,
    }
    return [dict(p) for p in periods], summary, meta


def run_c18acc_weekday_annot(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2020-01-02",
    date_end: str = "2026-07-03",
    n_slots: int = 3,
    profile: TimingProfile = "timeline",
    use_close_timing: bool = True,
) -> dict[str, Any]:
    close, opn, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(dates) < 2:
        raise ValueError(f"need ≥2 trade dates in [{date_start}, {date_end}]")

    if profile == "pool1":
        periods, summary, meta = _load_periods_pool1(conn, dates, n_slots=n_slots)
    else:
        periods, summary, meta = _load_periods_timeline(
            conn,
            dates,
            n_slots=n_slots,
            use_close_timing=use_close_timing,
        )

    legs = [
        annotate_period_weekday(p, full_dates=full_dates, close=close, opn=opn)
        for p in periods
    ]

    is_periods = _periods_in_window(periods, IS_START, IS_END)
    oos_periods = [p for p in periods if str(p["entry_date"]) >= OOS_START]
    is_legs = _periods_in_window(legs, IS_START, IS_END)
    oos_legs = [lg for lg in legs if lg["entry_date"] >= OOS_START]

    swap_legs = [lg for lg in legs if lg["is_swap_exit"]]

    return {
        "schema": "c18acc_weekday_annot-v1",
        "topic": "c18acc-weekday-execution",
        "phase": "phase0_annot",
        "date_start": date_start,
        "date_end": date_end,
        "n_slots": n_slots,
        "profile": profile,
        "meta": meta,
        "summary": _summarize_safe(periods),
        "n_legs": len(legs),
        "n_swap_exits": len(swap_legs),
        "splits": {
            "FULL": {
                "n_legs": len(legs),
                "summary": _summarize_safe(periods),
                "h_wd_0_entry": _friday_vs_non_friday(legs, "entry_weekday"),
                "h_wd_0_signal": _friday_vs_non_friday(legs, "signal_weekday"),
            },
            "IS": {
                "date_start": IS_START,
                "date_end": IS_END,
                "n_legs": len(is_legs),
                "summary": _summarize_safe(is_periods),
                "h_wd_0_entry": _friday_vs_non_friday(is_legs, "entry_weekday"),
            },
            "OOS": {
                "date_start": OOS_START,
                "date_end": date_end,
                "n_legs": len(oos_legs),
                "summary": _summarize_safe(oos_periods),
                "h_wd_0_entry": _friday_vs_non_friday(oos_legs, "entry_weekday"),
            },
        },
        "by_signal_weekday": _weekday_bucket_summary(legs, "signal_weekday"),
        "by_entry_weekday": _weekday_bucket_summary(legs, "entry_weekday"),
        "by_exit_weekday": _weekday_bucket_summary(legs, "exit_weekday"),
        "by_swap_weekday": _weekday_bucket_summary(swap_legs, "swap_weekday"),
        "weekend_exposure": _weekend_exposure_summary(legs),
        "slot_utilization_by_weekday": _slot_utilization_by_weekday(legs, dates),
        "legs": legs,
    }


def render_c18acc_weekday_annot_md(payload: dict[str, Any]) -> str:
    meta = payload.get("meta") or {}
    full = payload["splits"]["FULL"]
    h0 = full["h_wd_0_entry"]
    wknd = payload["weekend_exposure"]
    lines = [
        "# C18acc+S2 · weekday execution · Phase 0 描述統計",
        "",
        f"區間：**{payload['date_start']}** → **{payload['date_end']}** · "
        f"profile **{payload['profile']}** · **{payload['n_legs']}** legs · "
        f"{payload['n_slots']} 槽",
        "",
        f"- Pool：`{meta.get('candidate_pool', '—')}` · timing：`{meta.get('timing_mode', '—')}`",
        f"- Swap exits：**{payload['n_swap_exits']}** · "
        f"mean excess（FULL）：**{payload['summary'].get('mean_excess_pct', '—')}%**",
        "",
        "## H-WD-0 · 週五 vs 非週五（entry_weekday）",
        "",
        f"| 子樣本 | n | mean_excess |",
        f"|--------|---|-------------|",
        f"| 週五進場 | {h0['friday_n']} | {h0['friday_mean_excess_pct']} |",
        f"| 非週五進場 | {h0['non_friday_n']} | {h0['non_friday_mean_excess_pct']} |",
        "",
        f"**Δ excess（Fri − non-Fri）**：{h0['delta_excess_pp']} pp · "
        f"H-WD-0 pass：**{h0['h_wd_0_pass']}**",
        "",
        "## 依訊號日星期（signal_weekday）",
        "",
        "| 星期 | n | 勝率 vs 基準 | 均超額 |",
        "|------|---|-------------|--------|",
    ]
    ranked = sorted(
        payload["by_signal_weekday"].values(),
        key=lambda r: r.get("mean_excess_pct") or -9999,
        reverse=True,
    )
    for row in ranked:
        lines.append(
            f"| {row['weekday_zh']} | {row.get('n_periods', 0)} | "
            f"{row.get('win_rate_vs_bench_pct', '—')}% | "
            f"{row.get('mean_excess_pct', '—')}% |"
        )

    lines += [
        "",
        "## 依進場日星期（entry_weekday）",
        "",
        "| 星期 | n | 勝率 vs 基準 | 均超額 |",
        "|------|---|-------------|--------|",
    ]
    ranked_e = sorted(
        payload["by_entry_weekday"].values(),
        key=lambda r: r.get("mean_excess_pct") or -9999,
        reverse=True,
    )
    for row in ranked_e:
        lines.append(
            f"| {row['weekday_zh']} | {row.get('n_periods', 0)} | "
            f"{row.get('win_rate_vs_bench_pct', '—')}% | "
            f"{row.get('mean_excess_pct', '—')}% |"
        )

    lines += [
        "",
        "## 依出場日星期（exit_weekday）",
        "",
        "| 星期 | n | 均超額 |",
        "|------|---|--------|",
    ]
    for row in payload["by_exit_weekday"].values():
        lines.append(
            f"| {row['weekday_zh']} | {row.get('n_periods', 0)} | "
            f"{row.get('mean_excess_pct', '—')}% |"
        )

    if payload["n_swap_exits"]:
        lines += [
            "",
            "## 換倉日（swap exit · score_swap）",
            "",
            "| 星期 | n | 均超額 |",
            "|------|---|--------|",
        ]
        for row in payload["by_swap_weekday"].values():
            if row.get("n_periods", 0):
                lines.append(
                    f"| {row['weekday_zh']} | {row.get('n_periods', 0)} | "
                    f"{row.get('mean_excess_pct', '—')}% |"
                )

    lines += [
        "",
        "## 週末曝險（weekend exposure）",
        "",
        f"- 持倉過週末 leg 數：**{wknd['n_legs_held_over_weekend']}** "
        f"（{wknd['pct_legs_held_over_weekend']}%）",
        f"- 週末 gap 均值：**{wknd['mean_weekend_gap_ret_pct']}**% · "
        f"加總：**{wknd['sum_weekend_gap_ret_pct']}**%",
        f"- gap 占總超額比例：**{wknd['weekend_gap_share_of_total_excess_pct']}**%",
        "",
        "## 槽位占用 by weekday（交易日均值）",
        "",
        "| 星期 | 交易日數 | 平均占用槽 | 峰值 |",
        "|------|---------|-----------|------|",
    ]
    for row in payload["slot_utilization_by_weekday"].values():
        lines.append(
            f"| {row['weekday_zh']} | {row.get('n_trade_days', 0)} | "
            f"{row.get('mean_slots_used', '—')} | {row.get('max_slots_used', '—')} |"
        )

    sig_h0 = full["h_wd_0_signal"]
    lines += [
        "",
        "## 解讀備註",
        "",
        f"- signal_weekday Fri−nonFri Δ：**{sig_h0.get('delta_excess_pp')}** pp（描述用 · 非可執行規則）。",
        "- Phase 1+ 才引入 weekday gate；本報告 **不改** 進出規則。",
        "- 若 H-WD-0 未 pass · Phase 1「週五進場」先驗偏弱。",
        "",
        "---",
        "模組：`scripts/run_c18acc_weekday_annot.py`",
        "",
    ]
    return "\n".join(lines)
