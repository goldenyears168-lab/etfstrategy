#!/usr/bin/env python3
"""9217 松山在 2337 旺宏／6147 頎邦的買進擇時 + L1H7 跟單回測.

L1H7 SSOT：T+1 開盤進場、第7個交易日收盤出場、成本30bps（來回）、
β=1.15 對 IX0001 做超額報酬調整（比照 MINI_OPS_REFERENCE.md 既有協議）。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_l1h7_timing_2337_6147.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9217"
STOCKS = ["2337", "6147"]
HOLD_DAYS = 7
COST_RT = 0.003  # 30bps 來回
BETA = 1.15
CAMPAIGN_GAP = 10
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_buy_days(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, p.close, p.open
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind' AND b.buy > 0
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id))
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(stock_id,))
    return df.set_index("trade_date")


def timing_percentile(buy_days: pd.DataFrame, bars: pd.DataFrame, window: int = 20) -> pd.Series:
    close = bars["close"]
    roll_min = close.rolling(window).min()
    roll_max = close.rolling(window).max()
    pct = (close - roll_min) / (roll_max - roll_min)
    pct = pct.reindex(buy_days["trade_date"])
    return pct.reset_index(drop=True)


def merge_campaigns(dates: list[str], all_dates: list[str], gap: int = CAMPAIGN_GAP) -> list[str]:
    idx = {d: i for i, d in enumerate(all_dates)}
    dates = sorted(dates)
    campaigns = []
    cur_start = dates[0]
    last_idx = idx.get(dates[0])
    for d in dates[1:]:
        di = idx.get(d)
        if last_idx is not None and di is not None and di - last_idx <= gap:
            last_idx = di
            continue
        campaigns.append(cur_start)
        cur_start = d
        last_idx = di
    campaigns.append(cur_start)
    return campaigns


def l1h7_backtest(entry_signal_dates: list[str], bars: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    all_dates = bars.index.tolist()
    idx = {d: i for i, d in enumerate(all_dates)}
    bench_dates = bench.index.astype(str).tolist()
    bench_idx = {d: i for i, d in enumerate(bench_dates)}
    rows = []
    for sig_date in entry_signal_dates:
        if sig_date not in idx:
            continue
        i_sig = idx[sig_date]
        i_entry = i_sig + 1  # T+1 開盤進場
        i_exit = i_entry + HOLD_DAYS - 1  # 第7個交易日收盤
        if i_entry >= len(all_dates) or i_exit >= len(all_dates):
            continue
        entry_date = all_dates[i_entry]
        exit_date = all_dates[i_exit]
        entry_px = bars["open"].iloc[i_entry]
        exit_px = bars["close"].iloc[i_exit]
        if pd.isna(entry_px) or pd.isna(exit_px) or entry_px <= 0:
            continue
        raw_ret = exit_px / entry_px - 1.0
        net_ret = raw_ret - COST_RT
        if entry_date not in bench_idx or exit_date not in bench_idx:
            continue
        b0 = bench.iloc[bench_idx[entry_date]]
        b1 = bench.iloc[bench_idx[exit_date]]
        if pd.isna(b0) or pd.isna(b1) or b0 <= 0:
            continue
        bench_ret = b1 / b0 - 1.0
        excess = net_ret - BETA * bench_ret
        rows.append(
            {
                "signal_date": sig_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "raw_ret_pct": raw_ret * 100,
                "net_ret_pct": net_ret * 100,
                "bench_ret_pct": bench_ret * 100,
                "excess_pct": excess * 100,
            }
        )
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"label": label, "n": 0}
    vals = df["excess_pct"].dropna() / 100.0
    n = len(vals)
    mean = vals.mean() * 100
    win = (vals > 0).mean() * 100
    if n > 1 and vals.std() > 0:
        t_stat, p_val = stats.ttest_1samp(vals, 0)
    else:
        t_stat, p_val = np.nan, np.nan
    return {
        "label": label,
        "n": n,
        "mean_excess_pct": round(mean, 3),
        "win_rate_pct": round(win, 1),
        "t_stat": round(float(t_stat), 3) if not np.isnan(t_stat) else None,
        "p_value": round(float(p_val), 4) if not np.isnan(p_val) else None,
        "median_excess_pct": round(vals.median() * 100, 3),
    }


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    all_results = {}
    for stock_id in STOCKS:
        print(f"\n{'='*60}\n股票 {stock_id}\n{'='*60}")
        buy_days = load_buy_days(conn, stock_id)
        bars = load_bars(conn, stock_id)
        print(f"[INFO] 買進交易日數：{len(buy_days)}")

        # 擇時分析
        pct = timing_percentile(buy_days, bars)
        market_pct_all = ((bars["close"] - bars["close"].rolling(20).min()) /
                           (bars["close"].rolling(20).max() - bars["close"].rolling(20).min())).dropna()
        print(f"\n=== 擇時品質：買進日 vs 全期間 20日高低百分位 ===")
        print(f"他買進日百分位：中位數={pct.median():.3f}，平均={pct.mean():.3f}，n={pct.notna().sum()}")
        print(f"該股全部交易日百分位：中位數={market_pct_all.median():.3f}，平均={market_pct_all.mean():.3f}")
        ks_stat, ks_p = stats.ks_2samp(pct.dropna(), market_pct_all)
        mw_stat, mw_p = stats.mannwhitneyu(pct.dropna(), market_pct_all)
        print(f"KS檢定：stat={ks_stat:.3f}, p={ks_p:.2e}")
        print(f"Mann-Whitney U檢定：p={mw_p:.2e}")

        # 依買進金額分位數，看大單跟小單的擇時是否不同
        buy_days["pct"] = pct
        q75 = buy_days["buy_amt"].quantile(0.75)
        big = buy_days[buy_days["buy_amt"] >= q75]
        small = buy_days[buy_days["buy_amt"] < q75]
        print(f"\n大單（前25% 買進金額，n={len(big)}）擇時百分位中位數：{big['pct'].median():.3f}")
        print(f"小單（後75%，n={len(small)}）擇時百分位中位數：{small['pct'].median():.3f}")

        # L1H7 回測：全部買進日
        all_dates = bars.index.tolist()
        raw_signal_dates = buy_days["trade_date"].tolist()
        bt_all = l1h7_backtest(raw_signal_dates, bars, bench)
        s_all = summarize(bt_all, "全部買進日（原始，高度重疊）")
        print(f"\n=== L1H7 回測：全部買進日 ===")
        print(s_all)

        # L1H7 回測：合併行情（同一波買進合併，gap<=10交易日）
        campaign_dates = merge_campaigns(raw_signal_dates, all_dates)
        bt_camp = l1h7_backtest(campaign_dates, bars, bench)
        s_camp = summarize(bt_camp, "合併行情（去重疊）")
        print(f"\n=== L1H7 回測：合併行情（{len(campaign_dates)} 筆，去重疊） ===")
        print(s_camp)

        # L1H7 回測：只看大單（前25%金額）
        big_dates = big["trade_date"].tolist()
        bt_big = l1h7_backtest(big_dates, bars, bench)
        s_big = summarize(bt_big, "大單日（前25%金額）")
        print(f"\n=== L1H7 回測：大單日 ===")
        print(s_big)

        out_path = OUT_DIR / f"whale_9217_l1h7_{stock_id}_trades.csv"
        bt_all.to_csv(out_path, index=False)
        print(f"[OK] 全部買進日交易明細寫入 {out_path}")

        all_results[stock_id] = {"all": s_all, "campaign": s_camp, "big": s_big}

    conn.close()

    print(f"\n\n{'='*60}\n總結\n{'='*60}")
    for sid, r in all_results.items():
        print(f"\n{sid}:")
        for k, v in r.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
