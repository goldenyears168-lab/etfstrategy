"""FinPilot 策略本地重現（FinMind DB）· 月頻選股 + H9 持有 vs IX0001。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .copytrade_backtest import bench_return_entry_to_exit
from flow_returns import return_pct, stock_close, stock_open, trading_dates_after


@dataclass(frozen=True)
class FinPilotStrategySpec:
    strategy_id: str
    label: str
    finpilot_file: str


FINPILOT_STRATEGIES: tuple[FinPilotStrategySpec, ...] = (
    FinPilotStrategySpec("s01", "創250日新高", "s01_new_high.py"),
    FinPilotStrategySpec("s04", "60日動能+ROE>0", "s04_momentum_roe.py"),
    FinPilotStrategySpec("s05", "創新高+月營收年增>0", "s05_newhigh_revenue.py"),
    FinPilotStrategySpec("s06", "創新高+ROE>15%", "s06_newhigh_roe.py"),
)


def _wide_from_long(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Long → wide panel. pandas pivot mis-labels trade_date index on this dataset."""
    mat: dict[str, dict[str, float]] = {}
    for sid, td, val in zip(df["stock_id"], df["trade_date"], df[col], strict=True):
        mat.setdefault(td, {})[sid] = float(val)
    dates = sorted(mat)
    stocks = sorted({s for day in mat.values() for s in day})
    return pd.DataFrame(
        {s: [mat[d].get(s, float("nan")) for d in dates] for s in stocks},
        index=pd.Index(dates, name="trade_date"),
    )


def load_price_panels(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, open, close, volume, source
        FROM stock_daily_bars
        ORDER BY trade_date, stock_id,
            CASE source WHEN 'finmind' THEN 0 WHEN 'yfinance' THEN 1 ELSE 2 END
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("stock_daily_bars 無資料，請先跑 daily_sync / stock market sync")
    df = pd.DataFrame(rows, columns=["stock_id", "trade_date", "open", "close", "volume", "source"])
    df = df.drop_duplicates(subset=["stock_id", "trade_date"], keep="first")
    close = _wide_from_long(df, "close").astype(float)
    opn = _wide_from_long(df, "open").astype(float)
    vol = _wide_from_long(df, "volume").astype(float)
    return close, opn, vol


def load_fundamental_snapshot(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, as_of_date, roe_latest_q, revenue_yoy_pct
        FROM stock_fundamental
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["stock_id", "as_of_date", "roe_latest_q", "revenue_yoy_pct"])
    return pd.DataFrame(
        rows, columns=["stock_id", "as_of_date", "roe_latest_q", "revenue_yoy_pct"]
    )


def load_financial_history(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, period_date, period_type, metric, value
        FROM stock_financial_history
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["stock_id", "period_date", "period_type", "metric", "value"]
        )
    return pd.DataFrame(
        rows, columns=["stock_id", "period_date", "period_type", "metric", "value"]
    )


def _roe_at_date(fin: pd.DataFrame, stock_id: str, as_of: str) -> float | None:
    q = fin[
        (fin["stock_id"] == stock_id)
        & (fin["period_type"] == "quarter")
        & (fin["period_date"] <= as_of)
    ]
    if q.empty:
        return None
    dates = sorted(q["period_date"].unique(), reverse=True)
    for pd_date in dates:
        chunk = q[q["period_date"] == pd_date]
        ni = chunk.loc[chunk["metric"] == "net_income", "value"]
        eq = chunk.loc[chunk["metric"] == "equity", "value"]
        if ni.empty or eq.empty:
            continue
        equity = float(eq.iloc[0])
        if equity <= 0:
            continue
        return float(ni.iloc[0]) / equity * 100.0
    return None


def _revenue_yoy_at_date(fin: pd.DataFrame, stock_id: str, as_of: str) -> float | None:
    m = fin[
        (fin["stock_id"] == stock_id)
        & (fin["period_type"] == "month")
        & (fin["metric"] == "revenue")
        & (fin["period_date"] <= as_of)
    ]
    if m.empty:
        return None
    latest_row = m.sort_values("period_date").iloc[-1]
    latest_date = str(latest_row["period_date"])
    latest_rev = float(latest_row["value"])
    y, mo = int(latest_date[:4]), int(latest_date[5:7])
    prior_key = f"{y - 1}-{mo:02d}"
    prior = m[m["period_date"].str.startswith(prior_key)]
    if prior.empty:
        return None
    prior_rev = float(prior.sort_values("period_date").iloc[-1]["value"])
    if prior_rev <= 0:
        return None
    return (latest_rev - prior_rev) / prior_rev * 100.0


def pit_fundamental_at(
    fund: pd.DataFrame,
    fin_hist: pd.DataFrame,
    stock_ids: list[str],
    as_of: str,
) -> dict[str, dict[str, float | None]]:
    """as_of 當日可得最新基本面（history 優先，snapshot 補足）。"""
    out: dict[str, dict[str, float | None]] = {}
    for sid in stock_ids:
        roe = _roe_at_date(fin_hist, sid, as_of) if not fin_hist.empty else None
        rev_yoy = _revenue_yoy_at_date(fin_hist, sid, as_of) if not fin_hist.empty else None
        if fund.empty:
            out[sid] = {"roe_latest_q": roe, "revenue_yoy_pct": rev_yoy}
            continue
        sub = fund[(fund["stock_id"] == sid) & (fund["as_of_date"] <= as_of)].sort_values(
            "as_of_date"
        )
        if roe is None and not sub.empty:
            val = sub.iloc[-1]["roe_latest_q"]
            roe = float(val) if val is not None else None
        if rev_yoy is None and not sub.empty:
            val = sub.iloc[-1]["revenue_yoy_pct"]
            rev_yoy = float(val) if val is not None else None
        out[sid] = {"roe_latest_q": roe, "revenue_yoy_pct": rev_yoy}
    return out


def month_end_trading_dates(dates: list[str]) -> list[str]:
    s = pd.Series(dates, dtype="string")
    df = pd.DataFrame({"date": s, "ym": s.str.slice(0, 7)})
    return df.groupby("ym", sort=True)["date"].max().tolist()


def _top_n_by_value(
    mask: pd.Series,
    rank_values: pd.Series,
    n: int,
) -> list[str]:
    eligible = mask.fillna(False) & rank_values.notna()
    if not eligible.any():
        return []
    ranked = rank_values[eligible].sort_values(ascending=False).head(n)
    return ranked.index.astype(str).tolist()


def select_stocks(
    strategy_id: str,
    *,
    signal_date: str,
    close: pd.DataFrame,
    vol: pd.DataFrame,
    fund_snap: dict[str, dict[str, float | None]],
) -> list[str]:
    if signal_date not in close.index:
        return []
    c = close.loc[signal_date]
    v = vol.loc[signal_date]
    stock_ids = [str(x) for x in c.index]

    vol_ma20 = vol.loc[:signal_date].tail(20).mean()
    liquid = vol_ma20 > 3_000_000

    if strategy_id == "s01":
        hist = close.loc[:signal_date]
        if len(hist) < 250:
            return []
        new_high = c >= hist.tail(250).max()
        mask = new_high & liquid.reindex(c.index).fillna(False)
        return _top_n_by_value(mask, c, 20)

    if strategy_id == "s04":
        hist = close.loc[:signal_date]
        if len(hist) < 60:
            return []
        mom = c / hist.iloc[-60]
        top30 = mom.sort_values(ascending=False).head(30).index
        picks = []
        for sid in top30:
            roe = fund_snap.get(str(sid), {}).get("roe_latest_q")
            if roe is not None and roe > 0:
                picks.append(str(sid))
        return picks

    if strategy_id == "s05":
        hist = close.loc[:signal_date]
        if len(hist) < 250:
            return []
        new_high = c >= hist.tail(250).max()
        mask = pd.Series(False, index=c.index)
        for sid in c.index:
            sid_s = str(sid)
            rev = fund_snap.get(sid_s, {}).get("revenue_yoy_pct")
            liq = bool(liquid.get(sid, False))
            if rev is not None and rev > 0 and bool(new_high.get(sid, False)) and liq:
                mask[sid] = True
        return _top_n_by_value(mask, c, 20)

    if strategy_id == "s06":
        hist = close.loc[:signal_date]
        if len(hist) < 250:
            return []
        new_high = c >= hist.tail(250).max()
        mask = pd.Series(False, index=c.index)
        for sid in c.index:
            sid_s = str(sid)
            roe = fund_snap.get(sid_s, {}).get("roe_latest_q")
            if roe is not None and roe > 15 and bool(new_high.get(sid, False)):
                mask[sid] = True
        return _top_n_by_value(mask, c, 20)

    raise ValueError(f"unknown strategy_id: {strategy_id}")


def basket_return_h9(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    entry_date: str,
    *,
    hold_days: int = 9,
) -> float | None:
    if not stock_ids:
        return None
    exit_dates = trading_dates_after(conn, entry_date, count=hold_days)
    if len(exit_dates) < hold_days:
        return None
    exit_date = exit_dates[hold_days - 1]
    rets: list[float] = []
    for sid in stock_ids:
        p0 = stock_open(conn, sid, entry_date)
        p1 = stock_close(conn, sid, exit_date)
        if p0 is None or p1 is None:
            continue
        rets.append(return_pct(p0, p1))
    if not rets:
        return None
    return sum(rets) / len(rets)


def run_strategy_h9_periods(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    hold_days: int = 9,
    window_start: str | None = None,
    window_end: str | None = None,
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

        fund_snap = pit_fundamental_at(fund, fin_hist, list(close.columns.astype(str)), month_end)
        picks = select_stocks(
            strategy_id,
            signal_date=month_end,
            close=close,
            vol=vol,
            fund_snap=fund_snap,
        )
        if not picks:
            continue

        port_ret = basket_return_h9(conn, picks, entry_date, hold_days=hold_days)
        if port_ret is None:
            continue
        exit_dates = trading_dates_after(conn, entry_date, count=hold_days)
        exit_date = exit_dates[hold_days - 1]
        bench_ret = bench_return_entry_to_exit(
            conn, entry_date, exit_date, entry_price_mode="open"
        )
        if bench_ret is None:
            continue
        periods.append(
            {
                "signal_month_end": month_end,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "n_stocks": len(picks),
                "return_pct": port_ret,
                "bench_return_pct": bench_ret,
                "beat_bench": port_ret > bench_ret,
                "gross_win": port_ret > 0,
            }
        )
    return periods


def summarize_periods(periods: list[dict]) -> dict:
    if not periods:
        return {
            "n_periods": 0,
            "win_rate_gross_pct": None,
            "win_rate_vs_bench_pct": None,
            "mean_return_pct": None,
            "mean_bench_pct": None,
            "window_start": None,
            "window_end": None,
        }
    n = len(periods)
    return {
        "n_periods": n,
        "win_rate_gross_pct": round(sum(1 for p in periods if p["gross_win"]) / n * 100, 2),
        "win_rate_vs_bench_pct": round(sum(1 for p in periods if p["beat_bench"]) / n * 100, 2),
        "mean_return_pct": round(sum(p["return_pct"] for p in periods) / n, 4),
        "mean_bench_pct": round(sum(p["bench_return_pct"] for p in periods) / n, 4),
        "window_start": min(p["entry_date"] for p in periods),
        "window_end": max(p["entry_date"] for p in periods),
    }
