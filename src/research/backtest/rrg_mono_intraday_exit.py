"""RRG mono hold7 · 出場訊號假說 + 盤中 1 分 K 賣點 sweep。

SSG 固定：D4 mono fresh 收盤建倉（A 腿）· 僅變更出場規則與觸發日盤中執行。
日線訊號（何時賣）與盤中執行（幾點賣）分離，對照 intraday-exit-playbook 設計。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_lens_score_swap import _rebalance_minutes
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar, simulate_mono_hold7
from research.backtest.rrg_mono_intraday_ab import (
    LENGTH,
    intraday_price_scale,
    scaled_seg_last,
)
from rrg_mono_daily_brief import HOLD_DAYS, LOOKBACK, ScanRow, _feat
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import kbar_day_has_data, load_kbar_day_closes, price_at_or_before_minute

ExitSignalMode = Literal[
    "hold7",
    "quad_weak",
    "quad_lagging",
    "ll_streak",
    "mono_break",
    "seg_decay",
    "accel_d4",
]
ExitTimingMode = Literal["close", "poll_scale", "poll_quad"]

DEFAULT_EXIT_QUADRANTS = ("weakening", "lagging")


@dataclass
class ExitVariantConfig:
    variant_id: str = "E0"
    label: str = "hold7 收盤基線"
    signal_mode: ExitSignalMode = "hold7"
    streak_days: int = 1
    min_hold_days: int = 1
    max_hold_days: int = HOLD_DAYS
    exit_quadrants: tuple[str, ...] = DEFAULT_EXIT_QUADRANTS
    seg_weak_ratio: float = 0.85
    accel_hold_day: int = 4
    timing_mode: ExitTimingMode = "close"
    poll_interval_min: int = 5
    confirm_polls: int = 1
    no_exit_before: str = "09:30"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_quadrants"] = list(self.exit_quadrants)
        return d


DEFAULT_EXIT_SWEEP: list[ExitVariantConfig] = [
    ExitVariantConfig("E0", "hold7 收盤基線", "hold7", max_hold_days=7, timing_mode="close"),
    ExitVariantConfig(
        "E1",
        "象限轉弱 weakening/lagging · 連續1日 · 收盤賣",
        "quad_weak",
        streak_days=1,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="close",
    ),
    ExitVariantConfig(
        "E2",
        "象限轉弱 · 連續2日 · 收盤賣",
        "quad_weak",
        streak_days=2,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="close",
    ),
    ExitVariantConfig(
        "E3",
        "位移連續3日左下 ll_streak · 收盤賣",
        "ll_streak",
        streak_days=3,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="close",
    ),
    ExitVariantConfig(
        "E4",
        "位移連續3日左下 · 5m scale 盤中賣",
        "ll_streak",
        streak_days=3,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="poll_scale",
        poll_interval_min=5,
        confirm_polls=1,
    ),
    ExitVariantConfig(
        "E5",
        "mono 加速中斷 mono_break · 收盤賣",
        "mono_break",
        streak_days=1,
        min_hold_days=1,
        max_hold_days=7,
        timing_mode="close",
    ),
    ExitVariantConfig(
        "E6",
        "D4 未再加速 accel_d4 · 收盤賣",
        "accel_d4",
        accel_hold_day=4,
        min_hold_days=4,
        max_hold_days=7,
        timing_mode="close",
    ),
    ExitVariantConfig(
        "E7",
        "D4 未再加速 · 5m scale confirm=2 盤中賣",
        "accel_d4",
        accel_hold_day=4,
        min_hold_days=4,
        max_hold_days=7,
        timing_mode="poll_scale",
        poll_interval_min=5,
        confirm_polls=2,
    ),
    ExitVariantConfig(
        "E8",
        "seg_last 衰減至進場85% · 5m scale 盤中賣",
        "seg_decay",
        seg_weak_ratio=0.85,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="poll_scale",
        poll_interval_min=5,
        confirm_polls=1,
    ),
    ExitVariantConfig(
        "E9",
        "象限 lagging · 5m full_rrg 盤中賣",
        "quad_lagging",
        streak_days=1,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="poll_quad",
        poll_interval_min=5,
        confirm_polls=1,
    ),
    ExitVariantConfig(
        "E10",
        "象限轉弱 · 15m scale confirm=2",
        "quad_weak",
        streak_days=1,
        min_hold_days=2,
        max_hold_days=10,
        timing_mode="poll_scale",
        poll_interval_min=15,
        confirm_polls=2,
    ),
]


def _trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


def _hold_dates(full_dates: list[str], entry_date: str, max_hold: int) -> list[str]:
    if entry_date not in full_dates:
        return []
    idx = full_dates.index(entry_date)
    return full_dates[idx + 1 : idx + 1 + max_hold]


def _daily_feat(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
) -> dict[str, Any] | None:
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < LOOKBACK:
        return None
    return _feat(rs_ratio, rs_mom, full_dates, si, stock_id)


def _daily_condition(
    feat: dict[str, Any] | None,
    *,
    config: ExitVariantConfig,
    entry_seg_last: float,
) -> bool:
    if feat is None:
        return False
    mode = config.signal_mode
    if mode == "hold7":
        return False
    if mode == "quad_weak":
        q = str(feat.get("end_q") or "").lower()
        return q in {x.lower() for x in config.exit_quadrants}
    if mode == "quad_lagging":
        return str(feat.get("end_q") or "").lower() == "lagging"
    if mode == "ll_streak":
        return str(feat.get("trend") or "") == "down_left"
    if mode == "mono_break":
        return not bool(feat.get("mono_up"))
    if mode == "seg_decay":
        seg = float(feat.get("seg_last") or 0.0)
        return entry_seg_last > 0 and seg < entry_seg_last * config.seg_weak_ratio
    if mode == "accel_d4":
        trend = str(feat.get("trend") or "")
        return trend != "up_right" or not bool(feat.get("mono_up"))
    return False


def _resolve_signal_day(
    *,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    entry_date: str,
    stock_id: str,
    entry_seg_last: float,
    config: ExitVariantConfig,
) -> tuple[str | None, str]:
    """回傳 (signal_exit_date, reason)。"""
    if config.signal_mode == "hold7":
        dates = _hold_dates(full_dates, entry_date, config.max_hold_days)
        if not dates:
            return None, "hold7_no_calendar"
        target = dates[min(len(dates), config.max_hold_days) - 1]
        return target, "hold7_max"

    streak = 0
    for d in _hold_dates(full_dates, entry_date, config.max_hold_days):
        hold_day = _trading_days_between(full_dates, entry_date, d)
        feat = _daily_feat(rs_ratio, rs_mom, full_dates, d, stock_id)

        if config.signal_mode == "accel_d4":
            if hold_day < config.accel_hold_day:
                streak = 0
                continue
            if hold_day == config.accel_hold_day:
                if _daily_condition(feat, config=config, entry_seg_last=entry_seg_last):
                    if hold_day >= config.min_hold_days:
                        return d, "accel_d4"
            continue

        hit = _daily_condition(feat, config=config, entry_seg_last=entry_seg_last)
        streak = streak + 1 if hit else 0
        if streak >= config.streak_days and hold_day >= config.min_hold_days:
            return d, config.signal_mode

    dates = _hold_dates(full_dates, entry_date, config.max_hold_days)
    if dates:
        return dates[-1], "max_hold_fallback"
    return None, "no_exit"


def _intraday_feat(
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
    minute: str,
    kbar_bars: tuple[tuple[str, float], ...],
) -> dict[str, Any] | None:
    if trade_date not in close.index or stock_id not in close.columns:
        return None
    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    close_px = float(close.at[trade_date, stock_id])
    if close_px <= 0:
        return None
    px = price_at_or_before_minute(kbar_bars, minute)
    if px is None or px <= 0:
        px = close_px
    prov.at[trade_date, stock_id] = float(px)
    rs_r, rs_m, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)
    return _daily_feat(rs_r, rs_m, full_dates, trade_date, stock_id)


def _pick_intraday_exit(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    stock_id: str,
    exit_date: str,
    entry_seg_last: float,
    config: ExitVariantConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, str | None, bool]:
    """回傳 (exit_px, exit_minute, used_kbar)。"""
    if exit_date not in close.index or stock_id not in close.columns:
        return None, None, False

    close_px = float(close.at[exit_date, stock_id])
    if config.timing_mode == "close":
        return close_px, None, False

    key = (stock_id, exit_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, stock_id, exit_date)
    bars = kbar_cache[key]
    used_kbar = bool(bars)

    minutes = _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_exit_before,
    )
    row = ScanRow(
        stock_id=stock_id,
        stock_name="",
        fresh=True,
        mono=True,
        seg_last=entry_seg_last,
        disp=1.2,
        segs=[],
        quadrants=[],
        rs_ratio=100.0,
        rs_momentum=100.0,
        daily_pct=None,
    )
    confirm = 0
    for minute in minutes:
        if config.timing_mode == "poll_scale":
            px = price_at_or_before_minute(bars, minute)
            if px is None:
                px = close_px
            scale = intraday_price_scale(close_px, px)
            weak = scaled_seg_last(row, scale) < entry_seg_last * config.seg_weak_ratio
            confirm = confirm + 1 if weak else 0
        elif config.timing_mode == "poll_quad":
            feat = _intraday_feat(
                close=close,
                bench=bench,
                full_dates=full_dates,
                trade_date=exit_date,
                stock_id=stock_id,
                minute=minute,
                kbar_bars=bars,
            )
            q = str((feat or {}).get("end_q") or "").lower()
            weak = q in {x.lower() for x in config.exit_quadrants}
            confirm = confirm + 1 if weak else 0
        else:
            break

        if confirm >= config.confirm_polls:
            px = price_at_or_before_minute(bars, minute) or close_px
            return float(px), minute, used_kbar

    return close_px, None, used_kbar


def _settle_custom_exit(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    pos: dict[str, Any],
    *,
    exit_date: str,
    exit_px: float,
    exit_minute: str | None,
    exit_reason: str,
    config: ExitVariantConfig,
) -> dict[str, Any] | None:
    sid = str(pos["stock_id"])
    entry = str(pos["entry_date"])
    entry_px = pos.get("entry_px")
    if entry_px is None:
        if entry not in close.index:
            return None
        entry_px = float(close.at[entry, sid])
    else:
        entry_px = float(entry_px)
    if entry_px <= 0 or exit_px <= 0:
        return None
    ret = (exit_px / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry, exit_date, entry_price_mode="close")
    if bench is None:
        return None
    hold_days = _trading_days_between(
        list(close.index.astype(str)), entry, exit_date
    )
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": str(pos.get("signal_date") or entry),
        "entry_date": entry,
        "exit_date": exit_date,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "exit_minute": exit_minute,
        "exit_reason": exit_reason,
        "hold_days": hold_days,
        "variant_id": config.variant_id,
        "signal_mode": config.signal_mode,
        "timing_mode": config.timing_mode,
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "seg_last": pos.get("seg_last"),
        "slot": pos.get("slot"),
        "breadth_zone_200": pos.get("breadth_zone_200"),
    }


def apply_exit_variant_to_periods(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    config: ExitVariantConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = kbar_cache if kbar_cache is not None else {}
    out: list[dict[str, Any]] = []
    kbar_hits = 0
    kbar_checks = 0

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        entry_seg = float(pos.get("seg_last") or 0.0)
        exit_d, reason = _resolve_signal_day(
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=full_dates,
            entry_date=entry,
            stock_id=sid,
            entry_seg_last=entry_seg,
            config=config,
        )
        if not exit_d:
            continue
        kbar_checks += 1
        exit_px, exit_minute, used = _pick_intraday_exit(
            conn,
            close=close,
            bench=bench,
            full_dates=full_dates,
            stock_id=sid,
            exit_date=exit_d,
            entry_seg_last=entry_seg,
            config=config,
            kbar_cache=cache,
        )
        if used:
            kbar_hits += 1
        if exit_px is None:
            continue
        leg = _settle_custom_exit(
            conn,
            close,
            pos,
            exit_date=exit_d,
            exit_px=exit_px,
            exit_minute=exit_minute,
            exit_reason=reason,
            config=config,
        )
        if leg:
            out.append(leg)

    summary = summarize_periods(out)
    if out:
        summary["mean_excess_pct"] = round(sum(p["excess_pct"] for p in out) / len(out), 4)
        summary["mean_hold_days"] = round(sum(p["hold_days"] for p in out) / len(out), 2)
        summary["mean_return_pct"] = round(sum(p["return_pct"] for p in out) / len(out), 4)
    else:
        summary["mean_excess_pct"] = None
        summary["mean_hold_days"] = None
        summary["mean_return_pct"] = None
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "signal_mode": config.signal_mode,
            "timing_mode": config.timing_mode,
            "streak_days": config.streak_days,
            "min_hold_days": config.min_hold_days,
            "max_hold_days": config.max_hold_days,
            "poll_interval_min": config.poll_interval_min,
            "confirm_polls": config.confirm_polls,
            "kbar_coverage_pct": round(kbar_hits / kbar_checks * 100.0, 2) if kbar_checks else None,
            "n_periods": len(out),
        }
    )
    return out, summary


def run_exit_variant_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[ExitVariantConfig] | None = None,
    baseline_variant_id: str = "E0",
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    base_periods, base_summary = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        zone_filter=None,
        entry_price_mode="close",
    )

    grid = configs or DEFAULT_EXIT_SWEEP
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    summaries: list[dict[str, Any]] = []
    baseline_excess: float | None = None

    for cfg in grid:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        _, summary = apply_exit_variant_to_periods(
            conn,
            base_periods=base_periods,
            close=close,
            bench=bench,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=full_dates,
            config=cfg,
            kbar_cache=kbar_cache,
        )
        if cfg.variant_id == baseline_variant_id:
            baseline_excess = summary.get("mean_excess_pct")
        delta = None
        if baseline_excess is not None and summary.get("mean_excess_pct") is not None:
            delta = round(float(summary["mean_excess_pct"]) - float(baseline_excess), 4)
        summary["delta_vs_baseline_pp"] = delta
        summaries.append(summary)
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"mean_excess={summary.get('mean_excess_pct')} hold={summary.get('mean_hold_days')}",
            flush=True,
        )

    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    best = ranked[0] if ranked else None

    return {
        "date_start": date_start,
        "date_end": date_end,
        "baseline_variant_id": baseline_variant_id,
        "reference_entry": {
            "leg": "A",
            "n_periods": base_summary.get("n_periods") or len(base_periods),
            "mean_excess_pct": base_summary.get("mean_excess_pct"),
        },
        "ssg_note": (
            "訊號固定 D4 mono fresh 收盤建倉（simulate_mono_hold7 A 腿）· "
            "僅變更出場日線假說與觸發日盤中賣點"
        ),
        "hypotheses": {
            "quad_weak": "RRG 象限轉入 weakening/lagging",
            "ll_streak": "4 日軌跡位移連續 N 日 down_left（左下）",
            "mono_break": "mono_up 加速中斷",
            "accel_d4": f"持有第 {4} 日仍未 up_right+mono_up（D4 加速結束）",
            "seg_decay": "seg_last 跌至進場比例以下",
            "poll_scale": "觸發日 1 分 K scale seg_last 輪詢確認後賣",
            "poll_quad": "觸發日盤中 full RRG 象限確認後賣",
        },
        "summaries": summaries,
        "best": best,
    }


def audit_kbar_fair_subset(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    min_pct: float = 100.0,
) -> list[str]:
    """回傳 kbar 覆蓋達標的 exit_date 集合（公平子樣本）。"""
    if not periods:
        return []
    by_date: dict[str, list[str]] = {}
    for p in periods:
        d = str(p["exit_date"])
        by_date.setdefault(d, []).append(str(p["stock_id"]))
    fair: list[str] = []
    for d, sids in by_date.items():
        hits = sum(1 for sid in sids if kbar_day_has_data(conn, sid, d))
        if hits / len(sids) * 100.0 >= min_pct:
            fair.append(d)
    return sorted(fair)
