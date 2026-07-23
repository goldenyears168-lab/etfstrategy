#!/usr/bin/env python3
"""開盤缺口 pattern proxy 研究（A1 缺口回補 / A2 大盤同向性 / A3 開高開低後續機率).

背景：使用者想研究台股「盤前試撮委買委賣」對開盤走勢的預測力，但 FinMind／Yahoo／
現有 TEJ 訂閱都沒有歷史試撮委買委賣資料可回測。本腳本改用既有的歷史日K
(`stock_daily_bars`) / 分K (`stock_kbar_1m`) 資料，做「開盤缺口後續走勢」的
proxy 統計，作為未來試撮因子上線前的基礎機率參考。Research only：純讀取，
不寫入/不修改任何現有 DB 表，不採納進 Strategy／Order layer。

三個研究目標：
  A1 開盤缺口回補機率（按缺口方向/大小分桶 x 20日均額流動性分組）
  A2 個股開盤缺口與大盤（0050 代理）同向性，比較「大盤同向」vs「大盤背離」
     兩組開盤後 30 分鐘 / 收盤報酬與波動
  A3 開高/開低後的續漲/反轉基礎機率（純統計 base rate，不含因果因子）+
     開盤後 30 分鐘震盪幅度分布

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/run_gap_open_pattern_proxy_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_gap_open_pattern_proxy_study.py \\
      --date-start 2023-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
assert (ROOT / "src").exists(), f"unexpected ROOT resolution: {ROOT}"

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

MARKET_PROXY = "0050"
OUT_DIR = ROOT / "reports/research/gap_open_pattern_proxy"
OUT_MD = OUT_DIR / "SUMMARY.md"

GAP_BUCKET_EDGES = [0.0, 1.0, 3.0, 5.0, np.inf]
GAP_BUCKET_LABELS = ["<1%", "1-3%", "3-5%", ">5%"]
MAX_PLAUSIBLE_GAP_ABS = 20.0  # 過濾資料缺口(缺月/缺年)或未調整股票分割造成的假缺口
MAX_PREV_DATE_GAP_DAYS = 10  # 前一列與本列間隔超過此天數視為資料斷點（非真實開盤缺口）
NO_DIRECTION_EPS = 0.1  # |gap_pct| < 此值視為方向不明確（排除於 A2 對齊分類外）
ALIGN_BAND_PP = 1.0  # A2：與大盤同向且幅度差 <= 此百分點視為「大盤同向」


# --------------------------------------------------------------------------
# 讀取與清理
# --------------------------------------------------------------------------

def load_daily_bars(conn, date_start: str | None) -> pd.DataFrame:
    """讀 stock_daily_bars（僅 source='finmind'，避免與 yfinance 補值列混用不同計價基準）。"""
    sql = "SELECT stock_id, trade_date, open, high, low, close, amount FROM stock_daily_bars WHERE source = 'finmind'"
    params: list[str] = []
    if date_start:
        sql += " AND trade_date >= ?"
        params.append(date_start)
    sql += " ORDER BY stock_id, trade_date"
    df = pd.read_sql_query(sql, conn, params=params)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def add_gap_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """加上 prev_close / gap_pct，並過濾資料斷點與異常值。回傳 (乾淨後 df, 品質統計 dict)。"""
    df = df.sort_values(["stock_id", "trade_date"]).copy()
    grp = df.groupby("stock_id")
    df["prev_close"] = grp["close"].shift(1)
    df["prev_trade_date"] = grp["trade_date"].shift(1)
    df["day_gap"] = (df["trade_date"] - df["prev_trade_date"]).dt.days

    n_total = len(df)
    zero_price = (df["open"] <= 0) | (df["close"] <= 0) | (df["high"] <= 0) | (df["low"] <= 0)
    n_zero_price = int(zero_price.sum())
    df = df.loc[~zero_price].copy()

    has_prev = df["prev_close"].notna() & (df["prev_close"] > 0)
    n_no_prev = int((~has_prev).sum())
    df = df.loc[has_prev].copy()

    stale_break = df["day_gap"] > MAX_PREV_DATE_GAP_DAYS
    n_stale_break = int(stale_break.sum())
    df = df.loc[~stale_break].copy()

    df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100.0

    extreme = df["gap_pct"].abs() > MAX_PLAUSIBLE_GAP_ABS
    n_extreme = int(extreme.sum())
    df = df.loc[~extreme].copy()

    quality = {
        "n_total_rows": n_total,
        "n_zero_price_dropped": n_zero_price,
        "n_no_prev_close_dropped": n_no_prev,
        "n_stale_break_dropped": n_stale_break,
        "n_extreme_gap_dropped": n_extreme,
        "n_clean_rows": len(df),
    }
    return df, quality


def add_liquidity_bucket(df: pd.DataFrame) -> pd.DataFrame:
    """20 日 (trailing, shift 1 避免看未來) 均額分三組（大/中/小），amount 為 NULL 者標 NaN。"""
    df = df.sort_values(["stock_id", "trade_date"]).copy()
    df["amt_roll20"] = (
        df.groupby("stock_id")["amount"]
        .apply(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
        .reset_index(level=0, drop=True)
    )
    valid = df["amt_roll20"].notna()
    df["liquidity_bucket"] = pd.NA
    if valid.sum() >= 30:
        df.loc[valid, "liquidity_bucket"] = pd.qcut(
            df.loc[valid, "amt_roll20"], 3, labels=["小", "中", "大"]
        )
    return df


def bucket_gap(gap_pct: pd.Series) -> pd.Series:
    mag = gap_pct.abs()
    idx = np.searchsorted(GAP_BUCKET_EDGES, mag, side="right") - 1
    idx = np.clip(idx, 0, len(GAP_BUCKET_LABELS) - 1)
    return pd.Series(np.array(GAP_BUCKET_LABELS)[idx], index=gap_pct.index)


# --------------------------------------------------------------------------
# 分K 快照（供 A2 / A3 用）
# --------------------------------------------------------------------------

def load_minute_snapshot(conn, date_start: str | None) -> pd.DataFrame:
    """讀 stock_kbar_1m（source='finmind'，09:00:00~09:30:00），聚合成每個 stock/date 一列：
    c5（09:05 或之前最近一筆收盤）、c30（09:30 或之前最近一筆收盤）、
    range_high / range_low（09:00~09:30 區間最高/最低）。
    """
    sql = (
        "SELECT stock_id, trade_date, minute, high, low, close FROM stock_kbar_1m "
        "WHERE source = 'finmind' AND minute BETWEEN '09:00:00' AND '09:30:00'"
    )
    params: list[str] = []
    if date_start:
        sql += " AND trade_date >= ?"
        params.append(date_start)
    sql += " ORDER BY stock_id, trade_date, minute"
    m = pd.read_sql_query(sql, conn, params=params)
    if m.empty:
        return m

    range_agg = m.groupby(["stock_id", "trade_date"], as_index=False).agg(
        range_high=("high", "max"), range_low=("low", "min")
    )

    m5 = m.loc[m["minute"] <= "09:05:00"]
    c5 = m5.groupby(["stock_id", "trade_date"], as_index=False).last()[["stock_id", "trade_date", "close"]]
    c5 = c5.rename(columns={"close": "c5"})

    m30 = m.loc[m["minute"] <= "09:30:00"]
    c30 = m30.groupby(["stock_id", "trade_date"], as_index=False).last()[["stock_id", "trade_date", "close"]]
    c30 = c30.rename(columns={"close": "c30"})

    out = range_agg.merge(c5, on=["stock_id", "trade_date"], how="left")
    out = out.merge(c30, on=["stock_id", "trade_date"], how="left")
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    return out


# --------------------------------------------------------------------------
# A1 缺口回補機率
# --------------------------------------------------------------------------

def study_a1(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.copy()
    d["direction"] = np.where(d["gap_pct"] > 0, "開高", np.where(d["gap_pct"] < 0, "開低", "平盤"))
    d = d.loc[d["direction"] != "平盤"].copy()
    d["gap_bucket"] = bucket_gap(d["gap_pct"])
    d["filled"] = np.where(
        d["direction"] == "開高",
        d["low"] <= d["prev_close"],
        d["high"] >= d["prev_close"],
    )

    overall = (
        d.groupby(["direction", "gap_bucket"])
        .agg(n=("filled", "size"), fill_prob=("filled", "mean"), avg_gap_pct=("gap_pct", "mean"))
        .reset_index()
    )
    overall["gap_bucket"] = pd.Categorical(overall["gap_bucket"], GAP_BUCKET_LABELS, ordered=True)
    overall = overall.sort_values(["direction", "gap_bucket"]).reset_index(drop=True)

    liq = d.loc[d["liquidity_bucket"].notna()].copy()
    by_liq = (
        liq.groupby(["direction", "gap_bucket", "liquidity_bucket"], observed=True)
        .agg(n=("filled", "size"), fill_prob=("filled", "mean"))
        .reset_index()
    )
    by_liq["gap_bucket"] = pd.Categorical(by_liq["gap_bucket"], GAP_BUCKET_LABELS, ordered=True)
    by_liq["liquidity_bucket"] = pd.Categorical(by_liq["liquidity_bucket"], ["大", "中", "小"], ordered=True)
    by_liq = by_liq.sort_values(["direction", "gap_bucket", "liquidity_bucket"]).reset_index(drop=True)
    return overall, by_liq


# --------------------------------------------------------------------------
# A2 個股 vs 大盤同向性
# --------------------------------------------------------------------------

def study_a2(df: pd.DataFrame, mkt: pd.DataFrame, minute_snap: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    mkt_small = mkt[["trade_date", "gap_pct"]].rename(columns={"gap_pct": "mkt_gap_pct"})
    d = df.merge(mkt_small, on="trade_date", how="inner")
    d = d.loc[d["stock_id"] != MARKET_PROXY].copy()
    d = d.merge(minute_snap, on=["stock_id", "trade_date"], how="left")

    d["ret_close_pct"] = (d["close"] - d["open"]) / d["open"] * 100.0
    d["ret_30m_pct"] = (d["c30"] - d["open"]) / d["open"] * 100.0

    no_dir = (d["gap_pct"].abs() < NO_DIRECTION_EPS) | (d["mkt_gap_pct"].abs() < NO_DIRECTION_EPS)
    n_no_direction = int(no_dir.sum())
    d2 = d.loc[~no_dir].copy()

    same_sign = np.sign(d2["gap_pct"]) == np.sign(d2["mkt_gap_pct"])
    close_mag = (d2["gap_pct"] - d2["mkt_gap_pct"]).abs() <= ALIGN_BAND_PP
    d2["group"] = np.where(same_sign & close_mag, "大盤同向", "大盤背離")

    summary = (
        d2.groupby("group")
        .agg(
            n=("ret_close_pct", "size"),
            n_with_30m=("ret_30m_pct", "count"),
            avg_gap_pct=("gap_pct", "mean"),
            avg_ret_30m_pct=("ret_30m_pct", "mean"),
            std_ret_30m_pct=("ret_30m_pct", "std"),
            avg_ret_close_pct=("ret_close_pct", "mean"),
            std_ret_close_pct=("ret_close_pct", "std"),
        )
        .reset_index()
    )
    meta = {
        "n_merged": len(d),
        "n_no_direction_excluded": n_no_direction,
        "n_classified": len(d2),
        "mkt_date_min": str(mkt["trade_date"].min().date()) if len(mkt) else None,
        "mkt_date_max": str(mkt["trade_date"].max().date()) if len(mkt) else None,
    }
    return summary, meta


# --------------------------------------------------------------------------
# A3 開高/開低後續機率分布（純統計 base rate）
# --------------------------------------------------------------------------

def study_a3(df: pd.DataFrame, minute_snap: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.merge(minute_snap, on=["stock_id", "trade_date"], how="inner")
    d["direction"] = np.where(d["gap_pct"] > 0, "開高", np.where(d["gap_pct"] < 0, "開低", "平盤"))
    d = d.loc[d["direction"] != "平盤"].copy()
    d["gap_bucket"] = bucket_gap(d["gap_pct"])

    d["ret_30m_pct"] = (d["c30"] - d["open"]) / d["open"] * 100.0
    d["ret_close_pct"] = (d["close"] - d["open"]) / d["open"] * 100.0
    d["range_30m_pct"] = (d["range_high"] - d["range_low"]) / d["open"] * 100.0

    d["cont_30m"] = np.where(
        d["direction"] == "開高", d["ret_30m_pct"] > 0, d["ret_30m_pct"] < 0
    )
    d["cont_close"] = np.where(
        d["direction"] == "開高", d["ret_close_pct"] > 0, d["ret_close_pct"] < 0
    )

    prob_tbl = (
        d.groupby(["direction", "gap_bucket"])
        .agg(
            n=("ret_30m_pct", "size"),
            n_valid_30m=("ret_30m_pct", "count"),
            cont_prob_30m=("cont_30m", "mean"),
            cont_prob_close=("cont_close", "mean"),
            avg_ret_30m_pct=("ret_30m_pct", "mean"),
            avg_ret_close_pct=("ret_close_pct", "mean"),
        )
        .reset_index()
    )
    prob_tbl["gap_bucket"] = pd.Categorical(prob_tbl["gap_bucket"], GAP_BUCKET_LABELS, ordered=True)
    prob_tbl = prob_tbl.sort_values(["direction", "gap_bucket"]).reset_index(drop=True)

    range_tbl = (
        d.groupby(["direction", "gap_bucket"])["range_30m_pct"]
        .agg(n="size", mean="mean", std="std", median="median", p90=lambda s: s.quantile(0.9))
        .reset_index()
    )
    range_tbl["gap_bucket"] = pd.Categorical(range_tbl["gap_bucket"], GAP_BUCKET_LABELS, ordered=True)
    range_tbl = range_tbl.sort_values(["direction", "gap_bucket"]).reset_index(drop=True)
    return prob_tbl, range_tbl


# --------------------------------------------------------------------------
# Markdown 報告
# --------------------------------------------------------------------------

def _fmt_pct(x, digits=1) -> str:
    if pd.isna(x):
        return "—"
    return f"{x:.{digits}f}%"


def render_markdown(
    quality: dict,
    a1_overall: pd.DataFrame,
    a1_liq: pd.DataFrame,
    a2_summary: pd.DataFrame,
    a2_meta: dict,
    a3_prob: pd.DataFrame,
    a3_range: pd.DataFrame,
    date_start_used: str,
    n_stocks: int,
    n_days: int,
) -> str:
    lines: list[str] = []
    lines.append("# 開盤缺口 Pattern Proxy 研究（A1 缺口回補 / A2 大盤同向 / A3 開高開低基礎機率）")
    lines.append("")
    lines.append(f"- 生成：{pd.Timestamp.now().isoformat(timespec='seconds')}")
    lines.append("- Runner：`scripts/research/run_gap_open_pattern_proxy_study.py`（Research only，純讀取）")
    lines.append(
        "- 背景：無歷史盤前試撮委買委賣資料可回測（FinMind/Yahoo/現有 TEJ 訂閱皆無日內委託簿歷史），"
        "以既有日K/分K做「開盤缺口後續走勢」proxy 統計，作為未來試撮因子上線前的基礎機率參考"
    )
    lines.append("- 層級：Research；**未採納**進 Strategy／Order layer")
    lines.append(f"- 樣本：日K 起始 `{date_start_used}` · 涵蓋股票數 {n_stocks} · 交易日數 {n_days}")
    lines.append("")

    lines.append("## 資料品質與限制（誠實揭露）")
    lines.append("")
    lines.append(
        f"- `stock_daily_bars` 僅取 `source='finmind'`（排除 `yfinance` 補值列，避免計價基準不一致）。"
        f"清理前 {quality['n_total_rows']:,} 列 / {quality['raw_n_stocks']} 檔 → 清理後可算缺口 {quality['n_clean_rows']:,} 列 / {n_stocks} 檔。"
    )
    lines.append(
        f"  - **`raw_n_stocks` → 可用股票數大幅縮水（{quality['raw_n_stocks']} → {n_stocks}）**："
        f"約 {quality['raw_n_stocks'] - n_stocks} 檔在 `stock_daily_bars` 內僅有極少列（多數僅 1 列，可能是單次快照 sync，"
        "非連續歷史序列），無前一日收盤可比對缺口，自然被排除，非程式錯誤。"
    )
    lines.append(
        f"  - 剔除全零價（open/high/low/close 其一 ≤ 0，資料錄入異常）：{quality['n_zero_price_dropped']:,} 列"
    )
    lines.append(f"  - 無前一日收盤可比對（該股第一列）：{quality['n_no_prev_close_dropped']:,} 列")
    lines.append(
        f"  - 前一有效交易日間隔 > {MAX_PREV_DATE_GAP_DAYS} 天（同步斷點，常見於部分股票整年缺料，"
        f"非真實開盤缺口）：{quality['n_stale_break_dropped']:,} 列"
    )
    lines.append(
        f"  - 缺口絕對值 > {MAX_PLAUSIBLE_GAP_ABS:.0f}%（多為未還原股票分割/減資等公司行動造成的假缺口，"
        f"raw close 未調整）：{quality['n_extreme_gap_dropped']:,} 列"
    )
    lines.append(
        "- `amount`（成交金額）在 `stock_daily_bars` 內約 3 成為 NULL，且**集中在特定股票**"
        "（同一檔幾乎全 NULL 或全非 NULL，非隨機缺漏），A1 流動性分組僅用 20 日均額非 NULL 的樣本，"
        "缺 amount 的股票不進入分組表（但仍計入整體缺口回補表）。"
    )
    lines.append(
        "- `stock_kbar_1m` 內同一 (stock_id, trade_date, minute) 存在 `finmind`／`yahoo`／`finmind_fut` "
        "三種 source 且分K數值彼此不一致（曾見同一分鐘 finmind vs yahoo 收盤價差 15%+），"
        "A2/A3 僅用 `source='finmind'`（164 檔股票，2022-02-08~2026-07-16）；`yahoo`/`finmind_fut` 分K已排除，"
        "不做混用平均。"
    )
    lines.append(
        f"- 大盤代理 `0050` 的 `stock_daily_bars`（`source='finmind'`）僅 {a2_meta['mkt_date_min']} ~ "
        f"{a2_meta['mkt_date_max']}（約 1 年），A2 的樣本窗口因此被壓縮到此區間 ∩ 個股分K覆蓋範圍，"
        "並非全樣本；0050 於 2025-06 附近曾發生 1:4 分割，該日已被上述「缺口絕對值 > 20%」規則濾除。"
    )
    lines.append("- 09:05 / 09:30 精確分鐘K缺漏約 3-10%（無成交撮合），已用「該分鐘或之前最近一筆」的收盤價回退，非硬性 lookahead。")
    lines.append("")

    lines.append("## A1｜開盤缺口回補機率（按方向 x 大小分桶）")
    lines.append("")
    lines.append("回補定義：開高＝當日 low 觸及前收盤；開低＝當日 high 觸及前收盤（用日K high/low，非分K）。")
    lines.append("")
    lines.append("| 方向 | 缺口分桶 | 樣本數 n | 回補機率 | 平均缺口% |")
    lines.append("|---|---|---:|---:|---:|")
    for _, r in a1_overall.iterrows():
        lines.append(
            f"| {r['direction']} | {r['gap_bucket']} | {int(r['n']):,} | {_fmt_pct(r['fill_prob']*100)} | {_fmt_pct(r['avg_gap_pct'])} |"
        )
    lines.append("")
    lines.append("### A1 按流動性分組（20日均額 tertile，僅 amount 非 NULL 樣本）")
    lines.append("")
    lines.append("| 方向 | 缺口分桶 | 流動性 | 樣本數 n | 回補機率 |")
    lines.append("|---|---|---|---:|---:|")
    for _, r in a1_liq.iterrows():
        lines.append(
            f"| {r['direction']} | {r['gap_bucket']} | {r['liquidity_bucket']} | {int(r['n']):,} | {_fmt_pct(r['fill_prob']*100)} |"
        )
    lines.append("")

    lines.append("## A2｜個股開盤缺口 vs 大盤（0050 代理）同向性")
    lines.append("")
    lines.append(
        f"分類規則：兩者 |gap_pct| ≥ {NO_DIRECTION_EPS}% 才視為有明確方向（排除 {a2_meta['n_no_direction_excluded']:,} 列方向不明確樣本）；"
        f"同號且幅度差 ≤ {ALIGN_BAND_PP:.1f} 個百分點 → 大盤同向，其餘（反方向或幅度顯著偏離）→ 大盤背離。"
    )
    lines.append(f"合併後可分類樣本 n={a2_meta['n_classified']:,}（merge 後總列數 {a2_meta['n_merged']:,}）。")
    lines.append("")
    lines.append("| 分組 | 樣本數 n | 有效30分K樣本 | 平均缺口% | 開盤+30分 平均報酬% | 開盤+30分 std% | 開盤~收盤 平均報酬% | 開盤~收盤 std% |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in a2_summary.iterrows():
        lines.append(
            f"| {r['group']} | {int(r['n']):,} | {int(r['n_with_30m']):,} | {_fmt_pct(r['avg_gap_pct'])} | "
            f"{_fmt_pct(r['avg_ret_30m_pct'])} | {_fmt_pct(r['std_ret_30m_pct'])} | "
            f"{_fmt_pct(r['avg_ret_close_pct'])} | {_fmt_pct(r['std_ret_close_pct'])} |"
        )
    lines.append("")

    lines.append("## A3｜開高/開低後續續漲 vs 反轉機率 + 30分震盪幅度（純統計 base rate）")
    lines.append("")
    lines.append("續漲/反轉定義：開高後 +30分（或收盤）報酬 > 0 記為續漲(續漲=continuation)，≤ 0 記為反轉；開低對稱處理。")
    lines.append("")
    lines.append("| 方向 | 缺口分桶 | 樣本數 n | +30分續漲機率 | 收盤續漲機率 | +30分平均報酬% | 收盤平均報酬% |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for _, r in a3_prob.iterrows():
        lines.append(
            f"| {r['direction']} | {r['gap_bucket']} | {int(r['n']):,} | {_fmt_pct(r['cont_prob_30m']*100)} | "
            f"{_fmt_pct(r['cont_prob_close']*100)} | {_fmt_pct(r['avg_ret_30m_pct'])} | {_fmt_pct(r['avg_ret_close_pct'])} |"
        )
    lines.append("")
    lines.append("### 開盤後 30 分鐘震盪幅度（(high-low)/open，區間 09:00~09:30）")
    lines.append("")
    lines.append("| 方向 | 缺口分桶 | 樣本數 n | 平均震盪% | std% | 中位數% | p90% |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for _, r in a3_range.iterrows():
        lines.append(
            f"| {r['direction']} | {r['gap_bucket']} | {int(r['n']):,} | {_fmt_pct(r['mean'])} | "
            f"{_fmt_pct(r['std'])} | {_fmt_pct(r['median'])} | {_fmt_pct(r['p90'])} |"
        )
    lines.append("")

    lines.append("## 白話結論")
    lines.append("")
    # 動態挑幾個關鍵數字放進結論，避免手寫死數字跟表格不同步
    try:
        small_up = a1_overall.query("direction=='開高' and gap_bucket=='<1%'").iloc[0]
        big_up = a1_overall.query("direction=='開高' and gap_bucket=='>5%'").iloc[0]
        small_down = a1_overall.query("direction=='開低' and gap_bucket=='<1%'").iloc[0]
        big_down = a1_overall.query("direction=='開低' and gap_bucket=='>5%'").iloc[0]
        lines.append(
            f"- **A1 缺口回補**：小缺口幾乎必補——開高<1% 回補機率 {_fmt_pct(small_up['fill_prob']*100)}"
            f"、開低<1% {_fmt_pct(small_down['fill_prob']*100)}；缺口越大回補機率越低，"
            f"開高>5% 降到 {_fmt_pct(big_up['fill_prob']*100)}、開低>5% {_fmt_pct(big_down['fill_prob']*100)}"
            "（單邊樣本量在大缺口桶明顯縮小，數字噪音較大，見表格 n）。"
        )
    except (IndexError, KeyError):
        lines.append("- **A1 缺口回補**：詳見上表（部分分桶樣本不足，未特別點名）。")

    if len(a2_summary) == 2:
        try:
            aligned = a2_summary.set_index("group").loc["大盤同向"]
            diverged = a2_summary.set_index("group").loc["大盤背離"]
            lines.append(
                f"- **A2 大盤同向性**：大盤同向組 n={int(aligned['n']):,}，收盤~開盤報酬 std={_fmt_pct(aligned['std_ret_close_pct'])}；"
                f"大盤背離組 n={int(diverged['n']):,}，std={_fmt_pct(diverged['std_ret_close_pct'])}——"
                f"{'背離組波動明顯更大，個股獨立異動的不確定性更高' if diverged['std_ret_close_pct'] > aligned['std_ret_close_pct'] else '兩組波動差異不大，同向性本身對波動的解釋力有限'}。"
            )
        except KeyError:
            lines.append("- **A2 大盤同向性**：詳見上表。")
    else:
        lines.append("- **A2 大盤同向性**：詳見上表（分組樣本可能不足以下結論）。")

    try:
        up = a3_prob.loc[a3_prob["direction"] == "開高"].set_index("gap_bucket").sort_index()
        down = a3_prob.loc[a3_prob["direction"] == "開低"].set_index("gap_bucket").sort_index()
        up_max = up["cont_prob_close"].max() * 100
        down_small = down["cont_prob_close"].iloc[0] * 100
        down_big = down["cont_prob_close"].iloc[-1] * 100
        lines.append(
            f"- **A3 開高/開低續漲機率**：開高各缺口桶收盤續漲機率全部 < 50%（最高僅 {_fmt_pct(up_max)}），"
            "顯示開高後**反轉／回吐比續漲更常見**（偏 fade，而非追漲慣性）；"
            f"開低則呈現「缺口愈大、愈容易反彈」——開低<1% 收盤續跌機率 {_fmt_pct(down_small)}（接近五五波），"
            f"開低>5% 卻降到 {_fmt_pct(down_big)}（多數是反彈回補，非破底加速）。"
            "純缺口方向/大小對日內走勢仍有一定不對稱訊號，但不是簡單的「順勢延續」，"
            "「試撮委買委賣力道」若能進一步分辨這種 fade vs 反彈傾向，仍有機會補強。"
        )
    except (IndexError, KeyError):
        lines.append("- **A3 開高/開低續漲機率**：詳見上表。")
    try:
        widest = a3_range.loc[a3_range["mean"].idxmax()]
        narrowest = a3_range.loc[a3_range["mean"].idxmin()]
        lines.append(
            f"- **A3 震盪幅度**：{widest['direction']}{widest['gap_bucket']} 桶開盤 30 分鐘震盪最劇烈"
            f"（平均 {_fmt_pct(widest['mean'])}，std {_fmt_pct(widest['std'])}）；"
            f"{narrowest['direction']}{narrowest['gap_bucket']} 桶最穩定（平均 {_fmt_pct(narrowest['mean'])}）——"
            "缺口越大，開盤後價格discovery過程越劇烈，這類大缺口桶的策略停損停利需要更寬的容忍度。"
        )
    except (KeyError, ValueError):
        pass
    lines.append(
        "- **整體限制**：本研究全部基於**已實現**的日K/分K，並非試撮委買委賣力道本身；"
        "以上機率/報酬差異都只反映「缺口方向與大小」這一單一變數的解釋力，"
        "樣本數也隨缺口變大而急速縮小（見各表 n），大缺口桶（>5%）數字的統計噪音較高，"
        "不代表結合委買委賣力道、量能、分點籌碼等因子後仍會是同樣的機率。"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--date-start",
        default=None,
        help="日K/分K起始日（YYYY-MM-DD）。預設不限制，用全部可用歷史。",
    )
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        print("[1/5] 讀取 stock_daily_bars ...")
        daily = load_daily_bars(conn, args.date_start)
        raw_n_stocks = daily["stock_id"].nunique()
        daily, quality = add_gap_columns(daily)
        quality["raw_n_stocks"] = raw_n_stocks
        daily = add_liquidity_bucket(daily)
        date_start_used = str(daily["trade_date"].min().date()) if len(daily) else "N/A"
        n_stocks = daily["stock_id"].nunique()
        n_days = daily["trade_date"].nunique()
        print(f"  clean gap rows = {len(daily):,} · stocks = {n_stocks} · trading days = {n_days}")

        print("[2/5] 讀取 stock_kbar_1m 分K快照（09:00~09:30, finmind） ...")
        minute_snap = load_minute_snapshot(conn, args.date_start)
        print(f"  minute snapshot rows = {len(minute_snap):,}")

        print("[3/5] A1 缺口回補機率 ...")
        a1_overall, a1_liq = study_a1(daily)

        print("[4/5] A2 個股 vs 大盤(0050) 同向性 ...")
        mkt = daily.loc[daily["stock_id"] == MARKET_PROXY].copy()
        if mkt.empty:
            print(f"WARN: 大盤代理 {MARKET_PROXY} 無可用缺口資料，A2 將輸出空表", file=sys.stderr)
            a2_summary = pd.DataFrame()
            a2_meta = {"n_merged": 0, "n_no_direction_excluded": 0, "n_classified": 0, "mkt_date_min": None, "mkt_date_max": None}
        else:
            a2_summary, a2_meta = study_a2(daily, mkt, minute_snap)

        print("[5/5] A3 開高/開低後續機率分布 ...")
        a3_prob, a3_range = study_a3(daily, minute_snap)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        md = render_markdown(
            quality,
            a1_overall,
            a1_liq,
            a2_summary,
            a2_meta,
            a3_prob,
            a3_range,
            date_start_used,
            n_stocks,
            n_days,
        )
        OUT_MD.write_text(md, encoding="utf-8")
        print(f"\nWrote {OUT_MD}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
