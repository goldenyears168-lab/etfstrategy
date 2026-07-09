"""C18acc · POOL1 intraday exit loop research · poll_5m 逐 bar 掃描出場。

Phase 1 (intraday_loop): S2 quad_force_exit · prior-day streak + scaled RRG 5m poll。
Phase 2 (intraday_loop_full): + max_hold 末 poll · score_swap 盤中 margin 確認。
Phase 3 (intraday_loop_phase3): + hard_stop 1m −8% · score_swap 盤中 worst_held 重排。
Baseline XEL-0 = day_first_poll（出場日首 bar）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    build_daily_spread_rs_panel,
    resolve_oos_h2_window,
)
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import (
    DEFAULT_C_SWEEP,
    LENGTH,
    intraday_price_scale,
)
from research.backtest.rrg_mono_intraday_exit import _intraday_feat
from research.backtest.rrg_mono_score_swap_c import (
    QuadForceExitMode,
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    quad_force_exit_eligible,
    quad_force_exit_streak_days,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.rrg_lens_score_swap import _rebalance_minutes
from rrg_mono_daily_brief import ScanRow
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

PHASE3_HARD_STOP_PCT = -0.08

BREADTH_BUCKETS: dict[str, tuple[str, ...]] = {
    "risk_on": ("strong", "overbought"),
    "neutral": ("neutral",),
    "risk_off": ("weak", "oversold"),
}

BUCKET_LABELS: dict[str, str] = {
    "risk_on": "Strong/Overbought · 強勢/過熱",
    "neutral": "Neutral · 中性",
    "risk_off": "Weak/Oversold · 弱勢/超賣",
}

HXEL2_OOS_TOLERANCE_PP = -0.25
HXEL2_IS_TOLERANCE_PP = -0.5
HXEL2_REJECT_OOS_PP = -1.0
HXEL2_REGIME_RED_FLAG_PP = -2.0
HXEL2_REGIME_MIN_N = 3

IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-01"
OOS_H1_END_DEFAULT = "2026-06-30"
OOS_H2_START_DEFAULT = "2026-07-01"
OOS_H2_END_FORWARD_DEFAULT = None
MIN_OOS_H2_TRADE_DATES_DEFAULT = 20


def _bucket_for_zone(zone: str | None) -> str:
    z = str(zone or "unknown")
    for bucket, zones in BREADTH_BUCKETS.items():
        if z in zones:
            return bucket
    return "unknown"


def _stratify_periods_by_regime(periods: list[dict[str, Any]]) -> dict[str, Any]:
    by_bucket: dict[str, list[float]] = {b: [] for b in (*BREADTH_BUCKETS, "unknown")}
    by_zone: dict[str, list[float]] = {}
    for p in periods:
        excess = p.get("excess_pct")
        if excess is None:
            continue
        zone = str(p.get("breadth_zone_200") or "unknown")
        bucket = _bucket_for_zone(zone)
        by_bucket.setdefault(bucket, []).append(float(excess))
        by_zone.setdefault(zone, []).append(float(excess))

    def _bucket_stats(vals: list[float]) -> dict[str, Any]:
        return {
            "n": len(vals),
            "mean_excess_pct": round(sum(vals) / len(vals), 4),
        }

    return {
        "by_bucket": {b: _bucket_stats(v) for b, v in by_bucket.items() if v},
        "by_zone": {z: _bucket_stats(v) for z, v in by_zone.items() if v},
        "n_legs": len(periods),
    }


def _regime_bucket_deltas(
    strat0: dict[str, Any] | None,
    strat2: dict[str, Any] | None,
) -> dict[str, Any]:
    b0 = (strat0 or {}).get("by_bucket") or {}
    b2 = (strat2 or {}).get("by_bucket") or {}
    out: dict[str, Any] = {}
    for bucket in sorted(set(b0) | set(b2)):
        m0 = (b0.get(bucket) or {}).get("mean_excess_pct")
        m2 = (b2.get(bucket) or {}).get("mean_excess_pct")
        n0 = int((b0.get(bucket) or {}).get("n") or 0)
        n2 = int((b2.get(bucket) or {}).get("n") or 0)
        delta = round(float(m2) - float(m0), 4) if m0 is not None and m2 is not None else None
        out[bucket] = {
            "label": BUCKET_LABELS.get(bucket, bucket),
            "delta_excess_pp": delta,
            "n_xel0": n0,
            "n_xel2": n2,
            "mean_xel0": m0,
            "mean_xel2": m2,
        }
    return out


def evaluate_h_xel2_verdict(
    *,
    comparison: dict[str, Any],
    by_variant: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """H-XEL-2 · OOS 不劣化 + 出場語意改善 + G3 regime 無紅旗."""
    c12 = comparison.get("xel2_vs_xel0") or {}
    delta_is = c12.get("delta_excess_is_pp")
    delta_oos = c12.get("delta_excess_oos_h1_pp")

    is0 = by_variant.get("XEL-0", {}).get("IS", {})
    is2 = by_variant.get("XEL-2", {}).get("IS", {})
    oos0 = by_variant.get("XEL-0", {}).get("OOS_H1", {})
    oos2 = by_variant.get("XEL-2", {}).get("OOS_H1", {})

    x0_stats = (oos0.get("exit_minute_stats") if oos0 and not oos0.get("skipped") else None) or (
        (by_variant.get("XEL-0", {}).get("FULL", {}) or {}).get("exit_minute_stats")
    ) or (c12.get("xel0_exit_minute_stats") or {})
    x2_stats = (oos2.get("exit_minute_stats") if oos2 and not oos2.get("skipped") else None) or (
        (by_variant.get("XEL-2", {}).get("FULL", {}) or {}).get("exit_minute_stats")
    ) or (c12.get("exit_minute_stats") or {})
    pct0 = x0_stats.get("pct_not_0930")
    pct2 = x2_stats.get("pct_not_0930")
    execution_improves = (
        pct0 is not None and pct2 is not None and float(pct2) > float(pct0)
    ) or (pct0 == 0 and (pct2 or 0) > 0)

    regime_oos = c12.get("regime_bucket_deltas_oos") or {}
    regime_flags = [
        b
        for b, row in regime_oos.items()
        if row.get("delta_excess_pp") is not None
        and float(row["delta_excess_pp"]) < HXEL2_REGIME_RED_FLAG_PP
        and int(row.get("n_xel2") or 0) >= HXEL2_REGIME_MIN_N
        and int(row.get("n_xel0") or 0) >= HXEL2_REGIME_MIN_N
    ]

    oos_ok = delta_oos is None or float(delta_oos) >= HXEL2_OOS_TOLERANCE_PP
    is_ok = delta_is is None or float(delta_is) >= HXEL2_IS_TOLERANCE_PP
    g3_ok = len(regime_flags) == 0

    if delta_oos is not None and float(delta_oos) < HXEL2_REJECT_OOS_PP:
        status = "rejected"
        rationale = f"OOS H1 Δexcess {delta_oos}pp < {HXEL2_REJECT_OOS_PP}pp"
    elif oos_ok and is_ok and execution_improves and g3_ok:
        status = "supported"
        rationale = (
            f"OOS Δexcess {delta_oos}pp ≥ {HXEL2_OOS_TOLERANCE_PP} · "
            f"non-09:30 {pct0}%→{pct2}% · G3 clean"
        )
    elif delta_oos is not None and float(delta_oos) < HXEL2_OOS_TOLERANCE_PP:
        status = "rejected"
        rationale = f"OOS H1 Δexcess {delta_oos}pp below tolerance {HXEL2_OOS_TOLERANCE_PP}pp"
    elif not g3_ok:
        status = "open"
        rationale = f"G3 red flags in buckets: {', '.join(regime_flags)}"
    else:
        status = "open"
        rationale = "Mixed · execution improves but excess borderline or IS/OOS incomplete"

    return {
        "hypothesis_id": "H-XEL-2",
        "status": status,
        "rationale": rationale,
        "delta_excess_is_pp": delta_is,
        "delta_excess_oos_h1_pp": delta_oos,
        "execution_improves": execution_improves,
        "pct_not_0930_xel0": pct0,
        "pct_not_0930_xel2": pct2,
        "g3_regime_red_flags": regime_flags,
        "regime_bucket_deltas_is": c12.get("regime_bucket_deltas_is"),
        "regime_bucket_deltas_oos": regime_oos,
        "windows": {
            "IS": {"xel0": _row_kpi(is0), "xel2": _row_kpi(is2)},
            "OOS_H1": {"xel0": _row_kpi(oos0), "xel2": _row_kpi(oos2)},
        },
    }


def _row_kpi(row: dict[str, Any]) -> dict[str, Any]:
    if not row or row.get("skipped"):
        return {"skipped": True}
    return {
        "n_legs": row.get("n_legs"),
        "mean_excess_pct": row.get("mean_excess_pct"),
        "pct_not_0930": (row.get("exit_minute_stats") or {}).get("pct_not_0930"),
    }


def _attach_hxel2_comparison_fields(
    comparison: dict[str, Any],
    by_var: dict[str, dict[str, Any]],
) -> None:
    x0 = by_var.get("XEL-0", {})
    x2 = by_var.get("XEL-2", {})
    is0, is2 = x0.get("IS", {}), x2.get("IS", {})
    h10, h12 = x0.get("OOS_H1", {}), x2.get("OOS_H1", {})

    def _d(a: dict, b: dict, k: str) -> float | None:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            return None
        return round(float(bv) - float(av), 4)

    c12 = comparison.setdefault("xel2_vs_xel0", {})
    c12["delta_excess_is_pp"] = _d(is0, is2, "mean_excess_pct")
    c12["regime_strat_is"] = {
        "xel0": is0.get("regime_strat"),
        "xel2": is2.get("regime_strat"),
    }
    c12["regime_strat_oos"] = {
        "xel0": h10.get("regime_strat"),
        "xel2": h12.get("regime_strat"),
    }
    c12["regime_bucket_deltas_is"] = _regime_bucket_deltas(
        is0.get("regime_strat"), is2.get("regime_strat")
    )
    c12["regime_bucket_deltas_oos"] = _regime_bucket_deltas(
        h10.get("regime_strat"), h12.get("regime_strat")
    )
    if "xel0_exit_minute_stats" not in c12:
        full0 = x0.get("FULL", {})
        c12["xel0_exit_minute_stats"] = full0.get("exit_minute_stats")
    if h12:
        c12["exit_minute_stats"] = h12.get("exit_minute_stats")


def render_h_xel2_verdict_md(payload: dict[str, Any]) -> str:
    v = payload.get("hxel2_verdict") or {}
    c12 = (payload.get("comparison") or {}).get("xel2_vs_xel0") or {}
    lines = [
        "# H-XEL-2 verdict · XEL-2 vs XEL-0",
        "",
        f"窗口 `{payload.get('date_start')}` → `{payload.get('date_end')}`",
        "",
        f"**狀態：{v.get('status', '—')}** — {v.get('rationale', '')}",
        "",
        "## 主指標",
        "",
        "| 窗口 | Δ mean excess (XEL-2−XEL-0) | XEL-0 legs | XEL-2 legs |",
        "|------|------------------------------|------------|------------|",
    ]
    for win in ("IS", "OOS_H1"):
        w = (v.get("windows") or {}).get(win) or {}
        x0, x2 = w.get("xel0") or {}, w.get("xel2") or {}
        if x0.get("skipped") and x2.get("skipped"):
            continue
        delta = v.get("delta_excess_is_pp") if win == "IS" else v.get("delta_excess_oos_h1_pp")
        lines.append(
            f"| {win} | {delta} pp | {x0.get('n_legs', '—')} · {x0.get('mean_excess_pct', '—')}% "
            f"| {x2.get('n_legs', '—')} · {x2.get('mean_excess_pct', '—')}% |"
        )
    lines.extend(
        [
            "",
            "## 執行語意",
            "",
            f"- XEL-0 非 09:30 出場：**{v.get('pct_not_0930_xel0', '—')}%**",
            f"- XEL-2 非 09:30 出場：**{v.get('pct_not_0930_xel2', '—')}%**",
            f"- 改善：{'是' if v.get('execution_improves') else '否'}",
            "",
            "## G3 · Regime layer（環境層）分層 · breadth bucket",
            "",
        ]
    )
    for label, key in (("IS", "regime_bucket_deltas_is"), ("OOS H1", "regime_bucket_deltas_oos")):
        deltas = c12.get(key) or {}
        if not deltas:
            continue
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| Bucket | Δ excess pp | n₀ / n₂ | mean₀ / mean₂ |")
        lines.append("|--------|-------------|---------|-----------------|")
        for bucket, row in sorted(deltas.items()):
            lines.append(
                f"| {row.get('label', bucket)} | {row.get('delta_excess_pp')} | "
                f"{row.get('n_xel0')}/{row.get('n_xel2')} | "
                f"{row.get('mean_xel0')}% / {row.get('mean_xel2')}% |"
            )
        lines.append("")
    flags = v.get("g3_regime_red_flags") or []
    if flags:
        lines.append(f"G3 紅旗 bucket：**{', '.join(flags)}**")
    lines.append("")
    lines.append("## 門檻")
    lines.append("")
    lines.append(f"- OOS 容忍：Δexcess ≥ {HXEL2_OOS_TOLERANCE_PP} pp")
    lines.append(f"- IS 容忍：Δexcess ≥ {HXEL2_IS_TOLERANCE_PP} pp")
    lines.append(f"- 拒絕：OOS Δexcess < {HXEL2_REJECT_OOS_PP} pp")
    lines.append(f"- G3 紅旗：bucket Δ < {HXEL2_REGIME_RED_FLAG_PP} pp 且 n≥{HXEL2_REGIME_MIN_N}")
    return "\n".join(lines) + "\n"


def _prior_trade_date(full_dates: list[str], as_of: str) -> str | None:
    if as_of not in full_dates:
        return None
    idx = full_dates.index(as_of)
    if idx <= 0:
        return None
    return full_dates[idx - 1]


def intraday_quad_matches(mode: QuadForceExitMode, end_q: str) -> bool:
    q = str(end_q or "").lower()
    if mode == "weakening":
        return q == "weakening"
    if mode == "lagging":
        return q == "lagging"
    if mode == "weakening_or_lagging":
        return q in ("weakening", "lagging")
    return False


def try_intraday_quad_force_exit(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame | None,
    pos: dict[str, Any],
    as_of: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, str | None, int]:
    """Scan 5m polls on as_of · first bar where S2 force-exit fires.

    Streak = prior-day daily streak + 1 if intraday end_q matches mode.
    Loss check uses intraday px at trigger minute.
    """
    from research.backtest.rrg_mono_score_swap_c import (
        _effective_min_hold_days,
        _trading_days_between,
    )

    if config.quad_force_exit_mode == "none":
        return None, None, 0

    sid = str(pos["stock_id"])
    entry = str(pos["entry_date"])
    entry_px = float(pos["entry_px"])
    if entry_px <= 0 or as_of not in close.index or sid not in close.columns:
        return None, None, 0

    hold_days = _trading_days_between(full_dates, entry, as_of)
    eff_min_hold = _effective_min_hold_days(config, close, full_dates, pos, as_of)

    prev_d = _prior_trade_date(full_dates, as_of)
    prior_streak = (
        quad_force_exit_streak_days(
            rs_ratio,
            rs_mom,
            full_dates,
            as_of=prev_d,
            stock_id=sid,
            entry_date=entry,
            mode=config.quad_force_exit_mode,
        )
        if prev_d
        else 0
    )

    key = (sid, as_of)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, sid, as_of)
    bars = kbar_cache[key]
    close_px = float(close.at[as_of, sid])

    for minute in _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_trade_before,
    ):
        feat = _intraday_feat(
            close=close,
            bench=bench,
            full_dates=full_dates,
            trade_date=as_of,
            stock_id=sid,
            minute=minute,
            kbar_bars=bars,
        )
        if feat is None:
            continue
        if not intraday_quad_matches(config.quad_force_exit_mode, str(feat.get("end_q") or "")):
            continue

        px = price_at_or_before_minute(bars, minute)
        if px is None or px <= 0:
            px = close_px
        unrealized_ret = float(px) / entry_px - 1.0
        effective_streak = prior_streak + 1

        if not quad_force_exit_eligible(
            config,
            hold_days=hold_days,
            effective_min_hold=eff_min_hold,
            streak_days=effective_streak,
            unrealized_ret=unrealized_ret,
        ):
            continue
        return float(px), minute, effective_streak

    return None, None, 0


def _day_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[tuple[str, float], ...]:
    key = (stock_id, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, stock_id, trade_date)
    return kbar_cache[key]


def poll_px_at_minute(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    stock_id: str,
    trade_date: str,
    minute: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> float | None:
    if trade_date not in close.index or stock_id not in close.columns:
        return None
    bars = _day_bars(conn, stock_id, trade_date, kbar_cache)
    px = price_at_or_before_minute(bars, minute)
    if px is not None and px > 0:
        return float(px)
    close_px = float(close.at[trade_date, stock_id])
    return close_px if close_px > 0 else None


def poll_px_last(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    stock_id: str,
    trade_date: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, str | None]:
    minutes = _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_trade_before,
    )
    if not minutes:
        return None, None
    last = minutes[-1]
    px = poll_px_at_minute(
        conn,
        close=close,
        stock_id=stock_id,
        trade_date=trade_date,
        minute=last,
        config=config,
        kbar_cache=kbar_cache,
    )
    return px, last if px is not None else None


def intraday_swap_margin_ok(
    *,
    sell: dict[str, Any],
    buy: ScanRow,
    sell_px: float,
    buy_px: float,
    close_s: float,
    close_b: float,
    config: ScoreSwapCConfig,
) -> bool:
    margin = config.effective_margin
    scale_s = intraday_price_scale(close_s, sell_px)
    scale_b = intraday_price_scale(close_b, buy_px)
    held_scaled = float(sell.get("seg_last") or 0.0) * scale_s
    buy_scaled = float(buy.seg_last) * scale_b
    return buy_scaled > held_scaled + margin


def try_intraday_score_swap_px(
    conn: sqlite3.Connection,
    *,
    sell: dict[str, Any],
    buy: ScanRow,
    close: pd.DataFrame,
    as_of: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, str | None, float | None, str | None]:
    """日切選定 (sell,buy) 後 · 逐 5m 確認 scaled seg_last margin · 首觸發 bar 成交。"""
    sid_s = str(sell["stock_id"])
    sid_b = buy.stock_id
    if as_of not in close.index or sid_s not in close.columns or sid_b not in close.columns:
        return None, None, None, None
    close_s = float(close.at[as_of, sid_s])
    close_b = float(close.at[as_of, sid_b])
    if close_s <= 0 or close_b <= 0:
        return None, None, None, None

    for minute in _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_trade_before,
    ):
        sell_px = poll_px_at_minute(
            conn,
            close=close,
            stock_id=sid_s,
            trade_date=as_of,
            minute=minute,
            config=config,
            kbar_cache=kbar_cache,
        )
        buy_px = poll_px_at_minute(
            conn,
            close=close,
            stock_id=sid_b,
            trade_date=as_of,
            minute=minute,
            config=config,
            kbar_cache=kbar_cache,
        )
        if sell_px is None or buy_px is None:
            continue
        if not intraday_swap_margin_ok(
            sell=sell,
            buy=buy,
            sell_px=sell_px,
            buy_px=buy_px,
            close_s=close_s,
            close_b=close_b,
            config=config,
        ):
            continue
        return sell_px, minute, buy_px, minute
    return None, None, None, None


def _scaled_seg_at_minute(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    stock_id: str,
    seg_last: float,
    as_of: str,
    minute: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> float | None:
    if as_of not in close.index or stock_id not in close.columns:
        return None
    close_px = float(close.at[as_of, stock_id])
    if close_px <= 0:
        return None
    px = poll_px_at_minute(
        conn,
        close=close,
        stock_id=stock_id,
        trade_date=as_of,
        minute=minute,
        config=config,
        kbar_cache=kbar_cache,
    )
    if px is None:
        return None
    return float(seg_last) * intraday_price_scale(close_px, px)


def try_intraday_swap_repick(
    conn: sqlite3.Connection,
    *,
    slots: list[dict[str, Any]],
    swap_shortlist: list[ScanRow],
    held_ids: set[str],
    close: pd.DataFrame,
    as_of: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    held_accel_bypass: set[str] | None = None,
    full_dates: list[str] | None = None,
) -> tuple[
    dict[str, Any] | None,
    ScanRow | None,
    float | None,
    str | None,
    float | None,
    str | None,
]:
    """逐 5m · worst_held + scaled seg_last margin · 首組可行 (sell,buy) 成交。"""
    from research.backtest.rrg_mono_score_swap_c import (
        _effective_min_hold_days,
        _trading_days_between,
    )

    if config.swap_target != "worst_held" or config.sort_key != "seg_last":
        return None, None, None, None, None, None

    eligible = [r for r in swap_shortlist if r.stock_id not in held_ids]
    if not eligible or not slots or full_dates is None:
        return None, None, None, None, None, None

    margin = config.effective_margin
    bypass = held_accel_bypass or set()
    sell_pool = list(slots)
    if config.accel_sell_negative_only:
        sell_pool = [
            p
            for p in sell_pool
            if float(p.get("seg_last") or 0) < 0 or str(p["stock_id"]) in bypass
        ]
    if not sell_pool:
        return None, None, None, None, None, None

    for minute in _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_trade_before,
    ):
        scored_sells: list[tuple[dict[str, Any], float]] = []
        for pos in sell_pool:
            sc = _scaled_seg_at_minute(
                conn,
                close=close,
                stock_id=str(pos["stock_id"]),
                seg_last=float(pos.get("seg_last") or 0.0),
                as_of=as_of,
                minute=minute,
                config=config,
                kbar_cache=kbar_cache,
            )
            if sc is not None:
                scored_sells.append((pos, sc))
        if not scored_sells:
            continue
        sell, sell_scaled = min(scored_sells, key=lambda x: x[1])
        threshold = sell_scaled + margin

        best_buy: ScanRow | None = None
        best_buy_scaled = -1e18
        for row in eligible:
            sc = _scaled_seg_at_minute(
                conn,
                close=close,
                stock_id=row.stock_id,
                seg_last=float(row.seg_last),
                as_of=as_of,
                minute=minute,
                config=config,
                kbar_cache=kbar_cache,
            )
            if sc is None or sc <= threshold or sc <= best_buy_scaled:
                continue
            best_buy, best_buy_scaled = row, sc

        if best_buy is None:
            continue

        hold_days = _trading_days_between(full_dates, str(sell["entry_date"]), as_of)
        eff_min_hold = _effective_min_hold_days(config, close, full_dates, sell, as_of)
        if hold_days < eff_min_hold:
            continue

        sell_px = poll_px_at_minute(
            conn,
            close=close,
            stock_id=str(sell["stock_id"]),
            trade_date=as_of,
            minute=minute,
            config=config,
            kbar_cache=kbar_cache,
        )
        buy_px = poll_px_at_minute(
            conn,
            close=close,
            stock_id=best_buy.stock_id,
            trade_date=as_of,
            minute=minute,
            config=config,
            kbar_cache=kbar_cache,
        )
        if sell_px is None or buy_px is None:
            continue
        return sell, best_buy, sell_px, minute, buy_px, minute

    return None, None, None, None, None, None


def exit_loop_sweep_configs(
    *,
    variant_ids: list[str] | None = None,
) -> list[ScoreSwapCConfig]:
    """XEL-0 baseline · XEL-1 quad · XEL-2 full · XEL-3 +hard_stop +swap repick."""
    s2 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S2")
    champ = champion_score_swap_c_config()
    base = replace(
        s2,
        timing_mode=champ.timing_mode,
        candidate_pool="fresh_union_accel",
        poll_interval_min=champ.poll_interval_min,
        no_trade_before=champ.no_trade_before,
    )
    variants = [
        replace(
            base,
            variant_id="XEL-0",
            label="POOL1 · day-first poll exit",
            exit_loop_mode="day_first_poll",
        ),
        replace(
            base,
            variant_id="XEL-1",
            label="POOL1 · intraday 5m quad force-exit loop",
            exit_loop_mode="intraday_loop",
        ),
        replace(
            base,
            variant_id="XEL-2",
            label="POOL1 · intraday 5m full exit loop",
            exit_loop_mode="intraday_loop_full",
        ),
        replace(
            base,
            variant_id="XEL-3",
            label="POOL1 · intraday phase3 · hard_stop 1m + swap repick",
            exit_loop_mode="intraday_loop_phase3",
            sort_key="seg_last",
            buy_sort_key="seg_last",
            hard_stop_loss_pct=PHASE3_HARD_STOP_PCT,
            hard_stop_intraday=True,
            hard_stop_requires_min_hold=False,
        ),
    ]
    if variant_ids:
        allow = set(variant_ids)
        variants = [v for v in variants if v.variant_id in allow]
    return variants


def _exit_reason_stats(periods: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(str(p.get("exit_reason") or "—") for p in periods))


def _exit_minute_stats(periods: list[dict[str, Any]]) -> dict[str, Any]:
    mins: list[str] = []
    for p in periods:
        m = p.get("exit_minute")
        if m:
            mins.append(str(m))
    if not mins:
        return {"n_with_minute": 0, "n_total": len(periods), "top_minutes": []}
    from collections import Counter

    ctr = Counter(mins)
    return {
        "n_with_minute": len(mins),
        "n_total": len(periods),
        "top_minutes": ctr.most_common(5),
        "pct_not_0930": round(100.0 * sum(1 for m in mins if m != "09:30") / len(mins), 2),
    }


def _run_variant_window(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    date_start: str,
    date_end: str,
    label: str,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame,
    full_dates: list[str],
    fresh_by_date: dict,
    mono_by_date: dict,
    zone_by_date: dict[str, str],
    c0_cfg: Any,
    kbar_cache: dict,
    min_trade_dates: int = 5,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < min_trade_dates:
        return {
            "variant_id": cfg.variant_id,
            "label": label,
            "skipped": True,
            "n_trade_dates": len(trade_dates),
        }

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
        kbar_cache=kbar_cache,
        entry_c_config=c0_cfg,
        spread_rs_panel=spread_rs_panel,
    )
    n = len(periods)
    mean_excess = round(sum(p["excess_pct"] for p in periods) / n, 4) if n else None
    force_exits = sum(1 for p in periods if p.get("exit_reason") == "quad_force_exit")
    swap_exits = sum(1 for p in periods if p.get("exit_reason") == "score_swap")
    max_hold_exits = sum(1 for p in periods if p.get("exit_reason") == "max_hold")
    hard_stop_exits = sum(
        1
        for p in periods
        if str(p.get("exit_reason") or "").startswith("hard_stop")
    )
    return {
        "variant_id": cfg.variant_id,
        "exit_loop_mode": cfg.exit_loop_mode,
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "n_legs": n,
        "mean_excess_pct": mean_excess,
        "mean_return_pct": summary.get("mean_return_pct"),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": force_exits,
        "swap_exits": swap_exits,
        "max_hold_exits": max_hold_exits,
        "hard_stop_exits": hard_stop_exits,
        "exit_reason_counts": _exit_reason_stats(periods),
        "exit_minute_stats": _exit_minute_stats(periods),
        "regime_strat": _stratify_periods_by_regime(periods),
        "skipped": False,
    }


def merge_intraday_exit_loop_payloads(
    *payloads: dict[str, Any],
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    """Merge variant rows from multiple sweep runs · rebuild comparison + verdict."""
    rows: list[dict[str, Any]] = []
    for p in payloads:
        rows.extend(r for r in (p.get("rows") or []) if not r.get("skipped"))

    ds = date_start or payloads[0].get("date_start")
    de = date_end or payloads[0].get("date_end")

    by_var: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_var.setdefault(str(r["variant_id"]), {})[str(r["label"])] = r

    x0 = by_var.get("XEL-0", {})
    x1 = by_var.get("XEL-1", {})
    x2 = by_var.get("XEL-2", {})
    x3 = by_var.get("XEL-3", {})
    full0, full1, full2, full3 = (
        x0.get("FULL", {}),
        x1.get("FULL", {}),
        x2.get("FULL", {}),
        x3.get("FULL", {}),
    )
    h10, h11, h12, h13 = (
        x0.get("OOS_H1", {}),
        x1.get("OOS_H1", {}),
        x2.get("OOS_H1", {}),
        x3.get("OOS_H1", {}),
    )

    def _d(a: dict, b: dict, k: str) -> float | None:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            return None
        return round(float(bv) - float(av), 4)

    comparison = {
        "xel1_vs_xel0": {
            "delta_excess_full_pp": _d(full0, full1, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h10, h11, "mean_excess_pct"),
            "delta_force_exits": int(full1.get("force_exits") or 0) - int(full0.get("force_exits") or 0),
            "exit_minute_stats": full1.get("exit_minute_stats"),
        },
        "xel2_vs_xel0": {
            "delta_excess_full_pp": _d(full0, full2, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h10, h12, "mean_excess_pct"),
            "delta_swaps": int(full2.get("swaps_total") or 0) - int(full0.get("swaps_total") or 0),
            "delta_max_hold_exits": int(full2.get("max_hold_exits") or 0) - int(full0.get("max_hold_exits") or 0),
            "exit_minute_stats": full2.get("exit_minute_stats"),
        },
        "xel2_vs_xel1": {
            "delta_excess_full_pp": _d(full1, full2, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h11, h12, "mean_excess_pct"),
        },
        "xel3_vs_xel2": {
            "delta_excess_full_pp": _d(full2, full3, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h12, h13, "mean_excess_pct"),
            "delta_hard_stop_exits": int(full3.get("hard_stop_exits") or 0)
            - int(full2.get("hard_stop_exits") or 0),
            "delta_swaps": int(full3.get("swaps_total") or 0) - int(full2.get("swaps_total") or 0),
            "exit_minute_stats": full3.get("exit_minute_stats"),
        },
        "xel0_exit_minute_stats": full0.get("exit_minute_stats"),
    }

    c32 = comparison["xel3_vs_xel2"]
    stats3 = c32.get("exit_minute_stats") or {}
    verdict = {
        "summary": (
            f"XEL-3 vs XEL-2 · FULL Δexcess={c32.get('delta_excess_full_pp')}pp · "
            f"Δhard_stop={c32.get('delta_hard_stop_exits')} · "
            f"Δswaps={c32.get('delta_swaps')} · "
            f"non-09:30 exits={stats3.get('pct_not_0930', '—')}%"
        ),
        "phase": "phase3_hard_stop_swap_repick",
        "next": "OOS holdout verdict · optional showcase HTML for XEL-3",
    }

    return {
        "schema": "c18acc_intraday_exit_loop-v3",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": ds,
        "date_end": de,
        "comparison": comparison,
        "rows": rows,
        "by_variant": by_var,
        "verdict": verdict,
        "note": payloads[0].get("note"),
    }


def run_intraday_exit_loop_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_h1_start: str = OOS_START_DEFAULT,
    oos_h1_end: str = OOS_H1_END_DEFAULT,
    oos_h2_mode: Literal["forward", "historical"] = "forward",
    min_oos_h2_trade_dates: int = MIN_OOS_H2_TRADE_DATES_DEFAULT,
    variant_ids: list[str] | None = None,
    window_labels: list[str] | None = None,
    hxel2_verdict: bool = False,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]

    h2_start, h2_end, h2_mode = resolve_oos_h2_window(
        oos_h2_mode=oos_h2_mode,
        oos_h2_start=OOS_H2_START_DEFAULT,
        oos_h2_end=OOS_H2_END_FORWARD_DEFAULT,
        date_end=date_end,
    )
    h2_trade_dates = [d for d in full_dates if h2_start <= d <= h2_end]
    h2_ready = len(h2_trade_dates) >= min_oos_h2_trade_dates

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    kbar_cache: dict = {}

    windows: list[tuple[str, str, str]] = [
        ("IS", date_start, is_end),
        ("OOS_H1", oos_h1_start, min(oos_h1_end, date_end)),
        ("FULL", date_start, date_end),
    ]
    if h2_ready and h2_start <= h2_end:
        windows.insert(2, ("OOS_H2", h2_start, h2_end))
    if window_labels:
        allow = set(window_labels)
        windows = [w for w in windows if w[0] in allow]

    rows: list[dict[str, Any]] = []
    for cfg in exit_loop_sweep_configs(variant_ids=variant_ids):
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            min_td = 2 if win_label == "OOS_H2" else 5
            rows.append(
                _run_variant_window(
                    conn,
                    cfg=cfg,
                    date_start=w_start,
                    date_end=w_end,
                    label=win_label,
                    close=close,
                    bench=bench,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    spread_rs_panel=spread_rs_panel,
                    full_dates=full_dates,
                    fresh_by_date=fresh_by_date,
                    mono_by_date=mono_by_date,
                    zone_by_date=zone_by_date,
                    c0_cfg=c0_cfg,
                    kbar_cache=kbar_cache,
                    min_trade_dates=min_td,
                )
            )

    by_var: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("skipped"):
            continue
        by_var.setdefault(str(r["variant_id"]), {})[str(r["label"])] = r

    x0 = by_var.get("XEL-0", {})
    x1 = by_var.get("XEL-1", {})
    x2 = by_var.get("XEL-2", {})
    x3 = by_var.get("XEL-3", {})
    full0, full1, full2, full3 = (
        x0.get("FULL", {}),
        x1.get("FULL", {}),
        x2.get("FULL", {}),
        x3.get("FULL", {}),
    )
    h10, h11, h12, h13 = (
        x0.get("OOS_H1", {}),
        x1.get("OOS_H1", {}),
        x2.get("OOS_H1", {}),
        x3.get("OOS_H1", {}),
    )

    def _d(a: dict, b: dict, k: str) -> float | None:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            return None
        return round(float(bv) - float(av), 4)

    comparison = {
        "xel1_vs_xel0": {
            "delta_excess_full_pp": _d(full0, full1, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h10, h11, "mean_excess_pct"),
            "delta_force_exits": int(full1.get("force_exits") or 0) - int(full0.get("force_exits") or 0),
            "exit_minute_stats": full1.get("exit_minute_stats"),
        },
        "xel2_vs_xel0": {
            "delta_excess_full_pp": _d(full0, full2, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h10, h12, "mean_excess_pct"),
            "delta_swaps": int(full2.get("swaps_total") or 0) - int(full0.get("swaps_total") or 0),
            "delta_max_hold_exits": int(full2.get("max_hold_exits") or 0) - int(full0.get("max_hold_exits") or 0),
            "exit_minute_stats": full2.get("exit_minute_stats"),
        },
        "xel2_vs_xel1": {
            "delta_excess_full_pp": _d(full1, full2, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h11, h12, "mean_excess_pct"),
        },
        "xel3_vs_xel2": {
            "delta_excess_full_pp": _d(full2, full3, "mean_excess_pct"),
            "delta_excess_oos_h1_pp": _d(h12, h13, "mean_excess_pct"),
            "delta_hard_stop_exits": int(full3.get("hard_stop_exits") or 0)
            - int(full2.get("hard_stop_exits") or 0),
            "delta_swaps": int(full3.get("swaps_total") or 0) - int(full2.get("swaps_total") or 0),
            "exit_minute_stats": full3.get("exit_minute_stats"),
        },
        "xel0_exit_minute_stats": full0.get("exit_minute_stats"),
    }

    if hxel2_verdict or {"XEL-0", "XEL-2"}.issubset(by_var):
        _attach_hxel2_comparison_fields(comparison, by_var)

    c32 = comparison["xel3_vs_xel2"]
    stats3 = c32.get("exit_minute_stats") or {}
    hxel2 = (
        evaluate_h_xel2_verdict(comparison=comparison, by_variant=by_var)
        if hxel2_verdict or {"XEL-0", "XEL-2"}.issubset(by_var)
        else None
    )
    if hxel2:
        verdict = {
            "summary": f"H-XEL-2 · {hxel2['status']} · {hxel2['rationale']}",
            "phase": "hxel2_oos_regime_verdict",
            "next": "採納 XEL-2 showcase refresh" if hxel2["status"] == "supported" else "extend OOS or tune",
        }
    else:
        verdict = {
            "summary": (
                f"XEL-3 vs XEL-2 · FULL Δexcess={c32.get('delta_excess_full_pp')}pp · "
                f"Δhard_stop={c32.get('delta_hard_stop_exits')} · "
                f"Δswaps={c32.get('delta_swaps')} · "
                f"non-09:30 exits={stats3.get('pct_not_0930', '—')}%"
            ),
            "phase": "phase3_hard_stop_swap_repick",
            "next": "OOS holdout verdict · optional showcase HTML for XEL-3",
        }

    payload: dict[str, Any] = {
        "schema": "c18acc_intraday_exit_loop-v3",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "comparison": comparison,
        "rows": rows,
        "by_variant": by_var,
        "verdict": verdict,
        "note": (
            "XEL-3=XEL-2 + hard_stop 1m −8% + score_swap 盤中 worst_held scaled 重排。"
        ),
    }
    if hxel2:
        payload["hxel2_verdict"] = hxel2
    return payload


def render_intraday_exit_loop_md(payload: dict[str, Any]) -> str:
    cmp_ = payload.get("comparison") or {}
    c32 = cmp_.get("xel3_vs_xel2") or {}
    c12 = cmp_.get("xel2_vs_xel0") or {}
    lines = [
        "# C18acc · POOL1 intraday exit loop · Phase 3",
        "",
        f"窗口 `{payload.get('date_start')}` → `{payload.get('date_end')}`",
        "",
        "## 口徑",
        "",
        "- **XEL-0**：日切 · 出場日首 poll bar",
        "- **XEL-1**：+ quad_force 5m scaled RRG",
        "- **XEL-2**：+ max_hold 末 poll · score_swap 盤中 margin 確認",
        "- **XEL-3**：+ hard_stop 1m −8% · score_swap 盤中 worst_held scaled 重排",
        "",
        "## XEL-3 vs XEL-2（主對照）",
        "",
        "| 指標 | 值 |",
        "|------|-----|",
        f"| FULL Δ mean excess | {c32.get('delta_excess_full_pp')} pp |",
        f"| OOS H1 Δ mean excess | {c32.get('delta_excess_oos_h1_pp')} pp |",
        f"| Δ hard_stop exits | {c32.get('delta_hard_stop_exits')} |",
        f"| Δ swaps | {c32.get('delta_swaps')} |",
        "",
        f"- XEL-3 exit minutes: `{c32.get('exit_minute_stats')}`",
        "",
        "## XEL-2 vs XEL-0",
        "",
        f"- FULL Δ excess: {c12.get('delta_excess_full_pp')} pp",
        "",
        f"**{payload.get('verdict', {}).get('summary', '')}**",
        "",
        f"Next: {payload.get('verdict', {}).get('next', '')}",
    ]
    return "\n".join(lines) + "\n"
