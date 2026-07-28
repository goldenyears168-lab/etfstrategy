"""稽核 stock_daily_bars（source='finmind'）在 9217/9661 兩分點交易過的股票清單上，
是否存在類似 8358/1815 那樣「連續 N 個交易日完全沒有價格資料」的資料缺口。

背景：8358、1815 在 2020-01-01~2023-01-02 之間 stock_daily_bars 完全沒有資料，
導致用 INNER JOIN 接價格的分析會靜默丟棄這段期間的真實分點交易紀錄，產生假的
「建倉起點」。這支腳本系統性掃描同一個問題是否還污染了其他股票/分析。

方法：
1. 交易日曆用 daily_bars(code='IX0001') 在 [START, END] 之間的日期序列當基準
   （TEJ 優先，但 load_benchmark_close 的 per-date 優先序在這裡不重要，我們只要日期集合）。
2. 對每一檔在兩分點 stock_broker_branch_daily 出現過的 stock_id，抓出
   stock_daily_bars(source='finmind') 在同一區間內「有資料的日期」對齊到交易日曆的
   index，計算：
     - leading gap：日曆起點到第一筆資料之間的缺口
     - interior gap：兩筆相鄰資料之間的缺口（就是 8358/1815 那種「中間斷開」）
     - trailing gap：最後一筆資料到日曆終點之間的缺口
   缺口長度 >= GAP_THRESHOLD_DAYS（預設 60 個交易日）就記錄下來。
3. 對每個缺口，反查該分點在缺口期間是否真的有交易紀錄（stock_broker_branch_daily），
   量化「這個缺口如果被 INNER JOIN 掉，會犧牲多少筆真實分點交易日」。
4. 額外統計：完全沒有任何 finmind 價格資料的股票（不是「有缺口」，是「從來沒有」）。

輸出：
  reports/research/branch-footprint-screen/coverage_gap_audit_full.csv       -- 全部缺口明細
  reports/research/branch-footprint-screen/coverage_gap_audit_summary.csv    -- 每檔股票彙總
  reports/research/branch-footprint-screen/coverage_gap_zero_data_stocks.csv -- 完全無價格資料清單
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from stock_db import connect_ro, DEFAULT_DB_PATH  # noqa: E402

START = "2019-01-01"
END = "2026-07-22"
GAP_THRESHOLD_DAYS = 60  # 連續多少個「交易日曆」缺口才算異常（見報告理由說明）
BRANCHES = ("9217", "9661")

# 這次研究已經產出具體結論、需要交叉核對的股票
TRACKED_STOCKS = [
    "2337", "6147", "3189", "2344", "2408", "8358",
    "8046", "4958", "1303", "1815", "3006",
]

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "branch-footprint-screen"
OUT_FULL = OUT_DIR / "coverage_gap_audit_full.csv"
OUT_SUMMARY = OUT_DIR / "coverage_gap_audit_summary.csv"
OUT_ZERO = OUT_DIR / "coverage_gap_zero_data_stocks.csv"


def main() -> None:
    conn = connect_ro(DEFAULT_DB_PATH)

    # 1. 交易日曆
    cal = pd.read_sql_query(
        "SELECT DISTINCT date AS trade_date FROM daily_bars WHERE code='IX0001' "
        "AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(START, END),
    )["trade_date"].tolist()
    n_cal = len(cal)
    cal_idx = {d: i for i, d in enumerate(cal)}
    print(f"交易日曆 {START}~{END}: {n_cal} 個交易日 (依 IX0001)")

    # 2. 兩分點交易過的股票清單（向量化：一次 SQL 拿全部，不逐檔查）
    universe = pd.read_sql_query(
        "SELECT DISTINCT stock_id FROM stock_broker_branch_daily "
        "WHERE securities_trader_id IN (?,?) AND source='finmind'",
        conn, params=BRANCHES,
    )["stock_id"].tolist()
    universe_set = set(universe)
    print(f"9217/9661 兩分點合計交易過 {len(universe)} 檔股票")

    # 3. 一次拿全部 finmind 價格資料（在稽核區間內），再用 pandas 過濾/分組，避免逐檔查詢
    bars = pd.read_sql_query(
        "SELECT stock_id, trade_date FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ?",
        conn, params=(START, END),
    )
    bars = bars[bars["stock_id"].isin(universe_set)].copy()
    bars["idx"] = bars["trade_date"].map(cal_idx)
    n_dropped = bars["idx"].isna().sum()
    if n_dropped:
        print(f"注意：{n_dropped} 筆 bars 的 trade_date 不在 IX0001 交易日曆內，已排除")
    bars = bars.dropna(subset=["idx"])
    bars["idx"] = bars["idx"].astype(int)

    stocks_with_any_bar = set(bars["stock_id"].unique())
    zero_data_stocks = sorted(universe_set - stocks_with_any_bar)
    print(f"完全沒有任何 finmind 價格資料的股票數：{len(zero_data_stocks)} / {len(universe)}")

    # 4. 兩分點逐日交易紀錄（用來反查缺口期間是否有真實交易被污染），同樣一次撈全部
    branch_daily = pd.read_sql_query(
        "SELECT stock_id, securities_trader_id, trade_date FROM stock_broker_branch_daily "
        "WHERE securities_trader_id IN (?,?) AND source='finmind' "
        "AND trade_date BETWEEN ? AND ?",
        conn, params=(*BRANCHES, START, END),
    )
    branch_daily_by_stock: dict[str, pd.DataFrame] = {
        sid: g for sid, g in branch_daily.groupby("stock_id")
    }

    gap_rows = []
    summary_rows = []

    for sid, g in bars.groupby("stock_id"):
        idxs = np.sort(g["idx"].unique())
        first_i, last_i = idxs[0], idxs[-1]

        segments = []  # (gap_type, start_idx_inclusive, end_idx_inclusive)
        if first_i > 0:
            segments.append(("leading", 0, first_i - 1))
        if len(idxs) > 1:
            diffs = np.diff(idxs)
            gap_positions = np.where(diffs > 1)[0]
            for p in gap_positions:
                gs = idxs[p] + 1
                ge = idxs[p + 1] - 1
                segments.append(("interior", gs, ge))
        if last_i < n_cal - 1:
            segments.append(("trailing", last_i + 1, n_cal - 1))

        stock_gap_count = 0
        stock_max_gap_days = 0
        stock_interior_gap_days_total = 0
        stock_branch_days_lost_total = 0

        for gap_type, gs, ge in segments:
            gap_len = ge - gs + 1
            if gap_len < GAP_THRESHOLD_DAYS:
                continue
            gap_start_date = cal[gs]
            gap_end_date = cal[ge]

            # 反查缺口期間，這檔股票在兩分點是否真的有交易被(潛在)污染
            bdf = branch_daily_by_stock.get(sid)
            n_9217 = n_9661 = 0
            if bdf is not None:
                mask = (bdf["trade_date"] >= gap_start_date) & (bdf["trade_date"] <= gap_end_date)
                sub = bdf[mask]
                n_9217 = int((sub["securities_trader_id"] == "9217").sum())
                n_9661 = int((sub["securities_trader_id"] == "9661").sum())
            branch_days_lost = n_9217 + n_9661

            gap_rows.append({
                "stock_id": sid,
                "gap_type": gap_type,
                "gap_start_date": gap_start_date,
                "gap_end_date": gap_end_date,
                "gap_trading_days": gap_len,
                "branch_9217_trade_days_in_gap": n_9217,
                "branch_9661_trade_days_in_gap": n_9661,
                "branch_trade_days_in_gap_total": branch_days_lost,
                "is_tracked_stock": sid in TRACKED_STOCKS,
            })

            stock_gap_count += 1
            stock_max_gap_days = max(stock_max_gap_days, gap_len)
            if gap_type == "interior":
                stock_interior_gap_days_total += gap_len
            stock_branch_days_lost_total += branch_days_lost

        if stock_gap_count > 0:
            summary_rows.append({
                "stock_id": sid,
                "n_gaps_ge_threshold": stock_gap_count,
                "max_gap_trading_days": stock_max_gap_days,
                "interior_gap_trading_days_total": stock_interior_gap_days_total,
                "branch_trade_days_lost_total": stock_branch_days_lost_total,
                "is_tracked_stock": sid in TRACKED_STOCKS,
            })

    gap_df = pd.DataFrame(gap_rows).sort_values(
        ["is_tracked_stock", "gap_trading_days"], ascending=[False, False]
    )
    summary_df = pd.DataFrame(summary_rows).sort_values(
        ["is_tracked_stock", "max_gap_trading_days"], ascending=[False, False]
    )
    zero_df = pd.DataFrame({"stock_id": zero_data_stocks})
    zero_df["is_tracked_stock"] = zero_df["stock_id"].isin(TRACKED_STOCKS)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gap_df.to_csv(OUT_FULL, index=False)
    summary_df.to_csv(OUT_SUMMARY, index=False)
    zero_df.to_csv(OUT_ZERO, index=False)

    print(f"\n>= {GAP_THRESHOLD_DAYS} 個交易日的缺口：{len(gap_df)} 筆，涉及 {gap_df['stock_id'].nunique() if len(gap_df) else 0} 檔股票")
    print(f"寫入: {OUT_FULL}")
    print(f"寫入: {OUT_SUMMARY}")
    print(f"寫入: {OUT_ZERO}")

    print("\n=== 追蹤名單交叉核對 ===")
    for sid in TRACKED_STOCKS:
        if sid in zero_data_stocks:
            print(f"{sid}: 完全無 finmind 價格資料 (0 筆)")
            continue
        sub = summary_df[summary_df["stock_id"] == sid]
        if sub.empty:
            print(f"{sid}: 無 >= {GAP_THRESHOLD_DAYS} 交易日的缺口 -- 資料完整可信")
        else:
            row = sub.iloc[0]
            print(
                f"{sid}: 有 {int(row['n_gaps_ge_threshold'])} 個缺口, "
                f"最大缺口 {int(row['max_gap_trading_days'])} 交易日, "
                f"分點交易日落在缺口內共 {int(row['branch_trade_days_lost_total'])} 天"
            )


if __name__ == "__main__":
    main()
