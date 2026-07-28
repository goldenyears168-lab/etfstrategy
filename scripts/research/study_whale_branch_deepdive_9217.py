#!/usr/bin/env python3
"""9217（凱基-松山）深挖：泛用 Top1 買方框架 vs 既有 songshan-copytrade 專屬規則。

9217 是唯一有「已經實盤驗證」的分點（config/order.yaml songshan-copytrade，5日累積買進
>=0.5億 + 5日淨比>=95%），這次用本輪研究開發的通用「全部 Top1 買方日」框架回頭分析它，
目的是驗證泛用框架本身是否可信（跟已知的實盤成功程度方向是否一致）。

輸出：
  reports/research/branch-footprint-screen/whale_branch_deepdive_9217_alltop1_events.csv
  reports/research/branch-footprint-screen/whale_branch_deepdive_9217_alltop1_campaigns.csv
  reports/research/branch-footprint-screen/whale_branch_deepdive_9217_existing_rule_campaigns.csv
  reports/research/branch-footprint-screen/whale_branch_deepdive_9217_position_building.csv
  reports/research/branch-footprint-screen/whale_branch_deepdive_9217_timing_quality.csv
  reports/research/branch-footprint-screen/whale_branch_deepdive_9217.md

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_branch_deepdive_9217.py
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

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")

TRADER_ID = "9217"
BRANCH_NAME = "凱基-松山"
DATE_START = "2024-07-01"  # stock_broker_branch_daily 資料涵蓋率限制
DATE_END = "2026-07-22"  # branch daily 資料實際最後一天

TOP1_AMT_FLOOR = 5_000_000  # 500萬，泛用框架的雜訊濾網（非集中度門檻）
CAMPAIGN_GAP_TRADING_DAYS = 10  # 跟既有研究一致的行情合併規則
HORIZON = 9  # H9，比照既有研究、也是 songshan-copytrade hold_days=7 附近的常用尺度

# 既有 songshan-copytrade 專屬規則（config/order.yaml songshan-copytrade 區塊，
# scan_5d_net95 於 scripts/research/run_songshan_follow_watch.py）
EXISTING_BUY_FLOOR = 50_000_000  # 0.5億
EXISTING_NET_MIN = 0.95
EXISTING_WINDOW = 5  # 5 個交易日

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
MEGA_BLACKLIST_PATH = OUT_DIR / "ab58_xMega_copytrade/mega_blacklist_v1.json"
CROSSING_BUY_CSV = OUT_DIR / "whale_crossing_buy_branch_track_record.csv"
CROSSING_SELL_CSV = OUT_DIR / "whale_crossing_sell_branch_track_record.csv"
CROSSING_PAIR_CSV = OUT_DIR / "whale_crossing_pair_track_record.csv"
CROSSING_EVENTS_CSV = OUT_DIR / "whale_crossing_events.csv"
PRIOR_EDGE_STUDY_JSON = ROOT / "reports/research/kgi-songshan-copytrade/20260718_songshan_h10_edge_study.json"

OUT_ALLTOP1_EVENTS_CSV = OUT_DIR / "whale_branch_deepdive_9217_alltop1_events.csv"
OUT_ALLTOP1_CAMPAIGNS_CSV = OUT_DIR / "whale_branch_deepdive_9217_alltop1_campaigns.csv"
OUT_EXISTING_RULE_CAMPAIGNS_CSV = OUT_DIR / "whale_branch_deepdive_9217_existing_rule_campaigns.csv"
OUT_POSITION_CSV = OUT_DIR / "whale_branch_deepdive_9217_position_building.csv"
OUT_TIMING_CSV = OUT_DIR / "whale_branch_deepdive_9217_timing_quality.csv"
OUT_REPORT_MD = OUT_DIR / "whale_branch_deepdive_9217.md"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def _mega_etf_filter_sql(mega: set[str]) -> tuple[str, list]:
    """回傳 (SQL片段, params)，排除 mega 黑名單 + ETF（00開頭）+ 非4碼股票代號。"""
    placeholders = ",".join("?" * len(mega)) if mega else None
    clauses = [
        "length(b.stock_id) = 4",
        "b.stock_id NOT GLOB '00*'",
    ]
    params: list = []
    if mega:
        clauses.append(f"b.stock_id NOT IN ({placeholders})")
        params = list(mega)
    return " AND ".join(clauses), params


def compute_daily_top1_buy(conn, mega: set[str]) -> pd.DataFrame:
    """全市場每個 (trade_date, stock_id) 的 Top1 買方（買超金額最大者），不套任何集中度門檻。
    用 SQL window function 在資料庫端算，避免把 86M+ 列原始資料載進 pandas（記憶體會爆）。"""
    filt_sql, filt_params = _mega_etf_filter_sql(mega)
    query = f"""
        WITH ranked AS (
            SELECT
                b.trade_date,
                b.stock_id,
                b.securities_trader_id AS top1_trader_id,
                b.buy * p.close AS top1_amt,
                ROW_NUMBER() OVER (
                    PARTITION BY b.trade_date, b.stock_id
                    ORDER BY b.buy * p.close DESC
                ) AS rnk
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id = b.stock_id
             AND p.trade_date = b.trade_date
             AND p.source = 'finmind'
            WHERE b.source = 'finmind'
              AND b.trade_date >= ? AND b.trade_date <= ?
              AND b.buy > 0
              AND {filt_sql}
        )
        SELECT trade_date, stock_id, top1_trader_id, top1_amt
        FROM ranked WHERE rnk = 1
    """
    params = [DATE_START, DATE_END] + filt_params
    return pd.read_sql_query(query, conn, params=params)


def compute_daily_top1_sell(conn, mega: set[str]) -> pd.DataFrame:
    """全市場每個 (trade_date, stock_id) 的 Top1 賣方（賣超金額最大者），用來做 (e) 對倒檢查。"""
    filt_sql, filt_params = _mega_etf_filter_sql(mega)
    query = f"""
        WITH ranked AS (
            SELECT
                b.trade_date,
                b.stock_id,
                b.securities_trader_id AS top1_trader_id,
                b.sell * p.close AS top1_amt,
                ROW_NUMBER() OVER (
                    PARTITION BY b.trade_date, b.stock_id
                    ORDER BY b.sell * p.close DESC
                ) AS rnk
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id = b.stock_id
             AND p.trade_date = b.trade_date
             AND p.source = 'finmind'
            WHERE b.source = 'finmind'
              AND b.trade_date >= ? AND b.trade_date <= ?
              AND b.sell > 0
              AND {filt_sql}
        )
        SELECT trade_date, stock_id, top1_trader_id, top1_amt
        FROM ranked WHERE rnk = 1
    """
    params = [DATE_START, DATE_END] + filt_params
    return pd.read_sql_query(query, conn, params=params)


def load_trader_all_trades(conn, trader_id: str, mega: set[str]) -> pd.DataFrame:
    """單一分點在全樣本期間的全部交易原始資料（不限 Top1），用於建倉型態 cumsum +
    既有規則掃描。資料量小（單一分點），可以安全載進 pandas。"""
    filt_sql, filt_params = _mega_etf_filter_sql(mega)
    query = f"""
        SELECT
            b.trade_date, b.stock_id, b.buy, b.sell, b.net, p.close,
            b.buy * p.close AS buy_amt,
            b.sell * p.close AS sell_amt,
            b.net * p.close AS net_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id
         AND p.trade_date = b.trade_date
         AND p.source = 'finmind'
        WHERE b.source = 'finmind'
          AND b.securities_trader_id = ?
          AND b.trade_date >= ? AND b.trade_date <= ?
          AND {filt_sql}
        ORDER BY b.stock_id, b.trade_date
    """
    params = [trader_id, DATE_START, DATE_END] + filt_params
    return pd.read_sql_query(query, conn, params=params)


def merge_campaigns(events: pd.DataFrame, trading_dates: list[str], trader_col: str = "top1_trader_id") -> pd.DataFrame:
    """同一 (stock_id, trader_id) 短窗（<=CAMPAIGN_GAP_TRADING_DAYS 交易日）內連續事件日
    合併成一個「行情」，只取首日進場，避免偽獨立樣本。"""
    idx = {d: i for i, d in enumerate(trading_dates)}
    campaigns: list[dict] = []
    for (stock_id, trader_id), g in events.groupby(["stock_id", trader_col]):
        g = g.sort_values("trade_date")
        dates = g["trade_date"].tolist()
        cur_start = dates[0]
        cur_last_idx = idx.get(dates[0])
        n_in_campaign = 1
        for d in dates[1:]:
            di = idx.get(d)
            if cur_last_idx is not None and di is not None and di - cur_last_idx <= CAMPAIGN_GAP_TRADING_DAYS:
                cur_last_idx = di
                n_in_campaign += 1
                continue
            campaigns.append(
                {"stock_id": stock_id, trader_col: trader_id, "entry_date": cur_start, "n_events_in_campaign": n_in_campaign}
            )
            cur_start = d
            cur_last_idx = di
            n_in_campaign = 1
        campaigns.append(
            {"stock_id": stock_id, trader_col: trader_id, "entry_date": cur_start, "n_events_in_campaign": n_in_campaign}
        )
    return pd.DataFrame(campaigns).sort_values("entry_date").reset_index(drop=True)


def load_close_panel(conn, stock_ids: list[str]) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT trade_date, stock_id, close
        FROM stock_daily_bars
        WHERE source = 'finmind' AND stock_id IN ({placeholders})
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=stock_ids)
    return df.pivot(index="trade_date", columns="stock_id", values="close")


def forward_returns_h9(campaigns: pd.DataFrame, close: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    if campaigns.empty:
        return campaigns.assign(**{f"excess_h{HORIZON}": pd.Series(dtype=float)})
    dates = close.index.tolist()
    idx = {d: i for i, d in enumerate(dates)}
    bench = bench.reindex(dates)
    rows = []
    for _, c in campaigns.iterrows():
        entry = c["entry_date"]
        if entry not in idx or c["stock_id"] not in close.columns:
            continue
        i0 = idx[entry]
        j = i0 + HORIZON
        row = dict(c)
        if j >= len(dates):
            row[f"excess_h{HORIZON}"] = np.nan
            row[f"stock_ret_h{HORIZON}"] = np.nan
            rows.append(row)
            continue
        s0 = close[c["stock_id"]].iloc[i0]
        s1 = close[c["stock_id"]].iloc[j]
        b0 = bench.iloc[i0]
        b1 = bench.iloc[j]
        if pd.isna(s0) or pd.isna(s1) or pd.isna(b0) or pd.isna(b1) or s0 <= 0 or b0 <= 0:
            row[f"excess_h{HORIZON}"] = np.nan
            row[f"stock_ret_h{HORIZON}"] = np.nan
        else:
            stock_ret = s1 / s0 - 1.0
            bench_ret = b1 / b0 - 1.0
            row[f"excess_h{HORIZON}"] = stock_ret - bench_ret
            row[f"stock_ret_h{HORIZON}"] = stock_ret
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_with_stats(vals: pd.Series) -> dict:
    vals = vals.dropna()
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean_pct": None, "win_rate_pct": None, "sharpe": None,
                "t_stat": None, "p_value": None, "ci95_lo_pct": None, "ci95_hi_pct": None,
                "significant_95": None}
    mean = float(vals.mean())
    win = float((vals > 0).mean()) * 100
    sd = float(vals.std(ddof=1)) if n > 1 else float("nan")
    sharpe = mean / sd if sd and sd > 0 else None
    if n > 1 and sd > 0:
        t_stat, p_value = stats.ttest_1samp(vals, popmean=0.0)
        se = sd / np.sqrt(n)
        tcrit = stats.t.ppf(0.975, df=n - 1)
        ci_lo = mean - tcrit * se
        ci_hi = mean + tcrit * se
    else:
        t_stat, p_value, ci_lo, ci_hi = None, None, None, None
    return {
        "n": n,
        "mean_pct": round(mean * 100, 3),
        "win_rate_pct": round(win, 1),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "t_stat": round(float(t_stat), 3) if t_stat is not None else None,
        "p_value": round(float(p_value), 4) if p_value is not None else None,
        "ci95_lo_pct": round(ci_lo * 100, 3) if ci_lo is not None else None,
        "ci95_hi_pct": round(ci_hi * 100, 3) if ci_hi is not None else None,
        "significant_95": bool(p_value is not None and p_value < 0.05),
    }


def bootstrap_ci(vals: pd.Series, n_boot: int = 1000, seed: int = 42) -> tuple[float, float]:
    vals = vals.dropna().to_numpy()
    if len(vals) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    n = len(vals)
    for i in range(n_boot):
        sample = rng.choice(vals, size=n, replace=True)
        means[i] = sample.mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo) * 100, float(hi) * 100)


# ---------------------------------------------------------------------------
# (a) 全交易剖面 + (b) 建倉型態
# ---------------------------------------------------------------------------

def analyze_trading_profile(raw_9217: pd.DataFrame) -> dict:
    d = raw_9217.copy()
    top1_days = d[d["is_top1_buy"]]
    n_top1_days = len(top1_days)
    n_stocks = top1_days["stock_id"].nunique()
    amt = top1_days["buy_amt"]
    months = pd.to_datetime(top1_days["trade_date"]).dt.to_period("M")
    n_months = months.nunique()
    per_month = n_top1_days / n_months if n_months else 0.0

    stock_counts = top1_days.groupby("stock_id").size().sort_values(ascending=False)
    top10_share = stock_counts.head(10).sum() / n_top1_days * 100 if n_top1_days else None

    return {
        "n_top1_buy_days": n_top1_days,
        "n_distinct_stocks": n_stocks,
        "amt_median": float(amt.median()) if n_top1_days else None,
        "amt_mean": float(amt.mean()) if n_top1_days else None,
        "amt_max": float(amt.max()) if n_top1_days else None,
        "n_months_active": int(n_months),
        "top1_days_per_month": round(per_month, 2),
        "top10_stock_share_of_top1_days_pct": round(top10_share, 1) if top10_share is not None else None,
        "top_stocks": stock_counts.head(10).to_dict(),
    }


def analyze_position_building(conn, raw_all_9217: pd.DataFrame, top_stocks: list[str]) -> pd.DataFrame:
    """對交易最頻繁的股票，算累積淨部位時間序列，判斷建倉/當沖型態。"""
    rows = []
    for sid in top_stocks:
        g = raw_all_9217[raw_all_9217["stock_id"] == sid].sort_values("trade_date")
        if g.empty:
            continue
        g = g.copy()
        g["cum_net"] = g["net"].cumsum()
        max_pos = g["cum_net"].max()
        min_pos = g["cum_net"].min()
        final_pos = g["cum_net"].iloc[-1]
        first_date = g["trade_date"].iloc[0]
        last_date = g["trade_date"].iloc[-1]
        n_days_traded = len(g)
        # 判斉型態：final/max 比值高 -> 持續建倉；final 接近 0 但 max 曾經很高 -> 曾建倉後出清；
        # max 和 min 都在 0 附近震盪 -> 當沖/造市
        amplitude = max_pos - min_pos if pd.notna(max_pos) and pd.notna(min_pos) else None
        if amplitude and amplitude > 0:
            retain_ratio = final_pos / max_pos if max_pos and max_pos != 0 else None
        else:
            retain_ratio = None
        if max_pos is not None and max_pos > 0:
            if retain_ratio is not None and retain_ratio > 0.7:
                style = "持續建倉（部位保留>70%）"
            elif retain_ratio is not None and retain_ratio < 0.15:
                style = "建倉後出清（部位保留<15%）"
            else:
                style = "波段進出（部位中間水準）"
        else:
            style = "無明顯建倉（未曾正部位）"
        rows.append(
            {
                "stock_id": sid,
                "n_days_traded": n_days_traded,
                "first_date": first_date,
                "last_date": last_date,
                "max_cum_net_shares": max_pos,
                "min_cum_net_shares": min_pos,
                "final_cum_net_shares": final_pos,
                "retain_ratio": round(retain_ratio, 3) if retain_ratio is not None else None,
                "style": style,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# (c) 擇時品質
# ---------------------------------------------------------------------------

def compute_pct_in_range_20d(conn) -> pd.DataFrame:
    """全市場每日 (close - rolling_min20) / (rolling_max20 - rolling_min20)，
    排除 mega + ETF，用於跟 9217 買進日的百分位分布做比較。"""
    q = """
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source='finmind' AND trade_date >= ? AND trade_date <= ?
        ORDER BY stock_id, trade_date
    """
    df = pd.read_sql_query(q, conn, params=(DATE_START, DATE_END))
    df["roll_min20"] = df.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=20).min())
    df["roll_max20"] = df.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=20).max())
    rng = df["roll_max20"] - df["roll_min20"]
    df["pct_in_range20"] = np.where(rng > 0, (df["close"] - df["roll_min20"]) / rng, np.nan)
    return df[["stock_id", "trade_date", "pct_in_range20"]]


def load_songshan_crossing_check() -> dict:
    """(e) 檢查 9217 是否常出現在對倒名單裡（買賣雙邊都活躍）。"""
    result = {"in_buy_track_record": False, "in_sell_track_record": False, "in_pair_track_record": False}
    if CROSSING_BUY_CSV.exists():
        buy_df = pd.read_csv(CROSSING_BUY_CSV, dtype={"buy_trader_id": str})
        result["in_buy_track_record"] = TRADER_ID in set(buy_df["buy_trader_id"])
    if CROSSING_SELL_CSV.exists():
        sell_df = pd.read_csv(CROSSING_SELL_CSV, dtype={"sell_trader_id": str})
        result["in_sell_track_record"] = TRADER_ID in set(sell_df["sell_trader_id"])
    if CROSSING_PAIR_CSV.exists():
        pair_df = pd.read_csv(CROSSING_PAIR_CSV, dtype={"buy_trader_id": str, "sell_trader_id": str})
        result["in_pair_track_record"] = (
            (pair_df["buy_trader_id"] == TRADER_ID).any() or (pair_df["sell_trader_id"] == TRADER_ID).any()
        )
    return result


# ---------------------------------------------------------------------------
# 既有規則（scan_5d_net95）重新掃描 9217 歷史
# ---------------------------------------------------------------------------

def scan_existing_rule_events(raw_9217_all: pd.DataFrame, trading_dates: list[str]) -> pd.DataFrame:
    """向量化重現 scan_5d_net95：對 9217 每檔曾交易過的股票，在完整交易日曆上
    (缺值視為0) 算 rolling 5 日 buy_5d / sell_5d，net_ratio = (buy_5d-sell_5d)/buy_5d，
    找出 buy_5d>=0.5億 且 net_ratio>=0.95 的交易日（fire day）。"""
    stock_ids = sorted(raw_9217_all["stock_id"].unique().tolist())
    date_index = pd.Index(trading_dates, name="trade_date")
    fire_rows = []
    for sid in stock_ids:
        g = raw_9217_all[raw_9217_all["stock_id"] == sid].set_index("trade_date")
        buy_amt = g["buy_amt"].reindex(date_index, fill_value=0.0)
        sell_amt = g["sell_amt"].reindex(date_index, fill_value=0.0)
        buy_5d = buy_amt.rolling(EXISTING_WINDOW, min_periods=EXISTING_WINDOW).sum()
        sell_5d = sell_amt.rolling(EXISTING_WINDOW, min_periods=EXISTING_WINDOW).sum()
        net_ratio = (buy_5d - sell_5d) / buy_5d.replace(0, np.nan)
        fired = (buy_5d >= EXISTING_BUY_FLOOR) & (net_ratio >= EXISTING_NET_MIN)
        fired_dates = date_index[fired.fillna(False).to_numpy()]
        for d in fired_dates:
            fire_rows.append({"stock_id": sid, "trade_date": d, "top1_trader_id": TRADER_ID,
                               "buy_5d": float(buy_5d.loc[d]), "net_ratio_5d": float(net_ratio.loc[d])})
    return pd.DataFrame(fire_rows)


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    mega = load_mega_blacklist()
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted([d for d in bench.index.astype(str).unique().tolist() if DATE_START <= d <= DATE_END])

    print(f"[INFO] 用 SQL window function 在資料庫端算全市場 {DATE_START}..{DATE_END} 每日 Top1 買方/賣方"
          "（不套集中度門檻，避免把上億列原始資料載進 pandas）...")
    daily_top1_buy = compute_daily_top1_buy(conn, mega)
    print(f"[INFO] 全市場 (date,stock) Top1 買方紀錄數：{len(daily_top1_buy):,}")
    daily_top1_sell = compute_daily_top1_sell(conn, mega)
    print(f"[INFO] 全市場 (date,stock) Top1 賣方紀錄數：{len(daily_top1_sell):,}")

    # ---- 9217 全部交易原始資料（不限 Top1，用於建倉型態 cumsum + 既有規則掃描）
    raw_9217_all = load_trader_all_trades(conn, TRADER_ID, mega)
    print(f"[INFO] 9217 全部有交易紀錄的 (stock,date) 筆數：{len(raw_9217_all):,}，涉及股票數：{raw_9217_all['stock_id'].nunique()}")

    # ---- (a)(b) 交易剖面：9217 何時是 Top1 買方
    daily_top1_buy["is_top1_buy"] = True
    top1_9217 = daily_top1_buy[daily_top1_buy["top1_trader_id"] == TRADER_ID].copy()
    top1_9217_flagged = raw_9217_all.merge(
        top1_9217[["trade_date", "stock_id"]].assign(is_top1_buy=True),
        on=["trade_date", "stock_id"], how="left",
    )
    top1_9217_flagged["is_top1_buy"] = top1_9217_flagged["is_top1_buy"].fillna(False)
    profile = analyze_trading_profile(top1_9217_flagged)
    print(f"[INFO] 9217 全部 Top1 買方日：{profile['n_top1_buy_days']}，涉及股票 {profile['n_distinct_stocks']} 檔")

    top_stock_ids = list(profile["top_stocks"].keys())[:10]
    position_df = analyze_position_building(conn, raw_9217_all, top_stock_ids)
    position_df.to_csv(OUT_POSITION_CSV, index=False)
    print(f"[OK] 建倉型態明細寫入 {OUT_POSITION_CSV}")

    # ---- (c) 擇時品質
    pct_range = compute_pct_in_range_20d(conn)
    top1_9217_pct = top1_9217.merge(pct_range, on=["trade_date", "stock_id"], how="left")
    market_baseline = pct_range["pct_in_range20"].dropna()
    branch_pct = top1_9217_pct["pct_in_range20"].dropna()
    timing_summary = pd.DataFrame(
        [
            {
                "group": "9217_top1_buy_days",
                "n": len(branch_pct),
                "median_pct_in_range20": round(float(branch_pct.median()), 3) if len(branch_pct) else None,
                "mean_pct_in_range20": round(float(branch_pct.mean()), 3) if len(branch_pct) else None,
                "p25": round(float(branch_pct.quantile(0.25)), 3) if len(branch_pct) else None,
                "p75": round(float(branch_pct.quantile(0.75)), 3) if len(branch_pct) else None,
            },
            {
                "group": "market_all_days_baseline",
                "n": len(market_baseline),
                "median_pct_in_range20": round(float(market_baseline.median()), 3),
                "mean_pct_in_range20": round(float(market_baseline.mean()), 3),
                "p25": round(float(market_baseline.quantile(0.25)), 3),
                "p75": round(float(market_baseline.quantile(0.75)), 3),
            },
        ]
    )
    if len(branch_pct) > 1:
        mw_stat, mw_p = stats.mannwhitneyu(branch_pct, market_baseline, alternative="two-sided")
        timing_summary.loc[len(timing_summary)] = {
            "group": "mannwhitney_test", "n": len(branch_pct), "median_pct_in_range20": None,
            "mean_pct_in_range20": None, "p25": None, "p75": None,
        }
        timing_mw = {"u_stat": float(mw_stat), "p_value": float(mw_p)}
    else:
        timing_mw = {"u_stat": None, "p_value": None}
    timing_summary.to_csv(OUT_TIMING_CSV, index=False)
    print(f"[OK] 擇時品質明細寫入 {OUT_TIMING_CSV}")

    # ---- (d) 泛用 Top1 框架：全部 Top1 買方日 -> 合併行情 -> forward H9 + 統計檢定
    alltop1_events = daily_top1_buy[
        (daily_top1_buy["top1_trader_id"] == TRADER_ID) & (daily_top1_buy["top1_amt"] >= TOP1_AMT_FLOOR)
    ].copy()
    alltop1_events.to_csv(OUT_ALLTOP1_EVENTS_CSV, index=False)
    print(f"[INFO] 9217 Top1 買方日（金額>={TOP1_AMT_FLOOR/1e4:.0f}萬）原始日數：{len(alltop1_events)}")

    alltop1_campaigns = merge_campaigns(alltop1_events, trading_dates, trader_col="top1_trader_id")
    print(f"[INFO] 合併行情後：{len(alltop1_campaigns)} 筆")

    stock_ids_needed = set(alltop1_campaigns["stock_id"].unique().tolist()) if not alltop1_campaigns.empty else set()

    # ---- 既有規則：scan_5d_net95 重新掃描
    print("[INFO] 用既有 songshan-copytrade 規則（5日買>=0.5億 + 5日淨比>=95%）重新掃描 9217 歷史...")
    existing_events = scan_existing_rule_events(raw_9217_all, trading_dates)
    print(f"[INFO] 既有規則 fire 交易日數（未合併）：{len(existing_events)}")
    existing_campaigns = merge_campaigns(existing_events, trading_dates, trader_col="top1_trader_id") if not existing_events.empty else pd.DataFrame()
    print(f"[INFO] 既有規則合併行情後：{len(existing_campaigns)} 筆")
    stock_ids_needed |= set(existing_campaigns["stock_id"].unique().tolist()) if not existing_campaigns.empty else set()

    close = load_close_panel(conn, sorted(stock_ids_needed))

    alltop1_result = forward_returns_h9(alltop1_campaigns, close, bench)
    alltop1_result.to_csv(OUT_ALLTOP1_CAMPAIGNS_CSV, index=False)
    print(f"[OK] 泛用框架行情+forward return 明細寫入 {OUT_ALLTOP1_CAMPAIGNS_CSV}")

    existing_result = forward_returns_h9(existing_campaigns, close, bench) if not existing_campaigns.empty else pd.DataFrame()
    if not existing_result.empty:
        existing_result.to_csv(OUT_EXISTING_RULE_CAMPAIGNS_CSV, index=False)
        print(f"[OK] 既有規則行情+forward return 明細寫入 {OUT_EXISTING_RULE_CAMPAIGNS_CSV}")

    alltop1_stats = summarize_with_stats(alltop1_result[f"excess_h{HORIZON}"]) if not alltop1_result.empty else summarize_with_stats(pd.Series(dtype=float))
    existing_stats = summarize_with_stats(existing_result[f"excess_h{HORIZON}"]) if not existing_result.empty else summarize_with_stats(pd.Series(dtype=float))

    boot_lo, boot_hi = bootstrap_ci(alltop1_result[f"excess_h{HORIZON}"]) if not alltop1_result.empty else (float("nan"), float("nan"))
    boot_lo2, boot_hi2 = bootstrap_ci(existing_result[f"excess_h{HORIZON}"]) if not existing_result.empty else (float("nan"), float("nan"))

    # ---- (e) 對倒檢查
    crossing_check = load_songshan_crossing_check()
    n_buy_days = len(top1_9217)
    n_sell_days = len(daily_top1_sell[daily_top1_sell["top1_trader_id"] == TRADER_ID])
    sell_events_9217_as_seller = raw_9217_all[raw_9217_all["sell_amt"] > 0]
    n_days_any_sell = raw_9217_all[raw_9217_all["sell_amt"] > 0]["trade_date"].nunique()
    n_days_any_buy = raw_9217_all[raw_9217_all["buy_amt"] > 0]["trade_date"].nunique()

    conn.close()

    prior_edge = load_prior_edge_study()

    render_report(
        profile=profile,
        position_df=position_df,
        timing_summary=timing_summary,
        timing_mw=timing_mw,
        alltop1_stats=alltop1_stats,
        existing_stats=existing_stats,
        boot_ci_alltop1=(boot_lo, boot_hi),
        boot_ci_existing=(boot_lo2, boot_hi2),
        crossing_check=crossing_check,
        n_buy_days=n_buy_days,
        n_sell_days=n_sell_days,
        n_days_any_buy=n_days_any_buy,
        n_days_any_sell=n_days_any_sell,
        prior_edge=prior_edge,
    )
    return 0


def load_prior_edge_study() -> dict | None:
    if not PRIOR_EDGE_STUDY_JSON.exists():
        return None
    try:
        data = json.loads(PRIOR_EDGE_STUDY_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        holds = data["2_hold_sweep"]["holds"]
        h9 = next(h for h in holds if h["hold"] == 9)
        return {
            "generated_at": data.get("generated_at"),
            "spec": "amount>=40M · vol>=200k · Top1 · L1H9 · 60bps",
            "full": h9["costs"]["60bps"]["full"],
            "is": h9["costs"]["60bps"]["is"],
            "oos": h9["costs"]["60bps"]["oos"],
        }
    except (KeyError, StopIteration):
        return None


def render_report(
    *, profile, position_df, timing_summary, timing_mw, alltop1_stats, existing_stats,
    boot_ci_alltop1, boot_ci_existing, crossing_check, n_buy_days, n_sell_days,
    n_days_any_buy, n_days_any_sell, prior_edge,
) -> None:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    lines = [
        "# 9217（凱基-松山）深挖 · 泛用 Top1 框架 vs 既有 songshan-copytrade 規則",
        "",
        f"產出時間：{now}",
        f"資料範圍：{DATE_START} ~ {DATE_END}（stock_broker_branch_daily 涵蓋率限制起點）",
        "",
        "## 背景",
        "",
        "9217 凱基-松山是本專案現行 live 在跑的跟單策略 `songshan-copytrade` 的核心分點"
        "（config/order.yaml，5日累積買進>=0.5億 且 5日淨比>=95%）。這次深挖用「這輪研究"
        "開發的通用 Top1 買方框架」回頭驗證這個已經實盤跑過的分點，看兩套方法算出來的"
        "訊號品質、方向是否一致——如果一致，代表泛用框架本身可信；如果不一致，代表泛用框架"
        "可能有系統性偏誤。",
        "",
        "## (a) 全交易剖面",
        "",
        f"- 9217 全部 Top1 買方日（{DATE_START}起）：**{profile['n_top1_buy_days']}** 天",
        f"- 涉及股票數：**{profile['n_distinct_stocks']}** 檔",
        f"- 買超金額中位數：{fmt_amt(profile['amt_median'])}；平均：{fmt_amt(profile['amt_mean'])}；最大：{fmt_amt(profile['amt_max'])}",
        f"- 活躍月數：{profile['n_months_active']}，平均每月 Top1 買方天數：{profile['top1_days_per_month']}",
        f"- 前10大交易股票占全部 Top1 買方日數的比重：{profile['top10_stock_share_of_top1_days_pct']}%"
        + ("（分散度高，非壓寶少數幾檔）" if (profile['top10_stock_share_of_top1_days_pct'] or 100) < 50 else "（相對集中在少數股票）"),
        "",
        "前10大最常出手股票（Top1 買方日次數）：",
        "",
        "| 股票代號 | Top1 買方日次數 |",
        "|---|---|",
    ]
    for sid, cnt in profile["top_stocks"].items():
        lines.append(f"| {sid} | {cnt} |")

    lines += [
        "",
        "## (b) 建倉型態（前10大交易股票的累積淨部位）",
        "",
        "| 股票 | 交易天數 | 首日 | 末日 | 最大累積淨股數 | 最小累積淨股數 | 最終累積淨股數 | 部位保留比 | 型態判定 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in position_df.iterrows():
        lines.append(
            f"| {r['stock_id']} | {r['n_days_traded']} | {r['first_date']} | {r['last_date']} | "
            f"{r['max_cum_net_shares']:,.0f} | {r['min_cum_net_shares']:,.0f} | {r['final_cum_net_shares']:,.0f} | "
            f"{r['retain_ratio']} | {r['style']} |"
        )
    n_persist = (position_df["style"] == "持續建倉（部位保留>70%）").sum()
    n_total_pos = len(position_df)
    lines += [
        "",
        f"前{n_total_pos}檔重點交易股票中，{n_persist} 檔屬於「持續建倉」型態"
        + ("——支持 9217 是真的在做方向性建倉，而非單純造市/當沖。" if n_persist >= n_total_pos * 0.5 else
           "——建倉型態不算壓倒性一致，需搭配其他證據判斷。"),
        "",
        "## (c) 擇時品質（買在低檔還是追高）",
        "",
        "指標：`(close - rolling_min_20) / (rolling_max_20 - rolling_min_20)`，0=20日最低、1=20日最高。",
        "",
        "| 樣本 | n | 中位數 | 平均 | P25 | P75 |",
        "|---|---|---|---|---|---|",
    ]
    for _, r in timing_summary.iterrows():
        if r["group"] == "mannwhitney_test":
            continue
        lines.append(f"| {r['group']} | {r['n']} | {r['median_pct_in_range20']} | {r['mean_pct_in_range20']} | {r['p25']} | {r['p75']} |")

    mw_p = timing_mw.get("p_value")
    mw_note = (
        f"Mann-Whitney U 檢定 p-value = {round(mw_p, 4)}"
        + ("（統計上顯著不同於市場基準分布）" if mw_p is not None and mw_p < 0.05 else "（與市場基準分布無統計顯著差異）")
        if mw_p is not None else "樣本不足以做檢定。"
    )
    lines += [
        "",
        mw_note,
        "",
        "## (d) 統計顯著性 · 泛用 Top1 框架 vs 既有 songshan-copytrade 規則",
        "",
        "兩套方法都算 forward H9 excess return（vs IX0001）：",
        "",
        "| 方法 | n | H9 mean% | 勝率% | Sharpe | t 統計量 | p-value | t分布95% CI | bootstrap 95% CI | 統計顯著(p<0.05) |",
        "|---|---|---|---|---|---|---|---|---|---|",
        fmt_stats_row("泛用 Top1 框架（金額>=500萬，不套集中度門檻）", alltop1_stats, boot_ci_alltop1),
        fmt_stats_row("既有 songshan-copytrade 規則（5日買>=0.5億+5日淨比>=95%）", existing_stats, boot_ci_existing),
    ]

    lines += [
        "",
        "## 交叉比對：跟已知有效分點（980T +2.74%、9227 +4.7%）放同一量尺",
        "",
        "| 分點 | 樣本定義 | n | H9 mean% |",
        "|---|---|---|---|",
        f"| 9217 凱基-松山 | 泛用 Top1 框架 | {alltop1_stats['n']} | {alltop1_stats['mean_pct']} |",
        f"| 9217 凱基-松山 | 既有 songshan-copytrade 規則 | {existing_stats['n']} | {existing_stats['mean_pct']} |",
        "| 980T 元大自營 | branch_consistency alltop1（既有研究） | — | +2.74（既有研究數字，供參考） |",
        "| 9227 凱基-城中 | branch_consistency alltop1（既有研究） | 140 | +4.7（既有研究數字，供參考） |",
        "",
    ]

    if prior_edge:
        f = prior_edge["full"]
        i = prior_edge["is"]
        o = prior_edge["oos"]
        lines += [
            "## 對照：既有研究報告 `20260718_songshan_h10_edge_study.json`",
            "",
            f"規格：{prior_edge['spec']}（產出於 {prior_edge['generated_at']}）",
            "",
            "| 樣本 | n | 勝率% | mean% | mean_excess% |",
            "|---|---|---|---|---|",
            f"| Full | {f['n']} | {f['win_rate_pct']} | {f['mean_pct']} | {f['mean_excess_pct']} |",
            f"| IS | {i['n']} | {i['win_rate_pct']} | {i['mean_pct']} | {i['mean_excess_pct']} |",
            f"| OOS | {o['n']} | {o['win_rate_pct']} | {o['mean_pct']} | {o['mean_excess_pct']} |",
            "",
            "（此為既有研究用不同篩選條件——金額>=40M + 流動性門檻 vol>=200k + 單槽——"
            "算出的 L1H9 結果，跟本次泛用框架/既有規則的樣本定義不完全相同，僅供方向性對照，"
            "不是同一個統計量。）",
            "",
        ]
    else:
        lines += ["## 對照：既有研究報告", "", "未找到或無法解析 `20260718_songshan_h10_edge_study.json`。", ""]

    lines += [
        "## data/order/songshan_copytrade_ledger.json（實盤下單記錄）",
        "",
        "本機 `data/order/` 目錄下未找到 `songshan_copytrade_ledger.json`（可能只存在於實際跑"
        "live 單的機器上，本次研究環境沒有），故無法對照真實下單績效，此項從略。",
        "",
        "## (e) 對倒檢查（9217 是否買賣雙邊都活躍）",
        "",
        f"- 9217 出現在 `whale_crossing_buy_branch_track_record.csv`（買方對倒名單）：{crossing_check['in_buy_track_record']}",
        f"- 9217 出現在 `whale_crossing_sell_branch_track_record.csv`（賣方對倒名單）：{crossing_check['in_sell_track_record']}",
        f"- 9217 出現在 `whale_crossing_pair_track_record.csv`（配對名單）：{crossing_check['in_pair_track_record']}",
        "",
        f"補充統計（不限對倒事件，全樣本 {DATE_START}~{DATE_END}）：",
        f"- 9217 當 Top1 **買方**的天數：{n_buy_days}",
        f"- 9217 當 Top1 **賣方**的天數：{n_sell_days}",
        f"- 9217 有任何買進紀錄的天數：{n_days_any_buy}；有任何賣出紀錄的天數：{n_days_any_sell}",
        "",
    ]
    if n_sell_days == 0 and not crossing_check["in_buy_track_record"] and not crossing_check["in_sell_track_record"]:
        crossing_verdict = (
            "9217 完全沒有出現在既有的對倒（whale_crossing）候選名單裡，且當 Top1 賣方的天數為 0——"
            "這支持「松山是真正的方向性大戶（買方為主）」的假設，而非像 980T 那樣買賣雙邊都活躍的"
            "自營造市帳戶。這也間接支持 980T 需要被當作造市帳戶對照組看待的判斷。"
        )
    elif n_sell_days > 0 and n_sell_days < n_buy_days * 0.2:
        crossing_verdict = (
            f"9217 偶爾也會當 Top1 賣方（{n_sell_days} 天），但相對買方天數（{n_buy_days} 天）"
            "占比很小，整體仍偏買方主導，比較接近方向性大戶而非造市帳戶。"
        )
    else:
        crossing_verdict = (
            f"9217 當 Top1 賣方（{n_sell_days} 天）相對買方（{n_buy_days} 天）比例不低，"
            "買賣雙邊都算活躍，這點需要納入考量，不能完全排除部分部位是短打/避險。"
        )
    lines.append(crossing_verdict)
    lines.append("")

    lines += [
        "## 結論：泛用框架是否可信？",
        "",
        conclusion_text(alltop1_stats, existing_stats),
        "",
        "## 方法論摘要",
        "",
        f"- 泛用 Top1 框架：{DATE_START}~{DATE_END}，該分點是當日該股買超金額最大者（Top1）"
        f"且金額>={TOP1_AMT_FLOOR/1e4:.0f}萬，不套用任何集中度門檻（Top1+2占比/Top3占比/出手"
        "分點數），排除 mega 黑名單 + ETF（00開頭）。",
        f"- 既有規則（scan_5d_net95）：對 9217 每檔曾交易過的股票，在完整交易日曆上（缺值視為0）"
        f"算 rolling {EXISTING_WINDOW} 日買超金額總和 buy_5d 與賣超金額總和 sell_5d，"
        f"net_ratio=(buy_5d-sell_5d)/buy_5d，找出 buy_5d>={EXISTING_BUY_FLOOR/1e8:.1f}億 且 "
        f"net_ratio>={EXISTING_NET_MIN} 的交易日，向量化重現 "
        "`scripts/research/run_songshan_follow_watch.py::scan_5d_net95` 的邏輯（非直接呼叫，"
        "避免依賴其内部 as-of 迴圈效能，數學上等價）。",
        f"- 行情合併：同一 (stock_id, trader) 在 <={CAMPAIGN_GAP_TRADING_DAYS} 個交易日內的連續"
        "事件日視為同一波，只取首個事件日為進場點，避免偽獨立樣本灌水統計量（兩套方法採同一"
        "合併規則，確保可比較）。",
        f"- Excess return：進場日收盤 → T+{HORIZON} 收盤，個股報酬減 IX0001 同期報酬。",
        "- 統計檢定：對 excess return 樣本做單樣本 t-test（H0: mean=0），另外用 t 分布跟"
        "bootstrap（1000次重抽樣）各算一次 95% CI，互相印證。",
        "- 擇時品質基準：全市場（排除 mega+ETF）全部交易日的 pct_in_range20 分布，用"
        "Mann-Whitney U 檢定跟 9217 買進日樣本比較分布是否不同。",
        "- 樣本數警語：n < 30 時，勝率/均值抽樣誤差很大，結論僅供參考方向。",
    ]

    OUT_REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[OK] 報告寫入 {OUT_REPORT_MD}")


def fmt_amt(v) -> str:
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v/1e8:.2f}億"
    return f"{v/1e4:.0f}萬"


def fmt_stats_row(label: str, s: dict, boot_ci: tuple[float, float]) -> str:
    ci_t = f"[{s['ci95_lo_pct']}, {s['ci95_hi_pct']}]" if s["ci95_lo_pct"] is not None else "—"
    ci_b = f"[{round(boot_ci[0],3)}, {round(boot_ci[1],3)}]" if not (np.isnan(boot_ci[0]) if boot_ci[0] is not None else True) else "—"
    return (
        f"| {label} | {s['n']} | {s['mean_pct']} | {s['win_rate_pct']} | {s['sharpe']} | "
        f"{s['t_stat']} | {s['p_value']} | {ci_t} | {ci_b} | {s['significant_95']} |"
    )


def conclusion_text(alltop1_stats: dict, existing_stats: dict) -> str:
    a_mean = alltop1_stats.get("mean_pct")
    e_mean = existing_stats.get("mean_pct")
    a_n = alltop1_stats.get("n")
    e_n = existing_stats.get("n")
    a_sig = alltop1_stats.get("significant_95")
    e_sig = existing_stats.get("significant_95")

    parts = []
    if a_mean is not None and e_mean is not None:
        if a_mean > 0 and e_mean > 0:
            parts.append(
                f"兩套方法方向一致：泛用框架（n={a_n}）H9 mean={a_mean}%，既有規則（n={e_n}）"
                f"H9 mean={e_mean}%，都是正的。這支持泛用 Top1 買方框架本身是可信的分析工具——"
                "至少在 9217 這個已知有效的分點上，不會算出跟實盤驗證方向相反的結果。"
            )
        elif a_mean <= 0 and e_mean <= 0:
            parts.append(
                f"兩套方法都算出非正的 H9 excess return（泛用框架 mean={a_mean}%，既有規則 "
                f"mean={e_mean}%）——如果這與 9217 已知的實盤成功經驗矛盾，需要誠實指出可能的"
                "原因：實盤成功可能來自進場時機（09:25-09:40 chase_ask）、更嚴格的流動性/選股"
                "過濾、或本次泛用框架的合併/門檻設計本身有系統性偏誤，不能簡單斷言框架可信。"
            )
        else:
            parts.append(
                f"兩套方法方向不一致：泛用框架 mean={a_mean}%（n={a_n}），既有規則 mean={e_mean}%"
                f"（n={e_n}）。這代表「不套集中度/嚴格 5日窗口門檻的泛用 Top1 框架」跟「既有"
                "專屬 5日累積規則」對 9217 抓到的其實是不完全相同的一群訊號（existing rule 的"
                "5日窗口+95%淨比門檻遠比泛用500萬底線嚴格），差異本身不代表泛用框架有問題，"
                "但確實說明兩者不能直接互相取代，既有規則對這個分點仍是更精煉的訊號。"
            )
    else:
        parts.append("其中一套方法樣本量為0，無法比較方向一致性。")

    if a_n is not None and a_n < 30:
        parts.append(f"警語：泛用框架樣本 n={a_n} 偏小，統計檢定力有限。")
    if e_n is not None and e_n < 30:
        parts.append(f"警語：既有規則樣本 n={e_n} 偏小，統計檢定力有限。")
    if a_sig is False:
        parts.append("泛用框架的 mean 未達統計顯著（p>=0.05），不能排除是雜訊。")
    if e_sig is False:
        parts.append("既有規則的 mean 未達統計顯著（p>=0.05），不能排除是雜訊，儘管實盤已在跑。")

    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
