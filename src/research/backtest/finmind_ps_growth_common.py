"""FinMind marketplace · 低市銷率成長股 · shared PIT helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import (
    load_financial_history,
    load_fundamental_snapshot,
    load_price_panels,
)
from screener_universe import resolve_sync_watchlist
from stock_db import DEFAULT_DB_PATH, connect

TOPIC_PS_GROWTH = "finmind-low-ps-growth"
PS_RATIO_MAX = 5.0
PS_STORED_MAX = PS_RATIO_MAX * 100.0
REV_YOY_MIN_PCT = 50.0
MARGIN_MIN_PCT = 5.0
CAPITAL_MIN_NTD = 1_000_000_000.0
TURNOVER_MIN_NTD = 20_000_000.0
MARGIN_QUARTERS = 2
DEFAULT_DATE_START = "2020-12-01"


@dataclass(frozen=True)
class PsGrowthParams:
    ps_ratio_max: float = PS_RATIO_MAX
    rev_yoy_min_pct: float = REV_YOY_MIN_PCT
    margin_min_pct: float = MARGIN_MIN_PCT
    capital_min_ntd: float = CAPITAL_MIN_NTD
    turnover_min_ntd: float = TURNOVER_MIN_NTD
    margin_quarters: int = MARGIN_QUARTERS
    require_market_ma5: bool = True
    h3_use_net_margin_approx: bool = True


def resolve_universe_stock_ids(conn: sqlite3.Connection, universe: str) -> list[str]:
    rows = resolve_sync_watchlist(conn, universe)
    return sorted({str(r["stock_id"]) for r in rows})


def load_market_value_long(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, market_value_ntd, mcap_to_revenue_ttm
        FROM stock_market_value_daily
        WHERE source = 'finmind'
        ORDER BY stock_id, trade_date
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["stock_id", "trade_date", "market_value_ntd", "mcap_to_revenue_ttm"]
        )
    return pd.DataFrame(
        rows,
        columns=["stock_id", "trade_date", "market_value_ntd", "mcap_to_revenue_ttm"],
    )


def load_shareholding_long(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, capital_ntd
        FROM stock_shareholding_daily
        WHERE source = 'finmind'
        ORDER BY stock_id, trade_date
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["stock_id", "trade_date", "capital_ntd"])
    return pd.DataFrame(rows, columns=["stock_id", "trade_date", "capital_ntd"])


def ps_ratio_from_stored(stored: float | None) -> float | None:
    if stored is None:
        return None
    return float(stored) / 100.0


def _quarter_margin_at(
    fin_hist: pd.DataFrame,
    stock_id: str,
    period_date: str,
    *,
    allow_net_approx: bool,
) -> float | None:
    chunk = fin_hist[
        (fin_hist["stock_id"] == stock_id)
        & (fin_hist["period_type"] == "quarter")
        & (fin_hist["period_date"] == period_date)
    ]
    if chunk.empty:
        return None
    rev = chunk.loc[chunk["metric"] == "revenue", "value"]
    if rev.empty:
        return None
    revenue = float(rev.iloc[-1])
    if revenue <= 0:
        return None
    oi = chunk.loc[chunk["metric"] == "operating_income", "value"]
    if not oi.empty:
        return float(oi.iloc[-1]) / revenue * 100.0
    if not allow_net_approx:
        return None
    ni = chunk.loc[chunk["metric"] == "net_income", "value"]
    if ni.empty:
        return None
    return float(ni.iloc[-1]) / revenue * 100.0


def _precompute_quarter_margins(
    fin_hist: pd.DataFrame,
    stock_id: str,
    *,
    allow_net_approx: bool,
) -> list[tuple[str, float]]:
    q = fin_hist[
        (fin_hist["stock_id"] == stock_id) & (fin_hist["period_type"] == "quarter")
    ]
    out: list[tuple[str, float]] = []
    for pd_date in sorted(q["period_date"].unique()):
        m = _quarter_margin_at(
            fin_hist, stock_id, str(pd_date), allow_net_approx=allow_net_approx
        )
        if m is not None:
            out.append((str(pd_date), m))
    return out


def _margin_pass_at(
    margins: list[tuple[str, float]],
    as_of: str,
    *,
    min_pct: float,
    n_quarters: int,
) -> bool:
    vals = [m for d, m in margins if d <= as_of]
    if len(vals) < n_quarters:
        return False
    return all(m >= min_pct for m in vals[-n_quarters:])


def has_consecutive_quarter_margins(
    fin_hist: pd.DataFrame,
    stock_id: str,
    as_of: str,
    *,
    min_pct: float,
    n_quarters: int,
    allow_net_approx: bool,
) -> bool:
    margins = _precompute_quarter_margins(
        fin_hist, stock_id, allow_net_approx=allow_net_approx
    )
    return _margin_pass_at(
        margins, as_of, min_pct=min_pct, n_quarters=n_quarters
    )


def _build_margin_matrix(
    fin_hist: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    params: PsGrowthParams,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    cols: dict[str, list[bool]] = {}
    for sid in stock_ids:
        margins = _precompute_quarter_margins(
            fin_hist, sid, allow_net_approx=params.h3_use_net_margin_approx
        )
        cols[sid] = [
            _margin_pass_at(
                margins,
                d,
                min_pct=params.margin_min_pct,
                n_quarters=params.margin_quarters,
            )
            for d in cal
        ]
    return pd.DataFrame(cols, index=cal)


def build_ix_above_ma5_series(conn: sqlite3.Connection, cal: list[str]) -> pd.Series:
    bench = load_benchmark_close(conn)
    if bench.empty or not cal:
        return pd.Series(False, index=cal)
    sub = bench.reindex(cal).astype(float)
    ma5 = sub.rolling(5, min_periods=5).mean()
    flags = (sub > ma5).fillna(False)
    return flags.astype(bool)


def _build_ps_matrix(
    mv: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    ps_max: float,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    sub = mv[mv["stock_id"].isin(stock_ids) & mv["trade_date"].isin(cal)]
    if sub.empty:
        return pd.DataFrame(False, index=cal, columns=stock_ids)
    sub = sub.copy()
    sub["ps_ratio"] = sub["mcap_to_revenue_ttm"].apply(ps_ratio_from_stored)
    pivot = sub.pivot_table(index="trade_date", columns="stock_id", values="ps_ratio", aggfunc="last")
    pivot = pivot.reindex(index=cal, columns=stock_ids)
    return pivot.lt(ps_max).fillna(False).astype(bool)


def _build_capital_matrix(
    sh: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    min_cap: float,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    sub = sh[sh["stock_id"].isin(stock_ids) & sh["trade_date"].isin(cal)]
    if sub.empty:
        return pd.DataFrame(False, index=cal, columns=stock_ids)
    pivot = sub.pivot_table(index="trade_date", columns="stock_id", values="capital_ntd", aggfunc="last")
    pivot = pivot.reindex(index=cal, columns=stock_ids)
    return pivot.ge(min_cap).fillna(False).astype(bool)


def _build_turnover_matrix(
    close: pd.DataFrame,
    vol: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    min_turnover: float,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    cols = [s for s in stock_ids if s in close.columns and s in vol.columns]
    if not cols:
        return pd.DataFrame(False, index=cal, columns=stock_ids)
    turnover = close[cols].multiply(vol[cols])
    turnover = turnover.reindex(cal)
    rolled = turnover.rolling(5, min_periods=5).mean()
    flags = rolled.ge(min_turnover).reindex(columns=stock_ids).fillna(False)
    return flags.astype(bool)


def _rev_yoy_change_points(
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    stock_id: str,
) -> list[tuple[str, float]]:
    points: dict[str, float] = {}
    if not fund.empty:
        sub = fund[fund["stock_id"] == stock_id].sort_values("as_of_date")
        for _, row in sub.iterrows():
            val = row["revenue_yoy_pct"]
            if val is not None:
                points[str(row["as_of_date"])] = float(val)

    m = fin_hist[
        (fin_hist["stock_id"] == stock_id)
        & (fin_hist["period_type"] == "month")
        & (fin_hist["metric"] == "revenue")
    ].sort_values("period_date")
    rev_by_month = {str(r["period_date"]): float(r["value"]) for _, r in m.iterrows()}
    for pd_date in sorted(rev_by_month):
        y, mo = int(pd_date[:4]), int(pd_date[5:7])
        prior_dates = [d for d in rev_by_month if d.startswith(f"{y - 1}-{mo:02d}")]
        if not prior_dates:
            continue
        prior_rev = rev_by_month[sorted(prior_dates)[-1]]
        cur_rev = rev_by_month[pd_date]
        if prior_rev <= 0:
            continue
        points[pd_date] = (cur_rev - prior_rev) / prior_rev * 100.0

    if not points:
        return []
    out: list[tuple[str, float]] = []
    prev: float | None = None
    for d in sorted(points):
        y = points[d]
        if y != prev:
            out.append((d, y))
            prev = y
    return out


def _build_rev_yoy_matrix(
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    min_pct: float,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    cols: dict[str, list[bool]] = {}
    for sid in stock_ids:
        steps = _rev_yoy_change_points(fund, fin_hist, sid)
        flags: list[bool] = []
        idx = 0
        current: float | None = None
        for d in cal:
            while idx < len(steps) and steps[idx][0] <= d:
                current = steps[idx][1]
                idx += 1
            flags.append(current is not None and current >= min_pct)
        cols[sid] = flags
    return pd.DataFrame(cols, index=cal)


def _build_rev_yoy_val_matrix(
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
) -> pd.DataFrame:
    cols: dict[str, list[float | None]] = {}
    for sid in stock_ids:
        steps = _rev_yoy_change_points(fund, fin_hist, sid)
        vals: list[float | None] = []
        idx = 0
        current: float | None = None
        for d in cal:
            while idx < len(steps) and steps[idx][0] <= d:
                current = steps[idx][1]
                idx += 1
            vals.append(current)
        cols[sid] = vals
    return pd.DataFrame(cols, index=cal)


@dataclass(frozen=True)
class PsGrowthScreenMatrices:
    ps_low: pd.DataFrame
    rev_yoy: pd.DataFrame
    margin_2q: pd.DataFrame
    capital: pd.DataFrame
    turnover: pd.DataFrame
    market_ma5: pd.Series
    ps_ratio: pd.DataFrame
    rev_yoy_val: pd.DataFrame


def build_ps_growth_screen_matrices(
    *,
    conn: sqlite3.Connection,
    stock_ids: list[str],
    cal: list[str],
    close: pd.DataFrame,
    vol: pd.DataFrame,
    mv: pd.DataFrame,
    sh: pd.DataFrame,
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    params: PsGrowthParams | None = None,
) -> PsGrowthScreenMatrices:
    p = params or PsGrowthParams()
    ps_low = _build_ps_matrix(mv, stock_ids, cal, p.ps_ratio_max)
    rev_yoy = _build_rev_yoy_matrix(fund, fin_hist, stock_ids, cal, p.rev_yoy_min_pct)
    margin_2q = _build_margin_matrix(fin_hist, stock_ids, cal, p)
    capital = _build_capital_matrix(sh, stock_ids, cal, p.capital_min_ntd)
    turnover = _build_turnover_matrix(close, vol, stock_ids, cal, p.turnover_min_ntd)
    market_ma5 = build_ix_above_ma5_series(conn, cal)

    sub = mv[mv["stock_id"].isin(stock_ids) & mv["trade_date"].isin(cal)].copy()
    sub["ps_ratio"] = sub["mcap_to_revenue_ttm"].apply(ps_ratio_from_stored)
    ps_ratio = sub.pivot_table(index="trade_date", columns="stock_id", values="ps_ratio", aggfunc="last")
    ps_ratio = ps_ratio.reindex(index=cal, columns=stock_ids)

    rev_yoy_val = _build_rev_yoy_val_matrix(fund, fin_hist, stock_ids, cal)

    return PsGrowthScreenMatrices(
        ps_low=ps_low,
        rev_yoy=rev_yoy,
        margin_2q=margin_2q,
        capital=capital,
        turnover=turnover,
        market_ma5=market_ma5,
        ps_ratio=ps_ratio,
        rev_yoy_val=rev_yoy_val,
    )


def count_condition_passes(
    conn: sqlite3.Connection,
    universe: str,
    date_start: str,
    date_end: str,
    *,
    params: PsGrowthParams | None = None,
) -> dict[str, Any]:
    p = params or PsGrowthParams()
    stock_ids = resolve_universe_stock_ids(conn, universe)
    close, _, vol = load_price_panels(conn)
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    mv = load_market_value_long(conn)
    sh = load_shareholding_long(conn)
    fund = load_fundamental_snapshot(conn)
    fin_hist = load_financial_history(conn)

    matrices = build_ps_growth_screen_matrices(
        conn=conn,
        stock_ids=stock_ids,
        cal=cal,
        close=close,
        vol=vol,
        mv=mv,
        sh=sh,
        fund=fund,
        fin_hist=fin_hist,
        params=p,
    )
    price_ok = [s for s in stock_ids if s in close.columns]

    counts = {
        "h1_ps_low": 0,
        "h2_rev_yoy": 0,
        "h3_margin_2q": 0,
        "h4_capital": 0,
        "h5_turnover": 0,
        "m1_ix_ma5": 0,
        "full_screen": 0,
    }
    by_year: dict[str, int] = {}

    for d in cal:
        m1 = bool(matrices.market_ma5.loc[d]) if p.require_market_ma5 else True
        if p.require_market_ma5:
            counts["m1_ix_ma5"] += int(m1)
        if not m1:
            continue
        h1 = matrices.ps_low.loc[d, price_ok]
        h2 = matrices.rev_yoy.loc[d, price_ok]
        h3 = matrices.margin_2q.loc[d, price_ok]
        h4 = matrices.capital.loc[d, price_ok]
        h5 = matrices.turnover.loc[d, price_ok]
        counts["h1_ps_low"] += int(h1.sum())
        counts["h2_rev_yoy"] += int(h2.sum())
        counts["h3_margin_2q"] += int(h3.sum())
        counts["h4_capital"] += int(h4.sum())
        counts["h5_turnover"] += int(h5.sum())
        full = h1 & h2 & h3 & h4 & h5
        day_full = int(full.sum())
        counts["full_screen"] += day_full
        if day_full:
            by_year[d[:4]] = by_year.get(d[:4], 0) + day_full

    table_rows = conn.execute(
        """
        SELECT 'market_value' AS tbl, COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date)
        FROM stock_market_value_daily
        UNION ALL
        SELECT 'shareholding', COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date)
        FROM stock_shareholding_daily
        UNION ALL
        SELECT 'fundamental', COUNT(DISTINCT stock_id), MIN(as_of_date), MAX(as_of_date)
        FROM stock_fundamental
        UNION ALL
        SELECT 'financial_history', COUNT(DISTINCT stock_id), MIN(period_date), MAX(period_date)
        FROM stock_financial_history
        """
    ).fetchall()

    return {
        "universe": universe,
        "universe_size": len(stock_ids),
        "date_start": date_start,
        "date_end": date_end,
        "trading_days": len(cal),
        "condition_hits": counts,
        "full_screen_by_year": dict(sorted(by_year.items())),
        "table_coverage": [
            {"table": r[0], "stocks": r[1], "min_date": r[2], "max_date": r[3]}
            for r in table_rows
        ],
        "approx_notes": {
            "h3_margin": "net_income/revenue when operating_income missing",
            "m_filter": "M1 IX0001>MA5 only (v1-faithful-ix)",
            "ps_field": "mcap_to_revenue_ttm / 100",
        },
        "phase0_pass": counts["full_screen"] >= 30,
    }


def validate_ps_growth_data(
    db_path: str | Path | None = None,
    *,
    universe: str = "tw100",
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
) -> dict[str, Any]:
    from datetime import date as dt

    end = date_end or dt.today().isoformat()
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = connect(path)
    try:
        return count_condition_passes(conn, universe, date_start, end)
    finally:
        conn.close()
