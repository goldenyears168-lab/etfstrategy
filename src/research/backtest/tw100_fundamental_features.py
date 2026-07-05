"""TW100 PIT fundamental feature panels · month-end cross-section (Research layer)."""

from __future__ import annotations

import sqlite3
from typing import Iterable

import pandas as pd

FUNDAMENTAL_FEATURE_NAMES = (
    "ep_ttm",
    "bp",
    "dividend_yield",
    "roe_ttm",
    "revenue_yoy_pct",
)

_PE_BOUNDS = (0.5, 200.0)
_PB_BOUNDS = (0.05, 20.0)


def _clip_ratio(value: float | None, bounds: tuple[float, float]) -> float | None:
    if value is None or pd.isna(value):
        return None
    v = float(value)
    if v <= 0:
        return None
    lo, hi = bounds
    v = max(lo, min(hi, v))
    return v


def earnings_yield(pe: float | None) -> float | None:
    pe_c = _clip_ratio(pe, _PE_BOUNDS)
    if pe_c is None:
        return None
    return 1.0 / pe_c


def book_to_price(pb: float | None) -> float | None:
    pb_c = _clip_ratio(pb, _PB_BOUNDS)
    if pb_c is None:
        return None
    return 1.0 / pb_c


def load_fundamental_history_rows(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, as_of_date, pe, pb, dividend_yield, roe_ttm,
               revenue_yoy_pct, revenue_mom_accel_pp
        FROM stock_fundamental
        WHERE stock_id IN ({placeholders})
          AND as_of_date >= ?
          AND as_of_date <= ?
        ORDER BY as_of_date, stock_id
        """,
        [*stock_ids, start, end],
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def _load_quarterly_roe(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, pd.DataFrame]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, period_date, metric, value
        FROM stock_financial_history
        WHERE stock_id IN ({placeholders})
          AND period_type = 'quarter'
          AND metric IN ('net_income', 'equity')
        ORDER BY period_date
        """,
        stock_ids,
    ).fetchall()
    if not rows:
        return {}
    df = pd.DataFrame([dict(r) for r in rows])
    out: dict[str, pd.DataFrame] = {}
    for sid, grp in df.groupby("stock_id"):
        by_q: dict[str, dict[str, float]] = {}
        for _, row in grp.iterrows():
            qd = str(row["period_date"])[:10]
            by_q.setdefault(qd, {})[str(row["metric"])] = float(row["value"])
        roe_rows = []
        for qd in sorted(by_q.keys()):
            bucket = by_q[qd]
            ni = bucket.get("net_income")
            eq = bucket.get("equity")
            if ni is not None and eq is not None and eq > 0:
                roe_rows.append({"as_of_date": qd, "roe_ttm": ni / eq * 100.0})
        if roe_rows:
            out[str(sid)] = pd.DataFrame(roe_rows).sort_values("as_of_date")
    return out


def _load_monthly_revenue_yoy(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, pd.DataFrame]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, period_date, value
        FROM stock_financial_history
        WHERE stock_id IN ({placeholders})
          AND period_type = 'month'
          AND metric = 'revenue'
        ORDER BY period_date
        """,
        stock_ids,
    ).fetchall()
    if not rows:
        return {}
    out: dict[str, pd.DataFrame] = {}
    for sid, grp in pd.DataFrame([dict(r) for r in rows]).groupby("stock_id"):
        by_ym: dict[tuple[int, int], float] = {}
        for _, row in grp.iterrows():
            pd_str = str(row["period_date"])[:10]
            y, m = int(pd_str[:4]), int(pd_str[5:7])
            by_ym[(y, m)] = float(row["value"])
        yoy_rows = []
        for (y, m), rev in sorted(by_ym.items()):
            prev = by_ym.get((y - 1, m))
            if prev is not None and prev > 0:
                yoy_rows.append(
                    {
                        "as_of_date": f"{y:04d}-{m:02d}-01",
                        "revenue_yoy_pct": (rev / prev - 1.0) * 100.0,
                    }
                )
        if yoy_rows:
            out[str(sid)] = pd.DataFrame(yoy_rows).sort_values("as_of_date")
    return out


def _merge_asof_pit(
    month_ends: list[str],
    stock_id: str,
    hist: pd.DataFrame,
    roe_df: pd.DataFrame | None,
    rev_df: pd.DataFrame | None,
) -> dict[str, dict[str, float | None]]:
    me = pd.DataFrame({"as_of_date": month_ends})
    me["as_of_date"] = pd.to_datetime(me["as_of_date"])

    sub = hist[hist["stock_id"] == stock_id].copy()
    if not sub.empty:
        sub["as_of_date"] = pd.to_datetime(sub["as_of_date"])
        sub = sub.sort_values("as_of_date")
        merged = pd.merge_asof(me, sub, on="as_of_date", direction="backward")
    else:
        merged = me.copy()

    roe_m = None
    if roe_df is not None and not roe_df.empty:
        rdf = roe_df.copy()
        rdf["as_of_date"] = pd.to_datetime(rdf["as_of_date"])
        roe_m = pd.merge_asof(me, rdf.sort_values("as_of_date"), on="as_of_date", direction="backward")

    rev_m = None
    if rev_df is not None and not rev_df.empty:
        vdf = rev_df.copy()
        vdf["as_of_date"] = pd.to_datetime(vdf["as_of_date"])
        rev_m = pd.merge_asof(me, vdf.sort_values("as_of_date"), on="as_of_date", direction="backward")

    out: dict[str, dict[str, float | None]] = {}
    for i, dt in enumerate(month_ends):
        pe = merged.iloc[i].get("pe") if "pe" in merged.columns else None
        pb = merged.iloc[i].get("pb") if "pb" in merged.columns else None
        div = merged.iloc[i].get("dividend_yield") if "dividend_yield" in merged.columns else None
        roe = merged.iloc[i].get("roe_ttm") if "roe_ttm" in merged.columns else None
        rev = merged.iloc[i].get("revenue_yoy_pct") if "revenue_yoy_pct" in merged.columns else None
        if roe_m is not None:
            rv = roe_m.iloc[i].get("roe_ttm")
            if rv is not None and not pd.isna(rv):
                roe = rv
        if rev_m is not None:
            rv = rev_m.iloc[i].get("revenue_yoy_pct")
            if rv is not None and not pd.isna(rv):
                rev = rv
        out[dt] = {
            "ep_ttm": earnings_yield(pe if pe is not None and not pd.isna(pe) else None),
            "bp": book_to_price(pb if pb is not None and not pd.isna(pb) else None),
            "dividend_yield": None if div is None or pd.isna(div) else float(div),
            "roe_ttm": None if roe is None or pd.isna(roe) else float(roe),
            "revenue_yoy_pct": None if rev is None or pd.isna(rev) else float(rev),
        }
    return out


def build_fundamental_feature_panels(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    month_ends: Iterable[str],
    *,
    start: str,
    end: str,
) -> dict[str, pd.DataFrame]:
    """PIT fundamental panels indexed by month-end × stock_id."""
    dates = sorted(set(month_ends))
    hist = load_fundamental_history_rows(conn, stock_ids, start=start, end=end)
    roe_by_stock = _load_quarterly_roe(conn, stock_ids)
    rev_yoy_by_stock = _load_monthly_revenue_yoy(conn, stock_ids)

    panels = {name: pd.DataFrame(index=dates, columns=stock_ids, dtype=float) for name in FUNDAMENTAL_FEATURE_NAMES}

    for sid in stock_ids:
        pit = _merge_asof_pit(
            dates,
            sid,
            hist,
            roe_by_stock.get(sid),
            rev_yoy_by_stock.get(sid),
        )
        for dt, feats in pit.items():
            for name, val in feats.items():
                if val is not None:
                    panels[name].at[dt, sid] = val

    return panels


def fundamental_coverage_report(
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
            for name in FUNDAMENTAL_FEATURE_NAMES
        }
        per_date.append({"date": dt, **counts, "eligible": min(counts.values()) >= min_cross_section})
    eligible_n = sum(1 for row in per_date if row["eligible"])
    min_cov = min(
        (min(int(row[name]) for name in FUNDAMENTAL_FEATURE_NAMES) for row in per_date),
        default=0,
    )
    return {
        "n_month_ends": len(dates),
        "eligible_month_ends": eligible_n,
        "min_feature_coverage": min_cov,
        "sample": per_date[-3:] if per_date else [],
    }
