"""RRG Lens score-swap backtest · 日線 Lens + 盤中 RRG 加權 · Top-N 升級換倉。"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass, field
from statistics import mean, pstdev
from typing import Any, Literal

import pandas as pd

from flow_returns import trading_dates_after
from market_benchmark import load_benchmark_close
from market_breadth_ma import BREADTH_ZONES_ORDER, build_breadth_panel
from research.backtest.copytrade_backtest import bench_return_entry_to_exit
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from rrg_mono_daily_brief import LOOKBACK, _feat
from rrg_rotation import compute_rrg_panel
from stock_db import load_lens_daily_highlight, load_rrg_universe_scores
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

CandidateGate = Literal[
    "lens_only",
    "tier2",
    "mono_tier2",
    "mono_fresh_1d",
    "mono_fresh_2d",
    "leading_improving",
    "lens_convergence_ge2",
]
SwapTrigger = Literal["beat_held_best", "beat_held_median", "beat_held_worst"]
SellLeg = Literal["held_worst", "held_fastest_decay"]
IntradaySortKey = Literal["seg_last", "disp", "rs_momentum"]

DEFAULT_EXIT_QUADRANTS = ("weakening", "lagging")


@dataclass
class SwapConfig:
    alpha: float = 0.75
    max_slots: int = 3
    candidate_gate: str = "mono_tier2"
    entry_gate: str | None = None
    swap_gate: str | None = None
    rrg_length: int = 20
    rebalance_interval_min: int = 15
    confirm_bars: int = 2
    swap_trigger: str = "beat_held_best"
    sell_leg: str = "held_worst"
    score_margin: float = 0.0
    exit_quadrants: tuple[str, ...] = DEFAULT_EXIT_QUADRANTS
    min_hold_days: int = 1
    max_hold_days: int = 7
    max_swaps_per_day: int = 1
    watchlist_pit: str = "prior_close_lens"
    no_swap_before: str = "09:30"
    intraday_sort_key: str = "seg_last"
    allow_intraday_new_to_watchlist: bool = False
    exit_first: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_quadrants"] = list(self.exit_quadrants)
        return d


def _effective_gate(config: SwapConfig, *, role: Literal["entry", "swap"]) -> str:
    if role == "entry":
        return str(config.entry_gate or config.candidate_gate)
    return str(config.swap_gate or config.candidate_gate)


def _gate_candidates(
    universe: set[str],
    held_ids: set[str],
    gate: str,
    *,
    in_pool_fn,
    prior_lens_fn,
    intraday_by_id: dict[str, dict[str, Any]],
    rrg_yesterday: dict[str, dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    for sid in universe:
        if sid in held_ids:
            continue
        if not _passes_gate(
            gate,
            in_pool=in_pool_fn(sid),
            prior_lens=prior_lens_fn(sid),
            rrg_today=intraday_by_id.get(sid, {}),
            rrg_yesterday=rrg_yesterday.get(sid),
        ):
            continue
        if intraday_by_id.get(sid):
            out.append(sid)
    return out


def _prior_trading_date(full_dates: list[str], as_of: str) -> str | None:
    try:
        idx = full_dates.index(as_of)
    except ValueError:
        return None
    if idx <= 0:
        return None
    return full_dates[idx - 1]


def _rebalance_minutes(
    *,
    interval_min: int,
    no_swap_before: str,
    last_minute: str = "13:30",
) -> list[str]:
    start_h, start_m = (int(no_swap_before[:2]), int(no_swap_before[3:5]))
    end_h, end_m = (int(last_minute[:2]), int(last_minute[3:5]))
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    out: list[str] = []
    t = start_total
    while t <= end_total:
        hh, mm = divmod(t, 60)
        out.append(f"{hh:02d}:{mm:02d}")
        t += interval_min
    return out


def _rrg_row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    return {
        "stock_id": row["stock_id"],
        "quadrant": row["quadrant"],
        "tier2": int(row["tier2"] or 0),
        "mono_tier2": int(row["mono_tier2"] or 0),
        "mono_fresh": int(row["mono_fresh"] or 0),
        "seg_last": float(row["seg_last"]) if row["seg_last"] is not None else 0.0,
        "disp": float(row["disp"]) if row["disp"] is not None else 0.0,
        "rs_momentum": float(row["rs_momentum"]) if row["rs_momentum"] is not None else 100.0,
        "rs_ratio": float(row["rs_ratio"]) if row["rs_ratio"] is not None else 100.0,
    }


def _passes_gate(
    gate: str,
    *,
    in_pool: bool,
    prior_lens: dict[str, Any] | None,
    rrg_today: dict[str, Any],
    rrg_yesterday: dict[str, Any] | None,
) -> bool:
    if gate == "lens_only":
        return in_pool
    if gate == "tier2":
        return bool(rrg_today.get("tier2"))
    if gate == "mono_tier2":
        return bool(rrg_today.get("mono_tier2"))
    if gate == "mono_fresh_1d":
        return bool(rrg_today.get("mono_fresh"))
    if gate == "mono_fresh_2d":
        return bool(rrg_today.get("mono_fresh")) and bool(
            rrg_yesterday and rrg_yesterday.get("mono_fresh")
        )
    if gate == "leading_improving":
        q = str(rrg_today.get("quadrant") or "").lower()
        pq = str((rrg_yesterday or {}).get("quadrant") or "").lower()
        return q == "leading" and pq in ("improving", "lagging")
    if gate == "lens_convergence_ge2":
        conv = int((prior_lens or {}).get("signal_convergence") or 0)
        return in_pool and conv >= 2
    return in_pool


def _intraday_raw_score(rrg: dict[str, Any], sort_key: str) -> float:
    if sort_key == "disp":
        return float(rrg.get("disp") or 0.0)
    if sort_key == "rs_momentum":
        return float(rrg.get("rs_momentum") or 100.0)
    return float(rrg.get("seg_last") or 0.0)


def _zscore_map(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    vals = list(values.values())
    mu = mean(vals)
    sd = pstdev(vals) if len(vals) > 1 else 0.0
    if sd <= 1e-9:
        return {k: 0.0 for k in values}
    return {k: (v - mu) / sd for k, v in values.items()}


def _combined_scores(
    candidates: list[str],
    *,
    alpha: float,
    daily: dict[str, float],
    intraday: dict[str, float],
) -> dict[str, float]:
    dz = _zscore_map({s: daily.get(s, 0.0) for s in candidates})
    iz = _zscore_map({s: intraday.get(s, 0.0) for s in candidates})
    return {s: alpha * iz.get(s, 0.0) + (1.0 - alpha) * dz.get(s, 0.0) for s in candidates}


def _swap_threshold(
    held_scores: dict[str, float],
    trigger: str,
) -> float:
    if not held_scores:
        return float("-inf")
    vals = sorted(held_scores.values())
    if trigger == "beat_held_worst":
        return vals[0]
    if trigger == "beat_held_median":
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return vals[-1]


def _pick_sell_leg(
    held: list[dict[str, Any]],
    scores: dict[str, float],
    *,
    sell_leg: str,
    score_decay: dict[str, float],
) -> dict[str, Any] | None:
    if not held:
        return None
    if sell_leg == "held_fastest_decay":
        return min(held, key=lambda p: score_decay.get(str(p["stock_id"]), 0.0))
    return min(held, key=lambda p: scores.get(str(p["stock_id"]), float("inf")))


def _trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


def _settle_leg(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    *,
    stock_id: str,
    stock_name: str,
    entry_date: str,
    entry_px: float,
    exit_date: str,
    exit_px: float,
    signal_date: str,
    breadth_zone: str | None,
    config: SwapConfig,
) -> dict[str, Any] | None:
    if entry_px <= 0 or exit_px <= 0:
        return None
    if exit_date not in close.index or stock_id not in close.columns:
        return None
    ret = (exit_px / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry_date, exit_date, entry_price_mode="close")
    if bench is None:
        return None
    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "breadth_zone_200": breadth_zone or "unknown",
        "config": config.to_dict(),
    }


@dataclass
class SwapBacktestContext:
    conn: sqlite3.Connection
    close: pd.DataFrame
    full_dates: list[str]
    rs_ratio: pd.DataFrame
    rs_mom: pd.DataFrame
    lens_by_date: dict[str, dict[str, dict[str, Any]]]
    rrg_close_by_date: dict[str, dict[str, dict[str, Any]]]
    zone_by_date: dict[str, str]
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = field(default_factory=dict)

    def prior_lens_pool(self, trade_date: str) -> set[str]:
        prior = _prior_trading_date(self.full_dates, trade_date)
        if not prior:
            return set()
        return set(self.lens_by_date.get(prior, {}).keys())

    def daily_lens_score(self, trade_date: str, stock_id: str) -> float:
        prior = _prior_trading_date(self.full_dates, trade_date)
        if not prior:
            return 0.0
        row = self.lens_by_date.get(prior, {}).get(stock_id)
        if not row:
            return 0.0
        return float(row.get("lens_score") or 0.0)

    def prior_lens_row(self, trade_date: str, stock_id: str) -> dict[str, Any] | None:
        prior = _prior_trading_date(self.full_dates, trade_date)
        if not prior:
            return None
        return self.lens_by_date.get(prior, {}).get(stock_id)

    def kbar_bars(self, stock_id: str, trade_date: str) -> tuple[tuple[str, float], ...]:
        key = (stock_id, trade_date)
        if key not in self.kbar_cache:
            self.kbar_cache[key] = load_kbar_day_closes(self.conn, stock_id, trade_date)
        return self.kbar_cache[key]

    def intraday_rrg(
        self,
        trade_date: str,
        stock_id: str,
        minute: str,
    ) -> dict[str, Any]:
        close_row = self.rrg_close_by_date.get(trade_date, {}).get(stock_id)
        bars = self.kbar_bars(stock_id, trade_date)
        px = price_at_or_before_minute(bars, minute)
        if close_row and px and trade_date in self.close.index and stock_id in self.close.columns:
            close_px = float(self.close.at[trade_date, stock_id])
            if close_px > 0:
                scale = max(0.25, min(2.5, px / close_px))
                out = dict(close_row)
                out["seg_last"] = float(close_row.get("seg_last") or 0.0) * scale
                out["disp"] = float(close_row.get("disp") or 0.0) * scale
                mom = float(close_row.get("rs_momentum") or 100.0)
                out["rs_momentum"] = mom + (scale - 1.0) * 20.0
                return out
        if close_row:
            return dict(close_row)
        try:
            si = self.full_dates.index(trade_date)
        except ValueError:
            return {}
        if si < LOOKBACK - 1:
            return {}
        if px is None:
            if stock_id in self.close.columns and trade_date in self.close.index:
                px = float(self.close.at[trade_date, stock_id])
            else:
                return {}
        feat = _feat(self.rs_ratio, self.rs_mom, self.full_dates, si, stock_id)
        if feat is None:
            return {}
        return {
            "stock_id": stock_id,
            "quadrant": feat.get("end_q"),
            "tier2": int(feat["trend"] == "up_right" and feat["end_q"] == "leading" and 1 <= feat["disp"] < 2),
            "mono_tier2": int(
                feat["trend"] == "up_right"
                and feat["end_q"] == "leading"
                and 1 <= feat["disp"] < 2
                and feat.get("mono_up")
            ),
            "mono_fresh": 0,
            "seg_last": float(feat.get("seg_last") or 0.0),
            "disp": float(feat.get("disp") or 0.0),
            "rs_momentum": float(feat.get("rs_momentum") or 100.0),
            "rs_ratio": float(feat.get("rs_ratio") or 100.0),
        }


def build_swap_context(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> SwapBacktestContext:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    lens_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for d in trade_dates:
        rows = load_lens_daily_highlight(conn, d)
        lens_by_date[d] = {str(r["stock_id"]): r for r in rows}

    rrg_close_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for d in trade_dates:
        rows = load_rrg_universe_scores(conn, d, "close")
        rrg_close_by_date[d] = {str(r["stock_id"]): _rrg_row_dict(r) for r in rows}

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    return SwapBacktestContext(
        conn=conn,
        close=close,
        full_dates=full_dates,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        lens_by_date=lens_by_date,
        rrg_close_by_date=rrg_close_by_date,
        zone_by_date=zone_by_date,
    )


def simulate_lens_score_swap(
    ctx: SwapBacktestContext,
    trade_dates: list[str],
    config: SwapConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    minutes = _rebalance_minutes(
        interval_min=config.rebalance_interval_min,
        no_swap_before=config.no_swap_before,
    )
    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    swaps_total = 0
    entries_total = 0
    exits_quadrant = 0
    full_slot_checkpoints = 0
    kbar_hits = 0
    kbar_checks = 0

    for trade_date in trade_dates:
        swaps_today = 0
        prior_date = _prior_trading_date(ctx.full_dates, trade_date)
        pool = ctx.prior_lens_pool(trade_date)
        rrg_yesterday = ctx.rrg_close_by_date.get(prior_date or "", {})
        challenger_confirm: dict[str, int] = {}
        prev_scores: dict[str, float] = {}

        for minute in minutes:
            held_ids = {str(p["stock_id"]) for p in slots}
            intraday_by_id: dict[str, dict[str, Any]] = {}
            daily_scores: dict[str, float] = {}
            intraday_scores: dict[str, float] = {}

            universe = set(pool)
            if config.allow_intraday_new_to_watchlist:
                universe |= set(ctx.lens_by_date.get(trade_date, {}).keys())
            universe |= held_ids

            for sid in universe:
                kbar_checks += 1
                bars = ctx.kbar_bars(sid, trade_date)
                if bars:
                    kbar_hits += 1
                rrg_t = ctx.intraday_rrg(trade_date, sid, minute)
                if not rrg_t:
                    continue
                intraday_by_id[sid] = rrg_t
                daily_scores[sid] = ctx.daily_lens_score(trade_date, sid)
                intraday_scores[sid] = _intraday_raw_score(rrg_t, config.intraday_sort_key)

            score_decay = {
                sid: prev_scores.get(sid, 0.0) - intraday_scores.get(sid, 0.0)
                for sid in held_ids
            }
            prev_scores = dict(intraday_scores)

            # exits
            if config.exit_first:
                for pos in list(slots):
                    sid = str(pos["stock_id"])
                    rrg_t = intraday_by_id.get(sid) or ctx.rrg_close_by_date.get(trade_date, {}).get(sid, {})
                    q = str(rrg_t.get("quadrant") or "").lower()
                    held_days = _trading_days_between(
                        ctx.full_dates, str(pos["entry_date"]), trade_date
                    )
                    max_hold_hit = held_days >= config.max_hold_days
                    quad_exit = q in {x.lower() for x in config.exit_quadrants}
                    if (quad_exit and held_days >= config.min_hold_days) or max_hold_hit:
                        px = price_at_or_before_minute(ctx.kbar_bars(sid, trade_date), minute)
                        if px is None and trade_date in ctx.close.index:
                            px = float(ctx.close.at[trade_date, sid])
                        if px and px > 0:
                            leg = _settle_leg(
                                ctx.conn,
                                ctx.close,
                                stock_id=sid,
                                stock_name=str(pos.get("stock_name") or ""),
                                entry_date=str(pos["entry_date"]),
                                entry_px=float(pos["entry_px"]),
                                exit_date=trade_date,
                                exit_px=float(px),
                                signal_date=str(pos.get("signal_date") or trade_date),
                                breadth_zone=ctx.zone_by_date.get(str(pos.get("signal_date") or trade_date)),
                                config=config,
                            )
                            if leg:
                                if quad_exit:
                                    exits_quadrant += 1
                                periods.append(leg)
                                slots.remove(pos)

            # candidates · 建倉 gate / 換倉 gate 可分離
            def _in_pool(sid: str) -> bool:
                return sid in pool

            def _prior_lens_row(sid: str) -> dict[str, Any] | None:
                return ctx.prior_lens_row(trade_date, sid)

            entry_gate = _effective_gate(config, role="entry")
            swap_gate = _effective_gate(config, role="swap")
            entry_candidates = _gate_candidates(
                universe,
                held_ids,
                entry_gate,
                in_pool_fn=_in_pool,
                prior_lens_fn=_prior_lens_row,
                intraday_by_id=intraday_by_id,
                rrg_yesterday=rrg_yesterday,
            )
            swap_candidates = _gate_candidates(
                universe,
                held_ids,
                swap_gate,
                in_pool_fn=_in_pool,
                prior_lens_fn=_prior_lens_row,
                intraday_by_id=intraday_by_id,
                rrg_yesterday=rrg_yesterday,
            )

            combined_entry = _combined_scores(
                entry_candidates,
                alpha=config.alpha,
                daily=daily_scores,
                intraday=intraday_scores,
            )
            combined_swap = _combined_scores(
                swap_candidates,
                alpha=config.alpha,
                daily=daily_scores,
                intraday=intraday_scores,
            )
            # 持倉與 challenger 同一 z-score 母體（含 held）才可比較
            swap_score_ids = list(
                dict.fromkeys(swap_candidates + [str(p["stock_id"]) for p in slots])
            )
            combined_swap_all = _combined_scores(
                swap_score_ids,
                alpha=config.alpha,
                daily=daily_scores,
                intraday=intraday_scores,
            )
            ranked_entry = sorted(combined_entry.items(), key=lambda x: (-x[1], x[0]))
            ranked_swap = sorted(combined_swap.items(), key=lambda x: (-x[1], x[0]))

            held_scores = {
                str(p["stock_id"]): combined_swap_all.get(
                    str(p["stock_id"]), daily_scores.get(str(p["stock_id"]), 0.0)
                )
                for p in slots
            }
            free = config.max_slots - len(slots)

            # fill empty slots（entry_gate）
            for sid, sc in ranked_entry:
                if free <= 0:
                    break
                if sid in held_ids:
                    continue
                px = price_at_or_before_minute(ctx.kbar_bars(sid, trade_date), minute)
                if px is None and trade_date in ctx.close.index and sid in ctx.close.columns:
                    px = float(ctx.close.at[trade_date, sid])
                if px is None or px <= 0:
                    continue
                name = (ctx.prior_lens_row(trade_date, sid) or {}).get("stock_name", "")
                slots.append(
                    {
                        "stock_id": sid,
                        "stock_name": name,
                        "entry_date": trade_date,
                        "entry_minute": minute,
                        "entry_px": float(px),
                        "signal_date": prior_date or trade_date,
                    }
                )
                held_ids.add(sid)
                free -= 1
                entries_total += 1

            # swaps
            if len(slots) >= config.max_slots:
                full_slot_checkpoints += 1
            if (
                len(slots) >= config.max_slots
                and config.max_swaps_per_day != 0
                and swaps_today < config.max_swaps_per_day
            ):
                threshold = _swap_threshold(held_scores, config.swap_trigger)
                best_sid = None
                best_sc = None
                for sid, sc in ranked_swap:
                    if sid in held_ids:
                        continue
                    if sc <= threshold + config.score_margin:
                        challenger_confirm[sid] = 0
                        continue
                    challenger_confirm[sid] = challenger_confirm.get(sid, 0) + 1
                    if challenger_confirm[sid] >= config.confirm_bars:
                        if best_sc is None or sc > best_sc:
                            best_sid, best_sc = sid, sc
                if best_sid is not None:
                    sell_pos = _pick_sell_leg(
                        slots,
                        held_scores,
                        sell_leg=config.sell_leg,
                        score_decay=score_decay,
                    )
                    if sell_pos is not None:
                        sell_id = str(sell_pos["stock_id"])
                        sell_px = price_at_or_before_minute(ctx.kbar_bars(sell_id, trade_date), minute)
                        buy_px = price_at_or_before_minute(ctx.kbar_bars(best_sid, trade_date), minute)
                        if sell_px and buy_px and sell_px > 0 and buy_px > 0:
                            leg = _settle_leg(
                                ctx.conn,
                                ctx.close,
                                stock_id=sell_id,
                                stock_name=str(sell_pos.get("stock_name") or ""),
                                entry_date=str(sell_pos["entry_date"]),
                                entry_px=float(sell_pos["entry_px"]),
                                exit_date=trade_date,
                                exit_px=float(sell_px),
                                signal_date=str(sell_pos.get("signal_date") or trade_date),
                                breadth_zone=ctx.zone_by_date.get(str(sell_pos.get("signal_date") or trade_date)),
                                config=config,
                            )
                            if leg:
                                periods.append(leg)
                            slots.remove(sell_pos)
                            name = (ctx.prior_lens_row(trade_date, best_sid) or {}).get("stock_name", "")
                            slots.append(
                                {
                                    "stock_id": best_sid,
                                    "stock_name": name,
                                    "entry_date": trade_date,
                                    "entry_minute": minute,
                                    "entry_px": float(buy_px),
                                    "signal_date": prior_date or trade_date,
                                }
                            )
                            swaps_today += 1
                            swaps_total += 1
                            if config.max_swaps_per_day > 0 and swaps_today >= config.max_swaps_per_day:
                                break

    # close remaining at last date close
    if trade_dates:
        last = trade_dates[-1]
        for pos in list(slots):
            sid = str(pos["stock_id"])
            if last not in ctx.close.index or sid not in ctx.close.columns:
                continue
            px = float(ctx.close.at[last, sid])
            leg = _settle_leg(
                ctx.conn,
                ctx.close,
                stock_id=sid,
                stock_name=str(pos.get("stock_name") or ""),
                entry_date=str(pos["entry_date"]),
                entry_px=float(pos["entry_px"]),
                exit_date=last,
                exit_px=px,
                signal_date=str(pos.get("signal_date") or last),
                breadth_zone=ctx.zone_by_date.get(str(pos.get("signal_date") or last)),
                config=config,
            )
            if leg:
                periods.append(leg)

    summary = summarize_periods(periods)
    n = len(periods)
    summary["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4) if n else None
    summary["total_excess_pct"] = round(sum(p["excess_pct"] for p in periods), 4) if n else None
    summary["swaps_total"] = swaps_total
    summary["entries_total"] = entries_total
    summary["exits_quadrant"] = exits_quadrant
    summary["full_slot_checkpoints"] = full_slot_checkpoints
    summary["kbar_coverage_pct"] = round(kbar_hits / kbar_checks * 100.0, 2) if kbar_checks else 0.0
    summary["config"] = config.to_dict()

    buckets: dict[str, list[dict]] = {z: [] for z in BREADTH_ZONES_ORDER}
    for p in periods:
        z = p.get("breadth_zone_200")
        if z in buckets:
            buckets[z].append(p)
    summary["by_breadth_zone"] = {
        z: {
            "n": len(sub),
            "mean_excess_pct": round(sum(x["excess_pct"] for x in sub) / len(sub), 4) if sub else None,
            "win_rate_vs_bench": round(sum(1 for x in sub if x["beat_bench"]) / len(sub) * 100.0, 2)
            if sub
            else None,
        }
        for z, sub in buckets.items()
    }
    return periods, summary


def run_config_grid(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[SwapConfig],
) -> list[dict[str, Any]]:
    ctx = build_swap_context(conn, date_start=date_start, date_end=date_end)
    full_dates = ctx.full_dates
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    out: list[dict[str, Any]] = []
    for cfg in configs:
        _, summary = simulate_lens_score_swap(ctx, trade_dates, cfg)
        out.append(summary)
    return out
