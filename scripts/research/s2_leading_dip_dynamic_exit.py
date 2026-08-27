"""S2 rotation-exit transplant onto leading-dip (T+3 fixed hold) — read-only research.

Question: does a dynamic exit (bail at T+1/T+2 when underwater, C18acc-style
"cut losers early") beat leading-dip's current fixed T+3 hold?

Reuses reports/research/rrg/20260715_leading_dip_events.csv (leading-dip's
historical trigger events, already adopted/live). Each row has:
  date       = entry trading day (T)
  ex0        = entry excess (%, vs prior close) at trigger minute
  px3        = stock return (%) from intraday entry price to T+3 close
  ex3        = excess return (%) from entry to T+3 close, vs bench
  exit_date  = T+3 trading-day close date

We do not have the raw intraday entry price, but we can reconstruct it from
px3 and the T+3 close price (pulled fresh from stock_daily_bars):
  S_in = close(exit_date) / (1 + px3/100)

Then pull T+1 / T+2 close prices for the same sid from stock_daily_bars
(trading calendar derived from 0050's distinct trade_date, which trades every
market day) to build each trade's intraday-entry -> close path, and simulate
a dynamic-exit rule against the fixed T+3 baseline.
"""

from __future__ import annotations

import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
import stock_db  # noqa: E402

DB_PATH = stock_db.DEFAULT_DB_PATH
EVENTS_CSV = "reports/research/rrg/20260715_leading_dip_events.csv"
OUT_DIR = "reports/research/s2_exit_leading_dip_transplant"


def load_trading_calendar(con: sqlite3.Connection, start: str, end: str) -> list[str]:
    q = """SELECT DISTINCT trade_date FROM stock_daily_bars
           WHERE stock_id='0050' AND trade_date>=? AND trade_date<=?
           ORDER BY trade_date"""
    df = pd.read_sql(q, con, params=(start, end))
    return df["trade_date"].tolist()


def fwd_n(cal: list[str], d: str, n: int) -> str | None:
    try:
        i = cal.index(d)
    except ValueError:
        # d may not be exactly in cal (shouldn't happen for 0050) — find next >= d
        later = [x for x in cal if x >= d]
        if not later:
            return None
        i = cal.index(later[0])
    j = i + n
    if j >= len(cal):
        return None
    return cal[j]


def main() -> None:
    events = pd.read_csv(EVENTS_CSV)
    events = events.dropna(subset=["exit_date", "px3", "ex3"]).copy()
    events = events[events["exit_date"].astype(str).str.len() > 0].copy()
    events["sid"] = events["sid"].astype(str)
    n0 = len(events)

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cal_start = str(events["date"].min())
    cal_end = str(events["exit_date"].max())
    cal = load_trading_calendar(con, cal_start, cal_end)

    # Pull all needed sid/date close prices in one query.
    sids = sorted(events["sid"].unique())
    qmarks = ",".join("?" * len(sids))
    bars = pd.read_sql(
        f"""SELECT stock_id, trade_date, close FROM stock_daily_bars
            WHERE stock_id IN ({qmarks}) AND trade_date>=? AND trade_date<=?""",
        con,
        params=(*sids, cal_start, cal_end),
    )
    close_map: dict[tuple[str, str], float] = {
        (r.stock_id, r.trade_date): r.close for r in bars.itertuples()
    }

    rows = []
    skipped = []
    for r in events.itertuples():
        t0 = str(r.date)
        t3 = str(r.exit_date)
        sid = r.sid
        t1 = fwd_n(cal, t0, 1)
        t2 = fwd_n(cal, t0, 2)
        c3 = close_map.get((sid, t3))
        c1 = close_map.get((sid, t1)) if t1 else None
        c2 = close_map.get((sid, t2)) if t2 else None
        if c3 is None or c1 is None or c2 is None:
            skipped.append((sid, t0, t1, t2, t3))
            continue
        px3 = float(r.px3)
        s_in = c3 / (1.0 + px3 / 100.0)
        ret1 = (c1 / s_in - 1.0) * 100.0
        ret2 = (c2 / s_in - 1.0) * 100.0
        ret3 = px3  # by construction == (c3/s_in - 1)*100
        rows.append(
            {
                "date": t0,
                "sid": sid,
                "minute": r.minute,
                "t1": t1,
                "t2": t2,
                "t3": t3,
                "ret1": ret1,
                "ret2": ret2,
                "ret3": ret3,
                "ex3": float(r.ex3),
            }
        )

    df = pd.DataFrame(rows)
    n_used = len(df)

    def summarize(returns: pd.Series, label: str) -> dict:
        r = returns.dropna().astype(float)
        n = len(r)
        mean = r.mean()
        std = r.std(ddof=1) if n > 1 else float("nan")
        sharpe_per_trade = mean / std if std and std == std and std > 0 else float("nan")
        win_rate = (r > 0).mean()
        median = r.median()
        se = std / np.sqrt(n) if n > 1 else float("nan")
        t_stat = mean / se if se and se == se and se > 0 else float("nan")
        return {
            "label": label,
            "n": n,
            "mean_ret_pct": round(float(mean), 4),
            "median_ret_pct": round(float(median), 4),
            "std_pct": round(float(std), 4) if std == std else None,
            "win_rate": round(float(win_rate), 4),
            "sum_ret_pct": round(float(r.sum()), 4),
            "t_stat_vs_0": round(float(t_stat), 4) if t_stat == t_stat else None,
        }

    # Baseline: fixed T+3 hold.
    baseline = df["ret3"]

    # Dynamic variant A: exit at T+1 if ret1<0, elif exit at T+2 if ret2<0, else hold to T+3.
    def dyn_a(row) -> float:
        if row["ret1"] < 0:
            return row["ret1"]
        if row["ret2"] < 0:
            return row["ret2"]
        return row["ret3"]

    dynA = df.apply(dyn_a, axis=1)

    # Dynamic variant B: threshold cut — exit at T+1 if ret1 <= -3%, elif T+2 if ret2 <= -3%, else T+3.
    THRESH = -3.0

    def dyn_b(row) -> float:
        if row["ret1"] <= THRESH:
            return row["ret1"]
        if row["ret2"] <= THRESH:
            return row["ret2"]
        return row["ret3"]

    dynB = df.apply(dyn_b, axis=1)

    # Dynamic variant C: exit at T+2 only if ret2<0 (never cut at T+1; softer / fewer whipsaw cuts).
    def dyn_c(row) -> float:
        if row["ret2"] < 0:
            return row["ret2"]
        return row["ret3"]

    dynC = df.apply(dyn_c, axis=1)

    summaries = [
        summarize(baseline, "fixed_T+3 (current live rule)"),
        summarize(dynA, "dynamic_A: cut at T+1 or T+2 if ret<0"),
        summarize(dynB, f"dynamic_B: cut at T+1/T+2 if ret<={THRESH}%"),
        summarize(dynC, "dynamic_C: cut at T+2 only if ret2<0"),
    ]

    # Paired diffs vs baseline (paired t-test, same trades).
    from scipy import stats

    paired = []
    for name, series in [("dynA", dynA), ("dynB", dynB), ("dynC", dynC)]:
        d = (series - baseline).dropna()
        t, p = stats.ttest_rel(series, baseline)
        paired.append(
            {
                "variant": name,
                "mean_diff_pct": round(float(d.mean()), 4),
                "n": len(d),
                "t_stat": round(float(t), 4),
                "p_value": round(float(p), 6),
                "n_exited_early": None,
            }
        )

    n_early_a = int(((df["ret1"] < 0) | ((df["ret1"] >= 0) & (df["ret2"] < 0))).sum())
    n_early_b = int(((df["ret1"] <= THRESH) | ((df["ret1"] > THRESH) & (df["ret2"] <= THRESH))).sum())
    n_early_c = int((df["ret2"] < 0).sum())
    paired[0]["n_exited_early"] = n_early_a
    paired[1]["n_exited_early"] = n_early_b
    paired[2]["n_exited_early"] = n_early_c

    import json

    out = {
        "n_events_csv": n0,
        "n_used_with_bars": n_used,
        "n_skipped_missing_bars": len(skipped),
        "skipped": skipped,
        "summaries": summaries,
        "paired_vs_baseline": paired,
    }
    with open(f"{OUT_DIR}/s2_dynamic_exit_summary.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    df.to_csv(f"{OUT_DIR}/s2_dynamic_exit_trades.csv", index=False)

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
