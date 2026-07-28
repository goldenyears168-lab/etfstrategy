#!/usr/bin/env python3
"""9661 (富邦-新店/陳族元) 在 2408 南亞科 的深挖研究.

任務：
  1. 累積淨部位建倉時間軸分析
  2. （門檻 sweep 另外用 study_whale_9217_6147_threshold_sweep.py --trader-id 9661 --stock-id 2408 跑）
  3. 擇時分析（20日高低百分位，KS/Mann-Whitney 檢定，大單vs小單）
  4. 兩分點交叉比對：9217（凱基松山/廖學邦）vs 9661（富邦新店/陳族元）在 2408 的買賣時間序列比對
  5. 誠實評估（在 stdout 印出摘要，供人工判讀，不在此腳本下結論）

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9661_2408_deepdive.py
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

STOCK_ID = "2408"
TRADER_MAIN = "9661"
TRADER_OTHER = "9217"
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"


def load_trader_stock(conn, trader_id: str, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, b.sell, b.net, p.close, p.open
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind'
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(trader_id, stock_id))
    df["buy_amt"] = df["buy"] * df["close"]
    df["sell_amt"] = df["sell"] * df["close"]
    df["net_amt"] = df["net"] * df["close"]
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(stock_id,)).set_index("trade_date")


def section(title: str) -> None:
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def part1_accumulation_timeline(df: pd.DataFrame) -> pd.DataFrame:
    section("(1) 累積淨部位建倉時間軸 -- 9661 / 2408")
    g = df.sort_values("trade_date").copy()
    g["cum_net_shares"] = g["net"].cumsum()
    g["cum_net_amt"] = g["net_amt"].cumsum()

    n_days = len(g)
    max_shares = g["cum_net_shares"].max()
    min_shares = g["cum_net_shares"].min()
    final_shares = g["cum_net_shares"].iloc[-1]
    retention = final_shares / max_shares if max_shares else float("nan")
    print(f"總交易日數（有買或賣的日子）: {n_days}")
    print(f"起始日: {g['trade_date'].iloc[0]}，最新日: {g['trade_date'].iloc[-1]}")
    print(f"累積淨股數峰值: {max_shares:,.0f} 股（{max_shares/1e3:.0f} 張）")
    print(f"累積淨股數谷值: {min_shares:,.0f} 股")
    print(f"目前累積淨股數（最新一筆）: {final_shares:,.0f} 股（{final_shares/1e3:.0f} 張）")
    print(f"retention（final/max）: {retention:.4f}")

    # 分年度看建倉節奏
    g["year"] = g["trade_date"].str[:4]
    yearly = g.groupby("year").agg(
        n_days=("trade_date", "count"),
        buy_amt_yi=("buy_amt", lambda x: x.sum() / 1e8),
        sell_amt_yi=("sell_amt", lambda x: x.sum() / 1e8),
        net_amt_yi=("net_amt", lambda x: x.sum() / 1e8),
        net_shares_k=("net", lambda x: x.sum() / 1e3),
    ).round(3)
    print("\n分年度買賣金額（億元）與淨股數（張）:")
    print(yearly.to_string())

    # 里程碑：累積淨股數每跨過 max 的 10% 級距時的日期（觀察建倉節奏是否平滑或分批）
    print("\n累積淨股數達到峰值特定比例時的日期（觀察建倉是否平滑/分批）:")
    milestones = [0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    reached = set()
    for _, row in g.iterrows():
        for m in milestones:
            if m not in reached and max_shares > 0 and row["cum_net_shares"] >= m * max_shares:
                print(f"  達峰值 {m*100:.0f}%: {row['trade_date']} (累積 {row['cum_net_shares']:,.0f} 股)")
                reached.add(m)

    out_path = OUT_DIR / f"whale_9661_2408_accumulation_timeline.csv"
    g[["trade_date", "buy", "sell", "net", "buy_amt", "sell_amt", "net_amt", "cum_net_shares", "cum_net_amt"]].to_csv(
        out_path, index=False
    )
    print(f"\n[OK] 完整時間軸寫入 {out_path}")
    return g


def part3_timing(df_buy: pd.DataFrame, bars: pd.DataFrame) -> None:
    section("(3) 擇時分析 -- 9661 / 2408 （20日高低百分位）")
    close = bars["close"]
    roll_min = close.rolling(20).min()
    roll_max = close.rolling(20).max()
    pct_all = (close - roll_min) / (roll_max - roll_min)

    buy_only = df_buy[df_buy["buy"] > 0].copy()
    buy_only["pct"] = pct_all.reindex(buy_only["trade_date"]).values

    market_pct = pct_all.dropna()
    print(f"買進交易日數: {len(buy_only)}")
    print(f"他買進日 20日百分位: 中位數={buy_only['pct'].median():.3f}, 平均={buy_only['pct'].mean():.3f}, n={buy_only['pct'].notna().sum()}")
    print(f"該股全部交易日 20日百分位: 中位數={market_pct.median():.3f}, 平均={market_pct.mean():.3f}, n={len(market_pct)}")

    ks_stat, ks_p = stats.ks_2samp(buy_only["pct"].dropna(), market_pct)
    mw_stat, mw_p = stats.mannwhitneyu(buy_only["pct"].dropna(), market_pct)
    print(f"KS 檢定: stat={ks_stat:.4f}, p={ks_p:.4g}")
    print(f"Mann-Whitney U 檢定: p={mw_p:.4g}")

    q75 = buy_only["buy_amt"].quantile(0.75)
    big = buy_only[buy_only["buy_amt"] >= q75]
    small = buy_only[buy_only["buy_amt"] < q75]
    print(f"\n大單（前25%買進金額, n={len(big)}）擇時百分位中位數: {big['pct'].median():.3f}")
    print(f"小單（後75%, n={len(small)}）擇時百分位中位數: {small['pct'].median():.3f}")

    # 賣出日的擇時（觀察是否高點賣出）
    sell_only = df_buy[df_buy["sell"] > 0].copy()
    sell_only["pct"] = pct_all.reindex(sell_only["trade_date"]).values
    print(f"\n賣出交易日數: {len(sell_only)}")
    print(f"他賣出日 20日百分位: 中位數={sell_only['pct'].median():.3f}, 平均={sell_only['pct'].mean():.3f}")


def merge_campaigns(dates: list[str], all_dates: list[str], gap: int = 10) -> list[str]:
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


def l1h7_backtest(signal_dates: list[str], bars: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    all_dates = bars.index.tolist()
    idx = {d: i for i, d in enumerate(all_dates)}
    bdates = bench.index.astype(str).tolist()
    bidx = {d: i for i, d in enumerate(bdates)}
    rows = []
    for sig in signal_dates:
        if sig not in idx:
            continue
        i_entry = idx[sig] + 1
        i_exit = i_entry + 7 - 1
        if i_entry >= len(all_dates) or i_exit >= len(all_dates):
            continue
        ed, xd = all_dates[i_entry], all_dates[i_exit]
        ep, xp = bars["open"].iloc[i_entry], bars["close"].iloc[i_exit]
        if pd.isna(ep) or pd.isna(xp) or ep <= 0:
            continue
        if ed not in bidx or xd not in bidx:
            continue
        b0, b1 = bench.iloc[bidx[ed]], bench.iloc[bidx[xd]]
        if pd.isna(b0) or pd.isna(b1) or b0 <= 0:
            continue
        net = (xp / ep - 1.0) - 0.003
        bret = b1 / b0 - 1.0
        rows.append({"signal_date": sig, "excess_pct": (net - 1.15 * bret) * 100})
    return pd.DataFrame(rows)


def part2b_campaign_robustness(conn, df_buy: pd.DataFrame, bars: pd.DataFrame) -> None:
    section("(2b) 門檻 sweep 穩健性檢查 -- 原始每日訊號 vs 合併行情（去重疊）")
    print("提醒：門檻 sweep（study_whale_9217_6147_threshold_sweep.py）用的是「每日買進訊號」直接跑")
    print("L1H7（T+1進場,7交易日出場），同一波買進會產生連續多天高度重疊的訊號與報酬窗口，")
    print("t 檢定假設獨立樣本，重疊會嚴重灌水顯著性。這裡把同一波買進合併成 campaign（gap<=10交易日")
    print("視為同一波）之後再跑一次，看看訊號是否還有獨立層級的顯著性。\n")

    all_dates = bars.index.tolist()
    buy_only = df_buy[df_buy["buy"] > 0].copy()
    rows = []
    for floor_m in (5, 10, 20, 30, 50):
        floor = floor_m * 1e6
        sel = buy_only[buy_only["buy_amt"] >= floor]
        if len(sel) == 0:
            continue
        raw_bt = l1h7_backtest(sel["trade_date"].tolist(), bars, load_benchmark_close(conn, code="IX0001"))
        camp_dates = merge_campaigns(sel["trade_date"].tolist(), all_dates, gap=10)
        camp_bt = l1h7_backtest(camp_dates, bars, load_benchmark_close(conn, code="IX0001"))
        raw_vals = raw_bt["excess_pct"].dropna()
        camp_vals = camp_bt["excess_pct"].dropna()
        raw_t, raw_p = (stats.ttest_1samp(raw_vals, 0) if len(raw_vals) > 1 else (np.nan, np.nan))
        camp_t, camp_p = (stats.ttest_1samp(camp_vals, 0) if len(camp_vals) > 1 else (np.nan, np.nan))
        rows.append({
            "門檻": f">={floor_m}百萬元",
            "原始訊號_n": len(raw_vals),
            "原始_mean%": round(raw_vals.mean(), 3) if len(raw_vals) else None,
            "原始_median%": round(raw_vals.median(), 3) if len(raw_vals) else None,
            "原始_win%": round((raw_vals > 0).mean() * 100, 1) if len(raw_vals) else None,
            "原始_p": round(float(raw_p), 4) if not np.isnan(raw_p) else None,
            "合併campaign_n": len(camp_vals),
            "campaign_mean%": round(camp_vals.mean(), 3) if len(camp_vals) else None,
            "campaign_median%": round(camp_vals.median(), 3) if len(camp_vals) else None,
            "campaign_win%": round((camp_vals > 0).mean() * 100, 1) if len(camp_vals) else None,
            "campaign_p": round(float(camp_p), 4) if not np.isnan(camp_p) else None,
        })
    out = pd.DataFrame(rows)
    print(out.to_string(index=False))
    out_path = OUT_DIR / "whale_9661_2408_campaign_robustness_check.csv"
    out.to_csv(out_path, index=False)
    print(f"\n[OK] 寫入 {out_path}")


def part4_cross_branch(conn) -> pd.DataFrame:
    section("(4) 兩分點交叉比對 -- 9217（凱基松山/廖學邦） vs 9661（富邦新店/陳族元）在 2408")
    q = """
        SELECT trade_date, securities_trader_id, buy, sell
        FROM stock_broker_branch_daily
        WHERE stock_id='2408' AND securities_trader_id IN ('9217','9661') AND source='finmind'
        ORDER BY trade_date
    """
    raw = pd.read_sql_query(q, conn)
    raw["net"] = raw["buy"] - raw["sell"]

    piv_net = raw.pivot_table(index="trade_date", columns="securities_trader_id", values="net", aggfunc="sum").fillna(0)
    piv_buy = raw.pivot_table(index="trade_date", columns="securities_trader_id", values="buy", aggfunc="sum").fillna(0)
    piv_sell = raw.pivot_table(index="trade_date", columns="securities_trader_id", values="sell", aggfunc="sum").fillna(0)

    n_both_active = ((piv_buy.get("9217", 0) > 0) | (piv_sell.get("9217", 0) > 0)) & (
        (piv_buy.get("9661", 0) > 0) | (piv_sell.get("9661", 0) > 0)
    )
    print(f"9217 有交易的日數: {(piv_buy['9217'] > 0).sum() + (piv_sell['9217'] > 0).sum() - ((piv_buy['9217']>0)&(piv_sell['9217']>0)).sum()}")
    print(f"9661 有交易的日數: {(piv_buy['9661'] > 0).sum() + (piv_sell['9661'] > 0).sum() - ((piv_buy['9661']>0)&(piv_sell['9661']>0)).sum()}")
    both_active_days = piv_net.index[n_both_active]
    print(f"兩分點同一天都有交易（買或賣，不論方向）的日數: {len(both_active_days)}")

    # 同方向 net（都淨買或都淨賣）
    same_dir = np.sign(piv_net["9217"]) == np.sign(piv_net["9661"])
    same_dir_nonzero = same_dir & (piv_net["9217"] != 0) & (piv_net["9661"] != 0)
    print(f"兩分點同一天 net 買賣方向一致（都淨買或都淨賣，排除任一為0）的日數: {same_dir_nonzero.sum()} / {(piv_net['9217']!=0).sum()} 天 9217有淨部位")

    both_nonzero = (piv_net["9217"] != 0) & (piv_net["9661"] != 0)
    n_both_nonzero = both_nonzero.sum()
    if n_both_nonzero > 0:
        pct_same_dir = same_dir_nonzero.sum() / n_both_nonzero * 100
        print(f"在兩者都有淨部位變化的 {n_both_nonzero} 天中，方向一致比例: {pct_same_dir:.1f}%")

    # 買進日重疊：兩分點同一天都買進 (buy>0)
    both_buy = (piv_buy.get("9217", 0) > 0) & (piv_buy.get("9661", 0) > 0)
    print(f"\n兩分點同一天都買進(buy>0)的日數: {both_buy.sum()}")
    print(f"9217 買進日總數: {(piv_buy['9217']>0).sum()}, 9661 買進日總數: {(piv_buy['9661']>0).sum()}")
    # baseline: 若兩者完全獨立，預期重疊天數 = (n1/N)*(n2/N)*N
    N = len(piv_buy)
    n1 = (piv_buy["9217"] > 0).sum()
    n2 = (piv_buy["9661"] > 0).sum()
    expected_overlap = n1 * n2 / N
    print(f"若完全獨立隨機分布，預期重疊天數(近似): {expected_overlap:.1f}（實際 {both_buy.sum()}）")

    # 相關係數：每日 net 股數（皮爾森 + 史皮爾曼）
    valid = piv_net.dropna()
    pear_r, pear_p = stats.pearsonr(valid["9217"], valid["9661"])
    spear_r, spear_p = stats.spearmanr(valid["9217"], valid["9661"])
    print(f"\n每日 net 股數 皮爾森相關係數: r={pear_r:.4f}, p={pear_p:.4g}, n={len(valid)}")
    print(f"每日 net 股數 史皮爾曼相關係數: rho={spear_r:.4f}, p={spear_p:.4g}")

    # 每日 buy 金額相關（只看有交易的重疊子集，避免大量雙0膨脹相關係數）
    both_buy_days = piv_buy[(piv_buy["9217"] > 0) | (piv_buy["9661"] > 0)]
    if len(both_buy_days) > 5:
        pear_r2, pear_p2 = stats.pearsonr(both_buy_days["9217"], both_buy_days["9661"])
        print(f"（僅任一方有買進的 {len(both_buy_days)} 天）buy 股數皮爾森相關: r={pear_r2:.4f}, p={pear_p2:.4g}")

    # lead-lag: 9661 net 對 9217 net 的領先/落後相關（+/-5天）
    print("\nLead-lag 相關（9217 net 領先 k 天 vs 9661 net，k為正表示9217領先）:")
    s1 = valid["9217"]
    s2 = valid["9661"]
    for k in range(-5, 6):
        if k == 0:
            r, p = stats.pearsonr(s1, s2)
        elif k > 0:
            r, p = stats.pearsonr(s1.iloc[:-k].values, s2.iloc[k:].values)
        else:
            r, p = stats.pearsonr(s1.iloc[-k:].values, s2.iloc[:k].values)
        print(f"  k={k:+d}: r={r:.4f}, p={p:.4g}")

    # 累積淨部位比較圖表資料（輸出CSV給人工比對/畫圖）
    cmp_df = piv_net.copy()
    cmp_df.columns = [f"net_{c}" for c in cmp_df.columns]
    cmp_df["cum_net_9217"] = piv_net.get("9217", 0).cumsum()
    cmp_df["cum_net_9661"] = piv_net.get("9661", 0).cumsum()
    out_path = OUT_DIR / "whale_9661_vs_9217_2408_cross_compare.csv"
    cmp_df.reset_index().to_csv(out_path, index=False)
    print(f"\n[OK] 兩分點每日 net + 累積 net 序列寫入 {out_path}")

    return cmp_df


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)

    df = load_trader_stock(conn, TRADER_MAIN, STOCK_ID)
    bars = load_bars(conn, STOCK_ID)

    part1_accumulation_timeline(df)
    part2b_campaign_robustness(conn, df, bars)
    part3_timing(df, bars)
    part4_cross_branch(conn)

    conn.close()
    section("完成")
    print("門檻 sweep 請另外參考 whale_9661_2408_threshold_sweep.csv"
          "（用 study_whale_9217_6147_threshold_sweep.py --trader-id 9661 --stock-id 2408 產生）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
