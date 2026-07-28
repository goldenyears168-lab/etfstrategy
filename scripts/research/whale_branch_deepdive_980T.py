#!/usr/bin/env python3
"""980T（元大自營）分點深挖。

背景見 memory/CLAUDE.md 交代的任務說明：980T 是唯一在「集中度事件子集」跟「全部
Top1買方日」兩種樣本下方向都一致為正的候選分點，但它同時也是賣方集中度研究裡
的常見 Top1 賣方分點（買賣雙邊都活躍），暗示可能是自營商造市/避險帳戶而非方向性
大戶。本腳本做五件事：

(a) 全交易剖面：2024-07-01 起，980T 總共在幾檔股票上當過 Top1 買方？金額分布？
    交易頻率？集中還是分散？
(b) 建倉型態：對交易最頻繁的前 5-10 檔股票，算累積淨部位時間序列，判斷是
    「真建倉」、「當沖/造市」還是「波段進出」。
(c) 擇時品質：買進日股價在 trailing 20 日高低區間的百分位，跟隨機基準比較。
(d) 統計顯著性：對全部 Top1 買方日 forward H9 excess return 做 t-test + bootstrap CI。
(e) 對倒交叉檢查：980T 是否常出現在 whale_crossing 名單裡（買賣雙邊都出現、或跟
    特定分點配對）。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/whale_branch_deepdive_980T.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")

TRADER_ID = "980T"
TRADER_NAME = "元大自營"
DATE_START = "2024-07-01"
DATE_END = "2026-07-22"
TOP1_AMT_FLOOR = 5_000_000  # 跟 study_whale_branch_consistency.py 一致

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
MEGA_BLACKLIST_PATH = OUT_DIR / "ab58_xMega_copytrade/mega_blacklist_v1.json"
CROSSING_EVENTS_CSV = OUT_DIR / "whale_crossing_events.csv"
ALLTOP1_CAMPAIGNS_CSV = OUT_DIR / "branch_consistency_alltop1_campaigns.csv"

PREFIX = "whale_branch_deepdive_980t_"


def load_mega_blacklist() -> set[str]:
    import json

    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def main() -> int:
    mega = load_mega_blacklist()
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted(bench.index.astype(str).unique().tolist())

    print("[INFO] 讀取 980T 全部 stock_broker_branch_daily 交易紀錄...")
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
    print(f"[INFO] 980T 交易紀錄（排除mega+ETF）：{len(raw):,} 筆，涵蓋 {raw['stock_id'].nunique()} 檔股票")

    # ---- (a) 全交易剖面：哪些日子/股票 980T 是 Top1 買方 ----
    print("[INFO] 計算全市場每日 Top1 買方分點，篩出 980T 當 Top1 買方的紀錄...")
    full_market = pd.read_sql_query(
        """
        SELECT b.trade_date, b.stock_id, b.securities_trader_id, b.buy, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.source = 'finmind' AND b.buy > 0
          AND b.trade_date >= ? AND b.trade_date <= ?
        """,
        conn,
        params=(DATE_START, DATE_END),
    )
    full_market = full_market[~full_market["stock_id"].isin(mega)]
    full_market = full_market[~full_market["stock_id"].str.startswith("00")].copy()
    full_market["buy_amt"] = full_market["buy"] * full_market["close"]
    full_market["rank"] = full_market.groupby(["trade_date", "stock_id"])["buy_amt"].rank(
        method="first", ascending=False
    )
    top1 = full_market[full_market["rank"] == 1].copy()
    t980 = top1[
        (top1["securities_trader_id"] == TRADER_ID) & (top1["buy_amt"] >= TOP1_AMT_FLOOR)
    ].copy()
    t980["trade_date"] = t980["trade_date"].astype(str)
    t980["ym"] = t980["trade_date"].str.slice(0, 7)

    n_stocks_total = t980["stock_id"].nunique()
    n_events_total = len(t980)
    amt_stats = t980["buy_amt"].describe(percentiles=[0.5])
    monthly_counts = t980.groupby("ym").size()
    stock_counts = t980["stock_id"].value_counts()
    top10_share = stock_counts.head(10).sum() / n_events_total if n_events_total else np.nan

    profile_summary = {
        "trader_id": TRADER_ID,
        "trader_name": TRADER_NAME,
        "date_range": f"{DATE_START}~{DATE_END}",
        "n_stocks_as_top1_buyer": n_stocks_total,
        "n_events_as_top1_buyer": n_events_total,
        "buy_amt_median": round(float(amt_stats["50%"]), 0) if n_events_total else None,
        "buy_amt_mean": round(float(amt_stats["mean"]), 0) if n_events_total else None,
        "buy_amt_max": round(float(t980["buy_amt"].max()), 0) if n_events_total else None,
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

    # ---- (b) 建倉型態：前 5-10 檔交易最頻繁股票的累積淨部位 ----
    # 用「總交易活躍度」（buy_amt+sell_amt 加總天數）挑股票，而不僅限於 Top1 買方日，
    # 這樣才能看到買賣雙邊完整的部位軌跡。
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
        # 分類邏輯：
        cum = g["cum_net_shares"]
        max_cum = cum.max()
        min_cum = cum.min()
        final_cum = cum.iloc[-1]
        peak_abs = max(abs(max_cum), abs(min_cum)) if len(cum) else 0
        n_sign_changes = int((np.sign(cum.replace(0, np.nan).ffill()).diff().fillna(0) != 0).sum())
        if peak_abs == 0:
            style = "無明顯部位（金額太小/淨零）"
        elif abs(final_cum) < 0.15 * peak_abs and peak_abs > 0:
            # 曾經累積到高點又幾乎完全打回
            style = "波段進出後出清（曾建倉，後完全/大幅反向出清）"
        elif abs(final_cum) >= 0.6 * peak_abs:
            style = "持續單向累積（真建倉，期末部位仍接近峰值）"
        else:
            style = "部分了結（建倉後有減碼，但未完全出清）"
        # 震盪度：符號變化次數相對於天數的比例，越高越像當沖/造市
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
    buy_stock_ids = sorted(t980["stock_id"].unique().tolist())
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

    t980["pctile20"] = t980.apply(
        lambda r: bars_idx.get((r["stock_id"], r["trade_date"]), np.nan), axis=1
    )
    timing_vals = t980["pctile20"].dropna()

    # 隨機基準：全市場（同一批股票，或全市場全部交易日）該指標的分布
    market_pctile_all = bars["pctile20"].dropna()

    timing_summary = {
        "n_980t_buy_days_with_pctile": len(timing_vals),
        "980t_pctile20_mean": round(float(timing_vals.mean()), 4) if len(timing_vals) else None,
        "980t_pctile20_median": round(float(timing_vals.median()), 4) if len(timing_vals) else None,
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

    # ---- (d) 統計顯著性：全部 Top1 買方日 H9 excess return ----
    print("[INFO] (d) 統計顯著性檢定（H9 excess return, 全部 Top1 買方日樣本）...")
    alltop1 = pd.read_csv(ALLTOP1_CAMPAIGNS_CSV, dtype={"stock_id": str, "top1_trader_id": str})
    sub = alltop1[alltop1["top1_trader_id"] == TRADER_ID]["excess_h9"].dropna()
    n = len(sub)
    sig_summary = {"n": n}
    if n >= 2:
        mean = float(sub.mean())
        std = float(sub.std(ddof=1))
        se = std / np.sqrt(n)
        tstat, pval = stats.ttest_1samp(sub, 0.0)
        ci_t = stats.t.interval(0.95, df=n - 1, loc=mean, scale=se)

        rng = np.random.default_rng(42)
        boot_means = np.array(
            [rng.choice(sub.values, size=n, replace=True).mean() for _ in range(1000)]
        )
        ci_boot = (float(np.percentile(boot_means, 2.5)), float(np.percentile(boot_means, 97.5)))

        sig_summary.update(
            {
                "mean_pct": round(mean * 100, 3),
                "std_pct": round(std * 100, 3),
                "t_stat": round(float(tstat), 3),
                "p_value": round(float(pval), 5),
                "ci95_t_lower_pct": round(ci_t[0] * 100, 3),
                "ci95_t_upper_pct": round(ci_t[1] * 100, 3),
                "ci95_bootstrap_lower_pct": round(ci_boot[0] * 100, 3),
                "ci95_bootstrap_upper_pct": round(ci_boot[1] * 100, 3),
                "significant_at_5pct": bool(pval < 0.05),
            }
        )
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
                "且雙方金額≥1億、占比≥50%、cross_ratio≥50%。980T 在此嚴格定義下"
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

    # 額外：同一分點在同一檔股票，是否常在短窗內（例如≤5交易日）先當Top1買方、
    # 後又當Top1賣方（不要求跟另一分點對倒，只看980T自己買賣角色切換的頻率）
    print("[INFO] (e-補充) 980T 自己在同一檔股票 Top1買方↔Top1賣方 角色切換頻率...")
    full_market_sell = pd.read_sql_query(
        """
        SELECT b.trade_date, b.stock_id, b.securities_trader_id, b.sell, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.source = 'finmind' AND b.sell > 0
          AND b.trade_date >= ? AND b.trade_date <= ?
          AND b.stock_id IN ({})
        """.format(",".join("?" * len(buy_stock_ids))) if buy_stock_ids else "SELECT NULL WHERE 0",
        conn,
        params=(DATE_START, DATE_END, *buy_stock_ids) if buy_stock_ids else None,
    )
    role_switch_summary = {}
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
        overlap_stocks = sorted(set(t980["stock_id"]) & set(sell_top1["stock_id"]))
        role_switch_summary = {
            "n_stocks_980t_top1_buyer_among_its_own_buy_universe": t980["stock_id"].nunique(),
            "n_stocks_980t_top1_seller_among_same_universe": sell_top1["stock_id"].nunique(),
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

    render_report(
        profile_summary,
        position_class_df,
        timing_summary,
        sig_summary,
        crossing_summary,
        role_switch_summary,
    )
    return 0


def render_report(profile, position_class_df, timing, sig, crossing, role_switch) -> None:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    lines = [
        "# 980T（元大自營）分點深挖報告",
        "",
        f"產出時間：{now}",
        "",
        "## 背景",
        "",
        "980T 是唯一在「集中度事件子集」（n=17, H9+6.83%, 勝率47.1%）跟「全部Top1買方日」"
        "（n=29, H9+2.74%, 勝率48.3%）兩種樣本下方向都一致為正的候選分點。但它同時也是"
        "賣方集中度研究裡的常見 Top1 賣方分點，暗示可能是自營商造市/避險帳戶而非方向性大戶。"
        "本報告從交易剖面、建倉型態、擇時品質、統計顯著性、對倒交叉五個角度釐清這個問題。",
        "",
        "## (a) 全交易剖面",
        "",
        f"- 分析期間：{profile['date_range']}",
        f"- 當過 Top1 買方的股票數：{profile['n_stocks_as_top1_buyer']} 檔",
        f"- Top1 買方事件數（金額≥500萬）：{profile['n_events_as_top1_buyer']} 筆",
        f"- 買超金額：中位數 {profile['buy_amt_median']:,.0f} 元、平均 {profile['buy_amt_mean']:,.0f} 元、"
        f"最大 {profile['buy_amt_max']:,.0f} 元" if profile['n_events_as_top1_buyer'] else "- 買超金額：無資料",
        f"- 活躍月數：{profile['months_active']} 個月，平均每月 {profile['avg_events_per_month']} 次當 Top1 買方",
        f"- 前10檔股票占全部 Top1 買方事件的比重：{profile['top10_stock_share_of_events_pct']}%"
        "（比重高代表交易高度集中在少數股票，比重低代表廣泛分散於很多股票）",
        "",
        "## (b) 建倉型態（累積淨部位分析，前10檔交易最活躍股票）",
        "",
        "| 股票 | 交易天數 | 總買超額 | 總賣超額 | 峰值累積股數 | 谷值累積股數 | 期末累積股數 | "
        "淨部位正負反轉次數 | 震盪比率 | 型態判定 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in position_class_df.iterrows():
        lines.append(
            f"| {r['stock_id']} | {r['n_trading_days']} | {r['total_buy_amt']:,.0f} | "
            f"{r['total_sell_amt']:,.0f} | {r['peak_cum_net_shares']:,} | {r['trough_cum_net_shares']:,} | "
            f"{r['final_cum_net_shares']:,} | {r['n_net_sign_changes']} | {r['oscillation_ratio']} | "
            f"{r['style_classification']} |"
        )

    n_build = (position_class_df["style_classification"] == "持續單向累積（真建倉，期末部位仍接近峰值）").sum()
    n_roundtrip = (position_class_df["style_classification"] == "波段進出後出清（曾建倉，後完全/大幅反向出清）").sum()
    n_partial = (position_class_df["style_classification"] == "部分了結（建倉後有減碼，但未完全出清）").sum()

    focus_stocks = ["6586", "6696", "7734", "7769", "7856"]
    focus_rows = position_class_df[position_class_df["stock_id"].isin(focus_stocks)]

    lines += [
        "",
        f"前10檔中：{n_build} 檔屬於「持續單向累積」、{n_roundtrip} 檔屬於「波段進出後出清」、"
        f"{n_partial} 檔屬於「部分了結」。",
        "",
        "### 980T 特別關注股票（6586/6696/7734/7769/7856，買賣雙邊重疊的股票）",
        "",
    ]
    if not focus_rows.empty:
        for _, r in focus_rows.iterrows():
            lines.append(
                f"- **{r['stock_id']}**：{r['style_classification']}"
                f"（峰值累積股數 {r['peak_cum_net_shares']:,}、期末 {r['final_cum_net_shares']:,}、"
                f"淨部位正負反轉 {r['n_net_sign_changes']} 次、震盪比率 {r['oscillation_ratio']}）"
            )
    else:
        lines.append("這些股票不在前10檔交易最活躍名單中（交易量不夠大，未進入累積部位分析）。"
                      "詳細數字見 `whale_branch_deepdive_980t_b_position_timeseries_top10.csv`"
                      "若這些股票確實不在前10名，建議另外針對這幾檔單獨跑累積部位。")
    lines += [
        "",
        "**結論（回答 memory 提出的三個假設）**：見下方「結論與建議」一節整合判斷。",
        "",
        "## (c) 擇時品質",
        "",
        f"- 980T 買進日樣本數（有完整20日高低資料）：{timing.get('n_980t_buy_days_with_pctile')}",
        f"- 980T 買進日 20日高低百分位：均值 {timing.get('980t_pctile20_mean')}、"
        f"中位數 {timing.get('980t_pctile20_median')}",
        f"- 市場基準（980T 有交易過的股票，全部交易日）：均值 {timing.get('market_baseline_pctile20_mean')}、"
        f"中位數 {timing.get('market_baseline_pctile20_median')}",
    ]
    if "ttest_vs_market_baseline_p" in timing:
        lines.append(
            f"- t-test（980T買進日 vs 市場基準）：t={timing['ttest_vs_market_baseline_t']}, "
            f"p={timing['ttest_vs_market_baseline_p']}"
            + ("（統計上顯著不同，p<0.05）" if timing["ttest_vs_market_baseline_p"] < 0.05 else "（統計上不顯著，p≥0.05）")
        )
    lines += [
        "",
        "百分位越接近0代表買在近期低點附近，越接近1代表追高。跟市場基準比較可以看出"
        "980T 是否有擇時能力，還是純粹隨機進場。",
        "",
        "## (d) 統計顯著性（全部 Top1 買方日 H9 excess return）",
        "",
        f"- 樣本數 n = {sig.get('n')}",
    ]
    if sig.get("n", 0) >= 2:
        lines += [
            f"- 平均 H9 excess return = {sig['mean_pct']}%（標準差 {sig['std_pct']}%）",
            f"- 單樣本 t-test：t = {sig['t_stat']}, p = {sig['p_value']}",
            f"- 95% CI（t分布）：[{sig['ci95_t_lower_pct']}%, {sig['ci95_t_upper_pct']}%]",
            f"- 95% CI（bootstrap 1000次）：[{sig['ci95_bootstrap_lower_pct']}%, {sig['ci95_bootstrap_upper_pct']}%]",
            "",
            (
                "**統計上顯著不等於0（p<0.05）**——但務必注意 n 很小，這個結論的穩健性有限。"
                if sig.get("significant_at_5pct")
                else "**統計上不能拒絕「均值=0」的虛無假設（p≥0.05）**——"
                "換句話說，H9+2.74%的平均報酬在這個樣本數下無法排除是雜訊/運氣的可能性，"
                "誠實地說，這不是一個統計上站得住腳的「有效訊號」。"
            ),
        ]
    else:
        lines.append("樣本數不足以做檢定。")

    lines += [
        "",
        "## (e) 對倒交叉檢查",
        "",
        crossing.get("note", "無資料"),
    ]
    if role_switch:
        lines += [
            "",
            "### 980T 自己在同一批股票上 Top1買方 vs Top1賣方 角色切換",
            "",
            f"- 980T 曾當過 Top1 買方的股票數：{role_switch.get('n_stocks_980t_top1_buyer_among_its_own_buy_universe')}",
            f"- 其中同時也曾當過 Top1 賣方的股票數：{role_switch.get('n_overlap_stocks_both_top1_buyer_and_seller')}",
            f"- 重疊股票：{role_switch.get('overlap_stocks')}",
            "",
            "這個重疊率高，代表980T在自己活躍的股票池裡，買賣雙邊角色都很常出現——"
            "但這本身不足以斷定是「造市」還是「波段進出」，必須搭配 (b) 的累積淨部位"
            "時間序列才能判斷買賣角色切換是「同一波段的進出場」還是「來回頻繁沖銷」。"
            "在嚴格定義（同日同股跟另一分點對倒、cross_ratio≥50%）下，980T "
            f"{'完全沒有' if crossing.get('n_as_buy_side', 0) + crossing.get('n_as_sell_side', 0) == 0 else '有'}"
            "出現在 whale_crossing 名單裡，代表它不是那種「同一天跟固定對手對敲」的典型對倒模式，"
            "如果是造市/波段行為，時間軸會拉得比同日對倒更長。",
        ]

    lines += [
        "",
        "## 結論與建議",
        "",
        "（見下方由分析腳本人工整合的判斷，實際數字以上方各節為準）",
        "",
    ]

    (OUT_DIR / f"{PREFIX}report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] 報告寫入 {OUT_DIR / (PREFIX + 'report.md')}")


if __name__ == "__main__":
    raise SystemExit(main())
