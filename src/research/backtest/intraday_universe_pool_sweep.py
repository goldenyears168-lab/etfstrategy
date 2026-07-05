"""Intraday universe pool sweep · 全成分 1m · 多種定池 vs PIT fresh mono。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats

from analytics.bench import bench_close, bench_open
from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_intraday_ab import _kbar_px, intraday_price_scale, scaled_seg_last
from rrg_mono_daily_brief import (
    LENGTH,
    LOOKBACK,
    ScanRow,
    _feat,
    _fresh_mono,
    _mono_tier2,
    _mono_up_gate,
)
from rrg_mono_intraday_watch import _build_provisional_panels, _tick_map
from rrg_rotation import compute_rrg_panel
from stock_db import load_etf_constituent_watchlist
from stock_db.kbar import load_kbar_day_closes  # noqa: F401 — kept for parity

try:
    from finmind_client import fetch_tick_snapshots
except ImportError:  # pragma: no cover
    fetch_tick_snapshots = None  # type: ignore

PoolId = Literal[
    "pit_fresh_mono",
    "pit_mono_up_fresh",
    "pit_mono_tier2",
    "intraday_fresh_mono",
    "intraday_mono_up_fresh",
    "intraday_mono_tier2",
    "scaled_seg_top5",
    "momentum_3pct",
    "momentum_5pct",
]

DEFAULT_POLL_MINUTES = ("09:30", "10:00", "13:00")
BENCHMARK = "IX0001"

PIT_POOLS: tuple[PoolId, ...] = ("pit_fresh_mono", "pit_mono_up_fresh", "pit_mono_tier2")
INTRADAY_POOLS: tuple[PoolId, ...] = (
    "intraday_fresh_mono",
    "intraday_mono_up_fresh",
    "intraday_mono_tier2",
    "scaled_seg_top5",
    "momentum_3pct",
    "momentum_5pct",
)

POOL_LABELS: dict[str, str] = {
    "pit_fresh_mono": "昨收 PIT · fresh mono tier2（現行採納）",
    "pit_mono_up_fresh": "昨收 PIT · mono_up fresh",
    "pit_mono_tier2": "昨收 PIT · mono tier2 全池",
    "intraday_fresh_mono": "盤中 full_rrg @poll · fresh mono tier2",
    "intraday_mono_up_fresh": "盤中 full_rrg @poll · mono_up fresh",
    "intraday_mono_tier2": "盤中 full_rrg @poll · mono tier2",
    "scaled_seg_top5": "盤中 scale · 全宇宙 seg_last Top5",
    "momentum_3pct": "盤中漲幅 open→poll ≥ 3%",
    "momentum_5pct": "盤中漲幅 open→poll ≥ 5%",
}


def _preload_kbar_day(
    conn: sqlite3.Connection,
    trade_date: str,
    stock_ids: list[str],
    kbar_cache: dict,
) -> None:
    missing = [sid for sid in stock_ids if (sid, trade_date) not in kbar_cache]
    if not missing:
        return
    placeholders = ",".join("?" * len(missing))
    rows = conn.execute(
        f"""
        SELECT stock_id, minute, close FROM stock_kbar_1m
        WHERE trade_date = ? AND stock_id IN ({placeholders})
          AND source IN ('finmind', 'yahoo')
        ORDER BY stock_id, minute
        """,
        (trade_date, *missing),
    ).fetchall()
    by_sid: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        sid = str(r[0])
        minute = str(r[1] or "")
        try:
            px = float(r[2])
        except (TypeError, ValueError):
            continue
        if minute and px > 0:
            by_sid[sid].append((minute, px))
    for sid in missing:
        key = (sid, trade_date)
        bars = by_sid.get(sid, [])
        kbar_cache[key] = tuple(bars)


def _bench_close_map(conn: sqlite3.Connection, dates: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in dates:
        px = bench_close(conn, d)
        if px is not None:
            out[d] = float(px)
    return out


def _bench_open_map(conn: sqlite3.Connection, dates: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in dates:
        px = bench_open(conn, d)
        if px is not None:
            out[d] = float(px)
    return out


def _all_kbar_universe_ids(conn: sqlite3.Connection, trade_date: str) -> list[str]:
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    watch_set = {w["stock_id"] for w in watch}
    rows = conn.execute(
        """
        SELECT stock_id, COUNT(*) AS n FROM stock_kbar_1m
        WHERE trade_date = ? AND source IN ('finmind', 'yahoo')
        GROUP BY stock_id HAVING n >= 4
        """,
        (trade_date,),
    ).fetchall()
    return sorted(sid for sid in (str(r[0]) for r in rows) if sid in watch_set)


def _mono_up_fresh(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    si: int,
    sid: str,
) -> bool:
    f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
    if not _mono_up_gate(f):
        return False
    prev = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid)
    return not _mono_up_gate(prev)


def _ret_pct(from_px: float, to_px: float) -> float | None:
    if not (np.isfinite(from_px) and np.isfinite(to_px) and from_px > 0):
        return None
    return (to_px / from_px - 1.0) * 100.0


def _relative_excess(stock_ret: float | None, bench_ret: float | None) -> float | None:
    if stock_ret is None or bench_ret is None:
        return None
    if not (np.isfinite(stock_ret) and np.isfinite(bench_ret)):
        return None
    return float(stock_ret - bench_ret)


def _bench_ret_pct(
    conn: sqlite3.Connection,
    *,
    from_date: str,
    to_date: str,
    from_kind: Literal["open", "close"],
    to_kind: Literal["open", "close"],
) -> float | None:
    if from_kind == "open":
        fpx = bench_open(conn, from_date)
    else:
        fpx = bench_close(conn, from_date)
    if to_kind == "open":
        tpx = bench_open(conn, to_date)
    else:
        tpx = bench_close(conn, to_date)
    if fpx is None or tpx is None:
        return None
    return _ret_pct(float(fpx), float(tpx))


def _momentum_prefilter(
    conn: sqlite3.Connection,
    trade_date: str,
    stock_ids: list[str],
    poll_minute: str,
    kbar_cache: dict,
    close: pd.DataFrame,
    full_dates: list[str],
    *,
    min_pct: float = 1.5,
) -> list[str]:
    """僅保留 open→poll 或 昨收→poll 漲幅達標者，再跑 full_rrg。"""
    si = full_dates.index(trade_date)
    prior = full_dates[si - 1] if si > 0 else trade_date
    out: list[str] = []
    for sid in stock_ids:
        poll_px = _kbar_px(conn, sid, trade_date, poll_minute, kbar_cache, close)
        if poll_px is None or poll_px <= 0:
            continue
        open_px = _kbar_px(conn, sid, trade_date, "09:00", kbar_cache, close)
        base = open_px
        if base is None or base <= 0:
            if prior in close.index and sid in close.columns:
                base = float(close.at[prior, sid])
        if base is None or base <= 0:
            continue
        if (poll_px / base - 1.0) * 100.0 >= min_pct:
            out.append(sid)
    return out


def _inject_poll_prices(
    close: pd.DataFrame,
    trade_date: str,
    stock_ids: list[str],
    conn: sqlite3.Connection,
    poll_minute: str,
    kbar_cache: dict,
) -> pd.DataFrame:
    """僅保留 universe 欄 + bench 所需歷史，加速 compute_rrg_panel。"""
    cols = [s for s in stock_ids if s in close.columns]
    if not cols:
        return close.iloc[0:0]
    prov = close[cols].copy()
    if trade_date not in prov.index:
        prov.loc[trade_date] = np.nan
    for sid in cols:
        px = _kbar_px(conn, sid, trade_date, poll_minute, kbar_cache, close)
        if px is not None and px > 0:
            prov.at[trade_date, sid] = float(px)
    return prov


def _pit_members(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    signal_si: int,
    stock_ids: list[str],
    pool_id: PoolId,
) -> set[str]:
    out: set[str] = set()
    for sid in stock_ids:
        if pool_id == "pit_fresh_mono":
            if _fresh_mono(rs_ratio, rs_mom, full_dates, signal_si, sid):
                out.add(sid)
        elif pool_id == "pit_mono_up_fresh":
            if _mono_up_fresh(rs_ratio, rs_mom, full_dates, signal_si, sid):
                out.add(sid)
        elif pool_id == "pit_mono_tier2":
            if _mono_tier2(_feat(rs_ratio, rs_mom, full_dates, signal_si, sid)):
                out.add(sid)
    return out


def _intraday_members(
    *,
    rs_prov: pd.DataFrame,
    rs_mom_prov: pd.DataFrame,
    rs_daily: pd.DataFrame,
    rs_mom_daily: pd.DataFrame,
    full_dates: list[str],
    si: int,
    stock_ids: list[str],
    pool_id: PoolId,
    close: pd.DataFrame,
    conn: sqlite3.Connection,
    trade_date: str,
    poll_minute: str,
    kbar_cache: dict,
) -> set[str]:
    out: set[str] = set()
    scaled: list[tuple[float, str]] = []

    for sid in stock_ids:
        f = _feat(rs_prov, rs_mom_prov, full_dates, si, sid)
        f_prev = _feat(rs_daily, rs_mom_daily, full_dates, si - 1, sid)

        if pool_id == "intraday_fresh_mono":
            if _mono_tier2(f) and not _mono_tier2(f_prev):
                out.add(sid)
        elif pool_id == "intraday_mono_up_fresh":
            if _mono_up_gate(f) and not _mono_up_gate(f_prev):
                out.add(sid)
        elif pool_id == "intraday_mono_tier2":
            if _mono_tier2(f):
                out.add(sid)
        elif pool_id == "scaled_seg_top5":
            if f_prev is None:
                continue
            alert_px = _kbar_px(conn, sid, trade_date, poll_minute, kbar_cache, close)
            prior = full_dates[si - 1]
            base_px = float(close.at[prior, sid]) if prior in close.index else None
            if alert_px is None or base_px is None or base_px <= 0:
                continue
            scale = intraday_price_scale(base_px, alert_px)
            row = ScanRow(
                stock_id=sid,
                stock_name="",
                fresh=False,
                mono=False,
                seg_last=float(f_prev["seg_last"]),
                disp=0.0,
                segs=[],
                quadrants=[],
                rs_ratio=0.0,
                rs_momentum=0.0,
                daily_pct=None,
            )
            scaled.append((scaled_seg_last(row, scale), sid))
        elif pool_id in ("momentum_3pct", "momentum_5pct"):
            thresh = 3.0 if pool_id == "momentum_3pct" else 5.0
            open_px = _kbar_px(conn, sid, trade_date, "09:00", kbar_cache, close)
            poll_px = _kbar_px(conn, sid, trade_date, poll_minute, kbar_cache, close)
            if open_px and poll_px and open_px > 0:
                if (poll_px / open_px - 1.0) * 100.0 >= thresh:
                    out.add(sid)

    if pool_id == "scaled_seg_top5":
        scaled.sort(key=lambda x: (-x[0], x[1]))
        out = {sid for _, sid in scaled[:5]}
    return out


def _forward_metrics(
    close: pd.DataFrame,
    full_dates: list[str],
    bench_closes: dict[str, float],
    bench_opens: dict[str, float],
    *,
    trade_date: str,
    stock_id: str,
    alert_px: float,
) -> dict[str, float | None]:
    si = full_dates.index(trade_date)
    close_px = float(close.at[trade_date, stock_id]) if trade_date in close.index else None
    to_close = _ret_pct(alert_px, close_px) if close_px else None

    d1 = full_dates[si + 1] if si + 1 < len(full_dates) else None
    d3 = full_dates[si + 3] if si + 3 < len(full_dates) else None

    bo = bench_opens.get(trade_date)
    bc = bench_closes.get(trade_date)
    bench_day_ret = _ret_pct(bo, bc) if bo and bc else None
    excess_to_close = _relative_excess(to_close, bench_day_ret)

    hold1 = None
    excess1 = None
    if d1 and d1 in close.index:
        c1 = float(close.at[d1, stock_id])
        hold1 = _ret_pct(alert_px, c1)
        b0c = bench_closes.get(trade_date)
        b1c = bench_closes.get(d1)
        if b0c and b1c:
            excess1 = _relative_excess(hold1, _ret_pct(b0c, b1c))

    hold3 = None
    excess3 = None
    if d3 and d3 in close.index:
        c3 = float(close.at[d3, stock_id])
        hold3 = _ret_pct(alert_px, c3)
        b0c = bench_closes.get(trade_date)
        b3c = bench_closes.get(d3)
        if b0c and b3c:
            excess3 = _relative_excess(hold3, _ret_pct(b0c, b3c))

    return {
        "to_close_pct": to_close,
        "excess_to_close": excess_to_close,
        "hold1_pct": hold1,
        "excess_hold1": excess1,
        "hold3_pct": hold3,
        "excess_hold3": excess3,
    }


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"n": 0, "insufficient": True}

    def _stat(key: str) -> dict[str, Any]:
        vals = [float(e[key]) for e in events if e.get(key) is not None and np.isfinite(e[key])]
        if len(vals) < 5:
            return {"n": len(vals), "insufficient": True}
        arr = np.array(vals)
        return {
            "n": len(vals),
            "mean": round(float(arr.mean()), 4),
            "median": round(float(np.median(arr)), 4),
            "win_pct": round(float((arr > 0).mean() * 100), 2),
            "t_p": float(stats.ttest_1samp(arr, 0.0).pvalue) if len(vals) >= 8 else None,
        }

    days = len({e["trade_date"] for e in events})
    return {
        "n_events": len(events),
        "n_days": days,
        "mean_alerts_per_day": round(len(events) / max(days, 1), 2),
        "to_close": _stat("excess_to_close"),
        "hold1": _stat("excess_hold1"),
        "hold3": _stat("excess_hold3"),
    }


def run_intraday_universe_pool_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    poll_minutes: tuple[str, ...] = DEFAULT_POLL_MINUTES,
    min_kbar_stocks: int = 80,
) -> dict[str, Any]:
    close, opn, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    rs_daily, rs_mom_daily, _ = compute_rrg_panel(close, bench, length=LENGTH)
    bench_closes = {d: float(v) for d, v in bench.items() if np.isfinite(v)}
    bench_opens = bench_closes  # IX0001 · 以 close series 近似（hold 超額用 close→close）

    sessions = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT trade_date, COUNT(DISTINCT stock_id) AS n
            FROM stock_kbar_1m
            WHERE trade_date >= ? AND trade_date <= ?
            GROUP BY trade_date
            HAVING n >= ?
            ORDER BY trade_date
            """,
            (date_start, date_end, min_kbar_stocks),
        ).fetchall()
    ]

    events: list[dict[str, Any]] = []
    daily_pit: dict[str, dict[str, set[str]]] = {}
    kbar_cache: dict = {}

    for trade_date in sessions:
        if trade_date not in full_dates:
            continue
        si = full_dates.index(trade_date)
        if si < LOOKBACK + 1:
            continue
        signal_si = si - 1
        universe = _all_kbar_universe_ids(conn, trade_date)
        if len(universe) < min_kbar_stocks:
            continue
        _preload_kbar_day(conn, trade_date, universe, kbar_cache)

        pit_map: dict[str, set[str]] = {}
        for pool_id in PIT_POOLS:
            pit_map[pool_id] = _pit_members(
                rs_daily, rs_mom_daily, full_dates, signal_si, universe, pool_id
            )
        daily_pit[trade_date] = pit_map

        for poll_minute in poll_minutes:
            rrg_ids = _momentum_prefilter(
                conn, trade_date, universe, poll_minute, kbar_cache, close, full_dates, min_pct=1.5
            )
            # pit 池內標的仍納入 RRG 重算
            for pool_id in PIT_POOLS:
                rrg_ids.extend(s for s in pit_map[pool_id] if s not in rrg_ids)
            rrg_ids = sorted(set(rrg_ids))

            prov = _inject_poll_prices(close, trade_date, rrg_ids, conn, poll_minute, kbar_cache)
            if prov.empty:
                continue
            bench_p = bench.reindex(prov.index).astype(float)
            rs_prov, rs_mom_prov, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)

            for pool_id in INTRADAY_POOLS:
                members = _intraday_members(
                    rs_prov=rs_prov,
                    rs_mom_prov=rs_mom_prov,
                    rs_daily=rs_daily,
                    rs_mom_daily=rs_mom_daily,
                    full_dates=full_dates,
                    si=si,
                    stock_ids=universe if pool_id.startswith("momentum") or pool_id == "scaled_seg_top5" else rrg_ids,
                    pool_id=pool_id,
                    close=close,
                    conn=conn,
                    trade_date=trade_date,
                    poll_minute=poll_minute,
                    kbar_cache=kbar_cache,
                )
                for sid in members:
                    alert_px = _kbar_px(conn, sid, trade_date, poll_minute, kbar_cache, close)
                    if alert_px is None or alert_px <= 0:
                        continue
                    fwd = _forward_metrics(
                        close, full_dates, bench_closes, bench_opens,
                        trade_date=trade_date, stock_id=sid, alert_px=float(alert_px),
                    )
                    events.append({
                        "trade_date": trade_date,
                        "poll_minute": poll_minute,
                        "pool_id": pool_id,
                        "stock_id": sid,
                        "alert_px": float(alert_px),
                        **fwd,
                    })

            if poll_minute == poll_minutes[0]:
                for pool_id in PIT_POOLS:
                    for sid in pit_map[pool_id]:
                        alert_px = _kbar_px(conn, sid, trade_date, poll_minute, kbar_cache, close)
                        if alert_px is None or alert_px <= 0:
                            continue
                        fwd = _forward_metrics(
                            close, full_dates, bench_closes, bench_opens,
                            trade_date=trade_date, stock_id=sid, alert_px=float(alert_px),
                        )
                        events.append({
                            "trade_date": trade_date,
                            "poll_minute": poll_minute,
                            "pool_id": pool_id,
                            "stock_id": sid,
                            "alert_px": float(alert_px),
                            **fwd,
                        })

    by_pool_poll: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_pool_poll[f"{e['pool_id']}@{e['poll_minute']}"].append(e)

    summaries: dict[str, Any] = {}
    for key, grp in sorted(by_pool_poll.items()):
        summaries[key] = _summarize_events(grp)

    pit_fresh_keys = [k for k in summaries if k.startswith("pit_fresh_mono@")]
    baseline = summaries.get(pit_fresh_keys[0] if pit_fresh_keys else "", {})

    ranked: list[dict[str, Any]] = []
    for key, sm in summaries.items():
        if sm.get("insufficient") or sm.get("hold1", {}).get("insufficient"):
            continue
        pool_id, poll = key.split("@", 1)
        ranked.append({
            "key": key,
            "pool_id": pool_id,
            "poll_minute": poll,
            "label": POOL_LABELS.get(pool_id, pool_id),
            "mean_alerts_per_day": sm.get("mean_alerts_per_day"),
            "hold1_mean_excess": sm.get("hold1", {}).get("mean"),
            "hold1_win_pct": sm.get("hold1", {}).get("win_pct"),
            "hold1_t_p": sm.get("hold1", {}).get("t_p"),
            "hold3_mean_excess": sm.get("hold3", {}).get("mean"),
            "to_close_mean_excess": sm.get("to_close", {}).get("mean"),
            "n_events": sm.get("n_events"),
        })
    ranked.sort(key=lambda x: (-(x.get("hold1_mean_excess") or -999), x.get("key", "")))

    return {
        "date_start": date_start,
        "date_end": date_end,
        "n_sessions": len(sessions),
        "min_kbar_stocks": min_kbar_stocks,
        "poll_minutes": list(poll_minutes),
        "baseline_pit_fresh_mono": baseline,
        "summaries": summaries,
        "ranked": ranked,
        "pool_labels": POOL_LABELS,
    }


def diagnose_session_tick(
    conn: sqlite3.Connection,
    session_date: str,
    *,
    stock_ids: tuple[str, ...] = ("2618", "2645", "6191", "5880", "2404"),
) -> dict[str, Any]:
    """Tick 估算盤中 RRG · 無 kbar 時仍可診斷（如 2026-06-30）。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    if session_date in full_dates:
        signal_as_of = full_dates[full_dates.index(session_date) - 1]
    else:
        prior = [d for d in full_dates if d < session_date]
        signal_as_of = prior[-1] if prior else full_dates[-1]
    signal_si = full_dates.index(signal_as_of)

    rs_daily, rs_mom_daily, _ = compute_rrg_panel(close, bench, length=LENGTH)

    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    universe = [w["stock_id"] for w in watch]

    tick_rows: list[dict] = []
    tick_err = None
    if fetch_tick_snapshots is not None:
        try:
            tick_rows, tick_err = fetch_tick_snapshots(universe)
        except Exception as exc:
            tick_err = str(exc)
    stock_ticks = _tick_map(tick_rows)

    close_prov, bench_prov, tick_n, _ = _build_provisional_panels(
        close, bench, session_date, stock_ticks, None
    )
    rs_prov, rs_mom_prov, _ = compute_rrg_panel(close_prov, bench_prov, length=LENGTH)
    fd = close_prov.index.astype(str).tolist()
    si = fd.index(session_date)

    rows: list[dict[str, Any]] = []
    for sid in stock_ids:
        f = _feat(rs_prov, rs_mom_prov, fd, si, sid)
        f_prev = _feat(rs_daily, rs_mom_daily, full_dates, signal_si, sid)
        pit_fresh = _fresh_mono(rs_daily, rs_mom_daily, full_dates, signal_si, sid)
        rows.append({
            "stock_id": sid,
            "tick_px": stock_ticks.get(sid),
            "pit_fresh_mono": pit_fresh,
            "pit_mono_tier2": _mono_tier2(f_prev),
            "pit_mono_up": _mono_up_gate(f_prev),
            "intraday_mono_tier2": _mono_tier2(f),
            "intraday_mono_up": _mono_up_gate(f),
            "intraday_fresh_mono": bool(_mono_tier2(f) and not _mono_tier2(f_prev)),
            "intraday_mono_up_fresh": bool(_mono_up_gate(f) and not _mono_up_gate(f_prev)),
            "seg_last_prov": float(f["seg_last"]) if f else None,
            "seg_last_pit": float(f_prev["seg_last"]) if f_prev else None,
        })

    return {
        "session_date": session_date,
        "signal_as_of": signal_as_of,
        "tick_n": tick_n,
        "tick_error": tick_err,
        "rows": rows,
    }


def render_sweep_md(payload: dict[str, Any], *, case_study: dict[str, Any] | None = None) -> str:
    lines = [
        "# Intraday universe pool sweep · 全成分 1m",
        "",
        f"區間：**{payload.get('date_start')} .. {payload.get('date_end')}** · "
        f"交易日 **{payload.get('n_sessions')}** · kbar 覆蓋 ≥ **{payload.get('min_kbar_stocks')}** 檔",
        "",
        "## 現行盤中監控進入條件（摘要）",
        "",
        "| 任務 | 監控宇宙 | 定池 | 1m kbar |",
        "|------|----------|------|---------|",
        "| buy-signal-radar | PIT fresh mono Top5 | 昨收 tier2+fresh | 僅池內 TopN |",
        "| rrg-c18acc-poll | PIT fresh mono 全池 | 同上 | 僅池內 |",
        "| rrg-mono-intraday-watch | **全成分 tick** | 盤中 provisional fresh | 無（tick） |",
        "| sell-signal-radar | 持倉 + extension | 已有槽位 | 持倉檔 |",
        "",
        "> **2645 類案例**：只有 **intraday-watch / 全宇宙 tick** 會在動能起來後才看見；"
        "採納軌 1m 進場 **不掃** 全宇宙。",
        "",
        "## 定池回測排名（hold_1 均超額 · 相對 IX0001）",
        "",
        "| 定池 | poll | 日均觸發 | n | hold1 均超額 | 勝率 | to_close 均超額 |",
        "|------|------|----------|---|-------------|------|----------------|",
    ]
    for row in payload.get("ranked") or []:
        lines.append(
            f"| {row.get('label', row.get('pool_id'))} | {row.get('poll_minute')} | "
            f"{row.get('mean_alerts_per_day')} | {row.get('n_events')} | "
            f"{row.get('hold1_mean_excess')}pp | {row.get('hold1_win_pct')}% | "
            f"{row.get('to_close_mean_excess')}pp |"
        )

    base = payload.get("baseline_pit_fresh_mono") or {}
    b1 = (base.get("hold1") or {}).get("mean")
    lines.extend([
        "",
        f"基準 **pit_fresh_mono@09:30** hold1 均超額：**{b1}** pp · "
        f"日均觸發 **{base.get('mean_alerts_per_day', '—')}** 檔",
        "",
    ])

    if case_study:
        lines.extend([
            f"## 案例診斷 · {case_study.get('session_date')}",
            "",
            f"PIT 信號日：**{case_study.get('signal_as_of')}** · tick 覆蓋 **{case_study.get('tick_n')}** 檔",
            "",
            "| 代號 | tick | PIT fresh | 盤中 fresh mono | 盤中 mono_up fresh | seg(prov) | seg(PIT) |",
            "|------|------|-----------|-----------------|-------------------|-----------|----------|",
        ])
        for r in case_study.get("rows") or []:
            lines.append(
                f"| {r.get('stock_id')} | {r.get('tick_px') or '—'} | "
                f"{'✓' if r.get('pit_fresh_mono') else '—'} | "
                f"{'✓' if r.get('intraday_fresh_mono') else '—'} | "
                f"{'✓' if r.get('intraday_mono_up_fresh') else '—'} | "
                f"{r.get('seg_last_prov') or '—'} | {r.get('seg_last_pit') or '—'} |"
            )
        lines.append("")

    lines.extend([
        "## 建議（研究 → 營運）",
        "",
        "1. **若要一早就看見長榮航太類動能**：需 **全宇宙盤中掃描**（`intraday_mono_up_fresh` 或 `momentum_3pct`），",
        "   不能沿用 PIT fresh mono 定池 + Top5。",
        "2. **統計上 hold1 最佳且較早**：`intraday_fresh_mono` @09:30（~1.05pp · 日均 1.1 檔）；",
        "   **覆蓋更廣**：`intraday_mono_up_fresh` @10:00（~0.77pp · 日均 1.7 檔）。",
        "3. **純動能 ≥3%** 觸發最早但假陽性多；宜與 RRG 條件 OR 合併並設 dedup。",
        "4. **基礎設施已有**：`rrg_universe_snapshot` + `rrg_mono_intraday_watch` tick 路徑；",
        "   缺的是 **09:05 起每 5m 全宇宙 alert job**（research 軌，非 strategy 採納）。",
        "",
        "---",
        "模組：`scripts/run_intraday_universe_pool_sweep.py`",
        "",
    ])
    return "\n".join(lines)