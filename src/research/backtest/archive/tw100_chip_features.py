"""TW100 PIT chip feature panels · month-end cross-section (Research layer)."""

from __future__ import annotations

import sqlite3
from typing import Iterable

import pandas as pd

CHIP_FEATURE_NAMES = (
    "foreign_net_20d",
    "trust_net_20d",
    "dealer_net_20d",
    "three_inst_net_20d",
    "foreign_side_net_20d",
    "trust_side_net_20d",
    "margin_chg_20d",
    "short_chg_20d",
    "lending_chg_20d",
)

_ROLL_WINDOW = 20
_SIDE_MAP = {
    "foreign_side_net_20d": "Foreign_Investor",
    "trust_side_net_20d": "Investment_Trust",
}
_INST_MAP = {
    "foreign_net_20d": "foreign_net",
    "trust_net_20d": "investment_trust_net",
    "dealer_net_20d": "dealer_self_net",
    "three_inst_net_20d": "three_institution_net",
}


def _pivot_daily(
    rows: list[sqlite3.Row],
    *,
    date_col: str,
    stock_col: str,
    value_col: str,
    stock_ids: list[str],
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=stock_ids)
    df = pd.DataFrame([dict(r) for r in rows])
    df[date_col] = df[date_col].astype(str)
    wide = df.pivot_table(
        index=date_col,
        columns=stock_col,
        values=value_col,
        aggfunc="last",
    )
    wide = wide.reindex(columns=stock_ids)
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def _rolling_sum_at_month_ends(
    daily: pd.DataFrame,
    month_ends: list[str],
    window: int = _ROLL_WINDOW,
) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(index=month_ends, columns=daily.columns, dtype=float)
    rolled = daily.rolling(window=window, min_periods=max(5, window // 2)).sum()
    me_idx = pd.to_datetime(month_ends)
    out = rolled.reindex(me_idx, method="ffill")
    out.index = month_ends
    return out


def _rolling_mean_at_month_ends(
    daily: pd.DataFrame,
    month_ends: list[str],
    window: int = _ROLL_WINDOW,
) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(index=month_ends, columns=daily.columns, dtype=float)
    rolled = daily.rolling(window=window, min_periods=max(5, window // 2)).mean()
    me_idx = pd.to_datetime(month_ends)
    out = rolled.reindex(me_idx, method="ffill")
    out.index = month_ends
    return out


def load_institutional_net_panels(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str,
) -> dict[str, pd.DataFrame]:
    if not stock_ids:
        return {}
    ph = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, foreign_net, investment_trust_net,
               dealer_self_net, three_institution_net
        FROM stock_institutional_daily
        WHERE stock_id IN ({ph})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        ORDER BY trade_date
        """,
        [*stock_ids, start, end],
    ).fetchall()
    out: dict[str, pd.DataFrame] = {}
    for feat, col in _INST_MAP.items():
        out[feat] = _pivot_daily(rows, date_col="trade_date", stock_col="stock_id", value_col=col, stock_ids=stock_ids)
    return out


def load_institutional_side_net_panels(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str,
) -> dict[str, pd.DataFrame]:
    if not stock_ids:
        return {}
    ph = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, inst_name, net
        FROM stock_institutional_side_daily
        WHERE stock_id IN ({ph})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
          AND inst_name IN ('Foreign_Investor', 'Investment_Trust')
        ORDER BY trade_date
        """,
        [*stock_ids, start, end],
    ).fetchall()
    out: dict[str, pd.DataFrame] = {}
    if not rows:
        return out
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = df["trade_date"].astype(str)
    for feat, inst in _SIDE_MAP.items():
        sub = df[df["inst_name"] == inst]
        if sub.empty:
            out[feat] = pd.DataFrame(columns=stock_ids)
            continue
        wide = sub.pivot_table(index="trade_date", columns="stock_id", values="net", aggfunc="last")
        wide = wide.reindex(columns=stock_ids)
        wide.index = pd.to_datetime(wide.index)
        out[feat] = wide.sort_index()
    return out


def load_margin_panels(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str,
) -> dict[str, pd.DataFrame]:
    if not stock_ids:
        return {}
    ph = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, margin_change, short_change
        FROM stock_margin_daily
        WHERE stock_id IN ({ph})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        [*stock_ids, start, end],
    ).fetchall()
    return {
        "margin_chg_20d": _pivot_daily(
            rows, date_col="trade_date", stock_col="stock_id", value_col="margin_change", stock_ids=stock_ids
        ),
        "short_chg_20d": _pivot_daily(
            rows, date_col="trade_date", stock_col="stock_id", value_col="short_change", stock_ids=stock_ids
        ),
    }


def load_lending_panel(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, lending_change
        FROM stock_lending_daily
        WHERE stock_id IN ({ph})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        [*stock_ids, start, end],
    ).fetchall()
    return _pivot_daily(
        rows, date_col="trade_date", stock_col="stock_id", value_col="lending_change", stock_ids=stock_ids
    )


def load_daytrade_panel(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, daytrade_ratio_pct
        FROM stock_daytrade_daily
        WHERE stock_id IN ({ph})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        [*stock_ids, start, end],
    ).fetchall()
    return _pivot_daily(
        rows,
        date_col="trade_date",
        stock_col="stock_id",
        value_col="daytrade_ratio_pct",
        stock_ids=stock_ids,
    )


def build_chip_feature_panels(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    month_ends: Iterable[str],
    *,
    start: str,
    end: str,
) -> dict[str, pd.DataFrame]:
    """PIT chip panels at month-end · 20d rolling sums/means."""
    dates = sorted(set(month_ends))
    panels: dict[str, pd.DataFrame] = {
        name: pd.DataFrame(index=dates, columns=stock_ids, dtype=float) for name in CHIP_FEATURE_NAMES
    }

    inst = load_institutional_net_panels(conn, stock_ids, start=start, end=end)
    for feat in ("foreign_net_20d", "trust_net_20d", "dealer_net_20d", "three_inst_net_20d"):
        rolled = _rolling_sum_at_month_ends(inst[feat], dates)
        panels[feat] = rolled

    side = load_institutional_side_net_panels(conn, stock_ids, start=start, end=end)
    for feat in ("foreign_side_net_20d", "trust_side_net_20d"):
        rolled = _rolling_sum_at_month_ends(side.get(feat, pd.DataFrame()), dates)
        panels[feat] = rolled

    margin = load_margin_panels(conn, stock_ids, start=start, end=end)
    panels["margin_chg_20d"] = _rolling_sum_at_month_ends(margin["margin_chg_20d"], dates)
    panels["short_chg_20d"] = _rolling_sum_at_month_ends(margin["short_chg_20d"], dates)

    lending = load_lending_panel(conn, stock_ids, start=start, end=end)
    panels["lending_chg_20d"] = _rolling_sum_at_month_ends(lending, dates)

    return panels


def chip_coverage_report(
    panels: dict[str, pd.DataFrame],
    month_ends: list[str],
    *,
    min_cross_section: int = 30,
) -> dict[str, object]:
    dates = [d for d in month_ends if d in next(iter(panels.values())).index]
    per_date: list[dict[str, object]] = []
    for dt in dates:
        counts = {
            name: int(panels[name].loc[dt].notna().sum()) if dt in panels[name].index else 0
            for name in CHIP_FEATURE_NAMES
        }
        per_date.append({"date": dt, **counts, "eligible": min(counts.values()) >= min_cross_section})
    eligible_n = sum(1 for row in per_date if row["eligible"])
    min_cov = min(
        (min(int(row[name]) for name in CHIP_FEATURE_NAMES) for row in per_date),
        default=0,
    )
    return {
        "n_month_ends": len(dates),
        "eligible_month_ends": eligible_n,
        "min_feature_coverage": min_cov,
        "sample": per_date[-3:] if per_date else [],
    }
