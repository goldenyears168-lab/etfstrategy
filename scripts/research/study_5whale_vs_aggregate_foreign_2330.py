#!/usr/bin/env python3
"""比較「5家具名外資分點(1480/1560/1470/8440/1650)合計」vs「官方外資彙總 foreign_net」
對台積電(2330)的事件訊號效果量與穩健性。

方法論（依今日踩過的坑修正）：
  - rolling z-score 一律用「只看到當下這天為止」的 trailing window（expanding, 之後
    bounded 252 日 rolling），不對整段資料一次 rank(pct=True)。
  - event 一律用 t 日收盤後才知道的 z-score 去預測 t+1...t+20 的 forward return，
    不會有 t 日之內的 look-ahead。
  - 額外做 decluster（只取「新進入」極端賣超的第一天）版本，處理事件群聚/forward window
    重疊造成的自相關高估顯著性問題。
  - 額外做 reaction-vs-lead：event 是否群聚在大跌之後（反應），或大跌之前已經升高（領先）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"

FIVE_WHALES = {
    "1480": "美商高盛",
    "1560": "港商野村",
    "1470": "摩根士丹利",
    "8440": "摩根大通",
    "1650": "瑞銀",
}
STOCK = "2330"
ROLL_WINDOW = 252
MIN_PERIODS = 60
Z_THRESH = 2.0
HORIZONS = [1, 5, 10, 20]


def load_official(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT trade_date, close_price, foreign_net
        FROM stock_institutional_daily
        WHERE stock_id = ?
        ORDER BY trade_date
        """,
        conn,
        params=[STOCK],
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_whale5(conn: sqlite3.Connection) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in FIVE_WHALES)
    df = pd.read_sql_query(
        f"""
        SELECT trade_date, securities_trader_id, net
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = 'finmind'
          AND securities_trader_id IN ({placeholders})
        """,
        conn,
        params=[STOCK, *FIVE_WHALES.keys()],
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    wide = df.groupby("trade_date")["net"].sum().rename("whale5_net_shares")
    per_broker = df.pivot_table(
        index="trade_date", columns="securities_trader_id", values="net", aggfunc="sum"
    )
    n_brokers_present = per_broker.notna().sum(axis=1).rename("n_brokers_present")
    return pd.concat([wide, n_brokers_present], axis=1).reset_index()


def rolling_z(series: pd.Series) -> pd.Series:
    """Bounded rolling z-score, walk-forward safe: at row t uses only rows
    [t-ROLL_WINDOW+1 .. t] (inclusive of t, which is legitimate since t's value
    is known after close on day t, before we act on t+1 forward returns)."""
    mean = series.rolling(ROLL_WINDOW, min_periods=MIN_PERIODS).mean()
    std = series.rolling(ROLL_WINDOW, min_periods=MIN_PERIODS).std(ddof=0)
    z = (series - mean) / std.replace(0, np.nan)
    return z


def forward_returns(close: pd.Series) -> pd.DataFrame:
    out = {}
    logc = np.log(close)
    for h in HORIZONS:
        out[f"fwd_ret_{h}"] = logc.shift(-h) - logc
    return pd.DataFrame(out, index=close.index)


def hac_tstat(x: np.ndarray, lag: int) -> tuple[float, float]:
    """Newey-West HAC t-stat for mean(x)=0, lag = max forward horizon overlap."""
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 5:
        return np.nan, np.nan
    xm = x - x.mean()
    gamma0 = np.dot(xm, xm) / n
    var = gamma0
    for lg in range(1, min(lag, n - 1) + 1):
        w = 1 - lg / (lag + 1)
        cov = np.dot(xm[lg:], xm[:-lg]) / n
        var += 2 * w * cov
    se = np.sqrt(max(var, 1e-12) / n)
    t = x.mean() / se if se > 0 else np.nan
    return x.mean(), t


def event_stats(df: pd.DataFrame, event_col: str, label: str) -> pd.DataFrame:
    rows = []
    events = df[df[event_col]]
    n = len(events)
    for h in HORIZONS:
        col = f"fwd_ret_{h}"
        ev_vals = events[col].dropna().values
        base_vals = df[col].dropna().values
        if len(ev_vals) == 0:
            continue
        mean_ev, t_hac = hac_tstat(ev_vals, lag=h)
        mean_base = np.nanmean(base_vals)
        rows.append(
            {
                "signal": label,
                "horizon": h,
                "n_events": len(ev_vals),
                "mean_fwd_ret_event_%": mean_ev * 100,
                "mean_fwd_ret_baseline_%": mean_base * 100,
                "diff_%": (mean_ev - mean_base) * 100,
                "hac_tstat": t_hac,
            }
        )
    return pd.DataFrame(rows)


def decluster(flag: pd.Series) -> pd.Series:
    """Keep only first day of each consecutive True run."""
    prev = flag.shift(1).fillna(False)
    return flag & (~prev)


def main() -> int:
    conn = sqlite3.connect(DB)
    official = load_official(conn)
    whale5 = load_whale5(conn)
    conn.close()

    df = official.merge(whale5, on="trade_date", how="left")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["whale5_net_shares"] = df["whale5_net_shares"].fillna(0.0)
    df["n_brokers_present"] = df["n_brokers_present"].fillna(0)

    df["official_net_value"] = df["foreign_net"] * df["close_price"]
    df["whale5_net_value"] = df["whale5_net_shares"] * df["close_price"]

    df["z_official"] = rolling_z(df["official_net_value"])
    df["z_whale5"] = rolling_z(df["whale5_net_value"])

    fwd = forward_returns(df["close_price"])
    df = pd.concat([df, fwd], axis=1)

    coverage_start = df.loc[df["n_brokers_present"] >= 4, "trade_date"].min()
    coverage_end = df["trade_date"].max()
    print(f"5-whale table stock coverage (>=4/5 brokers present on same day): "
          f"{coverage_start} .. {coverage_end}")
    print(f"Full official-foreign coverage: {df['trade_date'].min()} .. {df['trade_date'].max()}")
    print(f"Total rows: {len(df)}")
    print()

    # Restrict comparison window to where whale5 data actually exists & is populated.
    window = df[df["trade_date"] >= coverage_start].copy()
    print(f"Comparison window: {window['trade_date'].min().date()} .. "
          f"{window['trade_date'].max().date()}  n={len(window)} trading days")

    # basic correlation between the two flow series (raw value, and z-score)
    corr_value = window[["official_net_value", "whale5_net_value"]].corr().iloc[0, 1]
    corr_z = window[["z_official", "z_whale5"]].dropna().corr().iloc[0, 1]
    print(f"corr(official_net_value, whale5_net_value) = {corr_value:.3f}")
    print(f"corr(z_official, z_whale5) = {corr_z:.3f}")
    frac_whale5_of_official = (
        window["whale5_net_value"].abs().sum() / window["official_net_value"].abs().sum()
    )
    print(f"sum|whale5_net_value| / sum|official_net_value| = {frac_whale5_of_official:.3f}")
    print()

    window["evt_official_sell"] = window["z_official"] <= -Z_THRESH
    window["evt_official_buy"] = window["z_official"] >= Z_THRESH
    window["evt_whale5_sell"] = window["z_whale5"] <= -Z_THRESH
    window["evt_whale5_buy"] = window["z_whale5"] >= Z_THRESH

    window["evt_official_sell_decl"] = decluster(window["evt_official_sell"])
    window["evt_whale5_sell_decl"] = decluster(window["evt_whale5_sell"])
    window["evt_official_buy_decl"] = decluster(window["evt_official_buy"])
    window["evt_whale5_buy_decl"] = decluster(window["evt_whale5_buy"])

    all_stats = []
    for col, label in [
        ("evt_official_sell", "official_sell_all"),
        ("evt_whale5_sell", "whale5_sell_all"),
        ("evt_official_sell_decl", "official_sell_declustered"),
        ("evt_whale5_sell_decl", "whale5_sell_declustered"),
        ("evt_official_buy", "official_buy_all"),
        ("evt_whale5_buy", "whale5_buy_all"),
        ("evt_official_buy_decl", "official_buy_declustered"),
        ("evt_whale5_buy_decl", "whale5_buy_declustered"),
    ]:
        all_stats.append(event_stats(window, col, label))
    stats_df = pd.concat(all_stats, ignore_index=True)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 200)
    print(stats_df.to_string(index=False, float_format=lambda x: f"{x:0.4f}"))
    print()

    # ---- reaction vs lead ----
    ret1 = np.log(window["close_price"]).diff()
    window["ret_t"] = ret1
    big_drop_thresh = ret1.quantile(0.02)  # bottom 2% single-day return days
    window["big_drop_day"] = ret1 <= big_drop_thresh
    print(f"big single-day drop threshold (2% quantile of daily log-return): "
          f"{big_drop_thresh*100:.2f}%  n_days={window['big_drop_day'].sum()}")

    def lead_lag_table(z_col: str, label: str) -> None:
        print(f"-- {label}: P(extreme-sell event at t | big-drop day in [t-k,t-1]) vs unconditional --")
        unconditional = (window[z_col] <= -Z_THRESH).mean()
        for k in [1, 2, 3, 5]:
            preceded = window["big_drop_day"].shift(1).rolling(k, min_periods=1).max().fillna(0).astype(bool)
            cond = window.loc[preceded, z_col] <= -Z_THRESH
            print(f"  k={k}: P(event|drop within {k}d before)={cond.mean():.3f} "
                  f"(n={preceded.sum()})  vs unconditional={unconditional:.3f}")
        # lead: is the signal already elevated in days BEFORE a big drop (foreknowledge)?
        print(f"-- {label}: is signal already extreme in days BEFORE a big-drop day (potential foreknowledge) --")
        for k in [1, 2, 3, 5]:
            precedes_drop = window["big_drop_day"].shift(-1).rolling(k, min_periods=1).max()
            precedes_drop = precedes_drop.shift(-(k - 1)).fillna(0).astype(bool)
            cond = window.loc[precedes_drop, z_col] <= -Z_THRESH
            print(f"  k={k}: P(event|drop within {k}d after)={cond.mean():.3f} "
                  f"(n={precedes_drop.sum()})  vs unconditional={unconditional:.3f}")
        print()

    lead_lag_table("z_official", "official")
    lead_lag_table("z_whale5", "whale5")

    # ---- cross-correlation flow(t) vs return(t+lag) ----
    print("-- cross-correlation: flow_z(t) vs next-day-and-beyond return, lags -10..+10 --")
    print("   (negative lag = flow leads return; positive lag = flow follows/reacts to return)")
    for z_col, label in [("z_official", "official"), ("z_whale5", "whale5")]:
        vals = []
        for lag in range(-10, 11):
            shifted_ret = ret1.shift(-lag)
            c = window[z_col].corr(shifted_ret)
            vals.append((lag, c))
        best = max(vals, key=lambda x: abs(x[1]))
        print(f"  {label}: peak |corr|={best[1]:.3f} at lag={best[0]} "
              f"({'flow leads' if best[0] < 0 else 'flow reacts/coincident' if best[0] >= 0 else ''})")
        print("   lag:corr = " + ", ".join(f"{lg}:{c:+.3f}" for lg, c in vals))
    print()

    # ---- sub-period stability (regime robustness) ----
    print("-- sub-period stability of sell-event forward return (declustered, h=5) --")
    sub_bounds = [
        ("2021-07-01", "2022-12-31"),
        ("2023-01-01", "2024-06-30"),
        ("2024-07-01", "2026-07-27"),
    ]
    for col, label in [("evt_official_sell_decl", "official"), ("evt_whale5_sell_decl", "whale5")]:
        print(f"  {label}:")
        for s, e in sub_bounds:
            sub = window[(window["trade_date"] >= s) & (window["trade_date"] <= e)]
            ev = sub[sub[col]]["fwd_ret_5"].dropna()
            if len(ev) == 0:
                print(f"    {s}..{e}: n=0")
                continue
            print(f"    {s}..{e}: n={len(ev)} mean_fwd5={ev.mean()*100:+.3f}% "
                  f"baseline={sub['fwd_ret_5'].mean()*100:+.3f}%")
    print()

    # ---- multivariate: does whale5 add info beyond official aggregate? ----
    print("-- incremental info: regress fwd_ret_5 on z_official and z_whale5 jointly --")
    reg_df = window[["z_official", "z_whale5", "fwd_ret_5"]].dropna()
    X = np.column_stack([np.ones(len(reg_df)), reg_df["z_official"], reg_df["z_whale5"]])
    y = reg_df["fwd_ret_5"].values
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    sigma2 = np.sum(resid**2) / (n - k)
    cov = sigma2 * np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    tvals = beta / se
    print(f"  n={n}")
    print(f"  const:      coef={beta[0]*100:+.4f}%  t={tvals[0]:+.2f}")
    print(f"  z_official: coef={beta[1]*100:+.4f}%  t={tvals[1]:+.2f}")
    print(f"  z_whale5:   coef={beta[2]*100:+.4f}%  t={tvals[2]:+.2f}")

    out_csv = ROOT / "scratch_5whale_vs_official_window.csv"
    window.to_csv(out_csv, index=False)
    print(f"\nsaved window data -> {out_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
