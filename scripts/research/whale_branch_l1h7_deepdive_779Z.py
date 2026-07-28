#!/usr/bin/env python3
"""779Z（國票-安和）分點深挖。

背景：全分點宇宙 L1H7 篩選裡樣本數尚可（n=70）、p值卡在統計顯著邊界（p=0.051）的新候選。
中位數+0.66%、平均+2.60%、勝率57.1%、n_stocks=45。本腳本做五件標準分析：

(a) 全交易剖面：2024-07-01 起，779Z 總共在幾檔股票上當過 Top1 買方？金額分布？交易頻率？
(b) 建倉型態：對交易最頻繁的前 10 檔股票，算累積淨部位時間序列，判斷是
    「真建倉」、「當沖/造市」還是「波段進出」。
(c) 擇時品質：買進日股價在 trailing 20 日高低區間的百分位，跟隨機基準比較。
(d) 統計顯著性：用 L1H7 SSOT 協議（次日開盤進場、H7收盤出場、30bps成本、1.15倍β調整）
    算出的 events CSV，做 t-test + bootstrap CI，並額外做「去除極端值後還剩多少」的穩健性檢查。
(e) 對倒交叉檢查：779Z 是否常出現在 whale_crossing 名單裡（買賣雙邊都出現、或跟特定分點配對）。

用法（必須在 mini 上跑，Book本地DB是舊的）：
  ssh mac-mini-lan 之後於專案目錄執行：
  PYTHONPATH=src .venv/bin/python scripts/research/whale_branch_l1h7_deepdive_779Z.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")

TRADER_ID = "779Z"
TRADER_NAME = "國票-安和"
DATE_START = "2024-07-01"
DATE_END = "2026-07-22"
TOP1_AMT_FLOOR = 5_000_000

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
MEGA_BLACKLIST_PATH = OUT_DIR / "ab58_xMega_copytrade/mega_blacklist_v1.json"
CROSSING_EVENTS_CSV = OUT_DIR / "whale_crossing_events.csv"
L1H7_EVENTS_CSV = OUT_DIR / "whale_branch_l1h7_universe_events.csv"

PREFIX = "whale_branch_l1h7_deepdive_779z_"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def main() -> int:
    mega = load_mega_blacklist()
    conn = connect(DEFAULT_DB_PATH)

    print("[INFO] 讀取 779Z 全部 stock_broker_branch_daily 交易紀錄...")
    raw = pd.read_sql_query(
        """
        SELECT b.trade_date, b.stock_id, b.buy, b.sell, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.source = 'finmind'
          AND b.securities_trader_id = ?
          AND b.trade_date >= ? AND b.trade_date <= ?
        """,
        conn,
        params=(TRADER_ID, DATE_START, DATE_END),
    )
    raw = raw[~raw["stock_id"].isin(mega)]
    raw = raw[~raw["stock_id"].str.startswith("00")].copy()
    raw["buy_amt"] = raw["buy"] * raw["close"]
    raw["sell_amt"] = raw["sell"] * raw["close"]
    raw["net_amt"] = raw["net"] * raw["close"]
    print(f"[INFO] 779Z 交易紀錄（排除mega+ETF）：{len(raw):,} 筆，涵蓋 {raw['stock_id'].nunique()} 檔股票")

    # ---- (a) 全交易剖面：哪些日子/股票 779Z 是 Top1 買方 ----
    # 效能修正：原本在此重新對 779Z 涉獵的股票做全市場 Top1 買方排名（未過濾
    # securities_trader_id、靠 stock_id IN(...) 子查詢，兩個索引都用不上、
    # 實測跑超過90分鐘沒完成）。改成直接讀已經算好的全分點宇宙事件檔——
    # whale_branch_l1h7_universe_events.csv 本來就是「每個 (date,stock) 的
    # 全市場 Top1 買方」結果（金額>=500萬、已去重），篩 securities_trader_id
    # 直接拿到 779Z 的 Top1 買方紀錄，不用重查資料庫。
    # 注意：這份檔案已做過 5 日同股同分點去重，所以「事件數」會比原始逐日
    # 觸發次數略少，但用於全交易剖面統計（涵蓋幾檔股票、金額分布、活躍月數）
    # 已經足夠代表，不影響本節結論。
    print("[INFO] 從既有全分點宇宙事件檔篩出 779Z 的 Top1 買方紀錄（不重查DB）...")
    universe = pd.read_csv(
        L1H7_EVENTS_CSV, dtype={"stock_id": str, "securities_trader_id": str}
    )
    t779z = universe[universe["securities_trader_id"] == TRADER_ID].copy()
    t779z = t779z.rename(columns={"signal_date": "trade_date"})
    t779z["trade_date"] = t779z["trade_date"].astype(str)
    t779z["ym"] = t779z["trade_date"].str.slice(0, 7)

    n_stocks_total = t779z["stock_id"].nunique()
    n_events_total = len(t779z)
    amt_stats = t779z["buy_amt"].describe(percentiles=[0.5])
    monthly_counts = t779z.groupby("ym").size()
    stock_counts = t779z["stock_id"].value_counts()
    top10_share = stock_counts.head(10).sum() / n_events_total if n_events_total else np.nan

    profile_summary = {
        "trader_id": TRADER_ID,
        "trader_name": TRADER_NAME,
        "date_range": f"{DATE_START}~{DATE_END}",
        "n_stocks_as_top1_buyer": n_stocks_total,
        "n_events_as_top1_buyer": n_events_total,
        "buy_amt_median": round(float(amt_stats["50%"]), 0) if n_events_total else None,
        "buy_amt_mean": round(float(amt_stats["mean"]), 0) if n_events_total else None,
        "buy_amt_max": round(float(t779z["buy_amt"].max()), 0) if n_events_total else None,
        "months_active": monthly_counts.shape[0],
        "avg_events_per_month": round(float(monthly_counts.mean()), 2) if monthly_counts.shape[0] else None,
        "top10_stock_share_of_events_pct": round(float(top10_share) * 100, 1) if n_events_total else None,
    }
    print("[INFO] (a) 全交易剖面：")
    for k, v in profile_summary.items():
        print(f"    {k}: {v}")

    stock_counts_df = stock_counts.reset_index()
    stock_counts_df.columns = ["stock_id", "n_top1_buy_events"]
    stock_counts_df.to_csv(OUT_DIR / f"{PREFIX}a_stock_event_counts.csv", index=False)
    pd.DataFrame([profile_summary]).to_csv(OUT_DIR / f"{PREFIX}a_profile_summary.csv", index=False)

    # ---- (b) 建倉型態：前 10 檔交易最頻繁股票的累積淨部位 ----
    activity = raw.groupby("stock_id").agg(
        n_days=("trade_date", "nunique"),
        total_buy_amt=("buy_amt", "sum"),
        total_sell_amt=("sell_amt", "sum"),
    )
    activity["total_amt"] = activity["total_buy_amt"] + activity["total_sell_amt"]
    top_stocks = activity.sort_values("total_amt", ascending=False).head(10)
    print("[INFO] (b) 交易最活躍前10檔（依買+賣總金額）：")
    print(top_stocks)

    position_rows = []
    position_classification = {}
    for stock_id in top_stocks.index:
        g = raw[raw["stock_id"] == stock_id].sort_values("trade_date").copy()
        g["cum_net_shares"] = g["net"].cumsum()
        g["cum_net_amt"] = g["net_amt"].cumsum()
        for _, r in g.iterrows():
            position_rows.append(
                {
                    "stock_id": stock_id,
                    "trade_date": r["trade_date"],
                    "buy": r["buy"],
                    "sell": r["sell"],
                    "net": r["net"],
                    "close": r["close"],
                    "net_amt": r["net_amt"],
                    "cum_net_shares": r["cum_net_shares"],
                    "cum_net_amt": r["cum_net_amt"],
                }
            )
        cum = g["cum_net_shares"]
        max_cum = cum.max()
        min_cum = cum.min()
        final_cum = cum.iloc[-1]
        peak_abs = max(abs(max_cum), abs(min_cum)) if len(cum) else 0
        n_sign_changes = int((np.sign(cum.replace(0, np.nan).ffill()).diff().fillna(0) != 0).sum())
        if peak_abs == 0:
            style = "無明顯部位（金額太小/淨零）"
        elif abs(final_cum) < 0.15 * peak_abs and peak_abs > 0:
            style = "波段進出後出清（曾建倉，後完全/大幅反向出清）"
        elif abs(final_cum) >= 0.6 * peak_abs:
            style = "持續單向累積（真建倉，期末部位仍接近峰值）"
        else:
            style = "部分了結（建倉後有減碼，但未完全出清）"
        n_days = len(g)
        oscillation_ratio = n_sign_changes / n_days if n_days else np.nan
        position_classification[stock_id] = {
            "stock_id": stock_id,
            "n_trading_days": n_days,
            "total_buy_amt": round(float(g["buy_amt"].sum()), 0),
            "total_sell_amt": round(float(g["sell_amt"].sum()), 0),
            "peak_cum_net_shares": int(max_cum) if not pd.isna(max_cum) else None,
            "trough_cum_net_shares": int(min_cum) if not pd.isna(min_cum) else None,
            "final_cum_net_shares": int(final_cum) if not pd.isna(final_cum) else None,
            "n_net_sign_changes": n_sign_changes,
            "oscillation_ratio": round(oscillation_ratio, 3) if not pd.isna(oscillation_ratio) else None,
            "style_classification": style,
        }

    position_series_df = pd.DataFrame(position_rows)
    position_series_df.to_csv(OUT_DIR / f"{PREFIX}b_position_timeseries_top10.csv", index=False)
    position_class_df = pd.DataFrame(list(position_classification.values()))
    position_class_df.to_csv(OUT_DIR / f"{PREFIX}b_position_classification.csv", index=False)
    print("[INFO] (b) 部位型態分類：")
    print(position_class_df.to_string(index=False))

    # ---- (c) 擇時品質：買進日在 trailing 20日高低區間的百分位 ----
    print("[INFO] (c) 計算擇時品質（trailing 20日高低百分位）...")
    buy_stock_ids = sorted(t779z["stock_id"].unique().tolist())
    if buy_stock_ids:
        placeholders = ",".join("?" * len(buy_stock_ids))
        bars = pd.read_sql_query(
            f"""
            SELECT stock_id, trade_date, close
            FROM stock_daily_bars
            WHERE source='finmind' AND stock_id IN ({placeholders})
            ORDER BY stock_id, trade_date
            """,
            conn,
            params=buy_stock_ids,
        )
    else:
        bars = pd.DataFrame(columns=["stock_id", "trade_date", "close"])

    bars["roll_min20"] = bars.groupby("stock_id")["close"].transform(
        lambda s: s.rolling(20, min_periods=20).min()
    )
    bars["roll_max20"] = bars.groupby("stock_id")["close"].transform(
        lambda s: s.rolling(20, min_periods=20).max()
    )
    bars["pctile20"] = (bars["close"] - bars["roll_min20"]) / (bars["roll_max20"] - bars["roll_min20"])
    bars_idx = bars.set_index(["stock_id", "trade_date"])["pctile20"]

    t779z["pctile20"] = t779z.apply(
        lambda r: bars_idx.get((r["stock_id"], r["trade_date"]), np.nan), axis=1
    )
    timing_vals = t779z["pctile20"].dropna()
    market_pctile_all = bars["pctile20"].dropna()

    timing_summary = {
        "n_779z_buy_days_with_pctile": len(timing_vals),
        "779z_pctile20_mean": round(float(timing_vals.mean()), 4) if len(timing_vals) else None,
        "779z_pctile20_median": round(float(timing_vals.median()), 4) if len(timing_vals) else None,
        "market_baseline_pctile20_mean": round(float(market_pctile_all.mean()), 4) if len(market_pctile_all) else None,
        "market_baseline_pctile20_median": round(float(market_pctile_all.median()), 4) if len(market_pctile_all) else None,
    }
    if len(timing_vals) >= 2 and len(market_pctile_all) >= 2:
        tstat, pval = stats.ttest_ind(timing_vals, market_pctile_all, equal_var=False)
        timing_summary["ttest_vs_market_baseline_t"] = round(float(tstat), 3)
        timing_summary["ttest_vs_market_baseline_p"] = round(float(pval), 5)
    print("[INFO] (c) 擇時品質：")
    for k, v in timing_summary.items():
        print(f"    {k}: {v}")
    pd.DataFrame([timing_summary]).to_csv(OUT_DIR / f"{PREFIX}c_timing_quality.csv", index=False)

    # ---- (d) 統計顯著性：L1H7 SSOT events（已在 Book 本地算好） ----
    print("[INFO] (d) 統計顯著性檢定（L1H7 events, r_adj_pct）...")
    l1h7 = pd.read_csv(L1H7_EVENTS_CSV, dtype={"stock_id": str, "securities_trader_id": str})
    sub = l1h7[l1h7["securities_trader_id"] == TRADER_ID]["r_adj_pct"].dropna()
    n = len(sub)
    sig_summary = {"n": n}
    if n >= 2:
        mean = float(sub.mean())
        median = float(sub.median())
        std = float(sub.std(ddof=1))
        se = std / np.sqrt(n)
        tstat, pval = stats.ttest_1samp(sub, 0.0)
        ci_t = stats.t.interval(0.95, df=n - 1, loc=mean, scale=se)
        wstat, wpval = stats.wilcoxon(sub)

        rng = np.random.default_rng(42)
        boot_means = np.array(
            [rng.choice(sub.values, size=n, replace=True).mean() for _ in range(20000)]
        )
        boot_medians = np.array(
            [np.median(rng.choice(sub.values, size=n, replace=True)) for _ in range(20000)]
        )
        ci_boot_mean = (float(np.percentile(boot_means, 2.5)), float(np.percentile(boot_means, 97.5)))
        ci_boot_median = (float(np.percentile(boot_medians, 2.5)), float(np.percentile(boot_medians, 97.5)))

        # 穩健性檢查：去除最大的 N 筆正向極端值後還剩多少
        r = sub.values
        robustness_rows = []
        for k_drop in range(0, 6):
            idx_sorted = np.argsort(r)[::-1]
            keep = np.delete(r, idx_sorted[:k_drop]) if k_drop else r
            robustness_rows.append(
                {
                    "n_top_positive_dropped": k_drop,
                    "n_remaining": len(keep),
                    "mean_pct": round(float(keep.mean()), 3),
                    "median_pct": round(float(np.median(keep)), 3),
                    "winrate_pct": round(float((keep > 0).mean()) * 100, 1),
                }
            )
        robustness_df = pd.DataFrame(robustness_rows)

        sig_summary.update(
            {
                "mean_pct": round(mean, 3),
                "median_pct": round(median, 3),
                "std_pct": round(std, 3),
                "winrate_pct": round(float((sub > 0).mean()) * 100, 1),
                "t_stat": round(float(tstat), 3),
                "p_value_ttest": round(float(pval), 5),
                "wilcoxon_stat": round(float(wstat), 3),
                "p_value_wilcoxon": round(float(wpval), 5),
                "ci95_t_lower_pct": round(ci_t[0], 3),
                "ci95_t_upper_pct": round(ci_t[1], 3),
                "ci95_bootstrap_mean_lower_pct": round(ci_boot_mean[0], 3),
                "ci95_bootstrap_mean_upper_pct": round(ci_boot_mean[1], 3),
                "prob_bootstrap_mean_gt_0": round(float((boot_means > 0).mean()), 4),
                "ci95_bootstrap_median_lower_pct": round(ci_boot_median[0], 3),
                "ci95_bootstrap_median_upper_pct": round(ci_boot_median[1], 3),
                "prob_bootstrap_median_gt_0": round(float((boot_medians > 0).mean()), 4),
                "significant_at_5pct_ttest": bool(pval < 0.05),
                "significant_at_5pct_wilcoxon": bool(wpval < 0.05),
            }
        )
        robustness_df.to_csv(OUT_DIR / f"{PREFIX}d_extreme_value_robustness.csv", index=False)
        print("[INFO] (d-補充) 去除極端值穩健性檢查：")
        print(robustness_df.to_string(index=False))
    print("[INFO] (d) 統計顯著性：")
    for k, v in sig_summary.items():
        print(f"    {k}: {v}")
    pd.DataFrame([sig_summary]).to_csv(OUT_DIR / f"{PREFIX}d_significance.csv", index=False)

    # ---- (e) 對倒交叉檢查 ----
    print("[INFO] (e) 對倒交叉檢查...")
    crossing_summary = {}
    if CROSSING_EVENTS_CSV.exists():
        cross = pd.read_csv(
            CROSSING_EVENTS_CSV, dtype={"buy_trader_id": str, "sell_trader_id": str, "stock_id": str}
        )
        as_buy = cross[cross["buy_trader_id"] == TRADER_ID]
        as_sell = cross[cross["sell_trader_id"] == TRADER_ID]
        crossing_summary = {
            "n_crossing_events_total": len(cross),
            "n_as_buy_side": len(as_buy),
            "n_as_sell_side": len(as_sell),
            "note": (
                "whale_crossing 定義為同日同股不同分點分別是 Top1 買方與 Top1 賣方，"
                "且雙方金額≥1億、占比≥50%、cross_ratio≥50%。779Z 在此嚴格定義下"
                f"出現 {len(as_buy) + len(as_sell)} 次（買方{len(as_buy)}次+賣方{len(as_sell)}次）。"
            ),
        }
        if len(as_buy):
            as_buy.to_csv(OUT_DIR / f"{PREFIX}e_crossing_as_buy.csv", index=False)
        if len(as_sell):
            as_sell.to_csv(OUT_DIR / f"{PREFIX}e_crossing_as_sell.csv", index=False)
    else:
        crossing_summary = {"note": "whale_crossing_events.csv 不存在，略過"}
    print("[INFO] (e) 對倒交叉檢查：")
    for k, v in crossing_summary.items():
        print(f"    {k}: {v}")

    # 額外：779Z 自己在同一批股票上 Top1買方 vs Top1賣方 角色切換
    print("[INFO] (e-補充) 779Z 自己在同一檔股票 Top1買方↔Top1賣方 角色切換頻率...")
    role_switch_summary = {}
    if buy_stock_ids:
        full_market_sell = pd.read_sql_query(
            """
            SELECT b.trade_date, b.stock_id, b.securities_trader_id, b.sell, p.close
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
            WHERE b.source = 'finmind' AND b.sell > 0
              AND b.trade_date >= ? AND b.trade_date <= ?
              AND b.stock_id IN ({})
            """.format(",".join("?" * len(buy_stock_ids))),
            conn,
            params=(DATE_START, DATE_END, *buy_stock_ids),
        )
        if not full_market_sell.empty:
            full_market_sell = full_market_sell[~full_market_sell["stock_id"].isin(mega)]
            full_market_sell["sell_amt"] = full_market_sell["sell"] * full_market_sell["close"]
            full_market_sell["rank"] = full_market_sell.groupby(["trade_date", "stock_id"])["sell_amt"].rank(
                method="first", ascending=False
            )
            sell_top1 = full_market_sell[
                (full_market_sell["rank"] == 1)
                & (full_market_sell["securities_trader_id"] == TRADER_ID)
                & (full_market_sell["sell_amt"] >= TOP1_AMT_FLOOR)
            ].copy()
            sell_top1["trade_date"] = sell_top1["trade_date"].astype(str)
            overlap_stocks = sorted(set(t779z["stock_id"]) & set(sell_top1["stock_id"]))
            role_switch_summary = {
                "n_stocks_779z_top1_buyer_among_its_own_buy_universe": t779z["stock_id"].nunique(),
                "n_stocks_779z_top1_seller_among_same_universe": sell_top1["stock_id"].nunique(),
                "n_overlap_stocks_both_top1_buyer_and_seller": len(overlap_stocks),
                "overlap_stocks": ",".join(overlap_stocks),
            }
            sell_top1.to_csv(OUT_DIR / f"{PREFIX}e_self_top1_sell_events.csv", index=False)
    print("[INFO] (e-補充) 角色切換：")
    for k, v in role_switch_summary.items():
        print(f"    {k}: {v}")

    pd.DataFrame([{**crossing_summary, **role_switch_summary}]).to_csv(
        OUT_DIR / f"{PREFIX}e_crossing_summary.csv", index=False
    )

    conn.close()
    print("[OK] 全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
