"""FinMind strategy marketplace · shared screener data + PIT helpers."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd

from research.backtest.finpilot_local_backtest import (
    load_financial_history,
    load_fundamental_snapshot,
    load_price_panels,
    pit_fundamental_at,
)
from screener_universe import resolve_sync_watchlist
from stock_db import connect, DEFAULT_DB_PATH

TOPIC_LOW_REBOUND = "finmind-low-rebound-foreign-it-sync"
FOREIGN_BUY_MIN = 500.0
IT_BUY_MIN = 200.0
INST_LOOKBACK_DAYS = 10
DIVIDEND_MIN_YEARS = 5
DIVIDEND_MIN_CASH = 1.0
ROE_MIN_PCT = 10.0


@dataclass(frozen=True)
class LowReboundParams:
    foreign_buy_min: float = FOREIGN_BUY_MIN
    it_buy_min: float = IT_BUY_MIN
    inst_lookback_days: int = INST_LOOKBACK_DAYS
    dividend_min_years: int = DIVIDEND_MIN_YEARS
    dividend_min_cash: float = DIVIDEND_MIN_CASH
    roe_min_pct: float = ROE_MIN_PCT


def resolve_universe_stock_ids(
    conn: sqlite3.Connection,
    universe: str,
) -> list[str]:
    rows = resolve_sync_watchlist(conn, universe)
    return sorted({str(r["stock_id"]) for r in rows})


def load_institutional_long(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, foreign_net, investment_trust_net,
               three_institution_net, dealer_self_net
        FROM stock_institutional_daily
        WHERE source = 'finmind'
        ORDER BY stock_id, trade_date
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "stock_id",
                "trade_date",
                "foreign_net",
                "investment_trust_net",
                "three_institution_net",
            ]
        )
    return pd.DataFrame(
        rows,
        columns=[
            "stock_id",
            "trade_date",
            "foreign_net",
            "investment_trust_net",
            "three_institution_net",
            "dealer_self_net",
        ],
    )


def load_technical_long(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, kd_cross_above_20, return_5d_pct,
               return_1d_pct, ma20
        FROM stock_technical_daily
        WHERE source = 'computed'
        ORDER BY stock_id, trade_date
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "stock_id",
                "trade_date",
                "kd_cross_above_20",
                "return_5d_pct",
            ]
        )
    return pd.DataFrame(
        rows,
        columns=[
            "stock_id",
            "trade_date",
            "kd_cross_above_20",
            "return_5d_pct",
            "return_1d_pct",
            "ma20",
        ],
    )


def load_dividend_long(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, fiscal_year, ex_cash_date, cash_dividend
        FROM stock_dividend_history
        WHERE source = 'finmind'
        ORDER BY stock_id, fiscal_year
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["stock_id", "fiscal_year", "ex_cash_date", "cash_dividend"]
        )
    return pd.DataFrame(
        rows, columns=["stock_id", "fiscal_year", "ex_cash_date", "cash_dividend"]
    )


def _parse_fiscal_year(raw: object) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s[:4].isdigit():
        return int(s[:4])
    m = re.match(r"^(\d{2,3})年", s)
    if m:
        return int(m.group(1)) + 1911
    return None


def has_consecutive_cash_dividend(
    div_rows: pd.DataFrame,
    stock_id: str,
    as_of: str,
    *,
    min_years: int = DIVIDEND_MIN_YEARS,
    min_cash: float = DIVIDEND_MIN_CASH,
) -> bool:
    sub = div_rows[
        (div_rows["stock_id"] == stock_id)
        & (div_rows["ex_cash_date"] <= as_of)
        & (div_rows["cash_dividend"].fillna(0) >= min_cash)
    ]
    if sub.empty:
        return False
    years = sorted(
        {
            y
            for y in (_parse_fiscal_year(v) for v in sub["fiscal_year"])
            if y is not None
        }
    )
    if len(years) < min_years:
        return False
    best = cur = 1
    for i in range(1, len(years)):
        if years[i] == years[i - 1] + 1:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best >= min_years


def inst_buy_in_window(
    inst: pd.DataFrame,
    stock_id: str,
    signal_date: str,
    cal: list[str],
    field: str,
    threshold: float,
    lookback_days: int,
) -> bool:
    if signal_date not in cal:
        return False
    idx = cal.index(signal_date)
    start = max(0, idx - lookback_days + 1)
    window = cal[start : idx + 1]
    sub = inst[(inst["stock_id"] == stock_id) & (inst["trade_date"].isin(window))]
    if sub.empty:
        return False
    vals = sub[field].dropna()
    return bool((vals > threshold).any())


def kd_cross_above_20(
    tech: pd.DataFrame,
    stock_id: str,
    signal_date: str,
) -> bool:
    row = tech[(tech["stock_id"] == stock_id) & (tech["trade_date"] == signal_date)]
    if row.empty:
        return False
    return int(row.iloc[-1]["kd_cross_above_20"] or 0) == 1


def roe_at_least(
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    stock_id: str,
    signal_date: str,
    min_pct: float,
) -> bool:
    snap = pit_fundamental_at(fund, fin_hist, [stock_id], signal_date).get(stock_id, {})
    roe = snap.get("roe_latest_q")
    if roe is None:
        return False
    return float(roe) >= min_pct


def _build_inst_exceed_matrix(
    inst: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    field: str,
    threshold: float,
    lookback_days: int,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    sub = inst[inst["stock_id"].isin(stock_ids) & inst["trade_date"].isin(cal)]
    if sub.empty:
        return pd.DataFrame(False, index=cal, columns=stock_ids)
    pivot = sub.pivot_table(index="trade_date", columns="stock_id", values=field, aggfunc="last")
    pivot = pivot.reindex(index=cal, columns=stock_ids)
    exceed = pivot.gt(threshold)
    return exceed.rolling(lookback_days, min_periods=1).max().fillna(False).astype(bool)


def _build_kd_matrix(
    tech: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    sub = tech[tech["stock_id"].isin(stock_ids) & tech["trade_date"].isin(cal)]
    if sub.empty:
        return pd.DataFrame(False, index=cal, columns=stock_ids)
    pivot = sub.pivot_table(
        index="trade_date", columns="stock_id", values="kd_cross_above_20", aggfunc="last"
    )
    pivot = pivot.reindex(index=cal, columns=stock_ids).fillna(0)
    return pivot.eq(1)


def _longest_consecutive_years(years: list[int]) -> int:
    if not years:
        return 0
    best = cur = 1
    for i in range(1, len(years)):
        if years[i] == years[i - 1] + 1:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


def _build_dividend_matrix(
    div: pd.DataFrame,
    stock_ids: list[str],
    cal: list[str],
    *,
    min_years: int,
    min_cash: float,
) -> pd.DataFrame:
    if not cal or not stock_ids:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=bool)
    cols: dict[str, list[bool]] = {}
    for sid in stock_ids:
        sub = div[(div["stock_id"] == sid) & (div["cash_dividend"].fillna(0) >= min_cash)]
        events: list[tuple[str, int]] = []
        for _, row in sub.iterrows():
            yr = _parse_fiscal_year(row["fiscal_year"])
            if yr is not None:
                events.append((str(row["ex_cash_date"]), yr))
        events.sort()
        flags: list[bool] = []
        for d in cal:
            years = sorted({y for ex, y in events if ex <= d})
            flags.append(_longest_consecutive_years(years) >= min_years)
        cols[sid] = flags
    return pd.DataFrame(cols, index=cal)


def _roe_change_points(
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    stock_id: str,
) -> list[tuple[str, float | None]]:
    """Sorted (as_of_date, roe) steps for forward-fill on trading calendar."""
    points: dict[str, float] = {}
    if not fin_hist.empty:
        q = fin_hist[
            (fin_hist["stock_id"] == stock_id) & (fin_hist["period_type"] == "quarter")
        ]
        for pd_date in sorted(q["period_date"].unique()):
            chunk = q[q["period_date"] == pd_date]
            ni = chunk.loc[chunk["metric"] == "net_income", "value"]
            eq = chunk.loc[chunk["metric"] == "equity", "value"]
            if ni.empty or eq.empty:
                continue
            equity = float(eq.iloc[0])
            if equity <= 0:
                continue
            points[str(pd_date)] = float(ni.iloc[0]) / equity * 100.0
    if not fund.empty:
        sub = fund[fund["stock_id"] == stock_id].sort_values("as_of_date")
        for _, row in sub.iterrows():
            val = row["roe_latest_q"]
            if val is not None:
                points[str(row["as_of_date"])] = float(val)
    if not points:
        return []
    out: list[tuple[str, float | None]] = []
    prev: float | None = None
    for d in sorted(points):
        roe = points[d]
        if roe != prev:
            out.append((d, roe))
            prev = roe
    return out


def _build_roe_matrix(
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
        steps = _roe_change_points(fund, fin_hist, sid)
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


@dataclass(frozen=True)
class LowReboundScreenMatrices:
    foreign: pd.DataFrame
    investment_trust: pd.DataFrame
    dividend: pd.DataFrame
    roe: pd.DataFrame
    kd: pd.DataFrame
    three_institution_net: pd.DataFrame
    return_5d_pct: pd.DataFrame


def build_low_rebound_screen_matrices(
    *,
    stock_ids: list[str],
    cal: list[str],
    inst: pd.DataFrame,
    tech: pd.DataFrame,
    div: pd.DataFrame,
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    params: LowReboundParams | None = None,
) -> LowReboundScreenMatrices:
    p = params or LowReboundParams()
    foreign = _build_inst_exceed_matrix(
        inst, stock_ids, cal, "foreign_net", p.foreign_buy_min, p.inst_lookback_days
    )
    it = _build_inst_exceed_matrix(
        inst,
        stock_ids,
        cal,
        "investment_trust_net",
        p.it_buy_min,
        p.inst_lookback_days,
    )
    dividend = _build_dividend_matrix(
        div, stock_ids, cal, min_years=p.dividend_min_years, min_cash=p.dividend_min_cash
    )
    roe = _build_roe_matrix(fund, fin_hist, stock_ids, cal, p.roe_min_pct)
    kd = _build_kd_matrix(tech, stock_ids, cal)

    inst_sub = inst[inst["stock_id"].isin(stock_ids) & inst["trade_date"].isin(cal)]
    three = inst_sub.pivot_table(
        index="trade_date", columns="stock_id", values="three_institution_net", aggfunc="last"
    ).reindex(index=cal, columns=stock_ids)
    tech_sub = tech[tech["stock_id"].isin(stock_ids) & tech["trade_date"].isin(cal)]
    ret5 = tech_sub.pivot_table(
        index="trade_date", columns="stock_id", values="return_5d_pct", aggfunc="last"
    ).reindex(index=cal, columns=stock_ids)

    return LowReboundScreenMatrices(
        foreign=foreign,
        investment_trust=it,
        dividend=dividend,
        roe=roe,
        kd=kd,
        three_institution_net=three,
        return_5d_pct=ret5,
    )


def count_condition_passes(
    conn: sqlite3.Connection,
    universe: str,
    date_start: str,
    date_end: str,
    *,
    params: LowReboundParams | None = None,
) -> dict[str, Any]:
    """Phase 0 · per-condition and full-screen counts."""
    p = params or LowReboundParams()
    stock_ids = set(resolve_universe_stock_ids(conn, universe))
    close, _, _ = load_price_panels(conn)
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    inst = load_institutional_long(conn)
    tech = load_technical_long(conn)
    div = load_dividend_long(conn)
    fund = load_fundamental_snapshot(conn)
    fin_hist = load_financial_history(conn)

    inst = inst[inst["stock_id"].isin(stock_ids)]
    tech = tech[tech["stock_id"].isin(stock_ids)]
    div = div[div["stock_id"].isin(stock_ids)]
    stock_list = sorted(stock_ids)
    price_ok = [s for s in stock_list if s in close.columns]

    matrices = build_low_rebound_screen_matrices(
        stock_ids=stock_list,
        cal=cal,
        inst=inst,
        tech=tech,
        div=div,
        fund=fund,
        fin_hist=fin_hist,
        params=p,
    )

    counts = {
        "e1_foreign_buy": 0,
        "e2_it_buy": 0,
        "e3_dividend_5y": 0,
        "e4_roe": 0,
        "e5_kd_cross": 0,
        "full_screen": 0,
    }
    by_year: dict[str, int] = {}

    for d in cal:
        e1 = matrices.foreign.loc[d, price_ok]
        e2 = matrices.investment_trust.loc[d, price_ok]
        e3 = matrices.dividend.loc[d, price_ok]
        e4 = matrices.roe.loc[d, price_ok]
        e5 = matrices.kd.loc[d, price_ok]
        counts["e1_foreign_buy"] += int(e1.sum())
        counts["e2_it_buy"] += int(e2.sum())
        counts["e3_dividend_5y"] += int(e3.sum())
        counts["e4_roe"] += int(e4.sum())
        counts["e5_kd_cross"] += int(e5.sum())
        full = e1 & e2 & e3 & e4 & e5
        day_full = int(full.sum())
        counts["full_screen"] += day_full
        if day_full:
            by_year[d[:4]] = by_year.get(d[:4], 0) + day_full

    table_rows = conn.execute(
        """
        SELECT 'institutional' AS tbl, COUNT(DISTINCT stock_id) AS stocks,
               MIN(trade_date) AS min_d, MAX(trade_date) AS max_d
        FROM stock_institutional_daily
        UNION ALL
        SELECT 'technical', COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date)
        FROM stock_technical_daily
        UNION ALL
        SELECT 'dividend', COUNT(DISTINCT stock_id), MIN(ex_cash_date), MAX(ex_cash_date)
        FROM stock_dividend_history
        UNION ALL
        SELECT 'fundamental', COUNT(DISTINCT stock_id), MIN(as_of_date), MAX(as_of_date)
        FROM stock_fundamental
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
            {
                "table": r[0],
                "stocks": r[1],
                "min_date": r[2],
                "max_date": r[3],
            }
            for r in table_rows
        ],
        "phase0_pass": counts["full_screen"] >= 50,
    }


def validate_low_rebound_data(
    db_path: str | Path | None = None,
    *,
    universe: str = "tw100",
    date_start: str = "2015-01-01",
    date_end: str | None = None,
) -> dict[str, Any]:
    from datetime import date
    from pathlib import Path

    end = date_end or date.today().isoformat()
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = connect(path)
    try:
        return count_condition_passes(conn, universe, date_start, end)
    finally:
        conn.close()
