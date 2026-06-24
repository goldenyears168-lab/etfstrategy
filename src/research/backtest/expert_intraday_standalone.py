"""Standalone expert intraday entry backtest · 1m PIT · 非 RRG/VCP 日線訊號疊加。

策略（各自獨立掃描 universe）：
  vwap_reclaim · vwap_bounce · bone_zone · orb · pivot_retest

出場（全策略一致）：進場當日盤中觸及 structure stop → 當日 stop 出場；
否則 hold N 個交易日收盤出場（預設 5）。
對照基準：IX0001 進場日收盤 → 出場日收盤。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

from analytics.bench import bench_return_entry_to_exit
from flow_returns import exit_close_date_from_entry, return_pct, stock_close
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_expert_entry import (
    DETECTORS,
    EntryTrigger,
    ExpertEntryMode,
    NO_TRADE_BEFORE,
    _is_bullish,
    _minute_ge,
    _norm_minute,
    _tradeable_bars,
    detect_pivot_retest_entry,
    enrich_bars,
)
from stock_db.etf import load_etf_constituent_watchlist
from stock_db.kbar import KbarBar, kbar_day_has_data, load_kbar_day_bars
from stock_db.vcp import load_vcp_screen_v2_for_date
from vcp_funnel_screen import MODEL_ID as VCP_FUNNEL_MODEL_ID

StandaloneMode = Literal["vwap_reclaim", "vwap_bounce", "bone_zone", "orb", "pivot_retest"]

DEFAULT_HOLD_DAYS = 5
ORB_START = "09:05"
ORB_END = "09:15"
ORB_VOL_MULT = 1.2

STANDALONE_MODES: tuple[StandaloneMode, ...] = (
    "vwap_reclaim",
    "vwap_bounce",
    "bone_zone",
    "orb",
    "pivot_retest",
)

MODE_LABELS: dict[StandaloneMode, str] = {
    "vwap_reclaim": "VWAP reclaim · 低於 VWAP 後陽線收上",
    "vwap_bounce": "VWAP bounce · 全日站上 · 觸線後下一根陽線",
    "bone_zone": "Bone Zone · 9/20 EMA 回踩 · 陽線收上 9 EMA",
    "orb": "ORB · 09:05–09:15 區間突破 · 陽線收上 + 量能",
    "pivot_retest": "Pivot retest · vcp_screen pivot · 突破回踩確認",
}


@dataclass(frozen=True)
class StandaloneConfig:
    hold_days: int = DEFAULT_HOLD_DAYS
    min_bars_per_day: int = 50


def detect_orb_entry(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """Opening range breakout long · OR=09:05–09:15 · 突破後陽線收上 + 量能過濾。"""
    enriched = enrich_bars(bars)
    or_rows = [
        r
        for r in enriched
        if _minute_ge(r.bar.minute, ORB_START) and r.bar.minute <= f"{ORB_END}:00"
    ]
    if len(or_rows) < 3:
        return None
    or_high = max(r.bar.high for r in or_rows)
    or_low = min(r.bar.low for r in or_rows)
    or_vols = [float(r.bar.volume or 0) for r in or_rows if r.bar.volume]
    avg_or_vol = sum(or_vols) / len(or_vols) if or_vols else 0.0
    after_or = [
        r
        for r in enriched
        if _minute_ge(r.bar.minute, NO_TRADE_BEFORE)
        and _minute_ge(r.bar.minute, f"{ORB_END}:00")
        and r.bar.minute > f"{ORB_END}:00"
    ]
    for row in after_or:
        vol = float(row.bar.volume or 0)
        if avg_or_vol > 0 and vol < ORB_VOL_MULT * avg_or_vol:
            continue
        if not _is_bullish(row.bar):
            continue
        if row.bar.close <= or_high:
            continue
        return EntryTrigger(
            mode="bone_zone",  # reuse mode field; standalone uses strategy_id
            entry_minute=row.bar.minute,
            entry_px=row.bar.close,
            stop_px=or_low,
        )
    return None


def detect_standalone_entry(
    mode: StandaloneMode,
    bars: tuple[KbarBar, ...],
    *,
    pivot_price: float | None = None,
) -> EntryTrigger | None:
    if mode == "orb":
        return detect_orb_entry(bars)
    if mode == "pivot_retest":
        if pivot_price is None or pivot_price <= 0:
            return None
        return detect_pivot_retest_entry(bars, pivot_price)
    fn = DETECTORS.get(mode)  # type: ignore[arg-type]
    if fn is None:
        return None
    return fn(bars)  # type: ignore[operator]


def _stop_hit_intraday(
    bars: tuple[KbarBar, ...],
    entry_minute: str,
    stop_px: float,
) -> bool:
    target = _norm_minute(entry_minute)
    for bar in bars:
        if _norm_minute(bar.minute) <= target:
            continue
        if bar.low <= stop_px:
            return True
    return False


def _load_pivot_map(conn: sqlite3.Connection, trade_date: str) -> dict[str, float]:
    rows = load_vcp_screen_v2_for_date(
        conn,
        trade_date,
        model_id=VCP_FUNNEL_MODEL_ID,
        min_score=0.0,
    )
    out: dict[str, float] = {}
    for r in rows:
        pivot = r["pivot_price"]
        if pivot is None:
            continue
        try:
            px = float(pivot)
        except (TypeError, ValueError):
            continue
        if px > 0:
            out[str(r["stock_id"])] = px
    return out


def kbar_universe_for_day(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    trade_date: str,
) -> list[str]:
    return [sid for sid in stock_ids if kbar_day_has_data(conn, sid, trade_date)]


def stocks_with_kbar_coverage(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    min_stock_days: int = 30,
) -> list[str]:
    """ETF 成分聯集 · 區間內至少有 min_stock_days 個有 1m K 的交易日。"""
    watch = [str(r["stock_id"]) for r in load_etf_constituent_watchlist(conn)]
    if not watch:
        return []
    placeholders = ",".join("?" * len(watch))
    rows = conn.execute(
        f"""
        SELECT stock_id, COUNT(DISTINCT trade_date) AS n
        FROM stock_kbar_1m
        WHERE stock_id IN ({placeholders})
          AND trade_date BETWEEN ? AND ?
        GROUP BY stock_id
        HAVING n >= ?
        ORDER BY stock_id
        """,
        (*watch, date_start, date_end, min_stock_days),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _period_from_trigger(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    trade_date: str,
    trig: EntryTrigger,
    hold_days: int,
    bars: tuple[KbarBar, ...],
    strategy: StandaloneMode,
) -> dict[str, Any] | None:
    entry_px = float(trig.entry_px)
    stop_px = float(trig.stop_px)
    if entry_px <= 0:
        return None
    exit_reason = "time"
    exit_date = trade_date
    exit_px = entry_px
    if _stop_hit_intraday(bars, trig.entry_minute, stop_px):
        exit_reason = "stop"
        exit_px = stop_px
    else:
        exit_d = exit_close_date_from_entry(conn, trade_date, hold_days)
        if exit_d is None:
            return None
        close_px = stock_close(conn, stock_id, exit_d)
        if close_px is None or close_px <= 0:
            return None
        exit_date = exit_d
        exit_px = float(close_px)
    ret = return_pct(entry_px, exit_px)
    bench = bench_return_entry_to_exit(conn, trade_date, exit_date, entry_price_mode="close")
    if bench is None:
        return None
    return {
        "stock_id": stock_id,
        "entry_date": trade_date,
        "exit_date": exit_date,
        "entry_minute": trig.entry_minute,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "stop_px": round(stop_px, 4),
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "exit_reason": exit_reason,
        "strategy": strategy,
    }


def simulate_standalone_strategy(
    conn: sqlite3.Connection,
    *,
    mode: StandaloneMode,
    trade_dates: list[str],
    stock_ids: list[str],
    config: StandaloneConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐日掃描 universe · 每檔每日最多一筆觸發。"""
    cfg = config or StandaloneConfig()
    periods: list[dict[str, Any]] = []
    bars_cache: dict[tuple[str, str], tuple[KbarBar, ...]] = {}
    n_kbar_checks = 0
    n_kbar_hits = 0
    n_pivot_skips = 0

    for trade_date in trade_dates:
        pivot_map: dict[str, float] = {}
        if mode == "pivot_retest":
            pivot_map = _load_pivot_map(conn, trade_date)
        day_stocks = kbar_universe_for_day(conn, stock_ids, trade_date)
        for stock_id in day_stocks:
            if mode == "pivot_retest" and stock_id not in pivot_map:
                n_pivot_skips += 1
                continue
            key = (stock_id, trade_date)
            n_kbar_checks += 1
            if key not in bars_cache:
                bars_cache[key] = load_kbar_day_bars(conn, stock_id, trade_date)
            bars = bars_cache[key]
            if not bars or len(bars) < cfg.min_bars_per_day:
                continue
            n_kbar_hits += 1
            pivot = pivot_map.get(stock_id) if mode == "pivot_retest" else None
            trig = detect_standalone_entry(mode, bars, pivot_price=pivot)
            if trig is None:
                continue
            period = _period_from_trigger(
                conn,
                stock_id=stock_id,
                trade_date=trade_date,
                trig=trig,
                hold_days=cfg.hold_days,
                bars=bars,
                strategy=mode,
            )
            if period is not None:
                periods.append(period)

    summary = summarize_periods(periods)
    excesses = [p["excess_pct"] for p in periods]
    summary["mean_excess_pct"] = round(sum(excesses) / len(excesses), 4) if excesses else None
    summary["n_stopped"] = sum(1 for p in periods if p["exit_reason"] == "stop")
    summary["n_time_exit"] = sum(1 for p in periods if p["exit_reason"] == "time")
    summary["max_drawdown_pct"] = _max_drawdown_pct(periods)
    summary["kbar_checks"] = n_kbar_checks
    summary["kbar_hits"] = n_kbar_hits
    summary["pivot_skips"] = n_pivot_skips
    return periods, summary


def _max_drawdown_pct(periods: list[dict[str, Any]]) -> float | None:
    """Equal-weight daily portfolio · 同日多筆訊號先均化再累積。"""
    if not periods:
        return None
    by_date: dict[str, list[float]] = {}
    for p in periods:
        by_date.setdefault(p["entry_date"], []).append(float(p["return_pct"]))
    daily_rets = [sum(v) / len(v) for v in by_date.values()]
    ordered_dates = sorted(by_date)
    daily_rets = [sum(by_date[d]) / len(by_date[d]) for d in ordered_dates]
    equity = 100.0
    peak = equity
    max_dd = 0.0
    for ret in daily_rets:
        equity *= 1.0 + ret / 100.0
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0
        max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def run_standalone_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    modes: tuple[StandaloneMode, ...] = STANDALONE_MODES,
    hold_days: int = DEFAULT_HOLD_DAYS,
    min_stock_days: int = 30,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    universe = stocks_with_kbar_coverage(
        conn,
        date_start=date_start,
        date_end=date_end,
        min_stock_days=min_stock_days,
    )
    cfg = StandaloneConfig(hold_days=hold_days)
    variants: list[dict[str, Any]] = []
    for mode in modes:
        periods, summary = simulate_standalone_strategy(
            conn,
            mode=mode,
            trade_dates=trade_dates,
            stock_ids=universe,
            config=cfg,
        )
        variants.append(
            {
                "strategy_id": mode,
                "label": MODE_LABELS[mode],
                "summary": summary,
                "sample_periods": periods[:5],
            }
        )
    bench_only = summary_bench_do_nothing(conn, trade_dates, hold_days)
    return {
        "date_start": date_start,
        "date_end": date_end,
        "exit_rule": f"structure stop same-day · else hold {hold_days} trading days close",
        "benchmark": "IX0001 close-to-close",
        "universe_note": (
            "ETF 成分股監控聯集 ∩ 區間內 ≥30 個有 1m K 的交易日 · "
            "pivot_retest 另需當日 vcp_screen pivot_price"
        ),
        "universe_size": len(universe),
        "trade_days": len(trade_dates),
        "hold_days": hold_days,
        "bench_do_nothing": bench_only,
        "variants": variants,
    }


def summary_bench_do_nothing(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    hold_days: int,
) -> dict[str, Any]:
    """Buy-and-hold bench：每個可進場窗口的 IX0001 報酬（無個股 alpha）。"""
    rets: list[float] = []
    for d in trade_dates:
        exit_d = exit_close_date_from_entry(conn, d, hold_days)
        if exit_d is None:
            continue
        bench = bench_return_entry_to_exit(conn, d, exit_d, entry_price_mode="close")
        if bench is not None:
            rets.append(bench)
    n = len(rets)
    return {
        "n_windows": n,
        "mean_bench_pct": round(sum(rets) / n, 4) if n else None,
        "label": f"IX0001 hold {hold_days}d · 每交易日窗口",
    }


def config_to_dict(cfg: StandaloneConfig) -> dict[str, Any]:
    return asdict(cfg)
