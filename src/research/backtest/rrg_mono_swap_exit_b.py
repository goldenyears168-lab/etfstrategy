"""RRG mono hold7 · 模式 B：結構弱（左下）+ 有更強 challenger 才換倉。

與 E sweep 差異：
  E · 結構弱就賣（不管有沒有更好的標的）
  B · 結構弱 且 當日 fresh mono 有 seg_last 更高者 → 換倉；否則續抱至 max_hold
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
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from rrg_mono_daily_brief import HOLD_DAYS, LOOKBACK, MAX_SLOTS, TOP_N, ScanRow, _feat, _mono_tier2
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

StructuralGate = Literal["down_left", "quad_weak"]
ChallengerBeat = Literal["entry_seg", "held_today_seg", "any_higher"]
ChallengerPool = Literal["fresh", "mono_tier2"]
TimingMode = Literal["close", "poll_5m"]
EntryLeg = Literal["A", "C0"]

DEFAULT_EXIT_QUADRANTS = ("weakening", "lagging")


@dataclass
class SwapExitBConfig:
    variant_id: str = "B1"
    label: str = "左下 1 日 + challenger 勝 entry seg · 收盤換"
    structural_gate: StructuralGate = "down_left"
    challenger_beat: ChallengerBeat = "entry_seg"
    min_hold_days: int = 2
    max_hold_days: int = HOLD_DAYS
    exit_quadrants: tuple[str, ...] = DEFAULT_EXIT_QUADRANTS
    timing_mode: TimingMode = "close"
    poll_interval_min: int = 5
    no_trade_before: str = "09:30"
    challenger_pool: ChallengerPool = "fresh"
    challenger_top_n: int = TOP_N
    seg_margin: float = 0.0
    entry_leg: EntryLeg = "A"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_quadrants"] = list(self.exit_quadrants)
        return d


DEFAULT_SWAP_B_SWEEP: list[SwapExitBConfig] = [
    SwapExitBConfig("B1", "左下 + challenger 勝 entry seg · 收盤換"),
    SwapExitBConfig(
        "B2",
        "左下 + challenger 勝當日 held seg · 收盤換",
        challenger_beat="held_today_seg",
    ),
    SwapExitBConfig(
        "B3",
        "左下 + challenger 勝 entry seg · 5m 盤中換",
        timing_mode="poll_5m",
    ),
    SwapExitBConfig(
        "B4",
        "象限 weakening/lagging + challenger · 收盤換",
        structural_gate="quad_weak",
    ),
    SwapExitBConfig("B5", "左下 + challenger · min_hold=1", min_hold_days=1),
    SwapExitBConfig("B6", "左下 + challenger · min_hold=3", min_hold_days=3),
    # --- 放寬 challenger 池 ---
    SwapExitBConfig(
        "B7",
        "mono_tier2 池（非 fresh）+ 勝 entry seg",
        challenger_pool="mono_tier2",
    ),
    SwapExitBConfig(
        "B8",
        "mono_tier2 + 勝 held 當日 seg",
        challenger_pool="mono_tier2",
        challenger_beat="held_today_seg",
    ),
    SwapExitBConfig(
        "B9",
        "mono_tier2 + any_higher（當日 seg 更高即可）",
        challenger_pool="mono_tier2",
        challenger_beat="any_higher",
    ),
    SwapExitBConfig(
        "B10",
        "mono_tier2 + top20 + seg_margin=0.05",
        challenger_pool="mono_tier2",
        challenger_beat="any_higher",
        challenger_top_n=20,
        seg_margin=0.05,
    ),
    SwapExitBConfig(
        "B11",
        "fresh top20 + any_higher",
        challenger_pool="fresh",
        challenger_beat="any_higher",
        challenger_top_n=20,
    ),
    SwapExitBConfig(
        "B12",
        "左下 + challenger · min_hold=5 · max_hold=10",
        min_hold_days=5,
        max_hold_days=10,
    ),
    SwapExitBConfig(
        "B13",
        "左下 + challenger · min_hold=5 · max_hold=10 · C0 進",
        min_hold_days=5,
        max_hold_days=10,
        entry_leg="C0",
    ),
    SwapExitBConfig(
        "B14",
        "左下 + challenger · min_hold=5 · max_hold=10 · held seg",
        min_hold_days=5,
        max_hold_days=10,
        challenger_beat="held_today_seg",
    ),
]


DEFAULT_C0_B_SWEEP: list[SwapExitBConfig] = [
    SwapExitBConfig(
        "CB1",
        "C0 進場 + 左下 B 換倉 · entry seg",
        entry_leg="C0",
    ),
    SwapExitBConfig(
        "CB2",
        "C0 進場 + 左下 B · held 當日 seg",
        entry_leg="C0",
        challenger_beat="held_today_seg",
    ),
    SwapExitBConfig(
        "CB3",
        "C0 進場 + 左下 B · 5m 盤中換",
        entry_leg="C0",
        timing_mode="poll_5m",
    ),
    SwapExitBConfig(
        "CB4",
        "C0 進場 + mono_tier2 challenger",
        entry_leg="C0",
        challenger_pool="mono_tier2",
        challenger_beat="held_today_seg",
    ),
]


def _fill_empty_slots(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    fresh_mono: list[ScanRow],
    slots: list[dict[str, Any]],
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    config: SwapExitBConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    kbar_stats: dict[str, int],
    entry_c_config: Any | None = None,
) -> None:
    used = {int(p["slot"]) for p in slots}
    free = [i for i in range(MAX_SLOTS) if i not in used]
    if not free or not fresh_mono:
        return

    if config.entry_leg == "C0":
        from research.backtest.rrg_mono_intraday_ab import (
            DEFAULT_C_SWEEP,
            _apply_intraday_entries,
        )

        c0_cfg = entry_c_config or next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
        before = {str(p["stock_id"]) for p in slots}
        tmp = {"slots": list(slots)}
        _apply_intraday_entries(
            conn,
            tmp,
            signal_date=as_of,
            entry_date=as_of,
            shortlist=list(fresh_mono),
            close=close,
            bench=bench,
            full_dates=full_dates,
            config=c0_cfg,
            kbar_cache=kbar_cache,
            kbar_stats=kbar_stats,
        )
        for p in tmp["slots"]:
            if str(p["stock_id"]) in before:
                continue
            slots.append(
                {
                    "slot": p.get("slot", free[0]),
                    "stock_id": p["stock_id"],
                    "stock_name": p.get("stock_name", ""),
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "entry_px": float(p["entry_px"]),
                    "seg_last": round(float(p.get("seg_last") or 0.0), 4),
                    "disp": round(float(p.get("disp") or 0.0), 4),
                    "entry_minute": p.get("entry_minute"),
                    "entry_leg": "C0",
                }
            )
        return

    held = {str(p["stock_id"]) for p in slots}
    for row in fresh_mono:
        if not free:
            break
        if row.stock_id in held:
            continue
        px = _entry_px(close, row.stock_id, as_of)
        if px is None:
            continue
        slot = free.pop(0)
        slots.append(
            {
                "slot": slot,
                "stock_id": row.stock_id,
                "stock_name": row.stock_name,
                "signal_date": as_of,
                "entry_date": as_of,
                "entry_px": float(px),
                "seg_last": round(row.seg_last, 4),
                "disp": round(row.disp, 4),
                "entry_minute": None,
                "entry_leg": "A",
            }
        )
        held.add(row.stock_id)


def _trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


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


def _passes_structural_gate(
    feat: dict[str, Any] | None,
    *,
    config: SwapExitBConfig,
) -> bool:
    if feat is None:
        return False
    if config.structural_gate == "down_left":
        return str(feat.get("trend") or "") == "down_left"
    q = str(feat.get("end_q") or "").lower()
    return q in {x.lower() for x in config.exit_quadrants}


def _challenger_threshold(
    *,
    held_entry_seg: float,
    held_today_seg: float | None,
    config: SwapExitBConfig,
) -> float:
    if config.challenger_beat == "entry_seg":
        base = held_entry_seg
    elif config.challenger_beat == "held_today_seg":
        base = held_today_seg if held_today_seg is not None else held_entry_seg
    else:
        base = min(held_entry_seg, held_today_seg if held_today_seg is not None else held_entry_seg)
    return max(0.0, base - config.seg_margin)


def _best_challenger(
    candidates: list[ScanRow],
    *,
    held_ids: set[str],
    held_entry_seg: float,
    held_today_seg: float | None,
    config: SwapExitBConfig,
) -> ScanRow | None:
    threshold = _challenger_threshold(
        held_entry_seg=held_entry_seg,
        held_today_seg=held_today_seg,
        config=config,
    )
    top_n = max(1, int(config.challenger_top_n))
    best: ScanRow | None = None
    for row in candidates[:top_n]:
        if row.stock_id in held_ids:
            continue
        if row.seg_last <= threshold:
            continue
        if best is None or row.seg_last > best.seg_last:
            best = row
    return best


def build_mono_tier2_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    close: pd.DataFrame | None = None,
    bench: pd.Series | None = None,
) -> dict[str, list[ScanRow]]:
    """當日 mono_tier2 全池（含非 fresh）· 依 seg_last 排序。"""
    from project_config import DEFAULT_ETF_CODES
    from stock_db import load_etf_constituent_watchlist

    if close is None or bench is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    full_dates = close.index.astype(str).tolist()
    date_set = set(trade_dates)

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for as_of in trade_dates:
        if as_of not in date_set:
            continue
        si = full_dates.index(as_of)
        if si < LOOKBACK:
            continue
        pool: list[ScanRow] = []
        for sid in universe:
            feat = _daily_feat(rs_ratio, rs_mom, full_dates, as_of, sid)
            if feat is None or not _mono_tier2(feat):
                continue
            pool.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=False,
                    mono=True,
                    seg_last=float(feat["seg_last"]),
                    disp=float(feat["disp"]),
                    segs=[float(x) for x in feat["segs"]],
                    quadrants=[q or "?" for q in feat["quadrants"]],
                    rs_ratio=float(feat["rs_ratio"]),
                    rs_momentum=float(feat["rs_momentum"]),
                    daily_pct=None,
                )
            )
        pool.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = pool
    return out


def _entry_px(
    close: pd.DataFrame,
    stock_id: str,
    trade_date: str,
) -> float | None:
    if trade_date not in close.index or stock_id not in close.columns:
        return None
    px = float(close.at[trade_date, stock_id])
    return px if px > 0 else None


def _swap_px(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    stock_id: str,
    trade_date: str,
    config: SwapExitBConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, str | None]:
    if trade_date not in close.index or stock_id not in close.columns:
        return None, None
    close_px = float(close.at[trade_date, stock_id])
    if config.timing_mode == "close":
        return close_px, None

    key = (stock_id, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, stock_id, trade_date)
    minutes = _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_trade_before,
    )
    for minute in minutes:
        px = price_at_or_before_minute(kbar_cache[key], minute)
        if px is not None and px > 0:
            return float(px), minute
    return close_px, None


def _settle_leg(
    conn: sqlite3.Connection,
    *,
    pos: dict[str, Any],
    exit_date: str,
    exit_px: float,
    exit_reason: str,
    config: SwapExitBConfig,
    full_dates: list[str],
) -> dict[str, Any] | None:
    sid = str(pos["stock_id"])
    entry = str(pos["entry_date"])
    entry_px = float(pos["entry_px"])
    if entry_px <= 0 or exit_px <= 0:
        return None
    ret = (exit_px / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry, exit_date, entry_price_mode="close")
    if bench is None:
        return None
    hold_days = _trading_days_between(full_dates, entry, exit_date)
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": str(pos.get("signal_date") or entry),
        "entry_date": entry,
        "exit_date": exit_date,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "exit_minute": pos.get("exit_minute"),
        "exit_reason": exit_reason,
        "hold_days": hold_days,
        "variant_id": config.variant_id,
        "structural_gate": config.structural_gate,
        "challenger_beat": config.challenger_beat,
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "seg_last": pos.get("seg_last"),
        "slot": pos.get("slot"),
    }


def simulate_swap_exit_b(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    fresh_by_date: dict[str, list[ScanRow]],
    zone_by_date: dict[str, str],
    config: SwapExitBConfig,
    mono_by_date: dict[str, list[ScanRow]] | None = None,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = kbar_cache if kbar_cache is not None else {}
    kbar_stats = {"hits": 0, "checks": 0}
    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    swaps = 0
    max_hold_exits = 0
    structural_only_days = 0

    def held_ids() -> set[str]:
        return {str(p["stock_id"]) for p in slots}

    for as_of in trade_dates:
        fresh_mono = fresh_by_date.get(as_of, [])
        challenger_pool = (
            mono_by_date.get(as_of, [])
            if config.challenger_pool == "mono_tier2" and mono_by_date is not None
            else fresh_mono
        )

        # max_hold 強制出場
        for pos in list(slots):
            entry = str(pos["entry_date"])
            hold_days = _trading_days_between(full_dates, entry, as_of)
            if hold_days < config.max_hold_days:
                continue
            px, minute = _swap_px(
                conn,
                close=close,
                stock_id=str(pos["stock_id"]),
                trade_date=as_of,
                config=config,
                kbar_cache=cache,
            )
            if px is None:
                continue
            pos["_full_dates"] = full_dates
            if minute:
                pos["exit_minute"] = minute
            leg = _settle_leg(
                conn,
                pos=pos,
                exit_date=as_of,
                exit_px=px,
                exit_reason="max_hold",
                config=config,
                full_dates=full_dates,
            )
            if leg:
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)
                slots.remove(pos)
                max_hold_exits += 1

        # 模式 B · 左下 + challenger 換倉
        for pos in list(slots):
            entry = str(pos["entry_date"])
            hold_days = _trading_days_between(full_dates, entry, as_of)
            if hold_days < config.min_hold_days:
                continue
            sid = str(pos["stock_id"])
            feat = _daily_feat(rs_ratio, rs_mom, full_dates, as_of, sid)
            if not _passes_structural_gate(feat, config=config):
                continue
            today_seg = float(feat.get("seg_last") or 0.0) if feat else None
            challenger = _best_challenger(
                challenger_pool,
                held_ids=held_ids() - {sid},
                held_entry_seg=float(pos.get("seg_last") or 0.0),
                held_today_seg=today_seg,
                config=config,
            )
            if challenger is None:
                structural_only_days += 1
                continue

            sell_px, sell_min = _swap_px(
                conn,
                close=close,
                stock_id=sid,
                trade_date=as_of,
                config=config,
                kbar_cache=cache,
            )
            buy_px, buy_min = _swap_px(
                conn,
                close=close,
                stock_id=challenger.stock_id,
                trade_date=as_of,
                config=config,
                kbar_cache=cache,
            )
            if sell_px is None or buy_px is None:
                continue

            pos["_full_dates"] = full_dates
            if sell_min:
                pos["exit_minute"] = sell_min
            leg = _settle_leg(
                conn,
                pos=pos,
                exit_date=as_of,
                exit_px=sell_px,
                exit_reason="swap_b",
                config=config,
                full_dates=full_dates,
            )
            if leg is None:
                continue
            leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
            leg["challenger_id"] = challenger.stock_id
            leg["challenger_seg_last"] = challenger.seg_last
            periods.append(leg)
            slots.remove(pos)
            slots.append(
                {
                    "slot": pos.get("slot"),
                    "stock_id": challenger.stock_id,
                    "stock_name": challenger.stock_name,
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "entry_px": float(buy_px),
                    "seg_last": round(challenger.seg_last, 4),
                    "disp": round(challenger.disp, 4),
                    "entry_minute": buy_min,
                }
            )
            swaps += 1

        # 空槽填倉
        _fill_empty_slots(
            conn,
            as_of=as_of,
            fresh_mono=fresh_mono,
            slots=slots,
            close=close,
            bench=bench,
            full_dates=full_dates,
            config=config,
            kbar_cache=cache,
            kbar_stats=kbar_stats,
        )

    # 期末平倉
    if trade_dates:
        last = trade_dates[-1]
        for pos in list(slots):
            px = _entry_px(close, str(pos["stock_id"]), last)
            if px is None:
                continue
            leg = _settle_leg(
                conn,
                pos=pos,
                exit_date=last,
                exit_px=px,
                exit_reason="window_end",
                config=config,
                full_dates=full_dates,
            )
            if leg:
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)

    summary = summarize_periods(periods)
    n = len(periods)
    if n:
        summary["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4)
        summary["mean_hold_days"] = round(sum(p["hold_days"] for p in periods) / n, 2)
        summary["mean_return_pct"] = round(sum(p["return_pct"] for p in periods) / n, 4)
    else:
        summary["mean_excess_pct"] = None
        summary["mean_hold_days"] = None
        summary["mean_return_pct"] = None
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "structural_gate": config.structural_gate,
            "challenger_beat": config.challenger_beat,
            "min_hold_days": config.min_hold_days,
            "max_hold_days": config.max_hold_days,
            "timing_mode": config.timing_mode,
            "challenger_pool": config.challenger_pool,
            "challenger_top_n": config.challenger_top_n,
            "seg_margin": config.seg_margin,
            "entry_leg": config.entry_leg,
            "kbar_entry_coverage_pct": (
                round(kbar_stats["hits"] / kbar_stats["checks"] * 100.0, 2)
                if kbar_stats["checks"]
                else None
            ),
            "swaps_total": swaps,
            "max_hold_exits": max_hold_exits,
            "structural_no_challenger_days": structural_only_days,
            "n_periods": n,
        }
    )
    return periods, summary


def run_c0_swap_b_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[SwapExitBConfig] | None = None,
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_intraday_ab import (
        DEFAULT_C_SWEEP,
        simulate_leg_c_variant,
    )

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    _, c0_hold7_summary = simulate_leg_c_variant(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        config=c0_cfg,
    )

    grid = configs or DEFAULT_C0_B_SWEEP
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    summaries: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        _, summary = simulate_swap_exit_b(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=cfg,
            mono_by_date=mono_by_date,
            kbar_cache=kbar_cache,
        )
        baseline = c0_hold7_summary.get("mean_excess_pct")
        delta = None
        if baseline is not None and summary.get("mean_excess_pct") is not None:
            delta = round(float(summary["mean_excess_pct"]) - float(baseline), 4)
        summary["delta_vs_c0_hold7_pp"] = delta
        summaries.append(summary)
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"swaps={summary.get('swaps_total')} mean_excess={summary.get('mean_excess_pct')}",
            flush=True,
        )

    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    return {
        "date_start": date_start,
        "date_end": date_end,
        "reference_c0_hold7": {
            "variant_id": "C0",
            "n_periods": c0_hold7_summary.get("n_periods"),
            "mean_excess_pct": c0_hold7_summary.get("mean_excess_pct"),
            "kbar_coverage_pct": c0_hold7_summary.get("kbar_coverage_pct"),
        },
        "reference_a_hold7": None,
        "ssg_note": "C0 scale 5m confirm=1 盤中進場 · B 換倉僅在 down_left + challenger",
        "summaries": summaries,
        "best": ranked[0] if ranked else None,
    }


def run_swap_exit_b_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[SwapExitBConfig] | None = None,
    baseline_variant_id: str = "B1",
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    from research.backtest.rrg_mono_backtest import simulate_mono_hold7

    hold7_periods, hold7_summary = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
    )
    baseline_excess = hold7_summary.get("mean_excess_pct")

    grid = configs or DEFAULT_SWAP_B_SWEEP
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    summaries: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        _, summary = simulate_swap_exit_b(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=cfg,
            mono_by_date=mono_by_date,
            kbar_cache=kbar_cache,
        )
        delta = None
        if baseline_excess is not None and summary.get("mean_excess_pct") is not None:
            delta = round(float(summary["mean_excess_pct"]) - float(baseline_excess), 4)
        summary["delta_vs_hold7_pp"] = delta
        summaries.append(summary)
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"swaps={summary.get('swaps_total')} mean_excess={summary.get('mean_excess_pct')}",
            flush=True,
        )

    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    return {
        "date_start": date_start,
        "date_end": date_end,
        "reference_hold7": {
            "n_periods": hold7_summary.get("n_periods") or len(hold7_periods),
            "mean_excess_pct": baseline_excess,
            "mean_hold_days": 7.0,
        },
        "ssg_note": (
            "A 腿收盤建倉 · 滿槽時僅在 structural_gate + challenger 同時成立才換 · "
            "否則續抱至 max_hold_days"
        ),
        "summaries": summaries,
        "best": ranked[0] if ranked else None,
    }


# re-export LENGTH for tests
from rrg_mono_daily_brief import LENGTH  # noqa: E402
