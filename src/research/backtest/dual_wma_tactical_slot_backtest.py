"""P0 hook_buy · 戰術槽與 C18acc 組合回測（Research layer）。"""

from __future__ import annotations

import itertools
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.dual_wma_signal_backtest import (
    ROUND_TRIP_COST_PCT,
    ExitRule,
    W20LeadingExitParams,
    _build_stock_frame_maps,
    _exit_frame_idx_w20_leading,
    _summarize_trades,
    _summarize_w20_exit_trades,
    _trade_return,
    build_regime_filter_context,
    collect_regime_filtered_trades,
    default_w20_leading_exit,
    imp_early_long_champion_entry,
    imp_early_long_frozen_regime,
    run_w20_leading_exit_grid,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import (
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.slot_portfolio_metrics import (
    _max_drawdown_pct,
    simulate_slot_portfolio,
    simulate_slot_portfolio_yield_new,
)
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import (
    DEFAULT_HOOK_PEAK_SIGNAL_MIN,
    DEFAULT_HOOK_POOL_MIN_PEAK,
    DEFAULT_HOOK_POOL_RETRACE_EPS,
    DEFAULT_HOOK_RETRACE_EPS,
    SPREAD_SIGNAL_MIN,
    annotate_hook_features,
    build_dual_wma_intraday_trajectories,
)

HookPoolTier = Literal["pool", "retrace", "improving", "strict"]


@dataclass(frozen=True)
class HookAnnotKey:
    retrace_eps: float
    peak_signal_min: float
    allow_mixed_peak: bool


@dataclass(frozen=True)
class HookEntryParams:
    hook_quality_min: float = 0.0
    retrace_eps: float = DEFAULT_HOOK_RETRACE_EPS
    peak_signal_min: float = 1.0
    allow_mixed_peak: bool = True
    pool_tier: HookPoolTier = "pool"
    pool_min_peak: float = DEFAULT_HOOK_POOL_MIN_PEAK
    pool_retrace_eps: float = DEFAULT_HOOK_POOL_RETRACE_EPS
    w20_rs_min: float | None = None
    w20_rs_max: float | None = None
    require_both_vectors: bool = False
    hook_retrace_pct_min: float | None = None
    spread_dist_min: float | None = None
    spread_dist_max: float | None = None
    hook_pool_peak_dist_min: float | None = None
    hook_pool_peak_dist_max: float | None = None
    first_per_day: bool = True

    def annot_key(self) -> HookAnnotKey:
        return HookAnnotKey(
            retrace_eps=self.retrace_eps,
            peak_signal_min=self.peak_signal_min,
            allow_mixed_peak=self.allow_mixed_peak,
        )


@dataclass(frozen=True)
class TacticalExitParams:
    cap_days: int = 3
    w20_rs_min_at_exit: float | None = None
    trail_after_w20: str | None = None


def hook_buy_champion_entry() -> HookEntryParams:
    """Phase 1b strict champion · 篩選用。"""
    return HookEntryParams(
        pool_tier="strict",
        retrace_eps=0.10,
        hook_quality_min=0.0,
        peak_signal_min=1.0,
        allow_mixed_peak=True,
        w20_rs_min=96.0,
        w20_rs_max=100.0,
        require_both_vectors=True,
    )


def hook_pool_default_entry() -> HookEntryParams:
    """寬網候選池 · 先撈再篩。"""
    return HookEntryParams(
        pool_tier="pool",
        first_per_day=True,
    )


def tactical_exit_params() -> TacticalExitParams:
    return TacticalExitParams(cap_days=3)


def _reannotate_trajectories(
    trajectories: list[dict[str, Any]],
    key: HookAnnotKey,
) -> list[dict[str, Any]]:
    """Re-tag hook geometry without rebuilding intraday panel."""
    out: list[dict[str, Any]] = []
    for t in trajectories:
        pts = [dict(p) for p in t["points"]]
        annotate_hook_features(
            pts,
            retrace_eps=key.retrace_eps,
            peak_signal_min=key.peak_signal_min,
            allow_mixed_peak=key.allow_mixed_peak,
        )
        out.append({**t, "points": pts})
    return out


def _build_annot_caches(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    keys: list[HookAnnotKey],
) -> tuple[
    dict[HookAnnotKey, list[dict[str, Any]]],
    dict[HookAnnotKey, dict[str, list[dict[str, Any] | None]]],
]:
    trajectories_by_key: dict[HookAnnotKey, list[dict[str, Any]]] = {}
    stock_maps_by_key: dict[HookAnnotKey, dict[str, list[dict[str, Any] | None]]] = {}
    for key in keys:
        print(
            f"  re-annotate hook · eps={key.retrace_eps} "
            f"peak_min={key.peak_signal_min} mixed={key.allow_mixed_peak} ...",
            flush=True,
        )
        tr = _reannotate_trajectories(trajectories, key)
        trajectories_by_key[key] = tr
        stock_maps_by_key[key] = _build_stock_frame_maps(tr, frames)
    return trajectories_by_key, stock_maps_by_key


def _matches_hook_buy(p: dict[str, Any], ep: HookEntryParams) -> bool:
    if not p.get("fresh"):
        return False
    w20 = p.get("w20") or {}
    w5 = p.get("w5") or {}
    srs = float(p.get("spread_rs") or 0)
    sms = float(p.get("spread_mom") or 0)

    tier = ep.pool_tier
    if tier == "pool":
        if not p.get("hook_candidate"):
            return False
    elif tier == "retrace":
        if not p.get("hook_candidate"):
            return False
        if w20.get("quadrant") != "improving":
            return False
    elif tier == "improving":
        if not p.get("hook_candidate"):
            return False
        if w20.get("quadrant") != "improving" or w5.get("quadrant") != "improving":
            return False
    else:
        if not p.get("hook_complete"):
            return False
        if w20.get("quadrant") != "improving" or w5.get("quadrant") != "improving":
            return False
        hq = float(p.get("hook_quality") or 0)
        if hq < ep.hook_quality_min:
            return False

    if ep.require_both_vectors:
        if not (srs >= 0 and sms >= 0):
            return False
    elif tier in ("improving", "strict") and not (srs >= 0 or sms >= 0):
        return False

    if ep.hook_quality_min > 0:
        hq = p.get("hook_quality")
        if hq is None or float(hq) < ep.hook_quality_min:
            return False

    if ep.hook_retrace_pct_min is not None:
        rp = p.get("hook_retrace_pct")
        if rp is None or float(rp) < ep.hook_retrace_pct_min:
            return False

    dist = float(p.get("spread_dist") or 0)
    if ep.spread_dist_min is not None and dist < ep.spread_dist_min:
        return False
    if ep.spread_dist_max is not None and dist > ep.spread_dist_max:
        return False

    if ep.hook_pool_peak_dist_min is not None:
        pk = p.get("hook_pool_peak_dist")
        if pk is None or float(pk) < ep.hook_pool_peak_dist_min:
            return False
    if ep.hook_pool_peak_dist_max is not None:
        pk = p.get("hook_pool_peak_dist")
        if pk is None or float(pk) > ep.hook_pool_peak_dist_max:
            return False

    w20_rs = float(w20.get("rs_ratio") or 0)
    if ep.w20_rs_min is not None and w20_rs < ep.w20_rs_min:
        return False
    if ep.w20_rs_max is not None and w20_rs > ep.w20_rs_max:
        return False
    return True


def _collect_hook_events(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    ep: HookEntryParams,
    *,
    trajectories_by_eps: dict[float, list[dict[str, Any]]] | None = None,
    trajectories_by_key: dict[HookAnnotKey, list[dict[str, Any]]] | None = None,
) -> list[tuple[str, int]]:
    if trajectories_by_key is not None:
        trajs = trajectories_by_key.get(ep.annot_key(), trajectories)
    elif trajectories_by_eps is not None:
        trajs = trajectories_by_eps.get(ep.retrace_eps, trajectories)
    else:
        trajs = trajectories
    frame_ids = [f["id"] for f in frames]
    events: list[tuple[str, int]] = []
    seen_day: set[tuple[str, str]] = set()
    for t in trajs:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if not p or not _matches_hook_buy(p, ep):
                continue
            d = frames[i]["date"]
            if ep.first_per_day:
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
            events.append((sid, i))
    return events


def _w20_exit_from_tactical(xp: TacticalExitParams) -> W20LeadingExitParams:
    return W20LeadingExitParams(
        min_hold_polls=0,
        w20_rs_min_at_exit=xp.w20_rs_min_at_exit,
        w5_gate=None,
        confirm_polls=1,
        cap_days=xp.cap_days,
        trail_after_w20=xp.trail_after_w20,
    )


def run_hook_buy_event_backtest(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    entry_params: HookEntryParams | None = None,
    exit_params: TacticalExitParams | None = None,
    _cache: dict[str, Any] | None = None,
    trajectories_by_eps: dict[float, list[dict[str, Any]]] | None = None,
    stock_maps_by_eps: dict[float, dict[str, list[dict[str, Any] | None]]] | None = None,
    trajectories_by_key: dict[HookAnnotKey, list[dict[str, Any]]] | None = None,
    stock_maps_by_key: dict[HookAnnotKey, dict[str, list[dict[str, Any] | None]]] | None = None,
) -> dict[str, Any]:
    ep = entry_params or hook_buy_champion_entry()
    xp = exit_params or tactical_exit_params()
    w20_exit = _w20_exit_from_tactical(xp)

    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    if stock_maps_by_key is not None and ep.annot_key() in stock_maps_by_key:
        stock_maps = stock_maps_by_key[ep.annot_key()]
    elif stock_maps_by_eps is not None and ep.retrace_eps in stock_maps_by_eps:
        stock_maps = stock_maps_by_eps[ep.retrace_eps]

    events = _collect_hook_events(
        trajectories,
        frames,
        ep,
        trajectories_by_eps=trajectories_by_eps,
        trajectories_by_key=trajectories_by_key,
    )
    trades: list[dict[str, Any]] = []
    for sid, ei in events:
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx_w20_leading(ei, frames, pts, w20_exit)
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if tr:
            pt = pts[ei] or {}
            tr["hook_quality"] = pt.get("hook_quality")
            tr["hook_peak_dist"] = pt.get("hook_peak_dist")
            tr["hook_retrace_pct"] = pt.get("hook_retrace_pct")
            trades.append(tr)

    summary = _summarize_trades(
        trades,
        imp_early_long_champion_entry(),
        ExitRule("exit_w20_leading", w20_exit.label()),
    )
    summary.update(
        {
            "entry_signal": "hook_buy",
            "pool_tier": ep.pool_tier,
            "hook_quality_min": ep.hook_quality_min,
            "retrace_eps": ep.retrace_eps,
            "peak_signal_min": ep.peak_signal_min,
            "allow_mixed_peak": ep.allow_mixed_peak,
            "require_both_vectors": ep.require_both_vectors,
            "w20_rs_min": ep.w20_rs_min,
            "w20_rs_max": ep.w20_rs_max,
            "hook_retrace_pct_min": ep.hook_retrace_pct_min,
            "spread_dist_min": ep.spread_dist_min,
            "spread_dist_max": ep.spread_dist_max,
            "hook_pool_peak_dist_min": ep.hook_pool_peak_dist_min,
            "hook_pool_peak_dist_max": ep.hook_pool_peak_dist_max,
            "exit_cap_days": xp.cap_days,
            "exit_trail_after_w20": xp.trail_after_w20,
        }
    )
    return {"trades": trades, "summary": summary, "events": len(events)}


def run_imp_early_long_event_backtest(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    _cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """凍結 champion 進出場 + tape=多頭 · 事件腿回測。"""
    ep = imp_early_long_champion_entry()
    xp = default_w20_leading_exit()
    regime = imp_early_long_frozen_regime()

    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    regime_ctx = build_regime_filter_context(conn, dates)
    trades = collect_regime_filtered_trades(
        conn,
        entry_params=ep,
        exit_params=xp,
        regime=regime,
        trajectories=trajectories,
        frames=frames,
        stock_maps=stock_maps,
        bench=bench,
        regime_ctx=regime_ctx,
    )
    summary = _summarize_w20_exit_trades(trades, ep, xp)
    summary.update(
        {
            "entry_signal": "imp_early_long",
            "regime_filter": regime.label(),
            "small_extension_max": ep.small_extension_max,
            "aligned_max": ep.aligned_max,
            "require_both_vectors": ep.require_both_vectors,
            "w20_rs_min": ep.w20_rs_min,
            "w20_rs_max": ep.w20_rs_max,
            "exit_rule": "exit_w20_leading",
        }
    )
    return {"trades": trades, "summary": summary, "events": len(trades)}


def trades_to_slot_periods(
    trades: list[dict[str, Any]],
    *,
    source_strategy: str = "hook_buy",
    priority: int = 2,
) -> list[dict[str, Any]]:
    periods: list[dict[str, Any]] = []
    for t in trades:
        periods.append(
            {
                "stock_id": t["stock_id"],
                "entry_date": t["entry_date"],
                "exit_date": t["exit_date"],
                "source_strategy": source_strategy,
                "_priority": priority,
                "excess_pct": t.get("excess_pct"),
                "net_pct": t.get("net_pct"),
            }
        )
    return periods


def load_c18acc_periods(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    n_slots: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from dataclasses import replace

    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar

    if len(trade_dates) < 2:
        return [], {"n_periods": 0}

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    td = [d for d in trade_dates if d in set(full_dates)]
    fresh_by_date = build_fresh_mono_calendar(conn, td)
    panel = build_breadth_panel(conn, date_start=td[0], date_end=td[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    cfg = replace(close_champion_score_swap_c_config(), n_slots=max(1, n_slots))
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=td,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
    )
    for p in periods:
        p["source_strategy"] = "c18acc"
        p["_priority"] = 1
    return periods, summary


def _daily_return_series(daily_nav: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    prev = None
    for row in daily_nav:
        d = str(row["date"])
        eq = float(row["equity_ntd"])
        if prev is not None and prev > 0:
            out[d] = (eq / prev - 1.0) * 100.0
        prev = eq
    return out


def _pearson_corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 3:
        return None
    xa, xb = a[:n], b[:n]
    ma, mb = sum(xa) / n, sum(xb) / n
    num = sum((xa[i] - ma) * (xb[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in xa))
    db = math.sqrt(sum((x - mb) ** 2 for x in xb))
    if da <= 0 or db <= 0:
        return None
    return round(num / (da * db), 4)


def _combined_metrics(
    *,
    total_capital: float,
    trade_dates: list[str],
    equities: list[float],
    daily_returns: list[float],
    pool_parts: list[dict[str, Any]],
) -> dict[str, Any]:
    years = len(trade_dates) / 252.0 if trade_dates else 0.0
    final = equities[-1] if equities else total_capital
    total_ret = (final / total_capital - 1.0) * 100.0 if total_capital > 0 else 0.0
    cagr = None
    if years > 0 and final > 0:
        cagr = round(((final / total_capital) ** (1.0 / years) - 1.0) * 100.0, 4)
    n_dr = len(daily_returns)
    sharpe = None
    if n_dr > 1:
        mean_dr = sum(daily_returns) / n_dr
        var = sum((x - mean_dr) ** 2 for x in daily_returns) / (n_dr - 1)
        std_dr = math.sqrt(var)
        sharpe = round(mean_dr / std_dr * math.sqrt(252.0), 4) if std_dr > 0 else None
    return {
        "total_capital_ntd": round(total_capital, 2),
        "final_equity_ntd": round(final, 2),
        "total_return_pct": round(total_ret, 4),
        "cagr_pct": cagr,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": _max_drawdown_pct(equities, total_capital),
        "pools": pool_parts,
    }


def run_partitioned_tactical_combo(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    c18acc_periods: list[dict[str, Any]],
    tactical_periods: list[dict[str, Any]],
    total_capital: float = 50_000.0,
    c18acc_slots: int = 2,
    tactical_slots: int = 1,
    tactical_pool: str = "hook_buy",
    cost_model: dict | None = None,
) -> dict[str, Any]:
    """獨立資金池：C18acc 佔 c18acc_slots / total_slots · 戰術腿佔其餘。"""
    total_slots = c18acc_slots + tactical_slots
    close, _, _ = load_price_panels(conn)
    panel = close.reindex(trade_dates)
    cap_c18 = total_capital * c18acc_slots / total_slots
    cap_tac = total_capital * tactical_slots / total_slots

    m_c18 = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        c18acc_periods,
        total_capital=cap_c18,
        n_slots=max(1, c18acc_slots),
        cost_model=cost_model,
        return_daily=True,
    )
    m_tac = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        tactical_periods,
        total_capital=cap_tac,
        n_slots=max(1, tactical_slots),
        cost_model=cost_model,
        return_daily=True,
    )

    nav_c18 = {r["date"]: float(r["equity_ntd"]) for r in (m_c18.get("daily_nav") or [])}
    nav_tac = {r["date"]: float(r["equity_ntd"]) for r in (m_tac.get("daily_nav") or [])}
    equities: list[float] = []
    daily_returns: list[float] = []
    prev = total_capital
    for d in trade_dates:
        eq = nav_c18.get(d, cap_c18) + nav_tac.get(d, cap_tac)
        equities.append(eq)
        if prev > 0:
            daily_returns.append((eq / prev - 1.0) * 100.0)
        prev = eq

    ret_c18 = _daily_return_series(m_c18.get("daily_nav") or [])
    ret_tac = _daily_return_series(m_tac.get("daily_nav") or [])
    common = sorted(set(ret_c18) & set(ret_tac))
    rho = _pearson_corr([ret_c18[d] for d in common], [ret_tac[d] for d in common])

    combo = _combined_metrics(
        total_capital=total_capital,
        trade_dates=trade_dates,
        equities=equities,
        daily_returns=daily_returns,
        pool_parts=[
            {"pool": "c18acc", "n_slots": c18acc_slots, "capital": cap_c18, **m_c18},
            {"pool": tactical_pool, "n_slots": tactical_slots, "capital": cap_tac, **m_tac},
        ],
    )
    combo["mode"] = "partitioned"
    combo["daily_return_corr"] = rho
    combo["n_trades"] = int(m_c18.get("n_trades") or 0) + int(m_tac.get("n_trades") or 0)
    return combo


def run_shared_pool_tactical_combo(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    c18acc_periods: list[dict[str, Any]],
    hook_periods: list[dict[str, Any]],
    total_capital: float = 50_000.0,
    n_slots: int = 3,
    cost_model: dict | None = None,
) -> dict[str, Any]:
    """共用槽池 · C18acc 優先（priority=1）· hook 次之（priority=2）。"""
    merged = sorted(
        [*c18acc_periods, *hook_periods],
        key=lambda p: (
            str(p.get("entry_date") or ""),
            int(p.get("_priority", 99)),
            str(p.get("stock_id") or ""),
        ),
    )
    close, _, _ = load_price_panels(conn)
    panel = close.reindex(trade_dates)
    m = simulate_slot_portfolio_yield_new(
        conn,
        panel,
        trade_dates,
        merged,
        total_capital=total_capital,
        n_slots=n_slots,
        cost_model=cost_model,
        eviction="lowest_priority",
        preempt_only_if_higher_priority=True,
        return_daily=True,
    )
    daily_nav = m.get("daily_nav") or []
    equities = [float(r["equity_ntd"]) for r in daily_nav]
    daily_returns: list[float] = []
    prev = total_capital
    for eq in equities:
        if prev > 0:
            daily_returns.append((eq / prev - 1.0) * 100.0)
        prev = eq
    combo = _combined_metrics(
        total_capital=total_capital,
        trade_dates=trade_dates,
        equities=equities,
        daily_returns=daily_returns,
        pool_parts=[{"pool": "shared", "n_slots": n_slots, **m}],
    )
    combo["mode"] = "shared_yield_priority"
    combo["n_preemptions"] = m.get("n_preemptions")
    return combo


def run_tactical_slot_study(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    total_capital: float = 50_000.0,
    c18acc_slots: int = 2,
    tactical_slots: int = 1,
    hook_entry: HookEntryParams | None = None,
    hook_exit: TacticalExitParams | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError("need ≥2 trade dates in panel")

    cache: dict[str, Any] = {}
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=trade_dates, etf_codes=etf_codes
    )
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    cache.update(
        {
            "frames": frames,
            "trajectories": trajectories,
            "bench": bench,
            "stock_maps": _build_stock_frame_maps(trajectories, frames),
        }
    )

    hook_result = run_hook_buy_event_backtest(
        conn,
        dates=trade_dates,
        etf_codes=etf_codes,
        entry_params=hook_entry,
        exit_params=hook_exit,
        _cache=cache,
    )
    hook_periods = trades_to_slot_periods(hook_result["trades"])

    c18acc_3, c18_summary_3 = load_c18acc_periods(conn, trade_dates, n_slots=3)
    c18acc_2, _c18_summary_2 = load_c18acc_periods(conn, trade_dates, n_slots=c18acc_slots)

    panel = close.reindex(trade_dates)
    baseline_3 = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        c18acc_3,
        total_capital=total_capital,
        n_slots=3,
        return_daily=True,
    )
    hook_only = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        hook_periods,
        total_capital=total_capital / 3,
        n_slots=1,
        return_daily=True,
    )

    partitioned = run_partitioned_tactical_combo(
        conn,
        trade_dates=trade_dates,
        c18acc_periods=c18acc_2,
        tactical_periods=hook_periods,
        total_capital=total_capital,
        c18acc_slots=c18acc_slots,
        tactical_slots=tactical_slots,
        tactical_pool="hook_buy",
    )
    shared = run_shared_pool_tactical_combo(
        conn,
        trade_dates=trade_dates,
        c18acc_periods=c18acc_3,
        hook_periods=hook_periods,
        total_capital=total_capital,
        n_slots=3,
    )

    overlap = 0
    if hook_periods and c18acc_3:
        c18_keys = {
            (p["stock_id"], p["entry_date"])
            for p in c18acc_3
        }
        overlap = sum(
            1 for p in hook_periods if (p["stock_id"], p["entry_date"]) in c18_keys
        )

    return {
        "date_from": trade_dates[0],
        "date_to": trade_dates[-1],
        "panel_meta": meta,
        "hook_events": hook_result,
        "c18acc_summary": c18_summary_3,
        "overlap_with_c18acc": overlap,
        "overlap_pct": round(100 * overlap / len(hook_periods), 1) if hook_periods else 0.0,
        "portfolios": {
            "c18acc_3slot_baseline": baseline_3,
            "hook_1slot_third_capital": hook_only,
            "partitioned_2plus1": partitioned,
            "shared_3slot_priority": shared,
        },
        "hypotheses": _evaluate_hypotheses(
            baseline=baseline_3,
            partitioned=partitioned,
            tactical_summary=hook_result["summary"],
        ),
    }


def run_imp_early_long_tactical_slot_study(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    total_capital: float = 50_000.0,
    c18acc_slots: int = 2,
    tactical_slots: int = 1,
) -> dict[str, Any]:
    """imp_early_long 凍結規格 · partitioned 2+1 vs C18acc 3 槽。"""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError("need ≥2 trade dates in panel")

    cache: dict[str, Any] = {}
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=trade_dates, etf_codes=etf_codes
    )
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    cache.update(
        {
            "frames": frames,
            "trajectories": trajectories,
            "bench": bench,
            "stock_maps": _build_stock_frame_maps(trajectories, frames),
        }
    )

    imp_result = run_imp_early_long_event_backtest(
        conn,
        dates=trade_dates,
        etf_codes=etf_codes,
        _cache=cache,
    )
    imp_periods = trades_to_slot_periods(
        imp_result["trades"],
        source_strategy="imp_early_long",
        priority=2,
    )

    c18acc_3, c18_summary_3 = load_c18acc_periods(conn, trade_dates, n_slots=3)
    c18acc_2, _c18_summary_2 = load_c18acc_periods(conn, trade_dates, n_slots=c18acc_slots)

    panel = close.reindex(trade_dates)
    baseline_3 = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        c18acc_3,
        total_capital=total_capital,
        n_slots=3,
        return_daily=True,
    )
    imp_only = simulate_slot_portfolio(
        conn,
        panel,
        trade_dates,
        imp_periods,
        total_capital=total_capital / 3,
        n_slots=1,
        return_daily=True,
    )

    partitioned = run_partitioned_tactical_combo(
        conn,
        trade_dates=trade_dates,
        c18acc_periods=c18acc_2,
        tactical_periods=imp_periods,
        total_capital=total_capital,
        c18acc_slots=c18acc_slots,
        tactical_slots=tactical_slots,
        tactical_pool="imp_early_long",
    )
    shared = run_shared_pool_tactical_combo(
        conn,
        trade_dates=trade_dates,
        c18acc_periods=c18acc_3,
        hook_periods=imp_periods,
        total_capital=total_capital,
        n_slots=3,
    )

    overlap = 0
    if imp_periods and c18acc_3:
        c18_keys = {
            (p["stock_id"], p["entry_date"])
            for p in c18acc_3
        }
        overlap = sum(
            1 for p in imp_periods if (p["stock_id"], p["entry_date"]) in c18_keys
        )

    hyp = _evaluate_hypotheses(
        baseline=baseline_3,
        partitioned=partitioned,
        tactical_summary=imp_result["summary"],
    )
    graduation = evaluate_imp_second_leg_graduation(
        tactical_summary=imp_result["summary"],
        partitioned=partitioned,
        baseline=baseline_3,
        overlap_pct=round(100 * overlap / len(imp_periods), 1) if imp_periods else 0.0,
    )

    return {
        "date_from": trade_dates[0],
        "date_to": trade_dates[-1],
        "panel_meta": meta,
        "tactical_signal": "imp_early_long",
        "tactical_events": imp_result,
        "c18acc_summary": c18_summary_3,
        "overlap_with_c18acc": overlap,
        "overlap_pct": round(100 * overlap / len(imp_periods), 1) if imp_periods else 0.0,
        "portfolios": {
            "c18acc_3slot_baseline": baseline_3,
            "imp_early_long_1slot_third_capital": imp_only,
            "partitioned_2plus1": partitioned,
            "shared_3slot_priority": shared,
        },
        "hypotheses": hyp,
        "graduation": graduation,
    }


def evaluate_imp_second_leg_graduation(
    *,
    tactical_summary: dict[str, Any],
    partitioned: dict[str, Any],
    baseline: dict[str, Any],
    overlap_pct: float,
    min_tactical_events: int = 10,
) -> dict[str, Any]:
    """G3 組合門檻 · 與 G2 OOS 合併判斷是否採納為第二條腿。"""
    tac_n = tactical_summary.get("n") or 0
    tac_excess = tactical_summary.get("mean_excess_pct")
    b_cagr = baseline.get("cagr_pct")
    p_cagr = partitioned.get("cagr_pct")
    rho = partitioned.get("daily_return_corr")

    notes: list[str] = []
    g3_pass = True
    if tac_n < min_tactical_events:
        g3_pass = False
        notes.append(f"tactical n={tac_n} < {min_tactical_events}")
    if tac_excess is None or tac_excess <= 0:
        g3_pass = False
        notes.append(f"tactical mean excess={tac_excess}% ≤ 0")

    cagr_up = None
    if b_cagr is not None and p_cagr is not None:
        cagr_up = p_cagr > b_cagr
        if not cagr_up:
            notes.append(f"partitioned CAGR {p_cagr}% ≤ baseline {b_cagr}%")

    low_corr = rho is not None and abs(rho) < 0.3
    combo_value = bool(cagr_up) or (low_corr and tac_excess is not None and tac_excess > 0)
    if g3_pass and not combo_value:
        g3_pass = False
        notes.append("no CAGR uplift and corr/alpha combo insufficient")

    return {
        "g3_combo_pass": g3_pass and combo_value,
        "min_tactical_events": min_tactical_events,
        "tactical_mean_excess_pct": tac_excess,
        "tactical_n": tac_n,
        "overlap_pct": overlap_pct,
        "cagr_up_vs_baseline": cagr_up,
        "daily_return_corr": rho,
        "notes": notes,
    }


def merge_graduation_verdict(
    *,
    g2: dict[str, Any],
    g3: dict[str, Any],
) -> dict[str, Any]:
    adopt = bool(g2.get("passed")) and bool(g3.get("g3_combo_pass"))
    if adopt:
        verdict = "ADOPT_SECOND_LEG"
        action = "採納 imp_early_long 為第二條腿（partitioned 2+1）"
    elif g2.get("passed"):
        verdict = "WEAK_COMBO"
        action = "G2 通過但組合未證明增值 · 維持 research · 不進 strategy"
    else:
        verdict = "CONVERGE_C18ACC"
        action = "收斂至 C18acc 強化（dual-WMA 出場 / extension overlay）"
    return {
        "verdict": verdict,
        "adopt_second_leg": adopt,
        "g2_pass": bool(g2.get("passed")),
        "g3_pass": bool(g3.get("g3_combo_pass")),
        "action": action,
    }


def _evaluate_hypotheses(
    *,
    baseline: dict[str, Any],
    partitioned: dict[str, Any],
    tactical_summary: dict[str, Any],
) -> dict[str, Any]:
    b_cagr = baseline.get("cagr_pct")
    p_cagr = partitioned.get("cagr_pct")
    b_dd = baseline.get("max_drawdown_pct")
    p_dd = partitioned.get("max_drawdown_pct")
    h_c1 = None
    h_c2 = None
    h_c4 = None
    if b_cagr is not None and p_cagr is not None:
        h_c1 = p_cagr > b_cagr
    rho = partitioned.get("daily_return_corr")
    if rho is not None:
        h_c2 = abs(rho) < 0.3
    if b_dd is not None and p_dd is not None and b_dd > 0:
        h_c4 = (p_dd - b_dd) / b_dd < 0.2
    return {
        "H-C1_cagr_up": h_c1,
        "H-C2_low_corr": h_c2,
        "H-C4_maxdd_bounded": h_c4,
        "hook_n": tactical_summary.get("n"),
        "hook_mean_excess_pct": tactical_summary.get("mean_excess_pct"),
        "tactical_n": tactical_summary.get("n"),
        "tactical_mean_excess_pct": tactical_summary.get("mean_excess_pct"),
    }


def hook_buy_entry_grid() -> list[HookEntryParams]:
    """Phase 1 · retrace_eps × hook_quality × W20 band（peak 固定 2.0 · 無 mixed）。"""
    grid: list[HookEntryParams] = []
    w20_bands: list[tuple[float | None, float | None]] = [
        (None, None),
        (98.0, 100.0),
        (96.0, 100.0),
    ]
    for retrace_eps, hq_min, both_vec, (rs_min, rs_max) in itertools.product(
        (0.05, 0.10, 0.15, 0.20),
        (0.0, 0.05, 0.10, 0.20),
        (False, True),
        w20_bands,
    ):
        grid.append(
            HookEntryParams(
                pool_tier="strict",
                retrace_eps=retrace_eps,
                hook_quality_min=hq_min,
                peak_signal_min=SPREAD_SIGNAL_MIN,
                allow_mixed_peak=False,
                require_both_vectors=both_vec,
                w20_rs_min=rs_min,
                w20_rs_max=rs_max,
                first_per_day=True,
            )
        )
    return grid


def hook_buy_entry_grid_1b() -> list[HookEntryParams]:
    """Phase 1b · peak_signal_min × allow_mixed × retrace × hq × W20。"""
    grid: list[HookEntryParams] = []
    w20_bands: list[tuple[float | None, float | None]] = [
        (None, None),
        (96.0, 100.0),
    ]
    for peak_min, mixed, retrace_eps, hq_min, (rs_min, rs_max) in itertools.product(
        (1.0, 1.5, 2.0),
        (False, True),
        (0.05, 0.10, 0.15),
        (0.0, 0.10),
        w20_bands,
    ):
        grid.append(
            HookEntryParams(
                pool_tier="strict",
                retrace_eps=retrace_eps,
                hook_quality_min=hq_min,
                peak_signal_min=peak_min,
                allow_mixed_peak=mixed,
                require_both_vectors=True,
                w20_rs_min=rs_min,
                w20_rs_max=rs_max,
                first_per_day=True,
            )
        )
    return grid


def _hook_entry_key(ep: HookEntryParams) -> tuple:
    return (
        ep.retrace_eps,
        ep.peak_signal_min,
        ep.allow_mixed_peak,
        ep.hook_quality_min,
        ep.require_both_vectors,
        ep.w20_rs_min,
        ep.w20_rs_max,
        ep.first_per_day,
    )


def _is_champion_hook_row(r: dict[str, Any]) -> bool:
    return (
        r.get("retrace_eps") == 0.10
        and r.get("pool_tier") == "strict"
        and r.get("peak_signal_min") == 1.0
        and r.get("allow_mixed_peak") is True
        and r.get("hook_quality_min") == 0.0
        and r.get("require_both_vectors") is True
        and r.get("w20_rs_min") == 96.0
        and r.get("w20_rs_max") == 100.0
    )


def _summary_to_hook_row(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_signal": "hook_buy",
        "pool_tier": summary.get("pool_tier"),
        "retrace_eps": summary.get("retrace_eps"),
        "peak_signal_min": summary.get("peak_signal_min"),
        "allow_mixed_peak": summary.get("allow_mixed_peak"),
        "hook_quality_min": summary.get("hook_quality_min"),
        "require_both_vectors": summary.get("require_both_vectors"),
        "w20_rs_min": summary.get("w20_rs_min"),
        "w20_rs_max": summary.get("w20_rs_max"),
        "hook_retrace_pct_min": summary.get("hook_retrace_pct_min"),
        "spread_dist_min": summary.get("spread_dist_min"),
        "spread_dist_max": summary.get("spread_dist_max"),
        "hook_pool_peak_dist_min": summary.get("hook_pool_peak_dist_min"),
        "hook_pool_peak_dist_max": summary.get("hook_pool_peak_dist_max"),
        "exit_cap_days": summary.get("exit_cap_days"),
        "n": summary.get("n"),
        "mean_net_pct": summary.get("mean_net_pct"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "median_excess_pct": summary.get("median_excess_pct"),
        "win_rate": summary.get("win_rate"),
        "excess_win_rate": summary.get("excess_win_rate"),
        "mean_hold_polls": summary.get("mean_hold_polls"),
        "p10_net_pct": summary.get("p10_net_pct"),
    }


def _run_hook_grid_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    grid: list[HookEntryParams],
    exit_params: TacticalExitParams,
    phase_label: str,
) -> dict[str, Any]:
    print("  building dual WMA trajectories (once) ...", flush=True)
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    annot_keys = sorted({ep.annot_key() for ep in grid}, key=lambda k: (k.retrace_eps, k.peak_signal_min, k.allow_mixed_peak))
    trajectories_by_key, stock_maps_by_key = _build_annot_caches(
        trajectories, frames, annot_keys
    )

    cache = {
        "frames": frames,
        "trajectories": trajectories,
        "bench": bench,
        "stock_maps": stock_maps,
    }

    rows: list[dict[str, Any]] = []
    for i, ep in enumerate(grid, 1):
        if i % 24 == 0:
            print(f"  {phase_label} sweep {i}/{len(grid)} ...", flush=True)
        result = run_hook_buy_event_backtest(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            entry_params=ep,
            exit_params=exit_params,
            _cache=cache,
            trajectories_by_key=trajectories_by_key,
            stock_maps_by_key=stock_maps_by_key,
        )
        rows.append(_summary_to_hook_row(result["summary"]))

    imp_baseline = run_w20_leading_exit_grid(
        conn,
        dates=dates,
        entry_params=imp_early_long_champion_entry(),
        exit_params=W20LeadingExitParams(cap_days=exit_params.cap_days),
        etf_codes=etf_codes,
        _cache=cache,
    )
    imp_row = {
        "entry_signal": "imp_early_long",
        "retrace_eps": None,
        "peak_signal_min": None,
        "allow_mixed_peak": None,
        "hook_quality_min": None,
        "require_both_vectors": True,
        "w20_rs_min": 96.0,
        "w20_rs_max": 100.0,
        "exit_cap_days": exit_params.cap_days,
        "n": imp_baseline.get("n"),
        "mean_net_pct": imp_baseline.get("mean_net_pct"),
        "mean_excess_pct": imp_baseline.get("mean_excess_pct"),
        "median_excess_pct": imp_baseline.get("median_excess_pct"),
        "win_rate": imp_baseline.get("win_rate"),
        "excess_win_rate": imp_baseline.get("excess_win_rate"),
        "mean_hold_polls": imp_baseline.get("mean_hold_polls"),
        "p10_net_pct": imp_baseline.get("p10_net_pct"),
    }

    ranked = sorted(
        rows,
        key=lambda r: (-(r.get("mean_excess_pct") or -999.0), -(r.get("n") or 0)),
    )
    ranked_n = sorted(
        rows,
        key=lambda r: (-(r.get("n") or 0), -(r.get("mean_excess_pct") or -999.0)),
    )
    champion = next((r for r in rows if _is_champion_hook_row(r)), None)

    return {
        "phase": phase_label,
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "panel_meta": meta,
        "exit_cap_days": exit_params.cap_days,
        "exit_label": _w20_exit_from_tactical(exit_params).label(),
        "imp_early_long_baseline": imp_row,
        "champion_hook_row": champion,
        "rows": rows,
        "top_by_excess": ranked[:15],
        "top_by_n": ranked_n[:15],
        "by_retrace_eps": _aggregate_by_retrace(rows),
        "by_peak_signal_min": _aggregate_by_peak_min(rows),
    }


def run_hook_entry_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    xp = exit_params or tactical_exit_params()
    return _run_hook_grid_sweep(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        grid=hook_buy_entry_grid(),
        exit_params=xp,
        phase_label="Phase1",
    )


def run_hook_entry_sweep_1b(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    xp = exit_params or tactical_exit_params()
    return _run_hook_grid_sweep(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        grid=hook_buy_entry_grid_1b(),
        exit_params=xp,
        phase_label="Phase1b",
    )


def _min_n_for_pool_tier(tier: str | None) -> int:
    return {
        "pool": 20,
        "retrace": 10,
        "improving": 5,
        "strict": 3,
    }.get(str(tier or ""), 10)


def _fmt_band(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is None:
        return "全"
    if lo is None:
        return f"≤{hi}"
    if hi is None:
        return f"≥{lo}"
    return f"{lo}–{hi}"


def hook_pool_tier_entry_grid() -> list[HookEntryParams]:
    """Phase 2 · pool / retrace / improving 寬網超額篩選 sweep。"""
    grid: list[HookEntryParams] = []
    tiers: list[HookPoolTier] = ["pool", "retrace", "improving"]
    w20_bands: list[tuple[float | None, float | None]] = [
        (None, None),
        (96.0, 100.0),
        (98.0, 100.0),
        (100.0, None),
    ]
    retrace_mins: list[float | None] = [None, 0.10, 0.15, 0.20]
    spread_buckets: list[tuple[float | None, float | None]] = [
        (None, None),
        (None, 1.8),
        (1.5, None),
    ]
    peak_buckets: list[tuple[float | None, float | None]] = [
        (None, None),
        (1.2, None),
    ]
    for tier, (rs_min, rs_max), ret_min, (sd_min, sd_max), (pk_min, pk_max), both_vec in itertools.product(
        tiers,
        w20_bands,
        retrace_mins,
        spread_buckets,
        peak_buckets,
        (False, True),
    ):
        grid.append(
            HookEntryParams(
                pool_tier=tier,
                hook_quality_min=0.0,
                require_both_vectors=both_vec,
                w20_rs_min=rs_min,
                w20_rs_max=rs_max,
                hook_retrace_pct_min=ret_min,
                spread_dist_min=sd_min,
                spread_dist_max=sd_max,
                hook_pool_peak_dist_min=pk_min,
                hook_pool_peak_dist_max=pk_max,
                first_per_day=True,
            )
        )
    for tier, hq_min in itertools.product(("retrace", "improving"), (0.05, 0.10)):
        grid.append(
            HookEntryParams(
                pool_tier=tier,
                hook_quality_min=hq_min,
                require_both_vectors=True,
                w20_rs_min=96.0,
                w20_rs_max=100.0,
                first_per_day=True,
            )
        )
    return grid


def _aggregate_pool_tier_funnel(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tiers = ["pool", "retrace", "improving", "strict"]
    out: list[dict[str, Any]] = []
    for tier in tiers:
        tier_rows = [r for r in rows if r.get("pool_tier") == tier]
        if not tier_rows:
            continue
        baseline = next(
            (
                r
                for r in tier_rows
                if r.get("w20_rs_min") is None
                and r.get("hook_retrace_pct_min") is None
                and r.get("spread_dist_min") is None
                and r.get("spread_dist_max") is None
                and r.get("hook_pool_peak_dist_min") is None
                and r.get("require_both_vectors") is False
                and (r.get("hook_quality_min") or 0) == 0
            ),
            None,
        )
        min_n = _min_n_for_pool_tier(tier)
        pos = [
            r
            for r in tier_rows
            if (r.get("n") or 0) >= min_n and (r.get("mean_excess_pct") or -999) > 0
        ]
        best_ex = max(
            tier_rows,
            key=lambda r: (r.get("mean_excess_pct") or -999.0, r.get("n") or 0),
        )
        out.append(
            {
                "tier": tier,
                "baseline_n": baseline.get("n") if baseline else None,
                "baseline_excess": baseline.get("mean_excess_pct") if baseline else None,
                "grid_rows": len(tier_rows),
                "rows_pos_excess_min_n": len(pos),
                "best_excess": best_ex.get("mean_excess_pct"),
                "best_n": best_ex.get("n"),
                "min_n_threshold": min_n,
            }
        )
    return out


def run_hook_pool_tier_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    exit_params: TacticalExitParams | None = None,
) -> dict[str, Any]:
    xp = exit_params or tactical_exit_params()
    payload = _run_hook_grid_sweep(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        grid=hook_pool_tier_entry_grid(),
        exit_params=xp,
        phase_label="Phase2-pool",
    )
    rows = payload.get("rows") or []
    imp_ex = (payload.get("imp_early_long_baseline") or {}).get("mean_excess_pct")

    def _eligible(r: dict[str, Any]) -> bool:
        tier = r.get("pool_tier")
        n = r.get("n") or 0
        ex = r.get("mean_excess_pct")
        if ex is None or ex <= 0:
            return False
        return n >= _min_n_for_pool_tier(tier)

    eligible = [r for r in rows if _eligible(r)]
    top_by_excess = sorted(
        eligible,
        key=lambda r: (-(r.get("mean_excess_pct") or -999.0), -(r.get("n") or 0)),
    )[:10]
    top_by_n = sorted(
        eligible,
        key=lambda r: (-(r.get("n") or 0), -(r.get("mean_excess_pct") or -999.0)),
    )[:10]

    vs_imp = []
    for r in top_by_excess[:5]:
        ex = r.get("mean_excess_pct")
        delta = round(ex - imp_ex, 3) if ex is not None and imp_ex is not None else None
        vs_imp.append({**r, "vs_imp_early_long_pp": delta})

    payload["by_pool_tier"] = _aggregate_pool_tier_funnel(rows)
    payload["top_by_excess_min_n"] = top_by_excess
    payload["top_by_n_pos_excess"] = top_by_n
    payload["n_eligible"] = len(eligible)
    payload["n_grid"] = len(rows)
    payload["vs_imp_top5"] = vs_imp
    return payload


def render_hook_pool_tier_sweep_md(payload: dict[str, Any]) -> str:
    imp = payload.get("imp_early_long_baseline") or {}
    imp_ex = imp.get("mean_excess_pct")
    lines = [
        "# Dual WMA hook · Phase 2 pool/retrace 超額 Sweep",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}** · "
        f"成本 round-trip **{ROUND_TRIP_COST_PCT}%** · grid **{payload.get('n_grid')}** 列",
        "",
        "## 凍結出場",
        "",
        f"- **{payload['exit_label']}** · cap **{payload['exit_cap_days']}** 交易日",
        "",
        "## imp_early_long 對照（champion 進場 · 同出場）",
        "",
        f"- n={imp.get('n')} · 超額 **{imp_ex}%** · 勝率 {imp.get('win_rate')}% · "
        f"持有 {imp.get('mean_hold_polls')} 幀",
        "",
        "## 漏斗（tier 基線 → 有邊際篩選格）",
        "",
        "| tier | 基線 n | 基線超額% | grid | 超額>0 且 n≥門檻 | best 超額% | best n | 門檻 n |",
        "|------|--------|----------|------|------------------|-----------|--------|--------|",
    ]
    for row in payload.get("by_pool_tier") or []:
        lines.append(
            f"| {row.get('tier')} | {row.get('baseline_n')} | {row.get('baseline_excess')} | "
            f"{row.get('grid_rows')} | {row.get('rows_pos_excess_min_n')} | "
            f"{row.get('best_excess')} | {row.get('best_n')} | {row.get('min_n_threshold')} |"
        )

    lines.extend(
        [
            "",
            "## Top 10（超額 · n≥門檻 · 超額>0）",
            "",
            "| tier | W20 | ret_min | spread | peak | both | hq | n | 超額% | vs imp | 勝率 | 持有 |",
            "|------|-----|---------|--------|------|------|-----|---|-------|--------|------|------|",
        ]
    )
    for r in payload.get("top_by_excess_min_n") or []:
        ex = r.get("mean_excess_pct")
        delta = (
            f"{ex - imp_ex:+.3f}"
            if ex is not None and imp_ex is not None
            else "—"
        )
        lines.append(
            f"| {r.get('pool_tier')} | "
            f"{_fmt_band(r.get('w20_rs_min'), r.get('w20_rs_max'))} | "
            f"{r.get('hook_retrace_pct_min') or '—'} | "
            f"{_fmt_band(r.get('spread_dist_min'), r.get('spread_dist_max'))} | "
            f"{_fmt_band(r.get('hook_pool_peak_dist_min'), r.get('hook_pool_peak_dist_max'))} | "
            f"{r.get('require_both_vectors')} | {r.get('hook_quality_min')} | "
            f"{r.get('n')} | {ex} | {delta} | {r.get('win_rate')} | {r.get('mean_hold_polls')} |"
        )

    lines.extend(
        [
            "",
            "## Top 10（n · 超額>0 · n≥門檻）",
            "",
            "| tier | W20 | ret_min | spread | peak | both | n | 超額% | 勝率 |",
            "|------|-----|---------|--------|------|------|---|-------|------|",
        ]
    )
    for r in payload.get("top_by_n_pos_excess") or []:
        lines.append(
            f"| {r.get('pool_tier')} | "
            f"{_fmt_band(r.get('w20_rs_min'), r.get('w20_rs_max'))} | "
            f"{r.get('hook_retrace_pct_min') or '—'} | "
            f"{_fmt_band(r.get('spread_dist_min'), r.get('spread_dist_max'))} | "
            f"{_fmt_band(r.get('hook_pool_peak_dist_min'), r.get('hook_pool_peak_dist_max'))} | "
            f"{r.get('require_both_vectors')} | {r.get('n')} | {r.get('mean_excess_pct')} | "
            f"{r.get('win_rate')} |"
        )

    best = (payload.get("top_by_excess_min_n") or [None])[0]
    lines.extend(["", "## 建議（Research layer · 下一版戰術槽 champion）", ""])
    if best:
        lines.append(
            f"- **首選 tier={best.get('pool_tier')}** · W20 {_fmt_band(best.get('w20_rs_min'), best.get('w20_rs_max'))} · "
            f"hook_retrace≥{best.get('hook_retrace_pct_min') or 'pool 預設'} · "
            f"spread {_fmt_band(best.get('spread_dist_min'), best.get('spread_dist_max'))} · "
            f"both_vec={best.get('require_both_vectors')}"
        )
        lines.append(
            f"  - n={best.get('n')} · 超額 **{best.get('mean_excess_pct')}%** · "
            f"vs imp_early_long **{(best.get('mean_excess_pct') or 0) - (imp_ex or 0):+.3f}** pp"
        )
    else:
        lines.append("- 無格點同時滿足 n 門檻與正超額；維持 imp_early_long 或縮小至 retrace/improving 再驗。")

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- **pool**：`hook_candidate` 寬網（session peak 回撤）· 不要求 aligned 收斂。",
            "- **retrace**：pool + W20 Improving · **improving**：+ W5 Improving。",
            "- `hook_retrace_pct_min` 為進場點幾何門檻（疊加 pool 預設 5% 回撤）。",
            "- 門檻：pool n≥20 · retrace n≥10 · improving n≥5。",
        ]
    )
    return "\n".join(lines) + "\n"


def _count_tier_events(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    tier: HookPoolTier,
    *,
    first_per_day: bool,
    w20_rs_min: float | None = None,
    w20_rs_max: float | None = None,
) -> tuple[int, dict[str, int], int]:
    """Returns (n_events, per_day_counts, n_raw_polls)."""
    ep = HookEntryParams(
        pool_tier=tier,
        first_per_day=first_per_day,
        w20_rs_min=w20_rs_min,
        w20_rs_max=w20_rs_max,
        require_both_vectors=(tier in ("improving", "strict")),
    )
    events = _collect_hook_events(trajectories, frames, ep)
    per_day: dict[str, int] = {}
    for _sid, idx in events:
        d = frames[idx]["date"]
        per_day[d] = per_day.get(d, 0) + 1

    raw_polls = 0
    if tier == "pool" and not first_per_day:
        frame_ids = [f["id"] for f in frames]
        for t in trajectories:
            by_id = {p["frame_id"]: p for p in t["points"]}
            for fid in frame_ids:
                p = by_id.get(fid)
                if p and p.get("fresh") and p.get("hook_candidate"):
                    raw_polls += 1
    return len(events), per_day, raw_polls


def run_hook_pool_audit(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
) -> dict[str, Any]:
    """寬網反曲線候選池 · 漏斗計數（先撈再篩）。"""
    print("  building dual WMA trajectories (once) ...", flush=True)
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    n_stocks = len(trajectories)
    n_days = len(dates)
    universe_slots = n_stocks * n_days

    tiers: list[HookPoolTier] = ["pool", "retrace", "improving", "strict"]
    funnel: list[dict[str, Any]] = []
    for tier in tiers:
        n_1pd, per_day, _ = _count_tier_events(
            trajectories, frames, tier, first_per_day=True
        )
        n_all, _, _ = _count_tier_events(
            trajectories, frames, tier, first_per_day=False
        )
        days_with = sum(1 for c in per_day.values() if c > 0)
        mean_per_day = round(n_1pd / n_days, 1) if n_days else 0.0
        mean_per_active_day = (
            round(n_1pd / days_with, 1) if days_with else 0.0
        )
        funnel.append(
            {
                "tier": tier,
                "n_first_per_day": n_1pd,
                "n_all_polls": n_all,
                "days_with_events": days_with,
                "mean_per_day": mean_per_day,
                "mean_per_active_day": mean_per_active_day,
                "pct_universe": round(100 * n_1pd / universe_slots, 2)
                if universe_slots
                else 0.0,
            }
        )

    _, per_day_pool, _ = _count_tier_events(
        trajectories, frames, "pool", first_per_day=True
    )
    _, per_day_strict, _ = _count_tier_events(
        trajectories, frames, "strict", first_per_day=True
    )

    raw_candidate_polls = 0
    stock_days_candidate: set[tuple[str, str]] = set()
    frame_ids = [f["id"] for f in frames]
    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if p and p.get("fresh") and p.get("hook_candidate"):
                raw_candidate_polls += 1
                stock_days_candidate.add((sid, frames[i]["date"]))

    strict_champion = hook_buy_champion_entry()
    n_strict_champ, _, _ = _count_tier_events(
        trajectories,
        frames,
        "strict",
        first_per_day=True,
        w20_rs_min=strict_champion.w20_rs_min,
        w20_rs_max=strict_champion.w20_rs_max,
    )

    improving_w20_band = HookEntryParams(
        pool_tier="improving",
        first_per_day=True,
        w20_rs_min=96.0,
        w20_rs_max=100.0,
        require_both_vectors=True,
    )
    n_imp_band = len(_collect_hook_events(trajectories, frames, improving_w20_band))

    daily_rows: list[dict[str, Any]] = []
    all_dates = sorted({f["date"] for f in frames})
    for d in all_dates:
        daily_rows.append(
            {
                "date": d,
                "pool_n": per_day_pool.get(d, 0),
                "strict_n": per_day_strict.get(d, 0),
            }
        )
    for row in funnel:
        if row["tier"] == "pool":
            row["n_all_polls"] = len(stock_days_candidate)
            row["raw_candidate_polls"] = raw_candidate_polls

    return {
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "n_stocks": n_stocks,
        "n_days": n_days,
        "universe_stock_days": universe_slots,
        "pool_params": {
            "pool_min_peak": DEFAULT_HOOK_POOL_MIN_PEAK,
            "pool_retrace_eps": DEFAULT_HOOK_POOL_RETRACE_EPS,
        },
        "funnel": funnel,
        "stock_days_with_candidate": len(stock_days_candidate),
        "raw_candidate_polls": raw_candidate_polls,
        "strict_champion_n": n_strict_champ,
        "improving_w20_96_100_n": n_imp_band,
        "daily_pool_vs_strict": daily_rows,
        "panel_meta": meta,
    }


def render_hook_pool_audit_md(payload: dict[str, Any]) -> str:
    pp = payload.get("pool_params") or {}
    lines = [
        "# Dual WMA hook 候選池 · 寬網漏斗 Audit",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}**",
        f" · 監控清單 **{payload.get('n_stocks')}** 檔 × **{payload.get('n_days')}** 交易日",
        "",
        "## 寬網定義（先撈）",
        "",
        f"- 盤中 `spread_dist` 自當日高點回撤 ≥ **{pp.get('pool_retrace_eps')}**",
        f" · 當日峰值 ≥ **{pp.get('pool_min_peak')}**",
        "- **不要求** extension 分類 · **不要求** aligned 收斂",
        "",
        "## 漏斗（first_per_day · 每檔每日至多 1 筆）",
        "",
        "| tier | 額外條件 | n | 佔 universe% | 有訊號日 | 日均 | 活躍日均 |",
        "|------|----------|---|-------------|---------|------|---------|",
    ]
    tier_zh = {
        "pool": "寬網候選",
        "retrace": "+ W20 Improving",
        "improving": "+ W5 Improving",
        "strict": "+ aligned 收斂（舊 hook_complete）",
    }
    for row in payload.get("funnel") or []:
        tier = row.get("tier")
        lines.append(
            f"| {tier} | {tier_zh.get(tier, tier)} | {row.get('n_first_per_day')} | "
            f"{row.get('pct_universe')}% | {row.get('days_with_events')} | "
            f"{row.get('mean_per_day')} | {row.get('mean_per_active_day')} |"
        )

    lines.extend(
        [
            "",
            "## 原始候選量",
            "",
            f"- 有 hook_candidate 的 **stock-day**：**{payload.get('stock_days_with_candidate')}**",
            f"- 候選 **poll 幀數**（未去重）：**{payload.get('raw_candidate_polls')}**",
            f"- strict champion（W20 96–100）：**{payload.get('strict_champion_n')}**",
            f"- improving + W20 96–100：**{payload.get('improving_w20_96_100_n')}**",
            "",
            "## 解讀",
            "",
            "- 研究流程應為：**寬網 pool → 分層篩選 → 回測**，而非直接從 strict 開始。",
            "- `strict` 僅是漏斗最末段；先前 n=3 是因為要求 aligned 收斂。",
            "- 下一步：在 **pool / improving** 層做超額篩選（W20 band · hq · Regime）。",
        ]
    )
    return "\n".join(lines) + "\n"


def _aggregate_by_retrace(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[float, list[dict[str, Any]]] = {}
    for r in rows:
        eps = float(r.get("retrace_eps") or 0)
        buckets.setdefault(eps, []).append(r)
    out: list[dict[str, Any]] = []
    for eps in sorted(buckets):
        bucket = buckets[eps]
        with_n = [r for r in bucket if (r.get("n") or 0) > 0]
        best = max(bucket, key=lambda r: (r.get("mean_excess_pct") or -999.0, r.get("n") or 0))
        out.append(
            {
                "retrace_eps": eps,
                "grid_rows": len(bucket),
                "rows_with_events": len(with_n),
                "max_n": max((r.get("n") or 0) for r in bucket),
                "best_mean_excess_pct": best.get("mean_excess_pct"),
                "best_n": best.get("n"),
                "best_hook_quality_min": best.get("hook_quality_min"),
                "best_w20_band": (best.get("w20_rs_min"), best.get("w20_rs_max")),
            }
        )
    return out


def _aggregate_by_peak_min(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[float, bool], list[dict[str, Any]]] = {}
    for r in rows:
        key = (float(r.get("peak_signal_min") or 0), bool(r.get("allow_mixed_peak")))
        buckets.setdefault(key, []).append(r)
    out: list[dict[str, Any]] = []
    for (peak_min, mixed) in sorted(buckets):
        bucket = buckets[(peak_min, mixed)]
        with_n = [r for r in bucket if (r.get("n") or 0) > 0]
        best = max(bucket, key=lambda r: (r.get("n") or 0, r.get("mean_excess_pct") or -999.0))
        best_ex = max(bucket, key=lambda r: (r.get("mean_excess_pct") or -999.0, r.get("n") or 0))
        out.append(
            {
                "peak_signal_min": peak_min,
                "allow_mixed_peak": mixed,
                "grid_rows": len(bucket),
                "rows_with_events": len(with_n),
                "max_n": max((r.get("n") or 0) for r in bucket),
                "best_n": best.get("n"),
                "best_mean_excess_pct": best_ex.get("mean_excess_pct"),
                "best_excess_n": best_ex.get("n"),
                "best_retrace_eps": best.get("retrace_eps"),
            }
        )
    return out


def render_hook_entry_sweep_md(payload: dict[str, Any]) -> str:
    if payload.get("phase") == "Phase1b":
        return render_hook_entry_sweep_1b_md(payload)
    imp = payload["imp_early_long_baseline"]
    lines = [
        "# Dual WMA hook_buy · Phase 1 進場 Sweep",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}** · "
        f"成本 round-trip **{ROUND_TRIP_COST_PCT}%**",
        "",
        "## 凍結出場",
        "",
        f"- **{payload['exit_label']}** · cap **{payload['exit_cap_days']}** 交易日",
        "",
        "## imp_early_long 對照（champion 進場 · 同出場）",
        "",
        f"- n={imp.get('n')} · 超額 **{imp.get('mean_excess_pct')}%** · "
        f"中位超額 {imp.get('median_excess_pct')}% · 勝率 {imp.get('win_rate')}% · "
        f"持有 {imp.get('mean_hold_polls')} 幀",
        "",
        "## retrace_eps 彙總",
        "",
        "| retrace_eps | grid | 有事件列 | max n | best 超額% | best n | best hq_min | best W20 |",
        "|-------------|------|----------|-------|-----------|--------|-------------|----------|",
    ]
    for row in payload.get("by_retrace_eps") or []:
        w20 = row.get("best_w20_band")
        w20_s = f"{w20[0]}–{w20[1]}" if w20 and w20[0] is not None else "全 band"
        lines.append(
            f"| {row.get('retrace_eps')} | {row.get('grid_rows')} | "
            f"{row.get('rows_with_events')} | {row.get('max_n')} | "
            f"{row.get('best_mean_excess_pct')} | {row.get('best_n')} | "
            f"{row.get('best_hook_quality_min')} | {w20_s} |"
        )

    lines.extend(
        [
            "",
            "## Top 15（依平均超額）",
            "",
            "| retrace | hq_min | both_vec | W20 band | n | 超額% | 中位超額% | 勝率 | 持有幀 | P10淨% |",
            "|---------|--------|----------|----------|---|-------|-----------|------|--------|--------|",
        ]
    )
    for r in payload.get("top_by_excess") or []:
        rs_lo, rs_hi = r.get("w20_rs_min"), r.get("w20_rs_max")
        band = f"{rs_lo}–{rs_hi}" if rs_lo is not None else "全"
        lines.append(
            f"| {r.get('retrace_eps')} | {r.get('hook_quality_min')} | "
            f"{r.get('require_both_vectors')} | {band} | {r.get('n')} | "
            f"{r.get('mean_excess_pct')} | {r.get('median_excess_pct')} | "
            f"{r.get('win_rate')} | {r.get('mean_hold_polls')} | {r.get('p10_net_pct')} |"
        )

    champ = payload.get("champion_hook_row")
    if champ:
        lines.extend(
            [
                "",
                "## Champion 預設（retrace=0.10 · hq=0 · both_vec · W20 96–100）",
                "",
                f"- n={champ.get('n')} · 超額 **{champ.get('mean_excess_pct')}%** · "
                f"持有 {champ.get('mean_hold_polls')} 幀",
            ]
        )
        imp_ex = imp.get("mean_excess_pct")
        ch_ex = champ.get("mean_excess_pct")
        if imp_ex is not None and ch_ex is not None:
            delta = round(ch_ex - imp_ex, 3)
            lines.append(f"- vs imp_early_long 超額差：**{delta:+.3f}** pp")

    lines.extend(
        [
            "",
            "## Phase 1 假說",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
        ]
    )
    best = (payload.get("top_by_excess") or [{}])[0]
    best_n = best.get("n") or 0
    best_ex = best.get("mean_excess_pct")
    imp_ex = imp.get("mean_excess_pct")
    h1 = best_n > (imp.get("n") or 0) if imp.get("n") else None
    h2 = best_ex is not None and imp_ex is not None and best_ex > imp_ex
    eps010 = next(
        (x for x in payload.get("by_retrace_eps") or [] if x.get("retrace_eps") == 0.10),
        None,
    )
    h3 = eps010 is not None and (eps010.get("max_n") or 0) >= 5
    lines.append(
        f"| H1 | hook sweep 最佳 n > imp_early_long | "
        f"{'✅' if h1 else '❌' if h1 is False else '—'} |"
    )
    lines.append(
        f"| H2 | hook 最佳超額 > imp_early_long | "
        f"{'✅' if h2 else '❌' if h2 is False else '—'} |"
    )
    lines.append(
        f"| H3 | retrace=0.10 時 max_n ≥ 5 | "
        f"{'✅' if h3 else '❌' if h3 is False else '—'} |"
    )
    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- `retrace_eps` 在 sweep 時重新標註 `hook_complete`（不重建 intraday panel）。",
            "- 預設 champion：`retrace_eps=0.10` · `hook_quality_min=0` · W20 96–100 · both vectors。",
            "- 括號內數字 = 相對 imp_early_long 基線；樣本小時僅供方向。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_hook_entry_sweep_1b_md(payload: dict[str, Any]) -> str:
    imp = payload["imp_early_long_baseline"]
    lines = [
        "# Dual WMA hook_buy · Phase 1b 進場 Sweep（peak_signal_min）",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}** · "
        f"成本 round-trip **{ROUND_TRIP_COST_PCT}%** · grid **{len(payload.get('rows') or [])}** 列",
        "",
        "## 凍結出場",
        "",
        f"- **{payload['exit_label']}** · cap **{payload['exit_cap_days']}** 交易日",
        "",
        "## imp_early_long 對照",
        "",
        f"- n={imp.get('n')} · 超額 **{imp.get('mean_excess_pct')}%** · "
        f"勝率 {imp.get('win_rate')}% · 持有 {imp.get('mean_hold_polls')} 幀",
        "",
        "## peak_signal_min × allow_mixed 彙總",
        "",
        "| peak_min | mixed | grid | 有事件 | max n | best n | best 超額% | best excess_n |",
        "|----------|-------|------|--------|-------|--------|-----------|-------------|",
    ]
    for row in payload.get("by_peak_signal_min") or []:
        lines.append(
            f"| {row.get('peak_signal_min')} | {row.get('allow_mixed_peak')} | "
            f"{row.get('grid_rows')} | {row.get('rows_with_events')} | {row.get('max_n')} | "
            f"{row.get('best_n')} | {row.get('best_mean_excess_pct')} | "
            f"{row.get('best_excess_n')} |"
        )

    lines.extend(
        [
            "",
            "## Top 15（依 n · 擴源優先）",
            "",
            "| peak | mixed | retrace | hq | W20 | n | 超額% | 中位超額% | 勝率 | 持有幀 |",
            "|------|-------|---------|-----|-----|---|-------|-----------|------|--------|",
        ]
    )
    for r in payload.get("top_by_n") or []:
        rs_lo, rs_hi = r.get("w20_rs_min"), r.get("w20_rs_max")
        band = f"{rs_lo}–{rs_hi}" if rs_lo is not None else "全"
        lines.append(
            f"| {r.get('peak_signal_min')} | {r.get('allow_mixed_peak')} | "
            f"{r.get('retrace_eps')} | {r.get('hook_quality_min')} | {band} | "
            f"{r.get('n')} | {r.get('mean_excess_pct')} | {r.get('median_excess_pct')} | "
            f"{r.get('win_rate')} | {r.get('mean_hold_polls')} |"
        )

    lines.extend(
        [
            "",
            "## Top 15（依平均超額）",
            "",
            "| peak | mixed | retrace | hq | W20 | n | 超額% | 中位超額% | 勝率 | 持有幀 |",
            "|------|-------|---------|-----|-----|---|-------|-----------|------|--------|",
        ]
    )
    for r in payload.get("top_by_excess") or []:
        rs_lo, rs_hi = r.get("w20_rs_min"), r.get("w20_rs_max")
        band = f"{rs_lo}–{rs_hi}" if rs_lo is not None else "全"
        lines.append(
            f"| {r.get('peak_signal_min')} | {r.get('allow_mixed_peak')} | "
            f"{r.get('retrace_eps')} | {r.get('hook_quality_min')} | {band} | "
            f"{r.get('n')} | {r.get('mean_excess_pct')} | {r.get('median_excess_pct')} | "
            f"{r.get('win_rate')} | {r.get('mean_hold_polls')} |"
        )

    champ = payload.get("champion_hook_row")
    if champ:
        lines.extend(
            [
                "",
                "## Champion 預設（peak=1.5 · mixed · retrace=0.10 · W20 96–100）",
                "",
                f"- n={champ.get('n')} · 超額 **{champ.get('mean_excess_pct')}%** · "
                f"持有 {champ.get('mean_hold_polls')} 幀",
            ]
        )

    best_n_row = (payload.get("top_by_n") or [{}])[0]
    best_ex_row = (payload.get("top_by_excess") or [{}])[0]
    imp_n = imp.get("n") or 0
    h1 = (best_n_row.get("n") or 0) >= 5
    h2 = (best_n_row.get("n") or 0) > imp_n
    h3 = (
        best_ex_row.get("mean_excess_pct") is not None
        and imp.get("mean_excess_pct") is not None
        and best_ex_row.get("mean_excess_pct") > imp.get("mean_excess_pct")
    )
    peak15_mixed = next(
        (
            x
            for x in payload.get("by_peak_signal_min") or []
            if x.get("peak_signal_min") == 1.5 and x.get("allow_mixed_peak") is True
        ),
        None,
    )
    h4 = peak15_mixed is not None and (peak15_mixed.get("max_n") or 0) > (
        next(
            (
                y.get("max_n")
                for y in payload.get("by_peak_signal_min") or []
                if y.get("peak_signal_min") == 2.0 and y.get("allow_mixed_peak") is False
            ),
            0,
        )
        or 0
    )

    lines.extend(
        [
            "",
            "## Phase 1b 假說",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
            f"| H1b-1 | 放寬 peak 後 max_n ≥ 5 | {'✅' if h1 else '❌'} |",
            f"| H1b-2 | hook max_n > imp_early_long (n={imp_n}) | {'✅' if h2 else '❌'} |",
            f"| H1b-3 | 最佳超額 > imp_early_long | {'✅' if h3 else '❌'} |",
            f"| H1b-4 | peak1.5+mixed max_n > peak2.0 strict | {'✅' if h4 else '❌' if h4 is False else '—'} |",
            "",
            "## 解讀備註",
            "",
            "- **peak_signal_min**：extension/mixed 超前局部極大門檻（1.0 / 1.5 / 2.0）。",
            "- **allow_mixed_peak**：允許 spread_class=mixed（dist 1.5–2.0 帶）作為 peak。",
            "- Champion 預設更新為 peak=1.5 · mixed=True · retrace=0.10。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_tactical_slot_md(payload: dict[str, Any]) -> str:
    hook = payload["hook_events"]["summary"]
    pf = payload["portfolios"]
    base = pf["c18acc_3slot_baseline"]
    part = pf["partitioned_2plus1"]
    shared = pf["shared_3slot_priority"]
    hyp = payload["hypotheses"]

    lines = [
        "# Dual WMA hook_buy · 戰術槽組合回測",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}** · "
        f"成本 round-trip **{ROUND_TRIP_COST_PCT}%**（事件腿）",
        "",
        "## P0 · hook_buy 事件腿",
        "",
        f"- 事件數 n={hook.get('n')} · 超額 mean **{hook.get('mean_excess_pct')}%** · "
        f"勝率 {hook.get('win_rate')}% · 持有 {hook.get('mean_hold_polls')} 幀",
        f"- hook_quality_min={hook.get('hook_quality_min')} · "
        f"exit cap {hook.get('exit_cap_days')}d · W20→Leading",
        f"- 與 C18acc 進場重疊 {payload['overlap_with_c18acc']} / "
        f"{hook.get('n') or 0}（{payload['overlap_pct']}%）",
        "",
        "## 組合層級（50k NTD · 日內 M2M）",
        "",
        "| 模式 | 槽位 | CAGR% | 總報酬% | Sharpe | MaxDD% | n |",
        "|------|------|-------|---------|--------|--------|---|",
    ]

    def _row(label: str, slots: str, m: dict[str, Any]) -> None:
        lines.append(
            f"| {label} | {slots} | {m.get('cagr_pct', '—')} | "
            f"{m.get('total_return_pct', '—')} | {m.get('sharpe_ratio', '—')} | "
            f"{m.get('max_drawdown_pct', '—')} | {m.get('n_trades', m.get('n_periods', '—'))} |"
        )

    _row("C18acc baseline", "3", base)
    _row("Hook only", "1（1/3 資金）", pf["hook_1slot_third_capital"])
    _row("Partitioned 2+1", "2+1", part)
    _row("Shared priority", "3 共用", shared)

    lines.extend(
        [
            "",
            "### Partitioned 子池",
            "",
        ]
    )
    for pool in part.get("pools") or []:
        lines.append(
            f"- **{pool.get('pool')}** · {pool.get('n_slots')}槽 · "
            f"capital {pool.get('capital', 0):,.0f} · "
            f"CAGR {pool.get('cagr_pct')}% · MaxDD {pool.get('max_drawdown_pct')}%"
        )
    if part.get("daily_return_corr") is not None:
        lines.append(f"- 日報酬相關 ρ = **{part['daily_return_corr']}**")

    lines.extend(
        [
            "",
            "## 假說檢定（H-C）",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
            f"| H-C1 | 戰術槽提升 CAGR | {'✅' if hyp.get('H-C1_cagr_up') else '❌' if hyp.get('H-C1_cagr_up') is False else '—'} |",
            f"| H-C2 | 日報酬 ρ < 0.3 | {'✅' if hyp.get('H-C2_low_corr') else '❌' if hyp.get('H-C2_low_corr') is False else '—'} |",
            f"| H-C4 | MaxDD 增幅 < 20% | {'✅' if hyp.get('H-C4_maxdd_bounded') else '❌' if hyp.get('H-C4_maxdd_bounded') is False else '—'} |",
            "",
            "## 解讀備註",
            "",
            "- **Partitioned 2+1**：C18acc 2 槽（2/3 資金）+ hook_buy 1 槽（1/3 資金）獨立模擬後合併 NAV。",
            "- **Shared priority**：3 槽共用 · C18acc `_priority=1` · hook `_priority=2` · 僅高優先可搶槽。",
            f"- hook 事件腿 n={hyp.get('hook_n')} · 樣本小時僅供方向。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_imp_early_long_tactical_slot_md(payload: dict[str, Any]) -> str:
    tac = payload["tactical_events"]["summary"]
    pf = payload["portfolios"]
    base = pf["c18acc_3slot_baseline"]
    part = pf["partitioned_2plus1"]
    shared = pf["shared_3slot_priority"]
    hyp = payload["hypotheses"]
    grad = payload.get("graduation") or {}

    lines = [
        "# Dual WMA imp_early_long · partitioned slot vs C18acc",
        "",
        f"期間：**{payload['date_from']} → {payload['date_to']}** · "
        f"成本 round-trip **{ROUND_TRIP_COST_PCT}%**（事件腿）",
        "",
        "## 凍結規格",
        "",
        f"- 進場：small_ext={tac.get('small_extension_max')} · aligned={tac.get('aligned_max')} · "
        f"both_vec={tac.get('require_both_vectors')} · W20 RS {tac.get('w20_rs_min')}–{tac.get('w20_rs_max')}",
        f"- 出場：{tac.get('exit_rule')} · Regime：`{tac.get('regime_filter')}`",
        "",
        "## 事件腿",
        "",
        f"- n={tac.get('n')} · 超額 mean **{tac.get('mean_excess_pct')}%** · "
        f"中位超額 {tac.get('median_excess_pct')}% · 勝率 {tac.get('win_rate')}% · "
        f"超額勝率 {tac.get('excess_win_rate')}%",
        f"- 與 C18acc 進場重疊 {payload['overlap_with_c18acc']} / "
        f"{tac.get('n') or 0}（{payload['overlap_pct']}%）",
        "",
        "## 組合層級（50k NTD · 日內 M2M）",
        "",
        "| 模式 | 槽位 | CAGR% | 總報酬% | Sharpe | MaxDD% |",
        "|------|------|-------|---------|--------|--------|",
    ]

    def _row(label: str, slots: str, m: dict[str, Any]) -> None:
        lines.append(
            f"| {label} | {slots} | {m.get('cagr_pct', '—')} | "
            f"{m.get('total_return_pct', '—')} | {m.get('sharpe_ratio', '—')} | "
            f"{m.get('max_drawdown_pct', '—')} |"
        )

    _row("C18acc baseline", "3", base)
    _row("imp_early_long only", "1（1/3 資金）", pf["imp_early_long_1slot_third_capital"])
    _row("Partitioned 2+1", "2+1", part)
    _row("Shared priority", "3 共用", shared)

    lines.extend(["", "### Partitioned 子池", ""])
    for pool in part.get("pools") or []:
        lines.append(
            f"- **{pool.get('pool')}** · {pool.get('n_slots')}槽 · "
            f"capital {pool.get('capital', 0):,.0f} · "
            f"CAGR {pool.get('cagr_pct')}% · MaxDD {pool.get('max_drawdown_pct')}%"
        )
    if part.get("daily_return_corr") is not None:
        lines.append(f"- 日報酬相關 ρ = **{part['daily_return_corr']}**")

    lines.extend(
        [
            "",
            "## G3 組合門檻",
            "",
            f"- **結果**：{'**PASS**' if grad.get('g3_combo_pass') else '**FAIL**'}",
            f"- 戰術腿 n≥{grad.get('min_tactical_events')} · mean 超額 > 0 · "
            f"CAGR 提升或低相關正超額",
        ]
    )
    for note in grad.get("notes") or []:
        lines.append(f"- ⚠ {note}")

    lines.extend(
        [
            "",
            "## 假說檢定（H-C）",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
            f"| H-C1 | 戰術槽提升 CAGR | {'✅' if hyp.get('H-C1_cagr_up') else '❌' if hyp.get('H-C1_cagr_up') is False else '—'} |",
            f"| H-C2 | 日報酬 ρ < 0.3 | {'✅' if hyp.get('H-C2_low_corr') else '❌' if hyp.get('H-C2_low_corr') is False else '—'} |",
            f"| H-C4 | MaxDD 增幅 < 20% | {'✅' if hyp.get('H-C4_maxdd_bounded') else '❌' if hyp.get('H-C4_maxdd_bounded') is False else '—'} |",
        ]
    )
    return "\n".join(lines) + "\n"


def render_graduation_verdict_md(
    *,
    oos_payload: dict[str, Any],
    combo_payload: dict[str, Any],
    verdict: dict[str, Any],
) -> str:
    g2 = oos_payload.get("g2_oos_holdout") or {}
    g3 = combo_payload.get("graduation") or {}
    lines = [
        "# imp_early_long · G2 OOS + G3 Combo · 畢業裁決",
        "",
        f"組合回測期間：**{combo_payload.get('date_from')} → {combo_payload.get('date_to')}**",
        "",
        "## 裁決",
        "",
        f"- **Verdict**：`{verdict.get('verdict')}`",
        f"- **行動**：{verdict.get('action')}",
        f"- G2 OOS holdout：{'PASS' if verdict.get('g2_pass') else 'FAIL'}",
        f"- G3 partitioned combo：{'PASS' if verdict.get('g3_pass') else 'FAIL'}",
        "",
        "## G2 · OOS holdout",
        "",
        f"- 門檻：各 OOS 視窗 n≥{g2.get('min_events')} 且 mean 超額 > 0",
        f"- 結果：{'**PASS**' if g2.get('passed') else '**FAIL**'}",
    ]
    for note in g2.get("notes") or []:
        lines.append(f"- ⚠ {note}")

    is_f = oos_payload.get("is") or {}
    lines.append(
        f"- IS（tape=多頭）：n={is_f.get('n')} · 超額 **{is_f.get('mean_excess_pct')}%**"
    )
    lines.extend(["", "| 視窗 | n | 超額% |", "|------|---|-------|"])
    for r in oos_payload.get("oos") or []:
        lines.append(
            f"| {r.get('window')} | {r.get('n')} | {r.get('mean_excess_pct')} |"
        )

    tac = combo_payload["tactical_events"]["summary"]
    part = combo_payload["portfolios"]["partitioned_2plus1"]
    base = combo_payload["portfolios"]["c18acc_3slot_baseline"]
    lines.extend(
        [
            "",
            "## G3 · partitioned 2+1",
            "",
            f"- imp_early_long：n={tac.get('n')} · 超額 **{tac.get('mean_excess_pct')}%**",
            f"- 重疊 C18acc：{combo_payload.get('overlap_pct')}%",
            f"- C18acc 3槽 CAGR {base.get('cagr_pct')}% → Partitioned {part.get('cagr_pct')}%",
            f"- ρ = {part.get('daily_return_corr')}",
            f"- G3：{'**PASS**' if g3.get('g3_combo_pass') else '**FAIL**'}",
        ]
    )
    for note in g3.get("notes") or []:
        lines.append(f"- ⚠ {note}")

    return "\n".join(lines) + "\n"
