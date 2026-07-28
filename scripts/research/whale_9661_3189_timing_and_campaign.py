#!/usr/bin/env python3
"""9661 富邦新店 在 3189 景碩 的擇時分析 + 去重疊(campaign)版 L1H7 回測.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/whale_9661_3189_timing_and_campaign.py
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

TRADER_ID = "9661"
STOCK_ID = "3189"
HOLD_DAYS = 7
COST_RT = 0.003
BETA = 1.15
CAMPAIGN_GAP = 10
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_buy_days(conn) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, p.close, p.open
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind' AND b.buy > 0
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, STOCK_ID))
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def load_bars(conn) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(STOCK_ID,))
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
        i_entry = i_sig + 1
        i_exit = i_entry + HOLD_DAYS - 1
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
        rows.append({
            "signal_date": sig_date, "entry_date": entry_date, "exit_date": exit_date,
            "entry_px": entry_px, "exit_px": exit_px,
            "raw_ret_pct": raw_ret * 100, "net_ret_pct": net_ret * 100,
            "bench_ret_pct": bench_ret * 100, "excess_pct": excess * 100,
        })
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
        "label": label, "n": n,
        "mean_excess_pct": round(mean, 3),
        "median_excess_pct": round(vals.median() * 100, 3),
        "win_rate_pct": round(win, 1),
        "t_stat": round(float(t_stat), 3) if not np.isnan(t_stat) else None,
        "p_value": round(float(p_val), 4) if not np.isnan(p_val) else None,
    }


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    buy_days = load_buy_days(conn)
    bars = load_bars(conn)
    conn.close()

    print(f"[INFO] 買進交易日數：{len(buy_days)} / 全部交易日：{len(bars)}"
          f"（買進日佔比 {len(buy_days)/len(bars)*100:.1f}%，交易頻率極高）")

    # === 擇時分析：20日高低百分位 ===
    pct = timing_percentile(buy_days, bars)
    market_pct_all = ((bars["close"] - bars["close"].rolling(20).min()) /
                       (bars["close"].rolling(20).max() - bars["close"].rolling(20).min())).dropna()
    print("\n=== 擇時品質：買進日 vs 全期間 20日高低百分位（0=區間最低，1=區間最高） ===")
    print(f"他買進日百分位：中位數={pct.median():.3f}，平均={pct.mean():.3f}，n={pct.notna().sum()}")
    print(f"該股全部交易日百分位：中位數={market_pct_all.median():.3f}，平均={market_pct_all.mean():.3f}")
    ks_stat, ks_p = stats.ks_2samp(pct.dropna(), market_pct_all)
    mw_stat, mw_p = stats.mannwhitneyu(pct.dropna(), market_pct_all)
    print(f"KS檢定：stat={ks_stat:.3f}, p={ks_p:.2e}")
    print(f"Mann-Whitney U檢定：p={mw_p:.2e}")

    buy_days["pct"] = pct
    q75 = buy_days["buy_amt"].quantile(0.75)
    q90 = buy_days["buy_amt"].quantile(0.90)
    big = buy_days[buy_days["buy_amt"] >= q75]
    huge = buy_days[buy_days["buy_amt"] >= q90]
    small = buy_days[buy_days["buy_amt"] < q75]
    print(f"\n大單（前25% 買進金額，門檻≥{q75/1e6:.1f}百萬, n={len(big)}）擇時百分位中位數：{big['pct'].median():.3f}，平均：{big['pct'].mean():.3f}")
    print(f"超大單（前10%，門檻≥{q90/1e6:.1f}百萬, n={len(huge)}）擇時百分位中位數：{huge['pct'].median():.3f}，平均：{huge['pct'].mean():.3f}")
    print(f"小單（後75%，n={len(small)}）擇時百分位中位數：{small['pct'].median():.3f}，平均：{small['pct'].mean():.3f}")
    mw2_stat, mw2_p = stats.mannwhitneyu(big["pct"].dropna(), small["pct"].dropna())
    print(f"大單 vs 小單 擇時百分位 Mann-Whitney U檢定：p={mw2_p:.2e}")

    # === L1H7 回測：全部買進日（高度重疊，僅供參考） ===
    all_dates = bars.index.tolist()
    raw_signal_dates = buy_days["trade_date"].tolist()
    bt_all = l1h7_backtest(raw_signal_dates, bars, bench)
    s_all = summarize(bt_all, "全部買進日（原始，高度重疊）")

    # === L1H7 回測：合併行情（去重疊，gap<=10交易日視為同一波） ===
    campaign_dates = merge_campaigns(raw_signal_dates, all_dates, gap=CAMPAIGN_GAP)
    bt_camp = l1h7_backtest(campaign_dates, bars, bench)
    s_camp = summarize(bt_camp, f"合併行情去重疊（gap<={CAMPAIGN_GAP}交易日，{len(campaign_dates)}筆）")

    # === L1H7 回測：只看大單 / 超大單 ===
    bt_big = l1h7_backtest(big["trade_date"].tolist(), bars, bench)
    s_big = summarize(bt_big, "大單日（前25%金額）")
    bt_huge = l1h7_backtest(huge["trade_date"].tolist(), bars, bench)
    s_huge = summarize(bt_huge, "超大單日（前10%金額）")

    # === 大單去重疊（campaign merge 只套用在大單子集） ===
    big_dates_sorted = sorted(big["trade_date"].tolist())
    big_campaign_dates = merge_campaigns(big_dates_sorted, all_dates, gap=CAMPAIGN_GAP) if big_dates_sorted else []
    bt_big_camp = l1h7_backtest(big_campaign_dates, bars, bench)
    s_big_camp = summarize(bt_big_camp, f"大單去重疊（{len(big_campaign_dates)}筆）")

    print("\n=== L1H7 回測彙總 ===")
    for s in (s_all, s_camp, s_big, s_huge, s_big_camp):
        print(s)

    bt_all.to_csv(OUT_DIR / "whale_9661_3189_l1h7_all_trades.csv", index=False)
    bt_camp.to_csv(OUT_DIR / "whale_9661_3189_l1h7_campaign_trades.csv", index=False)
    print(f"\n[OK] 交易明細寫入 {OUT_DIR}/whale_9661_3189_l1h7_*.csv")

    # 自相關檢查：signal_date 之間的交易日距離分布，量化重疊程度
    dd = pd.Series(raw_signal_dates)
    idx_map = {d: i for i, d in enumerate(all_dates)}
    gaps = dd.map(idx_map).diff().dropna()
    print(f"\n[INFO] 買進訊號間交易日距離：中位數={gaps.median():.0f}, "
          f"距離<7交易日(與下一筆L1H7窗口重疊)的比例={(gaps < 7).mean()*100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
