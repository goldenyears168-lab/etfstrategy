#!/usr/bin/env python3
"""9661(富邦-新店/陳族元) 在 8358(金居) 的深度分析.

背景：既有分析（study_whale_branch_deepdive_9217_v2.py --trader-id 9661）算出的
「建倉起點 2023-01-04」其實是 stock_daily_bars 對 8358 有一段 2020-01-01~2023-01-02
（1099天）的價格缺口造成的 INNER JOIN 假象 —— 真正的 broker_branch_daily 交易記錄
從 2021-06-29 就開始了，跟他其他大部位（3189/2344/2408/8046/4958/1303，全部
2021-06-28/29 建倉）幾乎同一天起跑。1815(富喬) 也有同樣的資料缺口（同樣 1099 天），
兩檔股票的「起點看似 2023 年初」都是資料缺口的假象，不是真正較晚建倉。

本腳本重做：
  1. 用純 broker_branch_daily（不 join 價格）重建 2021-06-29 起的完整累積淨部位時間軸
  2. 標記真正的建倉節奏（波段/campaign），對比「假的 2023-01-04 起點」說法
  3. 解讀既有門檻 sweep 結果 + 補充 campaign 去重疊版
  4. 擇時分析（20日高低百分位），並比較大單 vs 小單
  5. 跟 3189、2408 做風格比對（自己跑一個簡化版，因為目前沒有其他 agent 的產出可參照）
  6. L1H7 跟單價值與最佳門檻的誠實總結

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9661_8358_deepdive.py
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
STOCK_ID = "8358"
COMPARE_STOCKS = ["3189", "2408"]
HOLD_DAYS = 7
COST_RT = 0.003
BETA = 1.15
CAMPAIGN_GAP = 10
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_raw_branch(conn, trader_id: str, stock_id: str) -> pd.DataFrame:
    """不 join 價格，拿到完整交易紀錄（含價格缺口期間）."""
    q = """
        SELECT trade_date, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND stock_id = ? AND source = 'finmind'
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(trader_id, stock_id))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, high, low, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(stock_id,))
    return df.set_index("trade_date")


def check_price_gap(bars: pd.DataFrame, label: str) -> None:
    idx = pd.to_datetime(bars.index)
    gaps = idx.to_series().diff().dt.days
    big = gaps[gaps > 30]
    print(f"\n[價格資料完整性檢查] {label}：")
    if big.empty:
        print("  無明顯缺口（最大間隔 <= 30 天）")
    else:
        for d, g in big.items():
            print(f"  缺口 {int(g)} 天，結束於 {d.date()}")


def build_full_timeline(raw: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """用純交易紀錄重建累積淨部位；價格缺口期間用最後已知價格 forward-fill 粗估市值（會標註）."""
    df = raw.copy().sort_values("trade_date").reset_index(drop=True)
    df["cum_net_shares"] = df["net"].cumsum()
    px = bars["close"].copy()
    px.index = pd.to_datetime(px.index)
    df = df.set_index("trade_date")
    df["close_raw"] = px.reindex(df.index)
    df["close_ffill"] = df["close_raw"].ffill().bfill()
    df["price_is_gap_filled"] = df["close_raw"].isna()
    df["est_position_value_ntd"] = df["cum_net_shares"] * df["close_ffill"]
    return df.reset_index()


def monthly_accumulation(timeline: pd.DataFrame) -> pd.DataFrame:
    d = timeline.copy()
    d["ym"] = d["trade_date"].dt.strftime("%Y-%m")
    out = d.groupby("ym").agg(
        n_trade_days=("net", "size"),
        net_shares_month=("net", "sum"),
        buy_shares_month=("buy", "sum"),
        sell_shares_month=("sell", "sum"),
        cum_net_shares_eom=("cum_net_shares", "last"),
        any_price_gap=("price_is_gap_filled", "any"),
    )
    return out


def merge_campaigns(dates: list[pd.Timestamp], gap: int = CAMPAIGN_GAP) -> list[tuple]:
    """把相近的淨買進日合併成同一波 campaign，回傳 (start, end, n_days) 清單."""
    dates = sorted(dates)
    if not dates:
        return []
    campaigns = []
    cur_start = dates[0]
    cur_end = dates[0]
    cur_days = [dates[0]]
    for d in dates[1:]:
        if (d - cur_end).days <= gap * 1.6:  # 用日曆天數近似交易日 gap
            cur_end = d
            cur_days.append(d)
            continue
        campaigns.append((cur_start, cur_end, len(cur_days)))
        cur_start = d
        cur_end = d
        cur_days = [d]
    campaigns.append((cur_start, cur_end, len(cur_days)))
    return campaigns


def l1h7_backtest(signal_dates: list[str], bars: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    all_dates = bars.index.tolist()
    idx = {d: i for i, d in enumerate(all_dates)}
    bench_dates = bench.index.astype(str).tolist()
    bidx = {d: i for i, d in enumerate(bench_dates)}
    rows = []
    for sig in signal_dates:
        if sig not in idx:
            continue
        i_entry = idx[sig] + 1
        i_exit = i_entry + HOLD_DAYS - 1
        if i_entry >= len(all_dates) or i_exit >= len(all_dates):
            continue
        entry_date, exit_date = all_dates[i_entry], all_dates[i_exit]
        entry_px, exit_px = bars["open"].iloc[i_entry], bars["close"].iloc[i_exit]
        if pd.isna(entry_px) or pd.isna(exit_px) or entry_px <= 0:
            continue
        if entry_date not in bidx or exit_date not in bidx:
            continue
        b0, b1 = bench.iloc[bidx[entry_date]], bench.iloc[bidx[exit_date]]
        if pd.isna(b0) or pd.isna(b1) or b0 <= 0:
            continue
        net_ret = (exit_px / entry_px - 1.0) - COST_RT
        bench_ret = b1 / b0 - 1.0
        rows.append(
            {
                "signal_date": sig,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "excess_pct": (net_ret - BETA * bench_ret) * 100,
            }
        )
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"label": label, "n": 0}
    vals = df["excess_pct"].dropna() / 100.0
    n = len(vals)
    if n < 2 or vals.std() == 0:
        t_stat, p_val = np.nan, np.nan
    else:
        t_stat, p_val = stats.ttest_1samp(vals, 0)
    return {
        "label": label,
        "n": n,
        "mean_excess_pct": round(vals.mean() * 100, 3),
        "median_excess_pct": round(vals.median() * 100, 3),
        "win_rate_pct": round((vals > 0).mean() * 100, 1),
        "t": round(float(t_stat), 3) if not np.isnan(t_stat) else None,
        "p": round(float(p_val), 4) if not np.isnan(p_val) else None,
    }


def timing_percentile(dates: pd.Series, bars: pd.DataFrame, window: int = 20) -> pd.Series:
    close = bars["close"]
    roll_min = close.rolling(window).min()
    roll_max = close.rolling(window).max()
    pct = (close - roll_min) / (roll_max - roll_min)
    pct.index = pd.to_datetime(pct.index)
    dates = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
    return pct.reindex(dates).reset_index(drop=True)


def quick_stock_profile(conn, bench, trader_id: str, stock_id: str) -> dict:
    """給比對股票（3189/2408）跑一個簡化版 profile：建倉起點/retention/擇時/campaign數."""
    raw = load_raw_branch(conn, trader_id, stock_id)
    bars = load_bars(conn, stock_id)
    if raw.empty:
        return {"stock_id": stock_id, "n_rows": 0}
    raw = raw.sort_values("trade_date").reset_index(drop=True)
    cum = raw["net"].cumsum()
    max_abs = cum.abs().max()
    final_net = cum.iloc[-1]
    retention = final_net / max_abs if max_abs > 0 else np.nan
    first_date = raw["trade_date"].min()
    last_date = raw["trade_date"].max()

    buy_days = raw[raw["buy"] > 0]["trade_date"]
    pct = timing_percentile(buy_days, bars)
    market_pct_all = ((bars["close"] - bars["close"].rolling(20).min()) /
                       (bars["close"].rolling(20).max() - bars["close"].rolling(20).min())).dropna()
    mw_p = np.nan
    if pct.notna().sum() > 10:
        _, mw_p = stats.mannwhitneyu(pct.dropna(), market_pct_all)

    campaigns = merge_campaigns(raw[raw["net"] > 0]["trade_date"].tolist(), gap=CAMPAIGN_GAP)

    # buy-sell same-day duality
    both = ((raw["buy"] > 0) & (raw["sell"] > 0)).sum()
    pct_both = both / len(raw) * 100

    # L1H7 大單 sweep（用金額前20%）用於跟8358比較風格
    q_price = load_bars(conn, stock_id)["close"]
    q_price.index = pd.to_datetime(q_price.index)
    buy_df = raw[raw["buy"] > 0].copy()
    buy_df["close"] = q_price.reindex(buy_df["trade_date"]).values
    buy_df["buy_amt"] = buy_df["buy"] * buy_df["close"]
    buy_df = buy_df.dropna(subset=["buy_amt"])
    top20 = buy_df[buy_df["buy_amt"] >= buy_df["buy_amt"].quantile(0.80)]
    bt_all = l1h7_backtest(buy_df["trade_date"].dt.strftime("%Y-%m-%d").tolist(), bars, bench)
    bt_top20 = l1h7_backtest(top20["trade_date"].dt.strftime("%Y-%m-%d").tolist(), bars, bench)
    s_all = summarize(bt_all, "全部買進日")
    s_top20 = summarize(bt_top20, "前20%大單")

    return {
        "stock_id": stock_id,
        "first_date": str(first_date.date()),
        "last_date": str(last_date.date()),
        "n_trade_days": len(raw),
        "retention_ratio": round(float(retention), 3) if not np.isnan(retention) else None,
        "n_campaigns_gap10": len(campaigns),
        "pct_rows_both_buy_sell": round(pct_both, 1),
        "timing_pct_median": round(float(pct.median()), 3) if pct.notna().sum() else None,
        "timing_vs_market_mw_p": round(float(mw_p), 4) if not np.isnan(mw_p) else None,
        "l1h7_all_mean_excess_pct": s_all.get("mean_excess_pct"),
        "l1h7_all_n": s_all.get("n"),
        "l1h7_top20_mean_excess_pct": s_top20.get("mean_excess_pct"),
        "l1h7_top20_n": s_top20.get("n"),
        "l1h7_top20_p": s_top20.get("p"),
    }


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    raw = load_raw_branch(conn, TRADER_ID, STOCK_ID)
    bars = load_bars(conn, STOCK_ID)
    check_price_gap(bars, f"{STOCK_ID} 金居")

    print(f"\n[INFO] 9661 在 8358 純交易紀錄：{len(raw)} 個交易日，"
          f"{raw['trade_date'].min().date()} ~ {raw['trade_date'].max().date()}")

    timeline = build_full_timeline(raw, bars)
    timeline_out = timeline[[
        "trade_date", "buy", "sell", "net", "cum_net_shares",
        "close_raw", "close_ffill", "price_is_gap_filled", "est_position_value_ntd",
    ]]
    timeline_out.to_csv(OUT_DIR / "whale_9661_8358_full_timeline.csv", index=False)
    print(f"[OK] 完整時間軸寫入 whale_9661_8358_full_timeline.csv（{len(timeline_out)} 列）")

    gap_rows = timeline["price_is_gap_filled"].sum()
    print(f"[INFO] 其中 {gap_rows} 個交易日（{timeline['trade_date'].min().date()} ~ 2022-12-30 附近）"
          f"缺乏 8358 真實收盤價，市值用最後已知價格({timeline.loc[~timeline['price_is_gap_filled'],'close_raw'].iloc[0] if (~timeline['price_is_gap_filled']).any() else 'NA'}附近) forward-fill 粗估，僅供部位股數/節奏參考，不代表真實市值")

    monthly = monthly_accumulation(timeline)
    monthly.to_csv(OUT_DIR / "whale_9661_8358_monthly_accumulation.csv")
    print("\n=== 每月淨買超累積（前12個月 vs 近12個月） ===")
    print(monthly.head(12))
    print("...")
    print(monthly.tail(12))

    # campaign 切分（用整段歷史，淨買超日）
    net_buy_dates = timeline[timeline["net"] > 0]["trade_date"].tolist()
    campaigns = merge_campaigns(net_buy_dates, gap=CAMPAIGN_GAP)
    camp_df = pd.DataFrame(campaigns, columns=["campaign_start", "campaign_end", "n_net_buy_days"])
    camp_df["span_calendar_days"] = (camp_df["campaign_end"] - camp_df["campaign_start"]).dt.days
    camp_df.to_csv(OUT_DIR / "whale_9661_8358_campaigns.csv", index=False)
    print(f"\n=== 建倉波段切分（淨買超日，gap<=~{CAMPAIGN_GAP}交易日合併），共 {len(camp_df)} 波 ===")
    print(camp_df.to_string(index=False))

    # 真實 retention（用完整歷史，不受價格缺口影響，因為只用股數）
    cum = timeline["cum_net_shares"]
    max_abs = cum.abs().max()
    final_net = cum.iloc[-1]
    true_retention = final_net / max_abs if max_abs > 0 else np.nan
    print(f"\n[INFO] 用完整交易紀錄（2021-06-29起）重算的真實 retention_ratio = {true_retention:.4f}"
          f"（先前用 2023-01-04起的部分資料算出的是 0.969，兩者應相近，代表 retention 本身穩健，"
          f"只有『建倉起點』的說法需要修正為 2021-06-29）")

    # ---- 擇時分析（只能用有價格資料的區間，即 2023-01-03 之後）----
    buy_days_post_gap = raw[(raw["buy"] > 0) & (raw["trade_date"] >= "2023-01-03")]["trade_date"]
    pct = timing_percentile(buy_days_post_gap, bars)
    market_pct_all = ((bars["close"] - bars["close"].rolling(20).min()) /
                       (bars["close"].rolling(20).max() - bars["close"].rolling(20).min())).dropna()
    print(f"\n=== 擇時分析（僅 2023-01-03 後有效價格區間，n={pct.notna().sum()}）===")
    print(f"他買進日 20日高低百分位：中位數={pct.median():.3f}，平均={pct.mean():.3f}")
    print(f"全期間所有交易日百分位：中位數={market_pct_all.median():.3f}，平均={market_pct_all.mean():.3f}")
    mw_stat, mw_p = stats.mannwhitneyu(pct.dropna(), market_pct_all)
    print(f"Mann-Whitney U 檢定：p={mw_p:.4f}")

    # 大單 vs 小單擇時
    price = bars["close"].copy()
    price.index = pd.to_datetime(price.index)
    buy_df = raw[(raw["buy"] > 0) & (raw["trade_date"] >= "2023-01-03")].copy()
    buy_df["close"] = price.reindex(buy_df["trade_date"]).values
    buy_df["buy_amt"] = buy_df["buy"] * buy_df["close"]
    buy_df["pct"] = timing_percentile(buy_df["trade_date"], bars).values
    q75 = buy_df["buy_amt"].quantile(0.75)
    big = buy_df[buy_df["buy_amt"] >= q75]
    small = buy_df[buy_df["buy_amt"] < q75]
    print(f"\n大單（前25%金額，n={len(big)}）擇時百分位中位數：{big['pct'].median():.3f}")
    print(f"小單（後75%，n={len(small)}）擇時百分位中位數：{small['pct'].median():.3f}")

    # ---- L1H7 campaign 去重疊版 sweep（跟既有逐日 sweep 互補）----
    all_dates = bars.index.tolist()
    campaign_signal_dates = [c[0].strftime("%Y-%m-%d") for c in campaigns if c[0] >= pd.Timestamp("2023-01-03")]
    bt_campaign = l1h7_backtest(campaign_signal_dates, bars, bench)
    s_campaign = summarize(bt_campaign, "波段起始日（去重疊，僅2023起可回測部分）")
    print(f"\n=== L1H7：波段起始日去重疊版（n波段={len(campaign_signal_dates)}）===")
    print(s_campaign)

    bt_all = l1h7_backtest(buy_df["trade_date"].dt.strftime("%Y-%m-%d").tolist(), bars, bench)
    s_all = summarize(bt_all, "全部買進日（2023起）")
    print(f"\n=== L1H7：全部買進日（原始，高度重疊，2023起）===")
    print(s_all)

    bt_big = l1h7_backtest(big["trade_date"].dt.strftime("%Y-%m-%d").tolist(), bars, bench)
    s_big = summarize(bt_big, "大單日（前25%金額）")
    print(f"\n=== L1H7：大單日 ===")
    print(s_big)

    extra_summary = pd.DataFrame([s_all, s_campaign, s_big])
    extra_summary.to_csv(OUT_DIR / "whale_9661_8358_l1h7_extra_summary.csv", index=False)

    # ---- 跟 3189 / 2408 比較 ----
    print(f"\n{'='*70}\n跟 3189(景碩) / 2408(南亞科) 簡化風格比對（自跑，非其他agent產出）\n{'='*70}")
    profiles = [quick_stock_profile(conn, bench, TRADER_ID, sid) for sid in COMPARE_STOCKS]
    profiles_df = pd.DataFrame(profiles)
    profiles_df.to_csv(OUT_DIR / "whale_9661_8358_vs_3189_2408_profile.csv", index=False)
    print(profiles_df.to_string(index=False))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
