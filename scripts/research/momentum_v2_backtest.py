"""
Momentum v2 backtest: re-validate the 3-12 month (Jegadeesh-Titman style) momentum signal
using the newly built stock_close_adjusted.adj_close_v2 (back-adjusted close price) instead
of the raw stock_daily_bars.close used in the original backtest.

READ-ONLY with respect to price_adjustment tables. Does not write to the DB.

Methodology is copied 1:1 from the original backtest
(/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/23d1e4ad-e00c-4bea-98bd-c10e716d0856/scratchpad/momentum_backtest.py
 + momentum_analysis.py) with ONE change: the price series used for (a) momentum-score
formation, (b) holding-period return, and (c) dollar-volume liquidity ranking is
adj_close_v2 (INNER JOIN stock_daily_bars for trade_date/volume, stock_close_adjusted for
the price itself) instead of stock_daily_bars.close.

Additionally runs:
  - The same grid on the ORIGINAL (raw close, unadjusted) results for side-by-side comparison
    (loaded from the pre-existing CSV so we don't waste FinMind calls / re-derive raw-close
    logic -- it's a pure DB read replay of the original script, so it's re-run here fresh
    from stock_daily_bars directly, no FinMind calls involved).
  - A sensitivity run on the adjusted data EXCLUDING all stocks that have a
    capital_reduction event anywhere in stock_price_adjustment_events (~280 stocks), since
    those are the ones flagged as still carrying residual adj_close_v2 error pre-reduction.

Usage:
  cd "/Users/jackm4/goldenstocks" && PYTHONPATH=src .venv/bin/python \
      scripts/research/momentum_v2_backtest.py
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from stock_db.util import DEFAULT_DB_PATH  # noqa: E402

DB = str(DEFAULT_DB_PATH)
OUT_DIR = "/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/e3b860e0-a0b6-46e3-a0b0-96f7ea2cc18a/scratchpad"

S = 21
F_LIST = [63, 126, 189, 252]
H_LIST = [21, 63]
N_UNIVERSE = 80
COST_ROUNDTRIP = 0.012
DATE_LO = "2014-06-01"
DATE_HI = "2026-07-24"
REPORT_START = pd.Timestamp("2024-07-27")
REPORT_END = pd.Timestamp("2026-07-27")


# ---------------------------------------------------------------------------
# Shared engine: given a long df[stock_id, trade_date, close, volume], run the
# full F x H grid and return a results dataframe (one row per F,H,rebalance_date).
# ---------------------------------------------------------------------------
def run_grid(df, label, exclude_ids=None, verbose=True):
    exclude_ids = exclude_ids or set()
    df = df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    df["dollar_vol"] = df["close"] * df["volume"].fillna(0)

    df["prev_close"] = df.groupby("stock_id")["close"].shift(1)
    df["log_ret_1d"] = np.log(df["close"] / df["prev_close"])
    df["anomaly_flag"] = df["log_ret_1d"].abs() > np.log(1.30)
    n_anom = df["anomaly_flag"].sum()
    if verbose:
        print(f"[{label}] rows={len(df)} stocks={df.stock_id.nunique()} "
              f"anomaly_days={n_anom} anomaly_stocks={df.loc[df.anomaly_flag,'stock_id'].nunique()}")

    all_dates = sorted(df["trade_date"].unique())
    date_idx = {d: i for i, d in enumerate(all_dates)}

    close_wide = df.pivot(index="trade_date", columns="stock_id", values="close").reindex(all_dates)
    dv_wide = df.pivot(index="trade_date", columns="stock_id", values="dollar_vol").reindex(all_dates)
    anomaly_wide = df.pivot(index="trade_date", columns="stock_id", values="anomaly_flag").reindex(all_dates).fillna(False)
    anomaly_cum = anomaly_wide.cumsum()
    traded_wide = close_wide.notna()
    cum_traded = traded_wide.cumsum()

    date_series = pd.Series(all_dates)
    month_key = date_series.dt.to_period("M")
    month_end_dates = sorted(date_series.groupby(month_key).max().tolist())

    results_rows = []
    for F in F_LIST:
        for H in H_LIST:
            for t in month_end_dates:
                t_idx = date_idx[t]
                f_idx = t_idx - F
                s_idx = t_idx - S
                if f_idx < 0 or s_idx < 0:
                    continue
                entry_idx = t_idx + 1
                exit_idx = entry_idx + H
                if exit_idx >= len(all_dates):
                    continue
                entry_date = all_dates[entry_idx]
                exit_date = all_dates[exit_idx]
                hold_start_in_window = (entry_date >= REPORT_START) and (entry_date <= REPORT_END)

                liq_start = max(0, t_idx - 250)
                liq_window = dv_wide.iloc[liq_start:t_idx]
                med_dv = liq_window.median(axis=0, skipna=True)

                if t_idx - 1 < 0:
                    continue
                listing_days = cum_traded.iloc[t_idx - 1]
                min_listing = F + S + 20
                eligible_listing = listing_days[listing_days >= min_listing].index
                if exclude_ids:
                    eligible_listing = eligible_listing.difference(pd.Index(exclude_ids))

                med_dv_eligible = med_dv.reindex(eligible_listing).dropna()
                universe = med_dv_eligible.sort_values(ascending=False).head(N_UNIVERSE).index
                if len(universe) < 20:
                    continue

                close_tS = close_wide.iloc[s_idx]
                close_tF = close_wide.iloc[f_idx]
                close_u_tS = close_tS.reindex(universe)
                close_u_tF = close_tF.reindex(universe)
                valid = close_u_tS.notna() & close_u_tF.notna() & (close_u_tF > 0)
                mom = np.log(close_u_tS[valid] / close_u_tF[valid]).dropna()
                if len(mom) < 20:
                    continue

                close_entry = close_wide.iloc[entry_idx].reindex(mom.index)
                close_exit = close_wide.iloc[exit_idx].reindex(mom.index)
                valid_hold = close_entry.notna() & close_exit.notna() & (close_entry > 0)
                hold_ret = (close_exit[valid_hold] / close_entry[valid_hold]) - 1.0
                mom_valid = mom.reindex(hold_ret.index)

                anom_end = anomaly_cum.iloc[exit_idx].reindex(hold_ret.index)
                anom_before_start = anomaly_cum.iloc[f_idx - 1].reindex(hold_ret.index) if f_idx - 1 >= 0 else 0
                has_anomaly = (anom_end - anom_before_start) > 0
                clean_mask = ~has_anomaly.fillna(True)
                hold_ret = hold_ret[clean_mask]
                mom_valid = mom_valid[clean_mask]

                n = len(hold_ret)
                if n < 20:
                    continue

                ranks = mom_valid.rank(pct=True)
                quintile = pd.cut(ranks, bins=[0, 0.2, 0.4, 0.6, 0.8, 1.0000001],
                                   labels=[1, 2, 3, 4, 5], include_lowest=True)
                q_means = hold_ret.groupby(quintile, observed=False).mean()

                if 5 not in q_means.index or 1 not in q_means.index:
                    continue
                winner_ret = q_means.get(5, np.nan)
                loser_ret = q_means.get(1, np.nan)
                wl = winner_ret - loser_ret
                rank_ic = mom_valid.rank().corr(hold_ret.rank())

                results_rows.append({
                    "F": F, "H": H, "t": t, "entry_date": entry_date, "exit_date": exit_date,
                    "n_universe": len(universe), "n_valid": n,
                    "winner_ret": winner_ret, "loser_ret": loser_ret, "wl_ret": wl,
                    "rank_ic": rank_ic, "hold_start_in_window": hold_start_in_window,
                    "q1": q_means.get(1, np.nan), "q2": q_means.get(2, np.nan),
                    "q3": q_means.get(3, np.nan), "q4": q_means.get(4, np.nan),
                    "q5": q_means.get(5, np.nan),
                })

    return pd.DataFrame(results_rows)


def newey_west_t(x, lag):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3:
        return np.nan, np.nan, n
    mean = x.mean()
    resid = x - mean
    lag = max(0, int(lag))
    gamma0 = np.sum(resid ** 2) / n
    var = gamma0
    for L in range(1, lag + 1):
        w = 1 - L / (lag + 1)
        cov = np.sum(resid[L:] * resid[:-L]) / n
        var += 2 * w * cov
    se = np.sqrt(var / n)
    if se == 0 or np.isnan(se):
        return np.nan, mean, n
    return mean / se, mean, n


def analyze_group(g, H):
    g = g.sort_values("t")
    wl = g["wl_ret"].values
    lag = max(0, int(round(H / 21)) - 1) if H else 0
    t_nw, mean_wl, n = newey_west_t(wl, lag)
    std_wl = np.nanstd(wl, ddof=1)
    sharpe_like = (mean_wl / std_wl * np.sqrt(12)) if std_wl > 0 else np.nan
    hit_rate = np.mean(wl > 0) if n else np.nan
    net_wl = mean_wl - COST_ROUNDTRIP
    q_means = g[["q1", "q2", "q3", "q4", "q5"]].mean()
    monotonic = all(q_means.iloc[i] <= q_means.iloc[i + 1] + 1e-9 for i in range(4))
    ic_mean = g["rank_ic"].mean()
    ic_t, _, _ = newey_west_t(g["rank_ic"].values, lag)
    return {
        "n_months": n, "mean_wl_%": mean_wl * 100 if mean_wl == mean_wl else np.nan,
        "std_wl_%": std_wl * 100, "sharpe_like": sharpe_like, "nw_t": t_nw,
        "hit_rate": hit_rate, "net_wl_%": net_wl * 100 if net_wl == net_wl else np.nan,
        "monotonic": monotonic, "q1_%": q_means["q1"] * 100, "q5_%": q_means["q5"] * 100,
        "ic_mean": ic_mean, "ic_nw_t": ic_t,
    }


def summarize(res, label):
    rows = []
    for (F, H), g in res.groupby(["F", "H"]):
        r = analyze_group(g, H)
        r["F"] = F
        r["H"] = H
        r["label"] = label
        rows.append(r)
    return pd.DataFrame(rows)


def main():
    conn = sqlite3.connect(DB)

    # ---- ORIGINAL (raw, unadjusted close) — re-derived fresh from stock_daily_bars only,
    # identical query/logic to the original script, no FinMind calls.
    print("Loading RAW (unadjusted) close data from stock_daily_bars...")
    df_raw = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close, volume
        FROM stock_daily_bars
        WHERE source='finmind' AND trade_date >= '{DATE_LO}' AND trade_date <= '{DATE_HI}'
          AND stock_id NOT LIKE '00%'
          AND close IS NOT NULL AND close > 0
        """,
        conn,
    )
    df_raw["trade_date"] = pd.to_datetime(df_raw["trade_date"])

    # ---- ADJUSTED (adj_close_v2) — inner join bars (trade_date/volume) with
    # stock_close_adjusted (adj_close_v2 price). Only rows where adj_close_v2 exists survive.
    print("Loading ADJUSTED (adj_close_v2) data via JOIN stock_daily_bars + stock_close_adjusted...")
    df_adj = pd.read_sql_query(
        f"""
        SELECT b.stock_id AS stock_id, b.trade_date AS trade_date,
               a.adj_close_v2 AS close, b.volume AS volume
        FROM stock_daily_bars b
        JOIN stock_close_adjusted a
          ON a.stock_id = b.stock_id AND a.trade_date = b.trade_date AND a.source = 'finmind'
        WHERE b.source='finmind' AND b.trade_date >= '{DATE_LO}' AND b.trade_date <= '{DATE_HI}'
          AND b.stock_id NOT LIKE '00%'
          AND a.adj_close_v2 IS NOT NULL AND a.adj_close_v2 > 0
        """,
        conn,
    )
    df_adj["trade_date"] = pd.to_datetime(df_adj["trade_date"])

    # ---- capital-reduction stock list (for sensitivity exclusion) ----
    cap_red_ids = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_price_adjustment_events WHERE event_type='capital_reduction'"
        ).fetchall()
    )
    conn.close()

    n_stocks_raw = df_raw.stock_id.nunique()
    n_stocks_adj = df_adj.stock_id.nunique()
    coverage_pct = 100.0 * n_stocks_adj / n_stocks_raw if n_stocks_raw else float("nan")
    print(f"\nRAW universe: {n_stocks_raw} stocks, {len(df_raw)} rows")
    print(f"ADJ universe (adj_close_v2 available): {n_stocks_adj} stocks, {len(df_adj)} rows "
          f"({coverage_pct:.1f}% of RAW stock count)")
    print(f"NOTE: adj_close_v2 backfill appears to still be running concurrently in the DB "
          f"as of this run -- the {n_stocks_adj}-stock figure is a snapshot, not necessarily final.")
    print(f"capital_reduction-flagged stocks available for exclusion test: {len(cap_red_ids)}")

    # ---- run grids ----
    print("\nRunning grid: RAW (unadjusted close)...")
    res_raw = run_grid(df_raw, "RAW")
    print(f"  -> {len(res_raw)} (F,H,t) observations")

    print("\nRunning grid: ADJUSTED (adj_close_v2)...")
    res_adj = run_grid(df_adj, "ADJ")
    print(f"  -> {len(res_adj)} (F,H,t) observations")

    print("\nRunning grid: ADJUSTED ex-capital-reduction stocks (sensitivity test)...")
    res_adj_excap = run_grid(df_adj, "ADJ_EXCAP", exclude_ids=cap_red_ids)
    print(f"  -> {len(res_adj_excap)} (F,H,t) observations")

    res_raw.to_csv(f"{OUT_DIR}/momentum_v2_results_raw.csv", index=False)
    res_adj.to_csv(f"{OUT_DIR}/momentum_v2_results_adj.csv", index=False)
    res_adj_excap.to_csv(f"{OUT_DIR}/momentum_v2_results_adj_excap.csv", index=False)

    # ---- full-history summaries (2015-2026, all month-end rebalances) ----
    sum_raw = summarize(res_raw, "RAW")
    sum_adj = summarize(res_adj, "ADJ")
    sum_excap = summarize(res_adj_excap, "ADJ_EXCAP")

    merged = sum_raw.merge(sum_adj, on=["F", "H"], suffixes=("_raw", "_adj"))
    merged = merged.merge(sum_excap, on=["F", "H"])
    merged = merged.rename(columns={c: c + "_excap" for c in
                                     ["n_months", "mean_wl_%", "nw_t", "hit_rate", "monotonic", "ic_mean", "ic_nw_t"]
                                     if c in merged.columns})

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 40)

    print("\n" + "=" * 130)
    print("FULL HISTORY (2015-2026) COMPARISON: RAW close vs adj_close_v2 vs adj_close_v2-ex-capital-reduction")
    print("=" * 130)
    cols = ["F", "H",
            "n_months_raw", "mean_wl_%_raw", "nw_t_raw", "monotonic_raw",
            "n_months_adj", "mean_wl_%_adj", "nw_t_adj", "monotonic_adj",
            "n_months_excap", "mean_wl_%_excap", "nw_t_excap", "monotonic_excap"]
    cols = [c for c in cols if c in merged.columns]
    print(merged[cols].to_string(index=False))

    merged.to_csv(f"{OUT_DIR}/momentum_v2_summary_comparison.csv", index=False)

    print("\nSaved:")
    print(f"  {OUT_DIR}/momentum_v2_results_raw.csv")
    print(f"  {OUT_DIR}/momentum_v2_results_adj.csv")
    print(f"  {OUT_DIR}/momentum_v2_results_adj_excap.csv")
    print(f"  {OUT_DIR}/momentum_v2_summary_comparison.csv")


if __name__ == "__main__":
    main()
