#!/usr/bin/env python3
"""VIXTWN 跟美國VIX的關聯性研究——日頻率為主，誠實標注即時/盤中比較做不到的原因.

使用者要求「擴大研究vixtwn跟美國vix即時數據的關聯性」。先查證資料可行性：
  - market_vix_1m 只有VIXTWN，且只有台股日盤session(09:00-13:45)，沒有夜盤
    （TMF夜盤交易15:00-05:00跟美股正常交易時段21:30-04:00(台灣時間)重疊，
    但這張表沒有夜盤資料，之前TMF那條研究線的memory也提過『VIXTWN 1m無2025
    歷史只能shadow驗』同一個資料缺口）。
  - 美國VIX(symbol='VIX', source='yahoo')只有日收盤，完全沒有分鐘資料。
  這代表『即時/盤中同步比較』目前資料做不到——不是不想做，是兩邊真正重疊的
  即時窗口在資料庫裡不存在。這裡改成做嚴謹的『日頻率』研究，包含因果正確的
  領先/落後結構（美股VIX收盤在台股開盤前就已知，屬於可用資訊；反過來台股
  VIXTWN收盤在美股當天收盤前，不能拿來『預測』美股VIX，只能當同期對照）。

範圍：market_vix_daily的VIXTWN用source='computed'（mini自算，2003~2026-08-07
全歷史）、VIX用source='yahoo'（2003~2026-07-31）。四組分析：
  1) 同期水位相關(level correlation)
  2) 同期變動相關(change correlation，Δ)
  3) 領先/落後：美股VIX(t-1)→VIXTWN(t)（因果正確方向，美股前一天收盤在台股
     開盤前已知）
  4) 領先/落後：VIXTWN(t)→美股VIX(t)（僅供對照，非因果預測，因為台股VIXTWN
     收盤時美股當天還沒收盤）
  5) 滾動相關性（每年一段）看關係是否穩定，還是集中在特定危機期間

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/vixtwn_us_vix_correlation_study.py
"""

from __future__ import annotations

import sqlite3

import numpy as np
from scipy import stats as sstats

import stock_db


def load_vix_series(con: sqlite3.Connection, symbol: str, source: str) -> dict[str, float]:
    rows = con.execute(
        "SELECT date, close FROM market_vix_daily WHERE symbol=? AND source=? AND close IS NOT NULL ORDER BY date",
        (symbol, source),
    ).fetchall()
    return {str(d): float(c) for d, c in rows}


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)

    vixtwn = load_vix_series(con, "VIXTWN", "computed")
    vix = load_vix_series(con, "VIX", "yahoo")

    print("=== VIXTWN vs 美國VIX 日頻率關聯性研究 ===")
    print(f"VIXTWN(computed): {len(vixtwn)}筆 {min(vixtwn)}~{max(vixtwn)}")
    print(f"VIX(yahoo): {len(vix)}筆 {min(vix)}~{max(vix)}")
    print(
        "\n⚠️ 資料可行性先講清楚：market_vix_1m只有VIXTWN、只有台股日盤09:00-13:45\n"
        "（無夜盤），美股VIX完全沒有分鐘資料——『即時/盤中同步比較』目前資料庫\n"
        "做不到，不是分析方法問題，是兩邊真正重疊的即時時間窗口不存在。以下全部\n"
        "是日頻率分析。\n"
    )

    common_dates = sorted(set(vixtwn) & set(vix))
    print(f"共同交易日: {len(common_dates)}\n")

    # --- 1) 同期水位相關 ---
    tw_level = np.array([vixtwn[d] for d in common_dates])
    us_level = np.array([vix[d] for d in common_dates])
    ic_level, p_level = sstats.spearmanr(us_level, tw_level)
    pearson_level, pp_level = sstats.pearsonr(us_level, tw_level)
    print("--- 1) 同期水位相關 ---")
    print(f"Spearman IC = {ic_level:+.3f} (p={p_level:.2e}) · Pearson r = {pearson_level:+.3f} (p={pp_level:.2e})")
    print(f"n = {len(common_dates)}\n")

    # --- 2) 同期變動(Δ)相關 ---
    tw_delta, us_delta = [], []
    for i in range(1, len(common_dates)):
        d0, d1 = common_dates[i - 1], common_dates[i]
        tw_delta.append(vixtwn[d1] - vixtwn[d0])
        us_delta.append(vix[d1] - vix[d0])
    tw_delta_arr, us_delta_arr = np.array(tw_delta), np.array(us_delta)
    ic_delta, p_delta = sstats.spearmanr(us_delta_arr, tw_delta_arr)
    print("--- 2) 同期日變動(Δ)相關 ---")
    print(f"Spearman IC(ΔVIX, ΔVIXTWN) = {ic_delta:+.3f} (p={p_delta:.2e}) · n={len(tw_delta)}\n")

    # --- 3) 領先/落後：美股VIX(t-1)→VIXTWN(t)（因果正確方向）---
    # 用排序後的index pointer取代重複list comprehension過濾（O(n)而非O(n^2)，
    # 邏輯也更容易驗證正確）。us_dates[j] = 最近一筆『日期 < d1』的美股VIX交易日，
    # us_dates[j-1] = 再前一筆，兩者相減 = 美股VIX『前一日變動』，在d1當天已知。
    us_dates = sorted(vix)
    tw_dates_all = sorted(vixtwn)
    us_idx = 0  # 指向 us_dates 中最後一個 < 目前 d1 的位置
    tw_lag_ret, us_lead_val = [], []
    for i in range(1, len(tw_dates_all)):
        d0_tw, d1 = tw_dates_all[i - 1], tw_dates_all[i]
        while us_idx + 1 < len(us_dates) and us_dates[us_idx + 1] < d1:
            us_idx += 1
        if us_idx < 1 or us_dates[us_idx] >= d1:
            continue  # 還沒有至少兩筆美股VIX資料在d1之前，跳過
        d0_us, d_prev_us = us_dates[us_idx], us_dates[us_idx - 1]
        us_lead_val.append(vix[d0_us] - vix[d_prev_us])
        tw_lag_ret.append(vixtwn[d1] - vixtwn[d0_tw])
    tw_lag_arr = np.array(tw_lag_ret)
    us_lead_arr = np.array(us_lead_val)
    ic_lead, p_lead = sstats.spearmanr(us_lead_arr, tw_lag_arr)
    print("--- 3) 領先/落後：美股VIX前一日變動 → 隔天VIXTWN變動（因果正確方向）---")
    print(f"Spearman IC = {ic_lead:+.3f} (p={p_lead:.2e}) · n={len(tw_lag_ret)}\n")

    # --- 5) 滾動年度相關性，看關係穩不穩定 ---
    print("--- 5) 逐年同期水位相關性（看關係是否穩定或集中在特定期間）---")
    years = sorted({d[:4] for d in common_dates})
    for y in years:
        yd = [d for d in common_dates if d.startswith(y)]
        if len(yd) < 30:
            continue
        yt = np.array([vixtwn[d] for d in yd])
        yu = np.array([vix[d] for d in yd])
        ic_y, p_y = sstats.spearmanr(yu, yt)
        marker = "***" if p_y < 0.001 else ("**" if p_y < 0.01 else ("*" if p_y < 0.05 else ""))
        print(f"  {y}: IC={ic_y:+.3f} (p={p_y:.3f}) n={len(yd)} {marker}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 領先/落後分析用『美股VIX前一日變動』對應『隔天VIXTWN變動』，這裡\n"
        "     的『前一日』是用日期字串比較找最近一筆，遇到假期/交易日曆不同步\n"
        "     時可能有1-2天誤差，不是精確的交易日對齊。\n"
        "  2) VIXTWN(computed)是mini自算，不是官方公佈的台指選擇權VIX，公式/\n"
        "     方法論跟美股CBOE VIX不完全對應，兩者『同水位』不代表同樣的\n"
        "     波動度定義基礎，相關性數字是統計對照，不是等價量表。\n"
        "  3) 真正的『即時/盤中』比較做不到——這是資料庫本身的缺口（VIXTWN 1分K\n"
        "     沒有夜盤資料、美股VIX完全沒有分鐘資料），要做到這一層需要額外\n"
        "     接美股VIX即時報價來源，不是這次研究範圍內能補的。"
    )


if __name__ == "__main__":
    main()
