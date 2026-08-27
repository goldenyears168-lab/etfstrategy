#!/usr/bin/env python3
"""2026-08-17: paper-tracking harness for the cross-sectional momentum factor
(Jegadeesh-Titman / George-Hwang style: rank all liquid TW stocks by trailing
6-month return, go long the top quintile / "short" the bottom quintile,
monthly rebalance) validated in this session's research thread.

Pure research tool -- READS market data only, writes to two DB tables
(cs_momentum_paper_holdings, signal_paper_days under run_id 'cs_momentum_*').
Does NOT touch config/order.yaml, config/strategy.yaml, src/order/, or any
broker API. No real capital is ever "deployed" (deployed_ntd stays 0).

Design intent: everything computed by this script FROM THE DAY IT FIRST RUNS
ONWARD is genuine forward/out-of-sample tracking -- it does not backfill
history from the backtest, specifically so the accumulated track record here
is not contaminated by in-sample knowledge. The backtest itself (2015-2026,
132 months, long-short Sharpe 1.20, t=3.91 ex-2026) lives in this session's
research notes, not in this table.

Methodology (must match the validated backtest exactly, or the paper record
means nothing):
  - Universe: all stock_daily_bars stocks (source=finmind) with a valid
    6-month-ago price and trailing 3-month avg daily turnover (`amount`)
    >= LIQ_THRESH.
  - Formation: last fully-completed calendar month-end <= latest cached data.
  - Rank by trailing 6-month return; top quintile (rank_pct>=0.8) = Q5 (long
    candidates), bottom quintile (rank_pct<=0.2) = Q1 ("short" leg, tracked
    for the long-short spread stat -- not itself a short position anyone is
    told to place).
  - Equal-weight within each quintile. Hold exactly 1 month, then reform.

Run monthly (safe to re-run any time -- idempotent: skips formation if this
month's row already exists; only closes out a period once its outcome_day
has actually elapsed in the data).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/cs_momentum_paper_tracker.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, "src")

import pandas as pd  # noqa: E402

from stock_db import connect  # noqa: E402
from stock_db.util import utc_now_iso  # noqa: E402

LIQ_THRESH = 30_000_000  # NT$/day, trailing 3mo avg turnover -- matches backtest
LOOKBACK_MONTHS = 6
RUN_Q5 = "cs_momentum_q5"
RUN_Q1 = "cs_momentum_q1"
RUN_LS = "cs_momentum_ls"


def last_completed_month_end(latest_date: pd.Timestamp) -> pd.Timestamp:
    """Last calendar month-end strictly before the month `latest_date` is in."""
    first_of_this_month = latest_date.replace(day=1)
    return first_of_this_month - pd.offsets.Day(1)


def load_panels(conn):
    df = pd.read_sql(
        "select stock_id, trade_date, close, amount, stock_id as sid "
        "from stock_daily_bars where source='finmind' and close>0",
        conn,
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    px = df.pivot(index="trade_date", columns="stock_id", values="close")
    amt = df.pivot(index="trade_date", columns="stock_id", values="amount")

    names = pd.read_sql(
        "select distinct stock_id, stock_name from investment_scores "
        "where stock_name is not null",
        conn,
    ).drop_duplicates("stock_id").set_index("stock_id")["stock_name"].to_dict()

    taiex = pd.read_sql("select date, close, source from daily_bars where code='IX0001'", conn)
    taiex["date"] = pd.to_datetime(taiex["date"])
    pref = {"tej": 3, "yahoo": 2, "finmind": 1}
    taiex["pref"] = taiex["source"].map(pref)
    taiex = taiex.sort_values("pref").drop_duplicates("date", keep="last").sort_values("date")
    taiex = taiex.set_index("date")["close"]
    return px, amt, names, taiex


def price_on_or_before(series: pd.Series, target: pd.Timestamp) -> tuple[pd.Timestamp | None, float | None]:
    sub = series[series.index <= target].dropna()
    if sub.empty:
        return None, None
    return sub.index[-1], float(sub.iloc[-1])


def form_portfolio(px: pd.DataFrame, amt: pd.DataFrame, formation_date: pd.Timestamp):
    lag_date = formation_date - pd.DateOffset(months=LOOKBACK_MONTHS)
    p_now_date, _ = None, None
    # nearest available trading day <= formation_date, per-column (use asof via reindex)
    px_upto = px[px.index <= formation_date]
    amt_upto = amt[amt.index <= formation_date]
    if px_upto.empty:
        raise RuntimeError("no price data at/ before formation_date")
    p_now = px_upto.iloc[-1]
    px_lag = px[px.index <= lag_date]
    if px_lag.empty:
        raise RuntimeError("no price data at/ before lag date -- not enough history yet")
    p_lag6 = px_lag.iloc[-1]
    liq = amt_upto.tail(63).mean()

    mom = p_now / p_lag6 - 1
    eligible = mom.index[(liq >= LIQ_THRESH) & p_now.notna() & p_lag6.notna() & mom.notna()]
    mom_e = mom[eligible]
    if len(mom_e) < 50:
        raise RuntimeError(f"only {len(mom_e)} eligible stocks -- universe too thin, aborting")
    ranks = mom_e.rank(pct=True)
    q5 = ranks[ranks >= 0.8]
    q1 = ranks[ranks <= 0.2]
    return p_now, ranks, q5, q1, len(mom_e)


def main() -> int:
    conn = connect()
    px, amt, names, taiex = load_panels(conn)
    latest_date = px.index.max()
    formation_date = last_completed_month_end(latest_date)
    next_formation = formation_date + pd.offsets.MonthEnd(1)
    # snap next_formation forward to the latest available trading day <= that month-end
    # (handled at close-out time, not here)

    fkey = formation_date.strftime("%Y-%m-%d")
    print(f"=== 跨個股動能因子 紙上追蹤 ===")
    print(f"最新可用資料日: {latest_date.date()}")
    print(f"本輪formation date(最近一個完整月底): {fkey}")

    cur = conn.execute(
        "select 1 from cs_momentum_paper_holdings where run_id=? and formation_date=? limit 1",
        (RUN_Q5, fkey),
    )
    already_formed = cur.fetchone() is not None

    if not already_formed:
        p_now, ranks, q5, q1, n_elig = form_portfolio(px, amt, formation_date)
        synced = utc_now_iso()
        rows_holdings = []
        for sid, rk in q5.items():
            rows_holdings.append((RUN_Q5, fkey, sid, names.get(sid), float(p_now[sid]),
                                   1.0 / len(q5), float(rk), n_elig, synced))
        for sid, rk in q1.items():
            rows_holdings.append((RUN_Q1, fkey, sid, names.get(sid), float(p_now[sid]),
                                   1.0 / len(q1), float(rk), n_elig, synced))
        conn.executemany(
            "insert or replace into cs_momentum_paper_holdings "
            "(run_id, formation_date, stock_id, stock_name, entry_price, weight, rank_pct, universe_size, synced_at) "
            "values (?,?,?,?,?,?,?,?,?)",
            rows_holdings,
        )
        out_key = next_formation.strftime("%Y-%m-%d")
        for run_id, n in [(RUN_Q5, len(q5)), (RUN_Q1, len(q1))]:
            conn.execute(
                "insert or replace into signal_paper_days "
                "(run_id, signal_day, outcome_day, deployed_ntd, status, synced_at) "
                "values (?,?,?,0,'open',?)",
                (run_id, fkey, out_key, synced),
            )
        conn.commit()
        print(f"新建立本月投組: Q5={len(q5)}檔 Q1={len(q1)}檔 (符合流動性門檻的宇宙共{n_elig}檔)")
    else:
        print("本月投組已經建立過,不重複formation。")

    # close out any open periods whose outcome_day has now elapsed
    open_rows = conn.execute(
        "select run_id, signal_day, outcome_day from signal_paper_days "
        "where run_id in (?,?) and status='open'",
        (RUN_Q5, RUN_Q1),
    ).fetchall()
    closed_any = False
    for run_id, signal_day, outcome_day in open_rows:
        outcome_ts = pd.Timestamp(outcome_day)
        if outcome_ts > latest_date:
            continue  # period hasn't actually elapsed in the data yet
        holdings = conn.execute(
            "select stock_id, entry_price, weight from cs_momentum_paper_holdings "
            "where run_id=? and formation_date=?",
            (run_id, signal_day),
        ).fetchall()
        if not holdings:
            continue
        rets = []
        for sid, entry_px, w in holdings:
            exit_date, exit_px = price_on_or_before(px[sid] if sid in px.columns else pd.Series(dtype=float), outcome_ts)
            if exit_px is None:
                continue
            rets.append(w * (exit_px / entry_px - 1))
        if not rets:
            continue
        port_ret = sum(rets)
        bench_date, bench_now = price_on_or_before(taiex, outcome_ts)
        bench_form_date, bench_form = price_on_or_before(taiex, pd.Timestamp(signal_day))
        bench_ret = (bench_now / bench_form - 1) if (bench_now and bench_form) else None
        alpha = (port_ret - bench_ret) if bench_ret is not None else None
        conn.execute(
            "update signal_paper_days set status='complete', day_return_pct=?, "
            "bench_return_pct=?, alpha_ntd=? where run_id=? and signal_day=?",
            (100 * port_ret, 100 * bench_ret if bench_ret is not None else None,
             alpha, run_id, signal_day),
        )
        closed_any = True
        print(f"結算 {run_id} {signal_day}->{outcome_day}: 報酬={100*port_ret:+.2f}% "
              f"(TAIEX同期={100*bench_ret:+.2f}%)" if bench_ret is not None else
              f"結算 {run_id} {signal_day}->{outcome_day}: 報酬={100*port_ret:+.2f}%")
    if closed_any:
        conn.commit()

    # report current (still-open) holdings, marked to market
    print(f"\n=== 目前持倉(formation={fkey}, 未實現) ===")
    for run_id, label in [(RUN_Q5, "Q5多方(動能贏家)"), (RUN_Q1, "Q1(動能輸家,僅供對照,非放空建議)")]:
        holdings = conn.execute(
            "select stock_id, stock_name, entry_price, weight from cs_momentum_paper_holdings "
            "where run_id=? and formation_date=?", (run_id, fkey)
        ).fetchall()
        if not holdings:
            continue
        unreal = []
        for sid, name, entry_px, w in holdings:
            _, cur_px = price_on_or_before(px[sid] if sid in px.columns else pd.Series(dtype=float), latest_date)
            if cur_px is None:
                continue
            unreal.append(w * (cur_px / entry_px - 1))
        port_unreal = sum(unreal) if unreal else float("nan")
        print(f"  {label}: {len(holdings)}檔  未實現報酬(至{latest_date.date()})={100*port_unreal:+.2f}%")

    # track record so far
    hist = pd.read_sql(
        "select run_id, signal_day, outcome_day, day_return_pct, bench_return_pct "
        "from signal_paper_days where run_id in (?,?) and status='complete' order by signal_day",
        conn, params=(RUN_Q5, RUN_Q1),
    )
    print(f"\n=== 已結算track record(真正樣本外,非回測) ===")
    if hist.empty:
        print("  尚無已結算月份(第一次跑,要等下個月formation後才有第一筆真正out-of-sample結果)。")
    else:
        print(hist.to_string(index=False))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
