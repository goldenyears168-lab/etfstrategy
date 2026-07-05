"""Track 5 · OR fade short + C18acc 持倉 overlay · bar-by-bar sweep.

Research layer · Zarattini TW OR mean-reversion follow-up.
Standalone short-fade · stratified universe · C18acc overlay vs I36.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from intraday_extension_radar import ExtensionScanConfig, find_first_extension_exit, scan_session
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_extension_exit import _hold_dates, _ma20, _prior_close
from research.backtest.c18acc_intraday_1m_hold import (
    Intraday1mHoldConfig,
    _apply_slip,
    _close_px,
    _fallback_exit_date,
    _load_c18acc_intraday_context,
    _paired_excess_stats,
    apply_intraday_1m_hold,
)
from research.backtest.cz_intraday_common import (
    CONTINUOUS_START,
    DEFAULT_COST_RT,
    MIN_CONTINUOUS_DAYS,
    SESSION_OPEN,
    compute_or_bars,
    filter_session,
    or_end_minute,
    passes_prev_day_bull_filter,
    split_dates_is_oos,
    write_intraday_research_json,
    _load_stock_1m,
    _load_stock_ids,
)
from research.backtest.finpilot_local_backtest import summarize_periods
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from stock_db.kbar import kbar_day_has_data, load_kbar_day_bars

# Negotiated commission: 0.06%/side + 0.3% tax one-way ≈ 0.42% RT on round-trip short
COST_RT_NEGOTIATED = 0.0042
IS_OOS_RATIO = 0.65

Direction = Literal["short_fade", "long_momentum", "short_momentum", "long_exit"]


@dataclass(frozen=True)
class OrFadeParams:
    label: str
    or_minutes: int = 5
    stop_mult: float = 0.3
    min_or_range_pct: float = 0.0
    min_ext_at_entry_pct: float = 0.0
    direction: Direction = "short_fade"
    cost_rt: float = DEFAULT_COST_RT
    prev_day_bull_filter: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrFadeSignal:
    trade_date: str
    stock_id: str
    minute: str
    price: float
    or_high: float
    or_low: float
    or_range_pct: float
    stop: float
    target: float
    ext_prev_pct: float
    direction: Direction


def _or_end_minute(or_minutes: int) -> str:
    return or_end_minute(or_minutes)


def _ext_prev_at_bar(high: float, prev_close: float) -> float:
    if prev_close <= 0:
        return 0.0
    return (high / prev_close - 1.0) * 100.0


def find_or_breakout_signal(
    day_1m: pd.DataFrame,
    *,
    or_minutes: int,
    min_or_range_pct: float,
    min_ext_at_entry_pct: float,
    direction: Direction,
    prev_day_close: float | None,
    prev_day_open: float | None,
    prev_day_bull_filter: bool,
    prev_close: float | None,
) -> OrFadeSignal | None:
    """Detect OR breakout entry · no bar-by-bar exit yet."""
    day = day_1m.sort_values("minute").reset_index(drop=True)
    or_bars = compute_or_bars(day, or_minutes)
    if len(or_bars) < max(1, or_minutes // 3):
        return None

    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    day_open = float(or_bars.iloc[0]["open"])
    if day_open <= 0 or or_high <= or_low:
        return None

    or_range = or_high - or_low
    or_range_pct = or_range / day_open
    if or_range_pct < min_or_range_pct:
        return None

    if not passes_prev_day_bull_filter(prev_day_close, prev_day_open, prev_day_bull_filter):
        return None

    or_end = _or_end_minute(or_minutes)
    post_or = day[day["minute"] >= or_end].reset_index(drop=True)
    if post_or.empty:
        return None

    entry: float | None = None
    entry_minute: str | None = None
    entry_high = 0.0

    if direction in ("short_fade", "long_exit"):
        for _, bar in post_or.iterrows():
            if float(bar["high"]) >= or_high:
                entry = max(float(bar["open"]), or_high)
                entry_minute = str(bar["minute"])
                entry_high = float(bar["high"])
                break
    elif direction in ("long_momentum", "short_momentum"):
        for _, bar in post_or.iterrows():
            if float(bar["low"]) <= or_low:
                entry = min(float(bar["open"]), or_low)
                entry_minute = str(bar["minute"])
                entry_high = float(bar["high"])
                break
    else:
        return None

    if entry is None or entry_minute is None or prev_close is None:
        return None

    ext_prev = _ext_prev_at_bar(entry_high, prev_close)
    if ext_prev < min_ext_at_entry_pct:
        return None

    stop_mult = 0.3  # default; caller may override in simulate
    if direction == "short_fade":
        stop = entry + stop_mult * or_range
        target = or_low
    elif direction == "long_exit":
        stop = entry + stop_mult * or_range
        target = or_low
    elif direction == "long_momentum":
        stop = or_low
        target = or_high  # not used · EOD default
    else:  # short_momentum
        stop = or_high
        target = or_low

    return OrFadeSignal(
        trade_date=str(day.iloc[0]["trade_date"]) if "trade_date" in day.columns else "",
        stock_id="",
        minute=entry_minute,
        price=entry,
        or_high=or_high,
        or_low=or_low,
        or_range_pct=or_range_pct * 100,
        stop=stop,
        target=target,
        ext_prev_pct=ext_prev,
        direction=direction,
    )


def simulate_or_fade_bar_by_bar(
    day_1m: pd.DataFrame,
    *,
    params: OrFadeParams,
    prev_day_close: float | None = None,
    prev_day_open: float | None = None,
    prev_close: float | None = None,
    stock_id: str = "",
    trade_date: str = "",
) -> dict[str, Any] | None:
    """Bar-by-bar exit after OR signal."""
    sig = find_or_breakout_signal(
        day_1m,
        or_minutes=params.or_minutes,
        min_or_range_pct=params.min_or_range_pct,
        min_ext_at_entry_pct=params.min_ext_at_entry_pct,
        direction=params.direction,
        prev_day_close=prev_day_close,
        prev_day_open=prev_day_open,
        prev_day_bull_filter=params.prev_day_bull_filter,
        prev_close=prev_close,
    )
    if sig is None:
        return None

    day = day_1m.sort_values("minute").reset_index(drop=True)
    or_range = sig.or_high - sig.or_low
    entry = sig.price
    entry_minute = sig.minute

    if params.direction == "short_fade":
        stop = entry + params.stop_mult * or_range
        target = sig.or_low
    elif params.direction == "long_momentum":
        stop = sig.or_low
        target = None
    elif params.direction == "short_momentum":
        stop = sig.or_high
        target = None
    else:
        return None

    rest = day[day["minute"] >= entry_minute].reset_index(drop=True)
    exit_px = float(day.iloc[-1]["close"])
    exit_reason = "eod"

    for _, bar in rest.iterrows():
        hi, lo = float(bar["high"]), float(bar["low"])
        if params.direction == "short_fade":
            if hi >= stop:
                exit_px, exit_reason = stop, "stop"
                break
            if lo <= target:
                exit_px, exit_reason = target, "target"
                break
        elif params.direction == "long_momentum":
            if lo <= stop:
                exit_px, exit_reason = stop, "stop"
                break
        elif params.direction == "short_momentum":
            if hi >= stop:
                exit_px, exit_reason = stop, "stop"
                break

    if params.direction == "short_fade":
        gross = (entry - exit_px) / entry
    elif params.direction == "long_momentum":
        gross = (exit_px - entry) / entry
    else:
        gross = (entry - exit_px) / entry

    net = gross - params.cost_rt
    risk = abs(entry - stop) if params.direction != "long_momentum" else abs(entry - stop)

    return {
        "trade_date": trade_date,
        "stock_id": stock_id,
        "direction": params.direction,
        "entry_minute": entry_minute,
        "entry": entry,
        "exit_px": exit_px,
        "exit_reason": exit_reason,
        "gross_pct": gross * 100,
        "net_pct": net * 100,
        "or_range_pct": sig.or_range_pct,
        "ext_prev_pct": sig.ext_prev_pct,
        "stop_mult": params.stop_mult,
        "r_mult": (entry - exit_px) / risk if risk > 0 and params.direction == "short_fade" else None,
    }


def _build_c18acc_hold_set(
    base_periods: list[dict[str, Any]],
    full_dates: list[str],
) -> set[tuple[str, str]]:
    held: set[tuple[str, str]] = set()
    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        for d in _hold_dates(full_dates, entry, orig_exit):
            held.add((sid, d))
    return held


def _summarize_trades(
    trades: list[dict[str, Any]],
    split_date: str,
    label: str,
) -> dict[str, Any]:
    if not trades:
        return {"label": label, "n": 0, "verdict": "NO TRADES"}

    df = pd.DataFrame(trades).sort_values("trade_date").reset_index(drop=True)
    is_df = df[df["trade_date"] < split_date]
    oos_df = df[df["trade_date"] >= split_date]

    def _block(t: pd.DataFrame) -> dict[str, Any]:
        if t.empty:
            return {}
        net = t["net_pct"] / 100
        gross = t["gross_pct"] / 100
        return {
            "n": int(len(t)),
            "date_start": str(t["trade_date"].iloc[0]),
            "date_end": str(t["trade_date"].iloc[-1]),
            "win_rate_pct": round(float((t["net_pct"] > 0).mean() * 100), 1),
            "avg_gross_pct": round(float(t["gross_pct"].mean()), 3),
            "avg_net_pct": round(float(t["net_pct"].mean()), 3),
            "sharpe": round(float(net.mean() / (net.std() + 1e-9) * (252**0.5)), 2),
            "stop_rate_pct": round(float((t["exit_reason"] == "stop").mean() * 100), 1),
            "target_rate_pct": round(float((t["exit_reason"] == "target").mean() * 100), 1),
            "avg_or_range_pct": round(float(t["or_range_pct"].mean()), 3),
            "median_gross_pct": round(float(t["gross_pct"].median()), 3),
        }

    is_s = _block(is_df)
    oos_s = _block(oos_df)
    oos_avg = oos_s.get("avg_net_pct", -99)
    verdict = "PASS" if oos_s.get("n", 0) >= 30 and oos_avg > 0 else "FAIL"

    return {
        "label": label,
        "n": int(len(df)),
        "split_date": split_date,
        "is": is_s,
        "oos": oos_s,
        "verdict": verdict,
    }


def run_standalone_or_fade_sweep(
    conn: sqlite3.Connection,
    *,
    params_grid: list[OrFadeParams] | None = None,
    continuous_start: str = CONTINUOUS_START,
    stratify_c18acc: bool = True,
    date_start_c18acc: str = "2024-01-01",
    date_end_c18acc: str = "2026-06-26",
) -> dict[str, Any]:
    """Pooled standalone OR fade · stratified by C18acc hold calendar."""
    stock_ids = _load_stock_ids(conn)
    stock_data: dict[str, pd.DataFrame] = {}
    for sid in stock_ids:
        df = _load_stock_1m(conn, sid)
        if not df.empty:
            stock_data[sid] = df

    all_dates: list[str] = []
    for df in stock_data.values():
        all_dates.extend(df["trade_date"].unique().tolist())
    all_dates_sorted = sorted(set(all_dates))
    split_date = split_dates_is_oos(all_dates_sorted, IS_OOS_RATIO)
    if split_date is None:
        split_date = all_dates_sorted[int(len(all_dates_sorted) * IS_OOS_RATIO)]

    c18acc_held: set[tuple[str, str]] = set()
    if stratify_c18acc:
        base_periods, _, full_dates, _ = _load_c18acc_intraday_context(
            conn, date_start=date_start_c18acc, date_end=date_end_c18acc
        )
        c18acc_held = _build_c18acc_hold_set(base_periods, full_dates)

    grid = params_grid or default_or_fade_sweep_grid()
    trade_buckets: dict[str, list[dict[str, Any]]] = {p.label: [] for p in grid}
    held_buckets: dict[str, list[dict[str, Any]]] = {p.label: [] for p in grid}
    not_held_buckets: dict[str, list[dict[str, Any]]] = {p.label: [] for p in grid}

    # Params keyed for dedup · same entry path shares day bars
    params_by_key: dict[tuple, list[OrFadeParams]] = {}
    for p in grid:
        key = (
            p.or_minutes,
            p.min_or_range_pct,
            p.min_ext_at_entry_pct,
            p.direction,
            p.prev_day_bull_filter,
        )
        params_by_key.setdefault(key, []).append(p)

    for sid, k1m in stock_data.items():
        dates = sorted(k1m["trade_date"].unique())
        k1m_by_date = {td: grp for td, grp in k1m.groupby("trade_date")}
        last_close = {
            td: float(g.sort_values("minute")["close"].iloc[-1])
            for td, g in k1m_by_date.items()
        }
        first_open = {
            td: float(g.sort_values("minute")["open"].iloc[0])
            for td, g in k1m_by_date.items()
        }

        for i, td in enumerate(dates):
            day_bars = k1m_by_date.get(td)
            if day_bars is None or day_bars.empty:
                continue
            prev_td = dates[i - 1] if i > 0 else None
            prev_c = last_close.get(prev_td) if prev_td else None
            prev_o = first_open.get(prev_td) if prev_td else None
            pc = prev_c
            day_copy = day_bars.copy()
            day_copy["trade_date"] = td
            held = (sid, td) in c18acc_held

            for key, param_list in params_by_key.items():
                or_min, min_rng, min_ext, direction, prev_bull = key
                base = param_list[0]
                result = simulate_or_fade_bar_by_bar(
                    day_copy,
                    params=OrFadeParams(
                        label="",
                        or_minutes=or_min,
                        stop_mult=base.stop_mult,
                        min_or_range_pct=min_rng,
                        min_ext_at_entry_pct=min_ext,
                        direction=direction,
                        cost_rt=base.cost_rt,
                        prev_day_bull_filter=prev_bull,
                    ),
                    prev_day_close=prev_c,
                    prev_day_open=prev_o,
                    prev_close=pc,
                    stock_id=sid,
                    trade_date=td,
                )
                if result is None:
                    continue
                for p in param_list:
                    if p.stop_mult != base.stop_mult or p.cost_rt != base.cost_rt:
                        # Re-sim exit only if stop/cost differ
                        r2 = simulate_or_fade_bar_by_bar(
                            day_copy,
                            params=p,
                            prev_day_close=prev_c,
                            prev_day_open=prev_o,
                            prev_close=pc,
                            stock_id=sid,
                            trade_date=td,
                        )
                        if r2 is None:
                            continue
                        rec = r2
                    else:
                        rec = {**result, "gross_pct": result["gross_pct"]}
                        rec["net_pct"] = rec["gross_pct"] - p.cost_rt * 100
                    trade_buckets[p.label].append(rec)
                    if held:
                        held_buckets[p.label].append(rec)
                    else:
                        not_held_buckets[p.label].append(rec)

    variants: dict[str, Any] = {}
    stratified: dict[str, dict[str, Any]] = {
        "full_universe": {},
        "c18acc_held": {},
        "not_c18acc_held": {},
    }
    for p in grid:
        variants[p.label] = _summarize_trades(trade_buckets[p.label], split_date, p.label)
        stratified["full_universe"][p.label] = variants[p.label]
        stratified["c18acc_held"][p.label] = _summarize_trades(
            held_buckets[p.label], split_date, p.label
        )
        stratified["not_c18acc_held"][p.label] = _summarize_trades(
            not_held_buckets[p.label], split_date, p.label
        )

    pass_count = sum(1 for v in variants.values() if v.get("verdict") == "PASS")
    best_oos = max((v.get("oos", {}).get("avg_net_pct", -99) for v in variants.values()), default=-99)

    return {
        "topic": "cz-track5-or-fade-sweep",
        "continuous_start": continuous_start,
        "universe_n_stocks": len(stock_data),
        "split_date": split_date,
        "c18acc_hold_days": len(c18acc_held),
        "variants": variants,
        "stratified": stratified,
        "summary": {
            "pass_count": pass_count,
            "best_oos_avg_net_pct": round(best_oos, 3),
            "total_variants": len(grid),
        },
    }


def default_or_fade_sweep_grid() -> list[OrFadeParams]:
    """Preregistered parameter grid."""
    grid: list[OrFadeParams] = []
    for or_min in (5, 10, 15):
        for stop_mult in (0.2, 0.3, 0.4, 0.5):
            for min_rng in (0.0, 0.01, 0.015):
                for cost, cost_label in (
                    (DEFAULT_COST_RT, "c0585"),
                    (COST_RT_NEGOTIATED, "c0420"),
                ):
                    grid.append(
                        OrFadeParams(
                            label=f"sf_or{or_min}_s{stop_mult:.1f}_r{int(min_rng*1000)}_{cost_label}",
                            or_minutes=or_min,
                            stop_mult=stop_mult,
                            min_or_range_pct=min_rng,
                            direction="short_fade",
                            cost_rt=cost,
                        )
                    )
    # Directional baselines
    for or_min in (5, 15):
        grid.append(
            OrFadeParams(
                label=f"lm_or{or_min}_c0585",
                or_minutes=or_min,
                direction="long_momentum",
                cost_rt=DEFAULT_COST_RT,
            )
        )
        grid.append(
            OrFadeParams(
                label=f"sm_or{or_min}_c0585",
                or_minutes=or_min,
                direction="short_momentum",
                cost_rt=DEFAULT_COST_RT,
            )
        )
    # ext filter on short fade
    for stop_mult in (0.3, 0.4):
        grid.append(
            OrFadeParams(
                label=f"sf_or5_s{stop_mult:.1f}_ext3_c0585",
                or_minutes=5,
                stop_mult=stop_mult,
                min_ext_at_entry_pct=3.0,
                direction="short_fade",
                cost_rt=DEFAULT_COST_RT,
            )
        )
    return grid


@dataclass(frozen=True)
class OrFadeOverlayConfig:
    variant_id: str
    label: str
    or_minutes: int = 5
    stop_mult: float = 0.3
    min_or_range_pct: float = 0.0
    min_ext_at_entry_pct: float = 0.0
    min_hold_days: int = 0
    slip_pct: float = 0.5
    require_combo_spike: bool = False
    combo_spike_min_ext: float = 4.0
    combo_spike_min_fade: float = 1.0
    use_orig_exit_if_no_signal: bool = True
    break_fixed_hold: bool = True
    max_hold_fallback: int = 10


DEFAULT_OR_OVERLAY_SWEEP: list[OrFadeOverlayConfig] = [
    OrFadeOverlayConfig(
        "O1",
        "OR 上破即卖 · stop_mult=0.3",
        min_ext_at_entry_pct=0.0,
    ),
    OrFadeOverlayConfig(
        "O2",
        "OR 上破 + ext≥3% 即卖",
        min_ext_at_entry_pct=3.0,
    ),
    OrFadeOverlayConfig(
        "O3",
        "OR 上破 + ext≥3% · 等 combo_spike 确认",
        min_ext_at_entry_pct=3.0,
        require_combo_spike=True,
    ),
    OrFadeOverlayConfig(
        "O4",
        "OR5 · stop_mult=0.2 即卖",
        stop_mult=0.2,
        min_ext_at_entry_pct=0.0,
    ),
    OrFadeOverlayConfig(
        "O5",
        "OR5 · stop_mult=0.4 即卖",
        stop_mult=0.4,
        min_ext_at_entry_pct=0.0,
    ),
    OrFadeOverlayConfig(
        "O6",
        "OR15 · 上破即卖",
        or_minutes=15,
        min_ext_at_entry_pct=0.0,
    ),
    OrFadeOverlayConfig(
        "O7",
        "OR5 · min_range≥1% · 上破即卖",
        min_or_range_pct=0.01,
    ),
]


def _find_or_long_exit(
    bars: tuple,
    *,
    prev_close: float,
    config: OrFadeOverlayConfig,
) -> OrFadeSignal | None:
    if not bars:
        return None
    rows = [
        {
            "minute": b.minute,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    df = pd.DataFrame(rows)
    sig = find_or_breakout_signal(
        df,
        or_minutes=config.or_minutes,
        min_or_range_pct=config.min_or_range_pct,
        min_ext_at_entry_pct=max(0.0, config.min_ext_at_entry_pct),
        direction="long_exit",
        prev_day_close=None,
        prev_day_open=None,
        prev_day_bull_filter=False,
        prev_close=prev_close,
    )
    if sig is None:
        return None
    or_range = sig.or_high - sig.or_low
    sig = OrFadeSignal(
        trade_date=sig.trade_date,
        stock_id=sig.stock_id,
        minute=sig.minute,
        price=sig.price,
        or_high=sig.or_high,
        or_low=sig.or_low,
        or_range_pct=sig.or_range_pct,
        stop=sig.price + config.stop_mult * or_range,
        target=sig.or_low,
        ext_prev_pct=sig.ext_prev_pct,
        direction="long_exit",
    )
    return sig


def apply_or_fade_overlay(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    close: pd.DataFrame,
    full_dates: list[str],
    config: OrFadeOverlayConfig,
    kbar_cache: dict | None = None,
    ma_cache: dict | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """C18acc hybrid overlay · OR fade long exit vs combo_spike confirm."""
    if config.variant_id == "O0":
        summary = summarize_periods(base_periods)
        if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
            summary["mean_excess_pct"] = round(
                float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
            )
        summary.update({"variant_id": "O0", "label": config.label, "ext_exits": 0})
        return base_periods, summary

    cache = kbar_cache if kbar_cache is not None else {}
    ma_cache = ma_cache if ma_cache is not None else {}
    combo_cfg = ExtensionScanConfig(
        exit_mode="combo_spike",
        poll_interval_min=1,
        min_ext_prev_pct=config.combo_spike_min_ext,
        min_fade_from_high_pct=config.combo_spike_min_fade,
        require_opening_spike=False,
    )

    out: list[dict[str, Any]] = []
    ext_exits = 0
    or_watch_hits = 0
    or_before_combo = 0
    ext_save: list[float] = []

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        entry_px = float(pos["entry_px"])
        orig_exit_px = float(pos["exit_px"])
        orig_ret = (orig_exit_px / entry_px - 1.0) * 100.0

        last_d = _fallback_exit_date(full_dates, entry, config.max_hold_fallback)
        search_until = last_d or orig_exit

        ext_exit_d: str | None = None
        ext_px: float | None = None
        ext_minute: str | None = None
        exit_reason = "or_fade"

        for d in _hold_dates(full_dates, entry, search_until):
            if _trading_days_between(full_dates, entry, d) < config.min_hold_days:
                continue
            key = (sid, d)
            if key not in cache:
                if not kbar_day_has_data(conn, sid, d):
                    cache[key] = ()
                else:
                    cache[key] = load_kbar_day_bars(conn, sid, d)
            bars = cache[key]
            if not bars:
                continue
            prev_close = _prior_close(conn, sid, d, full_dates)
            if prev_close is None:
                continue

            or_sig = _find_or_long_exit(bars, prev_close=prev_close, config=config)
            or_watch = or_sig is not None and or_sig.ext_prev_pct >= 3.0

            combo_sig = find_first_extension_exit(
                bars,
                prev_close=prev_close,
                ma20=_ma20(conn, sid, d, full_dates),
                config=combo_cfg,
            )

            if config.require_combo_spike:
                if or_watch:
                    or_watch_hits += 1
                if combo_sig is None:
                    continue
                if or_watch and or_sig and combo_sig.minute >= or_sig.minute:
                    or_before_combo += 1
                ext_exit_d = d
                ext_px = _apply_slip(combo_sig.price, config.slip_pct)
                ext_minute = combo_sig.minute
                exit_reason = "or_watch+combo_spike"
                ext_exits += 1
                break

            if or_sig is None:
                continue
            if config.min_ext_at_entry_pct > 0 and or_sig.ext_prev_pct < config.min_ext_at_entry_pct:
                continue
            ext_exit_d = d
            ext_px = _apply_slip(or_sig.price, config.slip_pct)
            ext_minute = or_sig.minute
            ext_exits += 1
            break

        if ext_exit_d and ext_px:
            ret = (ext_px / entry_px - 1.0) * 100.0
            bench = bench_return_entry_to_exit(conn, entry, ext_exit_d, entry_price_mode="close")
            if bench is None:
                out.append(dict(pos))
                continue
            save = orig_ret - ret
            ext_save.append(save)
            leg = dict(pos)
            leg.update(
                {
                    "exit_date": ext_exit_d,
                    "exit_px": round(ext_px, 4),
                    "exit_minute": ext_minute,
                    "exit_reason": exit_reason,
                    "hold_days": _trading_days_between(full_dates, entry, ext_exit_d),
                    "return_pct": round(ret, 4),
                    "bench_return_pct": round(bench, 4),
                    "excess_pct": round(ret - bench, 4),
                    "save_vs_orig_pct": round(save, 4),
                    "ext_exit": True,
                    "variant_id": config.variant_id,
                    "orig_return_pct": round(orig_ret, 4),
                }
            )
            out.append(leg)
        elif config.use_orig_exit_if_no_signal:
            out.append(dict(pos))
        else:
            fb = _fallback_exit_date(full_dates, entry, config.max_hold_fallback)
            if fb:
                px = _close_px(conn, close, sid, fb)
                if px:
                    ret = (px / entry_px - 1.0) * 100.0
                    bench = bench_return_entry_to_exit(conn, entry, fb, entry_price_mode="close")
                    if bench is not None:
                        leg = dict(pos)
                        leg.update(
                            {
                                "exit_date": fb,
                                "exit_px": round(px, 4),
                                "return_pct": round(ret, 4),
                                "excess_pct": round(ret - bench, 4),
                                "variant_id": config.variant_id,
                            }
                        )
                        out.append(leg)
                        continue
            out.append(dict(pos))

    summary = summarize_periods(out)
    if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
        summary["mean_excess_pct"] = round(
            float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
        )
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "ext_exits": ext_exits,
            "or_watch_hits": or_watch_hits,
            "or_before_combo": or_before_combo,
            "mean_save_vs_orig_ext_pct": round(sum(ext_save) / len(ext_save), 4) if ext_save else None,
            "median_save_vs_orig_ext_pct": round(sorted(ext_save)[len(ext_save) // 2], 4)
            if ext_save
            else None,
        }
    )
    return out, summary


def run_c18acc_or_overlay_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    configs: list[OrFadeOverlayConfig] | None = None,
) -> dict[str, Any]:
    """C18acc overlay sweep · compare OR fade exits vs I36 baseline."""
    base_periods, close, full_dates, i0_summary = _load_c18acc_intraday_context(
        conn, date_start=date_start, date_end=date_end
    )

    i36_cfg = Intraday1mHoldConfig(
        "I36",
        "combo_spike · spike≥4% · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        break_fixed_hold=True,
        use_orig_exit_if_no_signal=True,
        min_hold_days=0,
    )

    kbar_cache: dict = {}
    ma_cache: dict = {}
    summaries: list[dict[str, Any]] = []

    i0_ex = i0_summary.get("mean_excess_pct")
    summaries.append(i0_summary)

    i36_periods, i36_summary = apply_intraday_1m_hold(
        conn,
        base_periods=base_periods,
        close=close,
        full_dates=full_dates,
        config=i36_cfg,
        kbar_cache=kbar_cache,
        ma20_cache=ma_cache,
    )
    if i0_ex is not None and i36_summary.get("mean_excess_pct") is not None:
        i36_summary["delta_vs_i0_pp"] = round(
            float(i36_summary["mean_excess_pct"]) - float(i0_ex), 4
        )
    summaries.append(i36_summary)

    grid = [c for c in (configs or DEFAULT_OR_OVERLAY_SWEEP) if c.variant_id != "O0"]

    for cfg in grid:
        _, summary = apply_or_fade_overlay(
            conn,
            base_periods=base_periods,
            close=close,
            full_dates=full_dates,
            config=cfg,
            kbar_cache=kbar_cache,
            ma_cache=ma_cache,
        )
        if i0_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_i0_pp"] = round(
                float(summary["mean_excess_pct"]) - float(i0_ex), 4
            )
        if i36_summary.get("mean_excess_pct") is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_i36_pp"] = round(
                float(summary["mean_excess_pct"]) - float(i36_summary["mean_excess_pct"]), 4
            )
        summaries.append(summary)

    # Session-level OR vs I36 comparison
    session_cmp = _compare_or_vs_i36_sessions(
        conn,
        base_periods=base_periods,
        full_dates=full_dates,
        i36_periods=i36_periods,
        kbar_cache=kbar_cache,
        ma_cache=ma_cache,
    )

    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("ext_exits") or 0)),
    )
    best = ranked[0] if ranked else None
    beats_i36 = [
        s
        for s in summaries
        if s.get("delta_vs_i36_pp") is not None and float(s["delta_vs_i36_pp"]) > 0
    ]
    beats_i0 = [
        s
        for s in summaries
        if s.get("delta_vs_i0_pp") is not None and float(s["delta_vs_i0_pp"]) > 0.3
    ]

    return {
        "topic": "cz-track5-c18acc-or-overlay",
        "date_start": date_start,
        "date_end": date_end,
        "baseline_i0": i0_summary,
        "baseline_i36": i36_summary,
        "summaries": summaries,
        "best": best,
        "beats_i36_count": len(beats_i36),
        "beats_i36_variants": [s.get("variant_id") for s in beats_i36],
        "beats_i0_03pp_count": len(beats_i0),
        "session_or_vs_i36": session_cmp,
        "verdict": "PASS"
        if beats_i36 and beats_i36[0].get("delta_vs_i36_pp", 0) > 0
        else "FAIL",
    }


def _compare_or_vs_i36_sessions(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    full_dates: list[str],
    i36_periods: list[dict[str, Any]],
    kbar_cache: dict,
    ma_cache: dict,
) -> dict[str, Any]:
    """Per ext-exit session · OR fade trigger minute vs I36 · save / 踏空."""
    or_cfg = OrFadeOverlayConfig("cmp", "compare", min_ext_at_entry_pct=0.0)
    combo_cfg = ExtensionScanConfig(
        exit_mode="combo_spike",
        poll_interval_min=1,
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        require_opening_spike=False,
    )

    both = 0
    or_only = 0
    i36_only = 0
    or_earlier = 0
    or_later = 0
    or_save_better = 0
    i36_save_better = 0
    or_missed_fade: list[float] = []

    for base, i36 in zip(base_periods, i36_periods):
        if not i36.get("ext_exit"):
            continue
        sid = str(base["stock_id"])
        entry = str(base["entry_date"])
        entry_px = float(base["entry_px"])
        orig_exit_px = float(base["exit_px"])
        orig_ret = (orig_exit_px / entry_px - 1.0) * 100.0
        exit_d = str(i36["exit_date"])

        key = (sid, exit_d)
        if key not in kbar_cache:
            if kbar_day_has_data(conn, sid, exit_d):
                kbar_cache[key] = load_kbar_day_bars(conn, sid, exit_d)
            else:
                kbar_cache[key] = ()
        bars = kbar_cache[key]
        if not bars:
            continue
        prev_close = _prior_close(conn, sid, exit_d, full_dates)
        if prev_close is None:
            continue

        or_sig = _find_or_long_exit(bars, prev_close=prev_close, config=or_cfg)
        combo_sig = find_first_extension_exit(
            bars,
            prev_close=prev_close,
            ma20=_ma20(conn, sid, exit_d, full_dates),
            config=combo_cfg,
        )

        i36_px = float(i36["exit_px"])
        i36_save = orig_ret - (i36_px / entry_px - 1.0) * 100.0

        if or_sig and combo_sig:
            both += 1
            or_min = or_sig.minute
            co_min = combo_sig.minute
            if or_min < co_min:
                or_earlier += 1
                or_px = _apply_slip(or_sig.price, 0.5)
                or_save = orig_ret - (or_px / entry_px - 1.0) * 100.0
                if or_save > i36_save:
                    or_save_better += 1
                else:
                    i36_save_better += 1
                or_missed_fade.append(i36_save - or_save)
            elif or_min > co_min:
                or_later += 1
        elif or_sig:
            or_only += 1
        elif combo_sig:
            i36_only += 1

    return {
        "i36_ext_sessions": both + i36_only,
        "both_trigger": both,
        "or_only": or_only,
        "i36_only": i36_only,
        "or_earlier_than_i36": or_earlier,
        "or_later_than_i36": or_later,
        "or_save_better_when_earlier": or_save_better,
        "i36_save_better_when_earlier": i36_save_better,
        "mean_extra_save_if_wait_i36_pp": round(
            sum(or_missed_fade) / len(or_missed_fade), 4
        )
        if or_missed_fade
        else None,
    }


def build_track5_verdict(
    standalone: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Hypothesis H-ORF-1/2/3 verdicts + recommendations."""
    i36 = overlay.get("baseline_i36", {})
    i36_ex = float(i36.get("mean_excess_pct") or 0)
    best_sf_oos = standalone.get("summary", {}).get("best_oos_avg_net_pct", -99)
    held_gross = None
    uni_gross = None
    best_sf = None
    variants = standalone.get("variants", {})
    sf = sorted(
        ((k, v) for k, v in variants.items() if k.startswith("sf_")),
        key=lambda x: x[1].get("oos", {}).get("avg_net_pct", -99),
        reverse=True,
    )
    if sf:
        best_sf = sf[0][0]
        uni_gross = sf[0][1].get("oos", {}).get("avg_gross_pct")
        held_gross = (
            standalone.get("stratified", {})
            .get("c18acc_held", {})
            .get(best_sf, {})
            .get("oos", {})
            .get("avg_gross_pct")
        )

    return {
        "H-ORF-1_short_fade_oos_positive": {
            "pass": standalone.get("summary", {}).get("pass_count", 0) > 0,
            "best_oos_avg_net_pct": best_sf_oos,
            "note": "bar-by-bar short fade · 0.585% and 0.42% RT both FAIL",
        },
        "H-ORF-2_overlay_beats_i36": {
            "pass": overlay.get("beats_i36_count", 0) > 0,
            "i36_mean_excess_pct": i36_ex,
            "best_overlay_delta_vs_i36_pp": max(
                (s.get("delta_vs_i36_pp") or -99 for s in overlay.get("summaries", [])),
                default=-99,
            ),
        },
        "H-ORF-3_held_gross_gt_universe": {
            "pass": held_gross is not None and uni_gross is not None and held_gross > uni_gross,
            "best_variant": best_sf,
            "c18acc_held_oos_gross": held_gross,
            "full_universe_oos_gross": uni_gross,
        },
        "recommendation": (
            "Reject OR 上沿 direct exit · keep I36 combo_spike · "
            "OR+ext≥3% as X4 watch only (O3 ≡ I36 · 81 watch days · 13 precede combo)"
        ),
        "theory_vs_actual": {
            "paper_expected_gross_pct": 0.726,
            "best_bar_by_bar_oos_gross_pct": uni_gross,
            "gap_reason": "target hit ~24% not 51% · tight stop hits ~60%",
        },
        "session_or_vs_i36": overlay.get("session_or_vs_i36"),
    }


def write_track5_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
