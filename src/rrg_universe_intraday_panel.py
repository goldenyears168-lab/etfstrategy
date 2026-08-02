"""RRG Universe 盤中軌跡預算 · 日線歷史 + 當日 1m K 覆寫 close（full_rrg 模式）。"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from price_panels import load_price_panels
from research.backtest.rrg_lens_score_swap import _rebalance_minutes
from rrg_rotation import classify_quadrant, compute_rrg_panel
from rrg_universe_snapshot import _intraday_benchmark_price, kbar_close_at_minute
from stock_db import load_etf_constituent_watchlist
from stock_db.kbar import kbar_5m_close_at_minute, load_kbar_day_5m_closes

DEFAULT_POLL_INTERVAL_MIN = 30
DEFAULT_POLL_START = "09:00"
DEFAULT_POLL_END = "13:30"
DEFAULT_LENGTH = 5
WMA_SHORT_LENGTH = 5
WMA_LONG_LENGTH = 20
WMA_MICRO_LENGTH = 3  # optional micro leg · triple pullback research
WMA_ULTRA_LENGTH = 2  # ultra-short trigger leg · W2 vs W3 compare
SPREAD_ALIGNED_MAX = 1.5
SPREAD_SIGNAL_MIN = 2.0
SMALL_EXTENSION_MAX = 3.0
DEEP_PULLBACK_MIN = 3.0
DEFAULT_HOOK_RETRACE_EPS = 0.10
DEFAULT_HOOK_PEAK_SIGNAL_MIN = 1.0
DEFAULT_HOOK_POOL_MIN_PEAK = 0.5
DEFAULT_HOOK_POOL_RETRACE_EPS = 0.05
LOOKBACK_TRADING_DAYS = 80

DUAL_WMA_TRADE_SIGNALS: dict[str, dict[str, str]] = {
    "imp_extension": {
        "label_zh": "轉強超前",
        "detail": "W20 Improving · W5 Leading/右上 · Extension",
        "action": "觀察 / 小倉試單",
    },
    "imp_early_long": {
        "label_zh": "輪動早期",
        "detail": "W20 Improving · W5 Improving · Aligned/小Extension",
        "action": "候選做多",
    },
    "lead_pullback_buy": {
        "label_zh": "強勢回踩",
        "detail": "W20 Leading · W5 Improving/左下 · Pullback",
        "action": "最常用買點",
    },
    "lead_deep_pullback_wait": {
        "label_zh": "急跌等轉",
        "detail": "W20 Leading · W5 Lagging · 大Pullback",
        "action": "等 W5 回 Improving",
    },
    "hook_buy": {
        "label_zh": "反曲線買",
        "detail": "Extension peak → Aligned 收斂 · W20/W5 Improving",
        "action": "戰術做多",
    },
    "hook_pool": {
        "label_zh": "反曲線候選",
        "detail": "盤中 spread 弧線回撤（寬網 · 待篩選）",
        "action": "觀察池",
    },
}


def _ensure_session_row(
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
) -> tuple[pd.DataFrame, pd.Series]:
    """盤中即時：日線 bar 收盤後才進，故 session 尚不在 panel。

    以最近一筆收盤 forward-fill 出 session 佔位列（stock + benchmark），
    供 `_patch_intraday_close` 用當日 1m K 覆寫。無此佔位列時盤中池永遠為空。
    """
    dates = close.index.astype(str).tolist()
    if trade_date in dates or not dates:
        return close, bench
    close = close.copy()
    close.loc[trade_date] = close.iloc[-1]
    bench = bench.copy()
    last_b = float(bench.iloc[-1]) if len(bench) else float("nan")
    bench.loc[trade_date] = last_b
    return close, bench


def _slice_panels_for_date(
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
    *,
    lookback_days: int = LOOKBACK_TRADING_DAYS,
) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    full_dates = close.index.astype(str).tolist()
    if trade_date not in full_dates:
        return close.iloc[0:0], bench.iloc[0:0], []
    si = full_dates.index(trade_date)
    start_i = max(0, si - lookback_days + 1)
    idx = close.index[start_i : si + 1]
    prov = close.loc[idx].copy()
    bench_p = bench.reindex(prov.index).astype(float)
    return prov, bench_p, prov.index.astype(str).tolist()


def intraday_poll_minutes(
    *,
    interval_min: int = DEFAULT_POLL_INTERVAL_MIN,
    start: str = DEFAULT_POLL_START,
    end: str = DEFAULT_POLL_END,
) -> list[str]:
    return _rebalance_minutes(
        interval_min=interval_min,
        no_swap_before=start,
        last_minute=end,
    )


def build_intraday_frames(
    dates: list[str],
    *,
    poll_minutes: list[str] | None = None,
) -> list[dict[str, str]]:
    polls = poll_minutes or intraday_poll_minutes()
    frames: list[dict[str, str]] = []
    for d in dates:
        for minute in polls:
            frames.append(
                {
                    "id": f"{d}T{minute}",
                    "date": d,
                    "minute": minute,
                    "label": f"{d[5:]} {minute}",
                }
            )
    return frames


def _load_day_5m_bars(
    conn: sqlite3.Connection,
    trade_date: str,
    universe_ids: list[str],
    *,
    bar_minutes: int = 5,
) -> dict[str, tuple[tuple[str, float], ...]]:
    """Prefetch aggregated 5m closes for one session · one DB read per stock."""
    out: dict[str, tuple[tuple[str, float], ...]] = {}
    for sid in universe_ids:
        bars = load_kbar_day_5m_closes(
            conn, sid, trade_date, bar_minutes=bar_minutes
        )
        if bars:
            out[sid] = bars
    return out


def _patch_intraday_close(
    prov: pd.DataFrame,
    bench: pd.Series,
    conn: sqlite3.Connection,
    trade_date: str,
    minute: str,
    universe_ids: list[str],
    *,
    day_5m_bars: dict[str, tuple[tuple[str, float], ...]] | None = None,
    kbar_bar_minutes: int | None = None,
) -> set[str]:
    """覆寫當日 close；回傳有 K 線的 stock_id 集合。"""
    priced: set[str] = set()
    if trade_date not in prov.index:
        return priced
    use_5m = kbar_bar_minutes == 5 and day_5m_bars is not None
    for sid in universe_ids:
        if sid not in prov.columns:
            continue
        if use_5m:
            px = kbar_5m_close_at_minute(
                conn,
                sid,
                trade_date,
                minute,
                day_bars=day_5m_bars.get(sid),
            )
        else:
            px = kbar_close_at_minute(conn, sid, trade_date, minute)
        if px is not None and px > 0:
            prov.at[trade_date, sid] = float(px)
            priced.add(sid)
    bench_px = _intraday_benchmark_price(
        conn, trade_date, minute, price_mode="kbar"
    )
    if bench_px is not None and bench_px > 0 and trade_date in bench.index:
        bench.at[trade_date] = float(bench_px)
    return priced


def spread_class_snapshots_at_minute(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
    minute: str,
    stock_ids: list[str],
    short_length: int = WMA_SHORT_LENGTH,
    long_length: int = WMA_LONG_LENGTH,
    lookback_bars: int | None = None,
) -> dict[str, str]:
    """Per-stock WMA spread_class at one intraday poll (daily history + kbar patch).

    Only computes RRG for ``stock_ids``. Optional ``lookback_bars`` trims history;
    default uses ``LOOKBACK_TRADING_DAYS`` (≥ 3×WMA_LONG so terminal class matches
    full-history rolling WMA).
    """
    ids = [sid for sid in stock_ids if sid in close.columns]
    if not ids or trade_date not in close.index.astype(str):
        return {}
    mask = close.index.astype(str) <= trade_date
    prov = close.loc[mask, ids].copy()
    lb = LOOKBACK_TRADING_DAYS if lookback_bars is None else lookback_bars
    if lb > 0 and len(prov.index) > lb:
        # Keep session row: trim older history only.
        if trade_date in prov.index.astype(str):
            hist = prov.loc[prov.index.astype(str) < trade_date]
            session_row = prov.loc[[trade_date]]
            if len(hist.index) > lb - 1:
                hist = hist.iloc[-(lb - 1) :]
            prov = pd.concat([hist, session_row])
        else:
            prov = prov.iloc[-lb:]
    bench_p = bench.reindex(prov.index).astype(float)
    if trade_date not in prov.index:
        return {}
    priced = _patch_intraday_close(prov, bench_p, conn, trade_date, minute, ids)
    rs5, mom5, _ = compute_rrg_panel(prov, bench_p, length=short_length)
    rs20, mom20, _ = compute_rrg_panel(prov, bench_p, length=long_length)
    if trade_date not in rs5.index or trade_date not in rs20.index:
        return {}
    out: dict[str, str] = {}
    for sid in priced:
        if sid not in rs5.columns or sid not in rs20.columns:
            continue
        rv5 = rs5.at[trade_date, sid]
        mv5 = mom5.at[trade_date, sid]
        rv20 = rs20.at[trade_date, sid]
        mv20 = mom20.at[trade_date, sid]
        if any(v != v for v in (rv5, mv5, rv20, mv20)):
            continue
        sc, _, _, _ = classify_wma_spread(rv5, mv5, rv20, mv20)
        out[sid] = sc
    return out


def _densify_intraday_points(
    sparse: list[dict[str, Any]],
    frames: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Forward-fill after first 1m K so every poll slot has a display point."""
    if not sparse:
        return []
    fresh_by_id = {p["frame_id"]: p for p in sparse}
    out: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    started = False
    for f in frames:
        fid = f["id"]
        if fid in fresh_by_id:
            last = {k: v for k, v in fresh_by_id[fid].items() if k != "carried"}
            out.append(last)
            started = True
        elif started and last is not None:
            out.append(
                {
                    **last,
                    "frame_id": fid,
                    "date": f["date"],
                    "minute": f["minute"],
                    "carried": True,
                }
            )
    return out


def _rrg_leg(rv: float, mv: float) -> dict[str, Any]:
    return {
        "rs_ratio": round(rv, 2),
        "rs_momentum": round(mv, 2),
        "quadrant": classify_quadrant(rv, mv),
    }


def classify_wma_spread(
    w5_rs: float,
    w5_mom: float,
    w20_rs: float,
    w20_mom: float,
    *,
    aligned_max: float = SPREAD_ALIGNED_MAX,
    signal_min: float = SPREAD_SIGNAL_MIN,
) -> tuple[str, float, float, float]:
    """W5 vs W20 spread bucket: pullback / aligned / extension / mixed."""
    spread_rs = w5_rs - w20_rs
    spread_mom = w5_mom - w20_mom
    spread_dist = math.hypot(spread_rs, spread_mom)
    if spread_dist <= aligned_max:
        cls = "aligned"
    elif spread_rs < 0 and spread_mom < 0 and spread_dist >= signal_min:
        cls = "pullback"
    elif spread_rs > 0 and spread_mom > 0 and spread_dist >= signal_min:
        cls = "extension"
    else:
        cls = "mixed"
    return cls, round(spread_dist, 2), round(spread_rs, 2), round(spread_mom, 2)


def _curvature_second_diff(d_prev: float, d_curr: float, d_next: float) -> float:
    return d_prev - 2.0 * d_curr + d_next


def is_hook_peak_frame(
    point: dict[str, Any],
    *,
    peak_signal_min: float = SPREAD_SIGNAL_MIN,
    allow_mixed_peak: bool = False,
) -> bool:
    """盤中 spread 局部極大候選 · extension 或（可選）mixed 超前。"""
    dist = float(point.get("spread_dist") or 0)
    if dist < peak_signal_min:
        return False
    sc = str(point.get("spread_class") or "")
    srs = float(point.get("spread_rs") or 0)
    sms = float(point.get("spread_mom") or 0)
    if not (srs > 0 and sms > 0):
        return False
    if sc == "extension":
        return True
    if allow_mixed_peak and sc == "mixed":
        return True
    return False


def annotate_hook_features(
    points: list[dict[str, Any]],
    *,
    retrace_eps: float = DEFAULT_HOOK_RETRACE_EPS,
    peak_signal_min: float = DEFAULT_HOOK_PEAK_SIGNAL_MIN,
    allow_mixed_peak: bool = True,
    pool_min_peak: float = DEFAULT_HOOK_POOL_MIN_PEAK,
    pool_retrace_eps: float = DEFAULT_HOOK_POOL_RETRACE_EPS,
) -> None:
    """P0 · 寬網 hook_candidate + 嚴格 hook_complete（peak→aligned）。"""
    hook_keys = (
        "hook_candidate",
        "hook_pool_peak_dist",
        "hook_complete",
        "hook_peak_dist",
        "hook_peak_idx",
        "hook_retrace_pct",
        "hook_curvature",
        "hook_quality",
        "hook_extension_peak",
    )
    by_date: dict[str, list[int]] = {}
    for i, p in enumerate(points):
        for k in hook_keys:
            p.pop(k, None)
        by_date.setdefault(str(p["date"]), []).append(i)

    for indices in by_date.values():
        session_peak = 0.0
        for gi in indices:
            p = points[gi]
            if not p.get("fresh"):
                continue
            dist = float(p.get("spread_dist") or 0)
            if dist > session_peak:
                session_peak = dist
            if session_peak < pool_min_peak:
                continue
            retrace = (session_peak - dist) / session_peak if session_peak > 0 else 0.0
            if retrace >= pool_retrace_eps and dist < session_peak:
                p["hook_candidate"] = True
                p["hook_pool_peak_dist"] = round(session_peak, 2)
                p["hook_retrace_pct"] = round(retrace, 4)

        peak_local: int | None = None
        peak_dist = 0.0
        for li, gi in enumerate(indices):
            p = points[gi]
            dist = float(p.get("spread_dist") or 0)
            if is_hook_peak_frame(
                p,
                peak_signal_min=peak_signal_min,
                allow_mixed_peak=allow_mixed_peak,
            ):
                if peak_local is None or dist > peak_dist:
                    if peak_local is not None:
                        points[indices[peak_local]].pop("hook_extension_peak", None)
                    peak_local = li
                    peak_dist = dist
                    p["hook_extension_peak"] = True
            elif p.get("hook_extension_peak"):
                p.pop("hook_extension_peak", None)

        if peak_local is None or peak_dist <= 0:
            continue

        peak_gi = indices[peak_local]
        kappa: float | None = None
        if 0 < peak_local < len(indices) - 1:
            d0 = float(points[indices[peak_local - 1]].get("spread_dist") or 0)
            d1 = peak_dist
            d2 = float(points[indices[peak_local + 1]].get("spread_dist") or 0)
            kappa = _curvature_second_diff(d0, d1, d2)
            points[peak_gi]["hook_curvature"] = round(kappa, 4)

        for li in range(peak_local + 1, len(indices)):
            gi = indices[li]
            p = points[gi]
            if not p.get("fresh"):
                continue
            dist = float(p.get("spread_dist") or 0)
            sc = str(p.get("spread_class") or "")
            retrace = (peak_dist - dist) / peak_dist if peak_dist > 0 else 0.0
            srs = float(p.get("spread_rs") or 0)
            if sc == "aligned" and retrace >= retrace_eps and srs >= 0:
                p["hook_complete"] = True
                p["hook_peak_dist"] = round(peak_dist, 2)
                p["hook_peak_idx"] = peak_local
                p["hook_retrace_pct"] = round(retrace, 4)
                if kappa is not None:
                    p["hook_quality"] = round(max(0.0, -kappa * (peak_dist - dist)), 4)
                break


def classify_dual_wma_trade_signal(
    point: dict[str, Any],
    *,
    small_extension_max: float = SMALL_EXTENSION_MAX,
    deep_pullback_min: float = DEEP_PULLBACK_MIN,
) -> str | None:
    """四種可交易情境；其餘回傳 None（隱藏）。"""
    if not point.get("fresh"):
        return None
    w20 = point.get("w20") or {}
    w5 = point.get("w5") or {}
    w20q = w20.get("quadrant")
    w5q = w5.get("quadrant")
    sc = point.get("spread_class")
    dist = float(point.get("spread_dist") or 0)
    srs = float(point.get("spread_rs") or 0)
    sms = float(point.get("spread_mom") or 0)

    if w20q == "improving" and sc == "extension":
        if w5q == "leading" or (srs > 0 and sms > 0):
            return "imp_extension"

    if (
        point.get("hook_complete")
        and w20q == "improving"
        and w5q == "improving"
        and srs >= 0
        and sms >= 0
    ):
        return "hook_buy"

    if (
        point.get("hook_candidate")
        and w20q == "improving"
        and w5q == "improving"
        and srs >= 0
        and sms >= 0
    ):
        return "hook_pool"

    if w20q == "improving" and w5q == "improving":
        small_ext = sc == "extension" and dist < small_extension_max
        if sc == "aligned" or small_ext:
            if srs >= 0 or sms >= 0:
                return "imp_early_long"

    if w20q == "leading" and sc == "pullback":
        if w5q == "improving" or (srs < 0 and sms < 0):
            if w5q != "lagging" or dist < deep_pullback_min:
                return "lead_pullback_buy"

    if (
        w20q == "leading"
        and w5q == "lagging"
        and sc == "pullback"
        and dist >= deep_pullback_min
    ):
        return "lead_deep_pullback_wait"

    return None


def _annotate_trade_signals(points: list[dict[str, Any]]) -> None:
    for p in points:
        sig = classify_dual_wma_trade_signal(p)
        if sig:
            p["trade_signal"] = sig
        else:
            p.pop("trade_signal", None)


def _dual_point(
    *,
    frame_id: str,
    trade_date: str,
    minute: str,
    w5_rs: float,
    w5_mom: float,
    w20_rs: float,
    w20_mom: float,
) -> dict[str, Any]:
    spread_class, spread_dist, spread_rs, spread_mom = classify_wma_spread(
        w5_rs, w5_mom, w20_rs, w20_mom
    )
    return {
        "frame_id": frame_id,
        "date": trade_date,
        "minute": minute,
        "fresh": True,
        "w5": _rrg_leg(w5_rs, w5_mom),
        "w20": _rrg_leg(w20_rs, w20_mom),
        "spread_class": spread_class,
        "spread_dist": spread_dist,
        "spread_rs": spread_rs,
        "spread_mom": spread_mom,
    }


def build_intraday_universe_trajectories(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    length: int = DEFAULT_LENGTH,
    poll_minutes: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    """回傳 (frames, trajectories, meta)。"""
    if len(dates) < 1:
        raise ValueError("至少需要 1 個交易日")
    polls = poll_minutes or intraday_poll_minutes()
    frames = build_intraday_frames(dates, poll_minutes=polls)

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}
    hold_by_id = {w["stock_id"]: w.get("etf_hold_count", 0) for w in watch}
    universe_ids = [w["stock_id"] for w in watch]

    close, _, _ = load_price_panels(conn)
    universe_cols = [sid for sid in universe_ids if sid in close.columns]
    close = close[universe_cols]
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)

    point_buckets: dict[str, list[dict[str, Any]]] = {sid: [] for sid in universe_ids}
    frame_priced: list[int] = []
    n_frames_done = 0
    n_frames_total = len(dates) * len(polls)

    for trade_date in dates:
        if trade_date not in close.index:
            continue
        prov_base, bench_base, slice_dates = _slice_panels_for_date(
            close, bench, trade_date
        )
        if prov_base.empty or trade_date not in slice_dates:
            continue
        for minute in polls:
            prov = prov_base.copy()
            bench_p = bench_base.copy()
            priced = _patch_intraday_close(
                prov, bench_p, conn, trade_date, minute, universe_ids
            )
            frame_priced.append(len(priced))
            n_frames_done += 1
            if not priced:
                continue
            rs_ratio, rs_mom, _ = compute_rrg_panel(prov, bench_p, length=length)
            if trade_date not in rs_ratio.index:
                continue
            frame_id = f"{trade_date}T{minute}"
            for sid in priced:
                if sid not in rs_ratio.columns:
                    continue
                rv = rs_ratio.at[trade_date, sid]
                mv = rs_mom.at[trade_date, sid]
                if rv != rv or mv != mv:
                    continue
                rv_f, mv_f = float(rv), float(mv)
                point_buckets[sid].append(
                    {
                        "frame_id": frame_id,
                        "date": trade_date,
                        "minute": minute,
                        "rs_ratio": round(rv_f, 2),
                        "rs_momentum": round(mv_f, 2),
                        "quadrant": classify_quadrant(rv_f, mv_f),
                    }
                )
            if n_frames_done % 45 == 0:
                print(
                    f"  intraday RRG: {n_frames_done}/{n_frames_total} frames "
                    f"({trade_date} {minute})",
                    flush=True,
                )

    trajectories: list[dict[str, Any]] = []
    for sid in universe_ids:
        pts = _densify_intraday_points(point_buckets[sid], frames)
        if len(pts) < 1:
            continue
        trajectories.append(
            {
                "stock_id": sid,
                "stock_name": name_by_id.get(sid, ""),
                "etf_hold_count": hold_by_id.get(sid, 0),
                "points": pts,
                "start_quadrant": pts[0]["quadrant"],
                "end_quadrant": pts[-1]["quadrant"],
                "displacement": round(
                    math.hypot(
                        pts[-1]["rs_ratio"] - pts[0]["rs_ratio"],
                        pts[-1]["rs_momentum"] - pts[0]["rs_momentum"],
                    ),
                    2,
                ),
            }
        )
    trajectories.sort(key=lambda t: (-t["displacement"], t["stock_id"]))

    meta = {
        "n_frames": len(frames),
        "n_polls_per_day": len(polls),
        "n_trajectories": len(trajectories),
        "avg_priced_per_frame": (
            round(sum(frame_priced) / len(frame_priced), 1) if frame_priced else 0.0
        ),
        "min_priced_per_frame": min(frame_priced) if frame_priced else 0,
        "max_priced_per_frame": max(frame_priced) if frame_priced else 0,
    }
    return frames, trajectories, meta


def _micro_leg_key(length: int) -> str:
    return f"w{length}"


def build_dual_wma_intraday_trajectories(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    short_length: int = WMA_SHORT_LENGTH,
    long_length: int = WMA_LONG_LENGTH,
    poll_minutes: list[str] | None = None,
    micro_length: int | None = None,
    micro_lengths: tuple[int, ...] | None = None,
    stock_ids: list[str] | None = None,
    kbar_bar_minutes: int | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    """雙 WMA 盤中軌跡 · 每點含 w5 / w20 / spread_class。

    micro_length / micro_lengths: 額外短線腿 w{L}（例 w2、w3）· 不影響 trade_signal。
    stock_ids: 若提供，僅建構這些標的（研究回溯用）。
    kbar_bar_minutes: 5 → 日內定價用聚合 5m K（批次讀取 · 較快）。
    """
    if len(dates) < 1:
        raise ValueError("至少需要 1 個交易日")
    extra_micro = tuple(micro_lengths or ())
    if micro_length is not None and micro_length not in extra_micro:
        extra_micro = (*extra_micro, micro_length)
    polls = poll_minutes or intraday_poll_minutes()
    frames = build_intraday_frames(dates, poll_minutes=polls)

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}
    hold_by_id = {w["stock_id"]: w.get("etf_hold_count", 0) for w in watch}
    universe_ids = [w["stock_id"] for w in watch]
    if stock_ids is not None:
        want = set(stock_ids)
        universe_ids = [sid for sid in universe_ids if sid in want]

    close, _, _ = load_price_panels(conn)
    universe_cols = [sid for sid in universe_ids if sid in close.columns]
    close = close[universe_cols]
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)

    point_buckets: dict[str, list[dict[str, Any]]] = {sid: [] for sid in universe_ids}
    frame_priced: list[int] = []
    n_frames_done = 0
    n_frames_total = len(dates) * len(polls)

    for trade_date in dates:
        if trade_date not in close.index:
            continue
        prov_base, bench_base, slice_dates = _slice_panels_for_date(
            close, bench, trade_date
        )
        if prov_base.empty or trade_date not in slice_dates:
            continue
        day_5m_bars = (
            _load_day_5m_bars(
                conn,
                trade_date,
                universe_ids,
                bar_minutes=int(kbar_bar_minutes),
            )
            if kbar_bar_minutes == 5
            else None
        )
        for minute in polls:
            prov = prov_base.copy()
            bench_p = bench_base.copy()
            priced = _patch_intraday_close(
                prov,
                bench_p,
                conn,
                trade_date,
                minute,
                universe_ids,
                day_5m_bars=day_5m_bars,
                kbar_bar_minutes=kbar_bar_minutes,
            )
            frame_priced.append(len(priced))
            n_frames_done += 1
            if not priced:
                continue
            rs5, mom5, _ = compute_rrg_panel(prov, bench_p, length=short_length)
            rs20, mom20, _ = compute_rrg_panel(prov, bench_p, length=long_length)
            micro_panels: dict[int, tuple[Any, Any]] = {}
            for ml in extra_micro:
                rs_m, mom_m, _ = compute_rrg_panel(prov, bench_p, length=ml)
                micro_panels[ml] = (rs_m, mom_m)
            if trade_date not in rs5.index or trade_date not in rs20.index:
                continue
            frame_id = f"{trade_date}T{minute}"
            for sid in priced:
                if sid not in rs5.columns or sid not in rs20.columns:
                    continue
                rv5 = rs5.at[trade_date, sid]
                mv5 = mom5.at[trade_date, sid]
                rv20 = rs20.at[trade_date, sid]
                mv20 = mom20.at[trade_date, sid]
                if any(v != v for v in (rv5, mv5, rv20, mv20)):
                    continue
                pt = _dual_point(
                    frame_id=frame_id,
                    trade_date=trade_date,
                    minute=minute,
                    w5_rs=float(rv5),
                    w5_mom=float(mv5),
                    w20_rs=float(rv20),
                    w20_mom=float(mv20),
                )
                for ml, (rs_m, mom_m) in micro_panels.items():
                    key = _micro_leg_key(ml)
                    if sid in rs_m.columns and trade_date in rs_m.index:
                        rv_m = rs_m.at[trade_date, sid]
                        mv_m = mom_m.at[trade_date, sid]
                        if rv_m == rv_m and mv_m == mv_m:
                            pt[key] = _rrg_leg(float(rv_m), float(mv_m))
                point_buckets[sid].append(pt)
            if n_frames_done % 45 == 0:
                print(
                    f"  dual WMA intraday: {n_frames_done}/{n_frames_total} frames "
                    f"({trade_date} {minute})",
                    flush=True,
                )

    trajectories: list[dict[str, Any]] = []
    for sid in universe_ids:
        pts = _densify_intraday_points(point_buckets[sid], frames)
        if len(pts) < 1:
            continue
        for p in pts:
            if p.get("carried"):
                p["fresh"] = False
            elif "fresh" not in p:
                p["fresh"] = True
        annotate_hook_features(pts)
        _annotate_trade_signals(pts)
        trajectories.append(
            {
                "stock_id": sid,
                "stock_name": name_by_id.get(sid, ""),
                "etf_hold_count": hold_by_id.get(sid, 0),
                "points": pts,
                "start_quadrant": pts[0]["w20"]["quadrant"],
                "end_quadrant": pts[-1]["w20"]["quadrant"],
                "displacement": round(
                    math.hypot(
                        pts[-1]["w20"]["rs_ratio"] - pts[0]["w20"]["rs_ratio"],
                        pts[-1]["w20"]["rs_momentum"] - pts[0]["w20"]["rs_momentum"],
                    ),
                    2,
                ),
            }
        )
    trajectories.sort(key=lambda t: (-t["displacement"], t["stock_id"]))

    meta = {
        "n_frames": len(frames),
        "n_polls_per_day": len(polls),
        "n_trajectories": len(trajectories),
        "short_length": short_length,
        "long_length": long_length,
        "micro_length": micro_length,
        "micro_lengths": list(extra_micro),
        "avg_priced_per_frame": (
            round(sum(frame_priced) / len(frame_priced), 1) if frame_priced else 0.0
        ),
        "min_priced_per_frame": min(frame_priced) if frame_priced else 0,
        "max_priced_per_frame": max(frame_priced) if frame_priced else 0,
        "kbar_bar_minutes": kbar_bar_minutes,
    }
    return frames, trajectories, meta


def build_dual_wma_lead_pullback_observation_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    poll_minute: str,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[Any], str]:
    """單 poll 快掃 · 預設 lead_pullback_buy（fresh K · W20 Leading · pullback）。

    供 buy-signal-radar 觀測池使用；不建全期 trajectory。
    """
    from rrg_mono_daily_brief import ScanRow

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}
    universe_ids = [w["stock_id"] for w in watch]

    close, _, _ = load_price_panels(conn)
    universe_cols = [sid for sid in universe_ids if sid in close.columns]
    close = close[universe_cols]
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    # 盤中：session 尚不在日線 panel，補 forward-fill 佔位列供 1m K 覆寫
    close, bench = _ensure_session_row(close, bench, session)
    if session not in close.index.astype(str):
        return [], session

    prov, bench_p, slice_dates = _slice_panels_for_date(close, bench, session)
    if prov.empty or session not in slice_dates:
        return [], session

    prov = prov.copy()
    bench_p = bench_p.copy()
    priced = _patch_intraday_close(prov, bench_p, conn, session, poll_minute, universe_ids)
    if not priced:
        return [], session

    rs5, mom5, _ = compute_rrg_panel(prov, bench_p, length=WMA_SHORT_LENGTH)
    rs20, mom20, _ = compute_rrg_panel(prov, bench_p, length=WMA_LONG_LENGTH)
    rs3, mom3, _ = compute_rrg_panel(prov, bench_p, length=WMA_MICRO_LENGTH)
    if session not in rs5.index or session not in rs20.index:
        return [], session

    rows: list[ScanRow] = []
    for sid in priced:
        if sid not in rs5.columns or sid not in rs20.columns:
            continue
        rv5 = rs5.at[session, sid]
        mv5 = mom5.at[session, sid]
        rv20 = rs20.at[session, sid]
        mv20 = mom20.at[session, sid]
        if any(v != v for v in (rv5, mv5, rv20, mv20)):
            continue
        point = _dual_point(
            frame_id=f"{session}T{poll_minute}",
            trade_date=session,
            minute=poll_minute,
            w5_rs=float(rv5),
            w5_mom=float(mv5),
            w20_rs=float(rv20),
            w20_mom=float(mv20),
        )
        if classify_dual_wma_trade_signal(point) != "lead_pullback_buy":
            continue
        mv3: float | None = None
        if sid in mom3.columns and session in mom3.index:
            raw_mv3 = mom3.at[session, sid]
            if raw_mv3 == raw_mv3:  # not NaN
                mv3 = float(raw_mv3)
        w20q = point["w20"]["quadrant"]
        w5q = point["w5"]["quadrant"]
        spread_dist = float(point["spread_dist"])
        rows.append(
            ScanRow(
                stock_id=sid,
                stock_name=name_by_id.get(sid, ""),
                fresh=True,
                mono=False,
                seg_last=spread_dist,
                disp=spread_dist,
                segs=[],
                quadrants=[w20q, w5q, point["spread_class"]],
                rs_ratio=float(rv20),
                rs_momentum=float(mv20),
                daily_pct=None,
                composite_score=spread_dist,
                w3_mom=mv3,
                w5_mom=float(mv5),
            )
        )

    rows.sort(key=lambda r: (-float(r.composite_score or 0), r.stock_id))
    return rows, session


def _build_abc_v3_family_observation_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    poll_minute: str,
    matcher: Callable[[dict[str, Any]], bool],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    require_w20_lead_firm: bool = False,
) -> tuple[list[Any], str]:
    """單 poll 快掃 · ABC v3 家族 matcher（skip09 · 4 週期）· observe/order radar。

    `matcher` 決定是否為 v3（`matches_w3_rv_hl_abc_v3`）或 v3+f1
    （`matches_w3_rv_hl_abc_v3_f1`）；其餘掃描邏輯共用。

    ``require_w20_lead_firm``：gate G · ``w20_lead_firm`` · W20 MV ≥ 前一 poll
    （否則 T−1 EOD）。
    """
    from market_benchmark import previous_trading_date
    from research.backtest.triple_wma_pullback_sweep import matches_w20_lead_firm
    from research.backtest.w3_rv_hl_winner_profile import ABC_V3_ADOPTED_MIN_POLL_IDX
    from rrg_mono_daily_brief import ScanRow

    polls = intraday_poll_minutes()
    if poll_minute not in polls:
        return [], session
    poll_idx = polls.index(poll_minute)
    if poll_idx < ABC_V3_ADOPTED_MIN_POLL_IDX:
        return [], session

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}
    universe_ids = [w["stock_id"] for w in watch]

    close, _, _ = load_price_panels(conn)
    universe_cols = [sid for sid in universe_ids if sid in close.columns]
    close = close[universe_cols]
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    # 盤中：session 尚不在日線 panel，補 forward-fill 佔位列供 1m K 覆寫
    close, bench = _ensure_session_row(close, bench, session)
    if session not in close.index.astype(str):
        return [], session

    prov, bench_p, slice_dates = _slice_panels_for_date(close, bench, session)
    if prov.empty or session not in slice_dates:
        return [], session

    prov = prov.copy()
    bench_p = bench_p.copy()
    priced = _patch_intraday_close(prov, bench_p, conn, session, poll_minute, universe_ids)
    if not priced:
        return [], session

    rs5, mom5, _ = compute_rrg_panel(prov, bench_p, length=WMA_SHORT_LENGTH)
    rs20, mom20, _ = compute_rrg_panel(prov, bench_p, length=WMA_LONG_LENGTH)
    rs3, mom3, _ = compute_rrg_panel(prov, bench_p, length=WMA_MICRO_LENGTH)
    rs2, mom2, _ = compute_rrg_panel(prov, bench_p, length=WMA_ULTRA_LENGTH)
    if session not in rs5.index or session not in rs20.index:
        return [], session

    mom20_ref = mom20
    ref_date = session
    if require_w20_lead_firm:
        if poll_idx > ABC_V3_ADOPTED_MIN_POLL_IDX:
            prev_minute = polls[poll_idx - 1]
            prov0, bench0, _ = _slice_panels_for_date(close, bench, session)
            prov_ref = prov0.copy()
            bench_ref = bench0.copy()
            _patch_intraday_close(prov_ref, bench_ref, conn, session, prev_minute, universe_ids)
            _, mom20_ref, _ = compute_rrg_panel(prov_ref, bench_ref, length=WMA_LONG_LENGTH)
        else:
            t1 = previous_trading_date(conn, session)
            ref_date = t1 if t1 and t1 in mom20.index else ""

    rows: list[ScanRow] = []
    for sid in priced:
        if sid not in rs5.columns or sid not in rs20.columns:
            continue
        rv5 = rs5.at[session, sid]
        mv5 = mom5.at[session, sid]
        rv20 = rs20.at[session, sid]
        mv20 = mom20.at[session, sid]
        if any(v != v for v in (rv5, mv5, rv20, mv20)):
            continue
        pt = _dual_point(
            frame_id=f"{session}T{poll_minute}",
            trade_date=session,
            minute=poll_minute,
            w5_rs=float(rv5),
            w5_mom=float(mv5),
            w20_rs=float(rv20),
            w20_mom=float(mv20),
        )
        for rs_m, mom_m, ml in ((rs3, mom3, WMA_MICRO_LENGTH), (rs2, mom2, WMA_ULTRA_LENGTH)):
            if sid in rs_m.columns and session in rs_m.index:
                rv_m = rs_m.at[session, sid]
                mv_m = mom_m.at[session, sid]
                if rv_m == rv_m and mv_m == mv_m:
                    pt[_micro_leg_key(ml)] = _rrg_leg(float(rv_m), float(mv_m))
        _annotate_trade_signals([pt])
        if not matcher(pt):
            continue
        if require_w20_lead_firm:
            if not ref_date or sid not in mom20_ref.columns or ref_date not in mom20_ref.index:
                continue
            mv_ref = mom20_ref.at[ref_date, sid]
            if mv_ref != mv_ref:
                continue
            if not matches_w20_lead_firm(pt, {"w20": {"rs_momentum": float(mv_ref)}}):
                continue
        w20q = pt["w20"]["quadrant"]
        w5q = pt["w5"]["quadrant"]
        spread_dist = float(pt["spread_dist"])
        w3_leg = pt.get(_micro_leg_key(WMA_MICRO_LENGTH)) or {}
        mv3_val = w3_leg.get("rs_momentum")
        rows.append(
            ScanRow(
                stock_id=sid,
                stock_name=name_by_id.get(sid, ""),
                fresh=True,
                mono=False,
                seg_last=spread_dist,
                disp=spread_dist,
                segs=[],
                quadrants=[w20q, w5q, pt["spread_class"]],
                rs_ratio=float(rv20),
                rs_momentum=float(mv20),
                daily_pct=None,
                composite_score=spread_dist,
                w3_mom=float(mv3_val) if mv3_val is not None else None,
                w5_mom=float(mv5),
            )
        )

    rows.sort(key=lambda r: (-float(r.composite_score or 0), r.stock_id))
    return rows, session


def build_abc_v3_f1_observation_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    poll_minute: str,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[Any], str]:
    """單 poll 快掃 · ABC v3+f1 + ``w20_lead_firm``（skip09 · F∧G）· observe/order radar."""
    from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1

    return _build_abc_v3_family_observation_pool(
        conn,
        session=session,
        poll_minute=poll_minute,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        etf_codes=etf_codes,
        require_w20_lead_firm=True,
    )


def build_abc_v3_skip09_observation_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    poll_minute: str,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[Any], str]:
    """單 poll 快掃 · ABC v3 matcher（skip09 · A∩B∩C∩D∩E · 無 F gate）· observe-only radar."""
    from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3

    return _build_abc_v3_family_observation_pool(
        conn,
        session=session,
        poll_minute=poll_minute,
        matcher=matches_w3_rv_hl_abc_v3,
        etf_codes=etf_codes,
    )


def summarize_lead_pullback_showcase(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    *,
    top_n: int = 5,
) -> dict[str, Any]:
    """期內 lead_pullback_buy · fresh K 統計（成果展 KPI / 導航）。"""
    frame_ids = [f["id"] for f in frames]
    frame_index = {fid: i for i, fid in enumerate(frame_ids)}
    spreads: list[float] = []
    stocks: set[str] = set()
    frames_with_signal: set[int] = set()
    events: list[dict[str, Any]] = []

    for t in trajectories:
        sid = str(t.get("stock_id") or "")
        name = str(t.get("stock_name") or "")
        for p in t.get("points") or []:
            if p.get("trade_signal") != "lead_pullback_buy":
                continue
            if not p.get("fresh"):
                continue
            dist = float(p.get("spread_dist") or 0)
            fid = str(p.get("frame_id") or "")
            fi = frame_index.get(fid)
            if fi is None:
                continue
            spreads.append(dist)
            stocks.add(sid)
            frames_with_signal.add(fi)
            w5 = p.get("w5") or {}
            w20 = p.get("w20") or {}
            events.append(
                {
                    "stock_id": sid,
                    "stock_name": name,
                    "frame_idx": fi,
                    "frame_id": fid,
                    "date": p.get("date") or frames[fi]["date"],
                    "minute": p.get("minute") or frames[fi]["minute"],
                    "spread_dist": round(dist, 2),
                    "w5_quadrant": w5.get("quadrant"),
                    "w20_quadrant": w20.get("quadrant"),
                }
            )

    signal_frame_indices = sorted(frames_with_signal)
    spreads_sorted = sorted(spreads)
    n = len(spreads_sorted)

    def _pct(p: float) -> float | None:
        if n == 0:
            return None
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(spreads_sorted[idx], 2)

    top_cases = sorted(events, key=lambda e: (-e["spread_dist"], e["stock_id"]))[:top_n]

    return {
        "n_events": n,
        "n_stocks": len(stocks),
        "n_signal_frames": len(signal_frame_indices),
        "n_frames_total": len(frames),
        "spread_median": _pct(0.5),
        "spread_p75": _pct(0.75),
        "signal_frame_indices": signal_frame_indices,
        "top_cases": top_cases,
    }
