"""FinPilot s04（60日動能+ROE>0）分層選股與回測拆解。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .copytrade_backtest import bench_return_entry_to_exit
from .finpilot_local_backtest import (
    basket_return_h9,
    load_financial_history,
    load_fundamental_snapshot,
    load_price_panels,
    month_end_trading_dates,
    pit_fundamental_at,
    summarize_periods,
)
from flow_returns import return_pct, stock_close, stock_open, trading_dates_after


S04LayerMode = Literal[
    "mom_top10",
    "mom_top20",
    "mom_top30",
    "s04_full",
    "mom_top30_among_roe",
    "roe_positive_mom_top30",
    "random30",
]


@dataclass(frozen=True)
class S04LayerSpec:
    layer_id: str
    label: str
    mode: S04LayerMode
    mom_top_n: int = 30
    roe_min: float | None = None
    roe_prefilter: bool = False


S04_LAYER_SPECS: tuple[S04LayerSpec, ...] = (
    S04LayerSpec("L0", "隨機30檔（對照）", "random30", mom_top_n=30),
    S04LayerSpec("L1a", "動能 Top10（無 ROE）", "mom_top10", mom_top_n=10),
    S04LayerSpec("L1b", "動能 Top20（無 ROE）", "mom_top20", mom_top_n=20),
    S04LayerSpec("L1c", "動能 Top30（無 ROE）", "mom_top30", mom_top_n=30),
    S04LayerSpec("L2a", "ROE>0 池內動能 Top30", "mom_top30_among_roe", mom_top_n=30, roe_min=0.0, roe_prefilter=True),
    S04LayerSpec("L2b", "動能 Top30 再 ROE>0（s04 完整）", "s04_full", mom_top_n=30, roe_min=0.0),
    S04LayerSpec("L2c", "ROE>0 且動能 Top30", "roe_positive_mom_top30", mom_top_n=30, roe_min=0.0),
)


def _mom_series(
    close: pd.DataFrame, signal_date: str, *, lookback: int = 60
) -> pd.Series | None:
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    hist = close.loc[:signal_date]
    if len(hist) < lookback:
        return None
    c = close.loc[signal_date]
    return c / hist.iloc[-lookback]


def _mom60_series(close: pd.DataFrame, signal_date: str) -> pd.Series | None:
    return _mom_series(close, signal_date, lookback=60)


def build_roe_panel(
    fin_hist: pd.DataFrame,
    stock_ids: list[str],
    dates: list[str],
) -> dict[str, dict[str, float | None]]:
    """預先計算每 signal_date 的 PIT ROE。"""
    panel: dict[str, dict[str, float | None]] = {d: {} for d in dates}
    if fin_hist.empty:
        for d in dates:
            panel[d] = {sid: None for sid in stock_ids}
        return panel

    q = fin_hist[fin_hist["period_type"] == "quarter"]
    for sid in stock_ids:
        sq = q[q["stock_id"] == sid]
        if sq.empty:
            for d in dates:
                panel[d][sid] = None
            continue
        qdates = sorted(sq["period_date"].unique())
        roe_by_q: dict[str, float | None] = {}
        for qd in qdates:
            chunk = sq[sq["period_date"] == qd]
            ni = chunk.loc[chunk["metric"] == "net_income", "value"]
            eq = chunk.loc[chunk["metric"] == "equity", "value"]
            if ni.empty or eq.empty:
                roe_by_q[qd] = None
                continue
            equity = float(eq.iloc[0])
            roe_by_q[qd] = (
                float(ni.iloc[0]) / equity * 100.0 if equity > 0 else None
            )
        qi = 0
        for d in dates:
            while qi + 1 < len(qdates) and qdates[qi + 1] <= d:
                qi += 1
            panel[d][sid] = roe_by_q.get(qdates[qi]) if qdates[qi] <= d else None
    return panel


def fund_snap_from_roe_panel(
    roe_panel: dict[str, dict[str, float | None]],
    signal_date: str,
    stock_ids: list[str],
) -> dict[str, dict[str, float | None]]:
    row = roe_panel.get(signal_date, {})
    return {
        sid: {"roe_latest_q": row.get(sid), "revenue_yoy_pct": None} for sid in stock_ids
    }


def select_s04_layer(
    spec: S04LayerSpec,
    *,
    signal_date: str,
    close: pd.DataFrame,
    fund_snap: dict[str, dict[str, float | None]],
    rng_seed: int | None = None,
    mom_lookback: int = 60,
) -> tuple[list[str], dict]:
    """回傳 (picks, layer_meta)。"""
    mom = _mom_series(close, signal_date, lookback=mom_lookback)
    meta: dict = {
        "mom_lookback": mom_lookback,
        "mom_top_n": spec.mom_top_n,
        "n_universe": int(close.shape[1]),
        "n_mom_pool": 0,
        "n_roe_pass": 0,
        "n_dropped_by_roe": 0,
        "dropped_by_roe": [],
    }
    if mom is None:
        return [], meta

    def _roe_ok(sid: str) -> bool:
        if spec.roe_min is None:
            return True
        roe = fund_snap.get(sid, {}).get("roe_latest_q")
        return roe is not None and roe > spec.roe_min

    if spec.mode == "random30":
        import random

        pool = [str(x) for x in close.columns if pd.notna(close.loc[signal_date, x])]
        if not pool:
            return [], meta
        rnd = random.Random(rng_seed or hash(signal_date) % (2**32))
        k = min(spec.mom_top_n, len(pool))
        picks = rnd.sample(pool, k)
        meta["n_mom_pool"] = len(pool)
        return picks, meta

    if spec.roe_prefilter or spec.mode == "mom_top30_among_roe":
        roe_pool = [str(sid) for sid in mom.index if _roe_ok(str(sid))]
        meta["n_roe_pass"] = len(roe_pool)
        ranked = mom[roe_pool].sort_values(ascending=False).head(spec.mom_top_n)
        picks = [str(x) for x in ranked.index]
        meta["n_mom_pool"] = len(roe_pool)
        return picks, meta

    if spec.mode in ("mom_top10", "mom_top20", "mom_top30"):
        ranked = mom.sort_values(ascending=False).head(spec.mom_top_n)
        picks = [str(x) for x in ranked.index]
        meta["n_mom_pool"] = len(picks)
        return picks, meta

    if spec.mode in ("s04_full", "roe_positive_mom_top30"):
        top_mom = mom.sort_values(ascending=False).head(spec.mom_top_n)
        meta["n_mom_pool"] = len(top_mom)
        picks = []
        dropped = []
        for sid in top_mom.index:
            sid_s = str(sid)
            if _roe_ok(sid_s):
                picks.append(sid_s)
            else:
                dropped.append(sid_s)
        meta["n_roe_pass"] = len(picks)
        meta["n_dropped_by_roe"] = len(dropped)
        meta["dropped_by_roe"] = dropped
        return picks, meta

    raise ValueError(f"unknown mode: {spec.mode}")


def leg_returns_h9(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    entry_date: str,
    *,
    hold_days: int = 9,
) -> list[dict]:
    exit_dates = trading_dates_after(conn, entry_date, count=hold_days)
    if len(exit_dates) < hold_days:
        return []
    exit_date = exit_dates[hold_days - 1]
    bench_ret = bench_return_entry_to_exit(
        conn, entry_date, exit_date, entry_price_mode="open"
    )
    legs: list[dict] = []
    for sid in stock_ids:
        p0 = stock_open(conn, sid, entry_date)
        p1 = stock_close(conn, sid, exit_date)
        if p0 is None or p1 is None:
            continue
        ret = return_pct(p0, p1)
        legs.append(
            {
                "stock_id": sid,
                "return_pct": ret,
                "bench_return_pct": bench_ret,
                "beat_bench": bench_ret is not None and ret > bench_ret,
                "gross_win": ret > 0,
            }
        )
    return legs


def run_s04_layer_periods(
    conn: sqlite3.Connection,
    spec: S04LayerSpec,
    *,
    hold_days: int = 9,
    window_start: str | None = None,
    window_end: str | None = None,
    mom_lookback: int = 60,
) -> list[dict]:
    close, _opn, vol = load_price_panels(conn)
    fund = load_fundamental_snapshot(conn)
    fin_hist = load_financial_history(conn)
    cal = [str(d) for d in close.index]
    periods: list[dict] = []

    for month_end in month_end_trading_dates(cal):
        entry_candidates = trading_dates_after(conn, month_end, count=1)
        if not entry_candidates:
            continue
        entry_date = entry_candidates[0]
        if window_start and entry_date < window_start:
            continue
        if window_end and entry_date > window_end:
            continue

        fund_snap = pit_fundamental_at(
            fund, fin_hist, list(close.columns.astype(str)), month_end
        )
        picks, layer_meta = select_s04_layer(
            spec,
            signal_date=month_end,
            close=close,
            fund_snap=fund_snap,
            mom_lookback=mom_lookback,
        )
        if not picks:
            continue

        roe_dropped = layer_meta.get("dropped_by_roe") or []
        row = _append_s04_period(
            conn,
            signal_date=month_end,
            entry_date=entry_date,
            picks=picks,
            layer_meta=layer_meta,
            hold_days=hold_days,
            roe_dropped=roe_dropped,
        )
        if row:
            row["mom_lookback"] = mom_lookback
            periods.append(row)
    return periods


def _basket_return_from_panels(
    opn: pd.DataFrame,
    close: pd.DataFrame,
    stock_ids: list[str],
    entry_date: str,
    exit_date: str,
) -> float | None:
    rets: list[float] = []
    if entry_date not in opn.index or exit_date not in close.index:
        return None
    for sid in stock_ids:
        if sid not in opn.columns:
            continue
        p0 = opn.at[entry_date, sid]
        p1 = close.at[exit_date, sid]
        if pd.isna(p0) or pd.isna(p1) or float(p0) <= 0:
            continue
        rets.append(return_pct(float(p0), float(p1)))
    if not rets:
        return None
    return sum(rets) / len(rets)


def _append_s04_period(
    conn: sqlite3.Connection,
    *,
    signal_date: str,
    entry_date: str,
    picks: list[str],
    layer_meta: dict,
    hold_days: int,
    roe_dropped: list[str],
    opn: pd.DataFrame | None = None,
    close: pd.DataFrame | None = None,
    lightweight: bool = False,
) -> dict | None:
    exit_dates = trading_dates_after(conn, entry_date, count=hold_days)
    if len(exit_dates) < hold_days:
        return None
    exit_date = exit_dates[hold_days - 1]
    if opn is not None and close is not None:
        port_ret = _basket_return_from_panels(opn, close, picks, entry_date, exit_date)
    else:
        port_ret = basket_return_h9(conn, picks, entry_date, hold_days=hold_days)
    if port_ret is None:
        return None
    bench_ret = bench_return_entry_to_exit(
        conn, entry_date, exit_date, entry_price_mode="open"
    )
    if bench_ret is None:
        return None
    dropped_mean_ret = None
    kept_mean_ret = None
    dropped_beat_bench_pct = None
    if not lightweight:
        dropped_legs = leg_returns_h9(conn, roe_dropped, entry_date, hold_days=hold_days)
        kept_legs = leg_returns_h9(conn, picks, entry_date, hold_days=hold_days)
        dropped_mean_ret = (
            round(sum(l["return_pct"] for l in dropped_legs) / len(dropped_legs), 4)
            if dropped_legs
            else None
        )
        kept_mean_ret = (
            round(sum(l["return_pct"] for l in kept_legs) / len(kept_legs), 4)
            if kept_legs
            else None
        )
        dropped_beat_bench_pct = (
            round(
                sum(1 for l in dropped_legs if l["beat_bench"]) / len(dropped_legs) * 100,
                2,
            )
            if dropped_legs
            else None
        )
    return {
        "signal_date": signal_date,
        "signal_month_end": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "year": entry_date[:4],
        "month": entry_date[:7],
        "n_stocks": len(picks),
        "return_pct": port_ret,
        "bench_return_pct": bench_ret,
        "beat_bench": port_ret > bench_ret,
        "gross_win": port_ret > 0,
        "excess_pct": port_ret - bench_ret,
        "layer_meta": layer_meta,
        "dropped_n": len(roe_dropped),
        "dropped_mean_ret": dropped_mean_ret,
        "kept_mean_ret": kept_mean_ret,
        "dropped_beat_bench_pct": dropped_beat_bench_pct,
    }


def run_s04_daily_periods(
    conn: sqlite3.Connection,
    spec: S04LayerSpec,
    *,
    hold_days: int = 9,
    window_start: str | None = None,
    window_end: str | None = None,
    mom_lookback: int = 60,
    min_mom_days: int | None = None,
) -> list[dict]:
    """每個交易日訊號 → T+1 開盤進 → H9 出（與月頻相同選股規則，訊號頻率不同）。"""
    lookback = mom_lookback if min_mom_days is None else min_mom_days
    close, opn, _vol = load_price_panels(conn)
    fin_hist = load_financial_history(conn)
    cal = [str(d) for d in close.index]
    if len(cal) <= lookback:
        return []
    stock_ids = list(close.columns.astype(str))
    signal_dates = cal[lookback:]
    roe_panel = build_roe_panel(fin_hist, stock_ids, signal_dates)
    periods: list[dict] = []

    for signal_date in signal_dates:
        entry_candidates = trading_dates_after(conn, signal_date, count=1)
        if not entry_candidates:
            continue
        entry_date = entry_candidates[0]
        if window_start and entry_date < window_start:
            continue
        if window_end and entry_date > window_end:
            continue

        fund_snap = fund_snap_from_roe_panel(roe_panel, signal_date, stock_ids)
        picks, layer_meta = select_s04_layer(
            spec,
            signal_date=signal_date,
            close=close,
            fund_snap=fund_snap,
            mom_lookback=mom_lookback,
        )
        if not picks:
            continue
        roe_dropped = layer_meta.get("dropped_by_roe") or []
        row = _append_s04_period(
            conn,
            signal_date=signal_date,
            entry_date=entry_date,
            picks=picks,
            layer_meta=layer_meta,
            hold_days=hold_days,
            roe_dropped=roe_dropped,
            opn=opn,
            close=close,
            lightweight=True,
        )
        if row:
            row["mom_lookback"] = mom_lookback
            periods.append(row)
    return periods


def summarize_by_year(periods: list[dict]) -> list[dict]:
    if not periods:
        return []
    rows: list[dict] = []
    for year, grp in pd.DataFrame(periods).groupby("year"):
        sub = grp.to_dict("records")
        s = summarize_periods(sub)
        rows.append({"year": year, **s})
    return rows


def summarize_by_month(
    periods: list[dict],
    *,
    month_start: str = "2025-01",
    month_end: str = "2026-12",
) -> list[dict]:
    """依進場月 (YYYY-MM) 彙總；每期通常對應一個 rebalance 月。"""
    if not periods:
        return []
    rows: list[dict] = []
    for p in periods:
        ym = p["entry_date"][:7]
        if ym < month_start or ym > month_end:
            continue
        rows.append(
            {
                "month": ym,
                "entry_date": p["entry_date"],
                "exit_date": p["exit_date"],
                "n_stocks": p["n_stocks"],
                "return_pct": p["return_pct"],
                "bench_return_pct": p["bench_return_pct"],
                "excess_pct": p["excess_pct"],
                "beat_bench": p["beat_bench"],
                "gross_win": p["gross_win"],
                "dropped_n": p.get("dropped_n", 0),
            }
        )
    rows.sort(key=lambda r: r["month"])
    return rows


def aggregate_monthly_stats(month_rows: list[dict]) -> list[dict]:
    """多筆/月時彙總（一般每月一筆）。"""
    if not month_rows:
        return []
    out: list[dict] = []
    df = pd.DataFrame(month_rows)
    for month, grp in df.groupby("month", sort=True):
        sub = grp.to_dict("records")
        s = summarize_periods(
            [
                {
                    "return_pct": r["return_pct"],
                    "bench_return_pct": r["bench_return_pct"],
                    "beat_bench": r["beat_bench"],
                    "gross_win": r["gross_win"],
                }
                for r in sub
            ]
        )
        out.append({"month": month, "n_periods": len(sub), **s})
    return out


def roe_filter_marginal_summary(periods: list[dict]) -> dict:
    """僅 s04_full 層：ROE 濾網剔除部位的邊際效果。"""
    with_drop = [p for p in periods if p.get("dropped_n", 0) > 0]
    if not with_drop:
        return {
            "n_periods_with_drops": 0,
            "avg_dropped_n": None,
            "dropped_mean_ret": None,
            "kept_mean_ret": None,
            "dropped_beat_bench_pct": None,
        }
    return {
        "n_periods_with_drops": len(with_drop),
        "avg_dropped_n": round(sum(p["dropped_n"] for p in with_drop) / len(with_drop), 2),
        "dropped_mean_ret": round(
            sum(p["dropped_mean_ret"] for p in with_drop if p["dropped_mean_ret"] is not None)
            / max(1, sum(1 for p in with_drop if p["dropped_mean_ret"] is not None)),
            4,
        ),
        "kept_mean_ret": round(
            sum(p["kept_mean_ret"] for p in with_drop if p["kept_mean_ret"] is not None)
            / max(1, sum(1 for p in with_drop if p["kept_mean_ret"] is not None)),
            4,
        ),
        "dropped_beat_bench_pct": round(
            sum(
                p["dropped_beat_bench_pct"]
                for p in with_drop
                if p["dropped_beat_bench_pct"] is not None
            )
            / max(1, sum(1 for p in with_drop if p["dropped_beat_bench_pct"] is not None)),
            2,
        ),
    }
