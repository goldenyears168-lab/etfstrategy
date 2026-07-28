#!/usr/bin/env python3
"""9227 (凱基-城中) deep dive v2 — replicates study_whale_branch_consistency.py's
EXACT methodology (gross buy amount, MEGA+ETF exclusion, campaign merge with
<=10 trading day gap collapsed to first entry) as the primary robust sample,
then extends with multi-horizon stats, timing quality, position building,
and amount-threshold sweep.

  PYTHONPATH=src .venv/bin/python scripts/research/whale_branch_deepdive_9227_v2.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9227"
DATE_START = "2024-07-01"
DATE_END = "2026-07-22"
TOP1_AMT_FLOOR = 5_000_000
CAMPAIGN_GAP = 10
HORIZONS = (1, 3, 5, 9, 14, 20)

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
MEGA_PATH = OUT_DIR / "ab58_xMega_copytrade" / "mega_blacklist_v1.json"


def load_mega():
    return set(json.loads(MEGA_PATH.read_text(encoding="utf-8"))["symbols"])


def load_full_market_frame(conn, mega):
    q = """
        SELECT b.trade_date, b.stock_id, b.securities_trader_id, b.buy, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source='finmind'
        WHERE b.source='finmind' AND b.buy > 0
          AND b.trade_date >= ? AND b.trade_date <= ?
    """
    df = pd.read_sql_query(q, conn, params=(DATE_START, DATE_END))
    df = df[~df["stock_id"].isin(mega)]
    df = df[~df["stock_id"].str.startswith("00")].copy()
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def compute_daily_top1(df):
    df = df.copy()
    df["rank"] = df.groupby(["trade_date", "stock_id"])["buy_amt"].rank(method="first", ascending=False)
    top1 = df[df["rank"] == 1][["trade_date", "stock_id", "securities_trader_id", "buy_amt", "net"]].rename(
        columns={"securities_trader_id": "top1_trader_id", "net": "top1_net", "buy_amt": "top1_amt"}
    )
    return top1


def merge_campaigns(events, trading_dates):
    idx = {d: i for i, d in enumerate(trading_dates)}
    campaigns = []
    for (stock_id, trader_id), g in events.groupby(["stock_id", "top1_trader_id"]):
        g = g.sort_values("trade_date")
        dates = g["trade_date"].tolist()
        amts = g["top1_amt"].tolist()
        cur_start = dates[0]
        cur_last_idx = idx.get(dates[0])
        n_in = 1
        amt_sum = amts[0]
        for d, a in zip(dates[1:], amts[1:]):
            di = idx.get(d)
            if cur_last_idx is not None and di is not None and di - cur_last_idx <= CAMPAIGN_GAP:
                cur_last_idx = di
                n_in += 1
                amt_sum += a
                continue
            campaigns.append({"stock_id": stock_id, "top1_trader_id": trader_id, "entry_date": cur_start,
                               "n_events_in_campaign": n_in, "entry_amt": amt_sum})
            cur_start = d
            cur_last_idx = di
            n_in = 1
            amt_sum = a
        campaigns.append({"stock_id": stock_id, "top1_trader_id": trader_id, "entry_date": cur_start,
                           "n_events_in_campaign": n_in, "entry_amt": amt_sum})
    return pd.DataFrame(campaigns).sort_values("entry_date").reset_index(drop=True)


def load_close_panel(conn, stock_ids):
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""SELECT trade_date, stock_id, close FROM stock_daily_bars
            WHERE source='finmind' AND stock_id IN ({placeholders}) ORDER BY trade_date"""
    df = pd.read_sql_query(q, conn, params=stock_ids)
    return df.pivot(index="trade_date", columns="stock_id", values="close")


def forward_returns_multi(campaigns, close, bench, horizons=HORIZONS):
    dates = close.index.tolist()
    idx = {d: i for i, d in enumerate(dates)}
    bench = bench.reindex(dates)
    rows = []
    for _, c in campaigns.iterrows():
        entry = c["entry_date"]
        if entry not in idx or c["stock_id"] not in close.columns:
            continue
        i0 = idx[entry]
        row = dict(c)
        s0 = close[c["stock_id"]].iloc[i0]
        b0 = bench.iloc[i0]
        for h in horizons:
            j = i0 + h
            if j >= len(dates) or pd.isna(s0) or s0 <= 0 or pd.isna(b0) or b0 <= 0:
                row[f"h{h}_excess"] = np.nan
                continue
            s1 = close[c["stock_id"]].iloc[j]
            b1 = bench.iloc[j]
            if pd.isna(s1) or pd.isna(b1):
                row[f"h{h}_excess"] = np.nan
            else:
                row[f"h{h}_excess"] = ((s1 / s0 - 1.0) - (b1 / b0 - 1.0)) * 100
        rows.append(row)
    return pd.DataFrame(rows)


def timing_percentile_for_campaigns(conn, campaigns, close):
    stock_ids = campaigns["stock_id"].unique().tolist()
    placeholders = ",".join("?" * len(stock_ids))
    bars = pd.read_sql_query(
        f"""SELECT stock_id, trade_date, close FROM stock_daily_bars
            WHERE source='finmind' AND stock_id IN ({placeholders}) ORDER BY stock_id, trade_date""",
        conn, params=stock_ids,
    )
    bars["roll_min20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).min())
    bars["roll_max20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).max())
    bars["pctile"] = (bars["close"] - bars["roll_min20"]) / (bars["roll_max20"] - bars["roll_min20"])
    key = bars.set_index(["stock_id", "trade_date"])["pctile"]
    vals = [key.get((row.stock_id, row.entry_date), np.nan) for row in campaigns.itertuples()]
    return pd.Series(vals, index=campaigns.index), bars["pctile"].dropna()


def main():
    conn = connect(DEFAULT_DB_PATH)
    mega = load_mega()
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted(bench.index.astype(str).unique().tolist())

    cache_path = OUT_DIR / "_deepdive_9227_daily_top1_cache.parquet"
    if cache_path.exists():
        print(f"[INFO] loading cached daily_top1 from {cache_path}")
        daily_top1 = pd.read_parquet(cache_path)
    else:
        print("[INFO] loading full market frame (excl MEGA + ETF)...")
        raw = load_full_market_frame(conn, mega)
        daily_top1 = compute_daily_top1(raw)
        daily_top1.to_parquet(cache_path)
    print(f"[INFO] all (date,stock) top1-by-gross-buy rows: {len(daily_top1):,}")

    ev = daily_top1[(daily_top1["top1_trader_id"] == TRADER_ID) & (daily_top1["top1_amt"] >= TOP1_AMT_FLOOR)].copy()
    print(f"[INFO] 9227 raw top1-buy days (amt>=500萬, ex-MEGA/ETF): n={len(ev)}, stocks={ev['stock_id'].nunique()}")

    campaigns = merge_campaigns(ev, trading_dates)
    print(f"[INFO] merged campaigns (<=10td gap collapsed): n={len(campaigns)}")

    stock_ids = sorted(campaigns["stock_id"].unique().tolist())
    close = load_close_panel(conn, stock_ids)
    fwd = forward_returns_multi(campaigns, close, bench)
    fwd.to_csv(OUT_DIR / "whale_branch_deepdive_9227_v2_campaigns_returns.csv", index=False)

    print("\n=== (a) 全交易剖面（campaign 層級） ===")
    print(f"campaigns: n={len(campaigns)}, distinct stocks={campaigns['stock_id'].nunique()}")
    print(f"金額(億元): median={campaigns['entry_amt'].median()/1e8:.3f}, mean={campaigns['entry_amt'].mean()/1e8:.3f}, max={campaigns['entry_amt'].max()/1e8:.3f}")
    stock_counts = campaigns.groupby("stock_id").size().sort_values(ascending=False)
    print("Top10 個股 campaign 數:")
    print(stock_counts.head(10))
    hh = (stock_counts / stock_counts.sum()) ** 2
    print(f"HHI: {hh.sum():.4f} (1/N_eff={1/hh.sum():.1f})")
    print(f"每 campaign 平均事件天數 (n_events_in_campaign): mean={campaigns['n_events_in_campaign'].mean():.2f}, median={campaigns['n_events_in_campaign'].median():.1f}")

    print("\n=== (c) 擇時品質（campaign entry day）===")
    tp, market_baseline = timing_percentile_for_campaigns(conn, campaigns, close)
    print(f"9227 campaign entry 擇時百分位: n={tp.notna().sum()}, mean={tp.mean():.3f}, median={tp.median():.3f}")
    print(f"市場基準: n={len(market_baseline)}, mean={market_baseline.mean():.3f}, median={market_baseline.median():.3f}")
    ks = stats.ks_2samp(tp.dropna(), market_baseline)
    print(f"KS test: stat={ks.statistic:.4f}, p={ks.pvalue:.4g}")

    print("\n=== (d) 統計顯著性（多 horizon，campaign 層級）===")
    for h in HORIZONS:
        s = fwd[f"h{h}_excess"].dropna()
        if len(s) < 5:
            continue
        t, p = stats.ttest_1samp(s, 0)
        ci = stats.t.interval(0.95, df=len(s) - 1, loc=s.mean(), scale=stats.sem(s))
        rng = np.random.default_rng(7)
        boots = [np.mean(rng.choice(s.values, size=len(s), replace=True)) for _ in range(2000)]
        bci = (np.percentile(boots, 2.5), np.percentile(boots, 97.5))
        print(f"H{h}: n={len(s)}, mean={s.mean():.3f}%, median={s.median():.3f}%, win%={100*(s>0).mean():.1f}, "
              f"t={t:.2f}, p={p:.4g}, 95%CI=({ci[0]:.2f}%,{ci[1]:.2f}%), bootCI=({bci[0]:.2f}%,{bci[1]:.2f}%)")

    print("\n=== H9 robustness checks ===")
    h9 = fwd["h9_excess"].dropna()
    lo, hi = np.percentile(h9, [5, 95])
    trim = h9[(h9 >= lo) & (h9 <= hi)]
    print(f"winsorize-trim 5-95%: n={len(trim)}, mean={trim.mean():.3f}%")
    sorted_h9 = h9.sort_values(ascending=False)
    for k in (1, 2, 3, 5):
        print(f"移除最大 {k} 個正報酬後: mean={sorted_h9.iloc[k:].mean():.3f}% (n={len(sorted_h9)-k})")
    print(f"median H9: {h9.median():.3f}%")

    print("\n=== (b) 建倉型態（campaign 數最多的前 8 檔）===")
    top_stocks = stock_counts.head(8).index.tolist()
    all_trades = pd.read_sql_query(
        "SELECT trade_date, stock_id, net FROM stock_broker_branch_daily WHERE source='finmind' AND securities_trader_id=? AND trade_date>=? ORDER BY stock_id, trade_date",
        conn, params=(TRADER_ID, DATE_START),
    )
    for sid in top_stocks:
        g = all_trades[all_trades["stock_id"] == sid].sort_values("trade_date").copy()
        g["cum_net"] = g["net"].cumsum()
        maxp, minp, finalp = g["cum_net"].max(), g["cum_net"].min(), g["cum_net"].iloc[-1]
        print(f"  {sid}: n_days={len(g)}, max_cum={maxp:.0f}, min_cum={minp:.0f}, final_cum={finalp:.0f}, final/max={finalp/maxp if maxp else float('nan'):.2f}")

    print("\n=== 金額門檻 sweep (campaign entry_amt) ===")
    for thr_yi in (0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 1.5):
        sub = fwd[fwd["entry_amt"] >= thr_yi * 1e8]
        s = sub["h9_excess"].dropna()
        if len(s) < 5:
            print(f"  金額>={thr_yi}億: n={len(s)} (太少)")
            continue
        t, p = stats.ttest_1samp(s, 0)
        print(f"  金額>={thr_yi}億: n={len(s)}, mean={s.mean():.3f}%, median={s.median():.3f}%, win%={100*(s>0).mean():.1f}, t={t:.2f}, p={p:.4g}")

    campaigns.to_csv(OUT_DIR / "whale_branch_deepdive_9227_v2_campaigns.csv", index=False)
    print("\nDone.")


if __name__ == "__main__":
    main()
