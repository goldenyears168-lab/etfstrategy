#!/usr/bin/env python3
"""Beta-bucketed market-wide precursor study — 高 beta（高槓桿）股票買賣超 precursor 研究.

Research only（探索 topic，尚未登錄 `config/research.yaml`）。延伸既有分點
precursor 研究線（`run_market_crash_branch_precursor_scan.py` /
`run_market_rally_branch_precursor_scan.py` / `rally_buying_breadth_dashboard.md`）
的問題：四路獨立驗證都顯示「哪個分點」對大漲方向是雜訊（廣泛參與，非少數
分點領先），但「市場整體買超廣度」在 walk-forward 驗證下仍有中等判別力。

本研究把維度從「誰在買」換成「買的是什麼樣的股票」：是否「淨買賣超集中
流向高 beta（高槓桿特性）股票」比「哪個分點」或「單純市場整體買超廣度」
更能領先大漲／大跌？

方法（凍結協議）：
  1. Beta 計算（不用資料庫現成欄位，自行計算）：對 297 分點 cohort 實際
     交易過的每一檔股票，用 `daily_bars`（code=IX0001）與 `stock_daily_bars`
     （source=finmind）的日收盤價，算 trailing 60 個交易日 rolling beta
     （cov(stock_ret, mkt_ret)/var(mkt_ret)），視窗嚴格早於當日（no
     lookahead：t 日的 beta 只用到 t-1 日為止的報酬）。歷史 <60 個交易日
     的股票／日期，beta 留 NaN。
  2. 每個交易日，把當日「cohort 全體分點 對每一檔股票」的 net_amt（股數
     ×收盤價）加總成 (stock_id, trade_date) 層級，再用「當日最近可得的
     beta 值」（beta 序列 forward-fill）把股票分成 beta 四分位
     （Q4=最高 beta，Q1=最低），加總每個分位當日的 net_amt，得到
     `net_amt_beta_q1..q4` 四條日序列。另計算一個「beta 傾斜分數
     （beta tilt score）」= net_amt_beta_q4 / (|net_amt_beta_q4| +
     |net_amt_beta_q1|)：+1 = 當日淨買超全部集中在最高 beta 股票、
     -1 = 全部集中在最低 beta 股票的賣超、0 = 兩端規模相當或雙雙為 0。
  3. 對 `net_amt_beta_q4`（高 beta 買超金額）與 `beta_tilt` 兩條聚合序列，
     各自計算「前 5 個交易日累計」，再對序列自身歷史（就是本研究的整個
     視窗，~2 年）取百分位（self-normalized，與既有 `compute_lookback_
     percentiles` 同一手法，只是套用在單一聚合序列而非 297 個分點）。
  4. 對每個大漲事件，命中 = 該聚合序列的「前5日百分位」≥ 1-tail_pct
     （高 beta 買超異常重）。對每個大跌事件，命中 = 百分位 ≤ tail_pct
     （高 beta 賣超異常重，或 tilt 分數異常偏低）。命中率 vs. 基期
     tail_pct 做單尾二項檢定（n=1 series，非 297 分點排行）。
  5.（optional）用 `stock_margin_daily.margin_balance`（當日融資餘額，
     同日已知事實，非需 no-lookahead 的統計量）做另一種「槓桿」定義，
     複製同一套分位＋聚合＋自我常態化＋命中率流程，跟 beta 版本對照。

與既有分點腳本共用（direction-agnostic）的建構函式直接從
`run_market_crash_branch_precursor_scan.py` import，避免重複維護：
connect_ro / build_calendar / load_cohort / month_starts。cohort 宇宙、
分點×個股 net_amt 的 close 價格 join 邏輯與既有 pipeline 完全一致，只是
本腳本把 groupby 維度從「分點」換成「個股」。

輸出（`reports/research/branch-footprint-screen/`）：
  beta_group_precursor_daily_series.csv
  beta_group_precursor_summary.md

用法：
  .venv/bin/python scripts/research/run_market_precursor_beta_group_study.py
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "run_market_crash_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_crash_branch_precursor_scan.py",
)
_c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c)  # type: ignore[union-attr]

connect_ro = _c.connect_ro
build_calendar = _c.build_calendar
load_cohort = _c.load_cohort
month_starts = _c.month_starts

OUT = ROOT / "reports/research/branch-footprint-screen"
DB_PATH = ROOT / "data" / "replica" / "stocks.db"
SOURCE = "finmind"
BENCHMARK = "IX0001"
STOCK_ID_FILTER = "length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'"


# ---------------------------------------------------------------------------
# 1. Stock-day net_amt panel (cohort branches aggregated across ALL branches,
#    per stock instead of per branch — same universe/join logic as
#    build_branch_day_panel, different groupby dimension).
# ---------------------------------------------------------------------------
def build_stock_day_panel(
    conn, ids: list[str], *, start: str, end: str
) -> pd.DataFrame:
    """Month-chunked (stock × date) aggregate net_amt (NT$) across cohort branches."""
    id_set = set(ids)
    months = month_starts(start, end)
    chunks: list[pd.DataFrame] = []
    for mi, (a, b) in enumerate(months, 1):
        t0 = time.time()
        raw = pd.read_sql_query(
            f"""
            SELECT securities_trader_id, trade_date, stock_id, buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND trade_date >= ? AND trade_date <= ?
              AND stock_id != '__EMPTY__' AND {STOCK_ID_FILTER}
            """,
            conn,
            params=[SOURCE, a, b],
        )
        if raw.empty:
            print(f"  [{mi}/{len(months)}] {a}..{b} empty", flush=True)
            continue
        raw["securities_trader_id"] = raw["securities_trader_id"].astype(str)
        raw = raw[raw["securities_trader_id"].isin(id_set)]
        if raw.empty:
            print(f"  [{mi}/{len(months)}] {a}..{b} no cohort rows", flush=True)
            continue
        closes = pd.read_sql_query(
            f"""
            SELECT stock_id, trade_date, close
            FROM stock_daily_bars
            WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
              AND {STOCK_ID_FILTER}
            """,
            conn,
            params=[SOURCE, a, b],
        )
        raw["stock_id"] = raw["stock_id"].astype(str)
        raw["trade_date"] = raw["trade_date"].astype(str)
        closes["stock_id"] = closes["stock_id"].astype(str)
        closes["trade_date"] = closes["trade_date"].astype(str)
        merged = raw.merge(closes, on=["stock_id", "trade_date"], how="inner")
        merged["net_amt"] = merged["net"].astype(float) * merged["close"].astype(float)
        grp = merged.groupby(["stock_id", "trade_date"], as_index=False).agg(
            net_amt=("net_amt", "sum"),
            n_branches=("securities_trader_id", "nunique"),
        )
        chunks.append(grp)
        print(
            f"  [{mi}/{len(months)}] {a}..{b} raw={len(raw):,} rows "
            f"({time.time()-t0:.1f}s)",
            flush=True,
        )
    if not chunks:
        return pd.DataFrame(columns=["stock_id", "trade_date", "net_amt", "n_branches"])
    return pd.concat(chunks, ignore_index=True)


# ---------------------------------------------------------------------------
# 2a. Rolling stock beta (no lookahead) — beta computation.
# ---------------------------------------------------------------------------
def load_price_long(
    conn, stock_ids: list[str], *, start: str, end: str
) -> pd.DataFrame:
    """Daily close prices for the given stock universe, source=finmind."""
    ids_placeholder = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
          AND {STOCK_ID_FILTER} AND stock_id IN ({ids_placeholder})
        """,
        conn,
        params=[SOURCE, start, end, *stock_ids],
    )
    df["stock_id"] = df["stock_id"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def compute_rolling_beta(
    price_long: pd.DataFrame, mkt_ret: pd.Series, *, window: int
) -> pd.DataFrame:
    """beta[t] = cov(stock_ret, mkt_ret) / var(mkt_ret) over the `window` trading
    days STRICTLY BEFORE t (shift-then-roll: no lookahead). NaN if <window obs."""
    wide = price_long.pivot(index="trade_date", columns="stock_id", values="close")
    wide = wide.sort_index()
    ret = wide.pct_change(fill_method=None)
    mkt = mkt_ret.reindex(wide.index)

    ret_shift = ret.shift(1)
    mkt_shift = mkt.shift(1)
    cov = ret_shift.rolling(window).cov(mkt_shift)
    var = mkt_shift.rolling(window).var()
    beta = cov.div(var, axis=0)
    beta = beta.where(var.replace(0.0, np.nan).notna(), np.nan)

    beta_long = beta.reset_index().melt(
        id_vars="trade_date", var_name="stock_id", value_name="beta"
    )
    return beta_long.dropna(subset=["beta"])


def ffill_beta_asof(beta_long: pd.DataFrame, cal: list[str]) -> pd.DataFrame:
    """Forward-fill each stock's beta across the trading calendar so every day
    uses the most-recently-available beta value (per task spec, not the exact
    same-day value if that day itself is a price gap/halt)."""
    wide = beta_long.pivot(index="trade_date", columns="stock_id", values="beta")
    wide = wide.reindex(cal).ffill()
    long = wide.reset_index().melt(
        id_vars="trade_date", var_name="stock_id", value_name="beta_asof"
    )
    return long.dropna(subset=["beta_asof"])


# ---------------------------------------------------------------------------
# 2b. Beta-bucket construction — split stocks into beta quartiles each day,
#     sum net_amt within each quartile across all cohort branches.
# ---------------------------------------------------------------------------
def build_beta_quartile_daily_series(
    stock_day_panel: pd.DataFrame,
    beta_asof_long: pd.DataFrame,
    cal: list[str],
    *,
    min_stocks_per_day: int = 8,
) -> pd.DataFrame:
    merged = stock_day_panel.merge(beta_asof_long, on=["stock_id", "trade_date"], how="inner")
    rows = []
    for d, g in merged.groupby("trade_date"):
        if len(g) < min_stocks_per_day:
            continue
        try:
            q = pd.qcut(g["beta_asof"], 4, labels=["q1", "q2", "q3", "q4"], duplicates="drop")
        except ValueError:
            continue
        if q.cat.categories.size < 4:
            continue
        g = g.assign(beta_q=q)
        sums = g.groupby("beta_q", observed=False)["net_amt"].sum()
        counts = g.groupby("beta_q", observed=False)["stock_id"].nunique()
        row = {"trade_date": d}
        for ql in ["q1", "q2", "q3", "q4"]:
            row[f"net_amt_beta_{ql}"] = float(sums.get(ql, 0.0))
            row[f"n_stocks_beta_{ql}"] = int(counts.get(ql, 0))
        rows.append(row)
    daily = pd.DataFrame(rows)

    full = pd.DataFrame({"trade_date": cal})
    daily = full.merge(daily, on="trade_date", how="left")
    for ql in ["q1", "q2", "q3", "q4"]:
        daily[f"net_amt_beta_{ql}"] = daily[f"net_amt_beta_{ql}"].fillna(0.0)
        daily[f"n_stocks_beta_{ql}"] = daily[f"n_stocks_beta_{ql}"].fillna(0).astype(int)

    denom = daily["net_amt_beta_q4"].abs() + daily["net_amt_beta_q1"].abs()
    daily["beta_tilt"] = np.where(
        denom > 0, daily["net_amt_beta_q4"] / denom.replace(0.0, np.nan), np.nan
    )
    return daily.sort_values("trade_date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2c (optional). Margin-balance-based leverage bucket, same recipe.
# ---------------------------------------------------------------------------
def load_margin_long(conn, *, start: str, end: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, margin_balance
        FROM stock_margin_daily
        WHERE source=? AND trade_date >= ? AND trade_date <= ?
          AND margin_balance IS NOT NULL
        """,
        conn,
        params=[SOURCE, start, end],
    )
    df["stock_id"] = df["stock_id"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def build_margin_quartile_daily_series(
    stock_day_panel: pd.DataFrame,
    margin_long: pd.DataFrame,
    cal: list[str],
    *,
    min_stocks_per_day: int = 8,
) -> pd.DataFrame:
    """Same-day margin_balance is an already-known EOD fact (not a forward-looking
    statistic like beta), so no as-of/ffill step is needed — just an exact-date join."""
    merged = stock_day_panel.merge(margin_long, on=["stock_id", "trade_date"], how="inner")
    rows = []
    for d, g in merged.groupby("trade_date"):
        if len(g) < min_stocks_per_day:
            continue
        try:
            q = pd.qcut(g["margin_balance"], 4, labels=["q1", "q2", "q3", "q4"], duplicates="drop")
        except ValueError:
            continue
        if q.cat.categories.size < 4:
            continue
        g = g.assign(margin_q=q)
        sums = g.groupby("margin_q", observed=False)["net_amt"].sum()
        counts = g.groupby("margin_q", observed=False)["stock_id"].nunique()
        row = {"trade_date": d}
        for ql in ["q1", "q2", "q3", "q4"]:
            row[f"net_amt_margin_{ql}"] = float(sums.get(ql, 0.0))
            row[f"n_stocks_margin_{ql}"] = int(counts.get(ql, 0))
        rows.append(row)
    daily = pd.DataFrame(rows)

    full = pd.DataFrame({"trade_date": cal})
    daily = full.merge(daily, on="trade_date", how="left")
    for ql in ["q1", "q2", "q3", "q4"]:
        daily[f"net_amt_margin_{ql}"] = daily[f"net_amt_margin_{ql}"].fillna(0.0)
        daily[f"n_stocks_margin_{ql}"] = daily[f"n_stocks_margin_{ql}"].fillna(0).astype(int)

    denom = daily["net_amt_margin_q4"].abs() + daily["net_amt_margin_q1"].abs()
    daily["margin_tilt"] = np.where(
        denom > 0, daily["net_amt_margin_q4"] / denom.replace(0.0, np.nan), np.nan
    )
    return daily.sort_values("trade_date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2d (follow-up). Margin FLOW（融資流量／新增槓桿）bucket — user feedback: the
# level-based margin_balance bucket above answers "who currently carries the
# most leverage", not "who is freshly BUILDING UP leverage right now"（誰開始
# 借錢開槓桿）. This follow-up buckets by margin_change (day-over-day new
# margin borrowing) instead. We normalize margin_change by the PRIOR day's
# margin_balance（margin_change_ratio = margin_change / margin_balance.shift(1)）
# rather than using the raw level: raw margin_change has huge cross-sectional
# scale variance (std≈1,518 vs. median≈-3 in this window, dominated by a few
# large-float names), so ranking on it would mostly just re-rank by stock size,
# not by buildup intensity. The normalized ratio is well-behaved empirically
# (median≈-0.001, IQR≈[-0.018, +0.016], only ~0.08% of rows |ratio|>1) and
# isolates the growth-rate signal the user's hypothesis is actually about.
# ---------------------------------------------------------------------------
def load_margin_change_long(
    conn, *, start: str, end: str, buffer_days: int = 10
) -> pd.DataFrame:
    """margin_change + size-normalized margin_change_ratio (vs. prior-day balance).
    `buffer_days` pulls a short extra lookback so the first in-window date still
    has a valid prior-day balance for the shift(1)."""
    ext_start = (pd.Timestamp(start) - pd.Timedelta(days=buffer_days)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, margin_balance, margin_change
        FROM stock_margin_daily
        WHERE source=? AND trade_date >= ? AND trade_date <= ?
          AND margin_balance IS NOT NULL AND margin_change IS NOT NULL
        """,
        conn,
        params=[SOURCE, ext_start, end],
    )
    df["stock_id"] = df["stock_id"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["stock_id", "trade_date"])
    prev_balance = df.groupby("stock_id")["margin_balance"].shift(1)
    df["margin_change_ratio"] = df["margin_change"] / prev_balance.replace(0.0, np.nan)
    return df[df["trade_date"] >= start].reset_index(drop=True)


def build_margin_change_quartile_daily_series(
    stock_day_panel: pd.DataFrame,
    margin_change_long: pd.DataFrame,
    cal: list[str],
    *,
    min_stocks_per_day: int = 8,
) -> pd.DataFrame:
    """Same recipe as build_margin_quartile_daily_series, but bucket criterion is
    margin_change_ratio (flow) instead of margin_balance (level): q4 = fastest
    fresh leverage buildup that day, q1 = fastest deleveraging."""
    merged = stock_day_panel.merge(
        margin_change_long[["stock_id", "trade_date", "margin_change_ratio"]],
        on=["stock_id", "trade_date"],
        how="inner",
    ).dropna(subset=["margin_change_ratio"])
    rows = []
    for d, g in merged.groupby("trade_date"):
        if len(g) < min_stocks_per_day:
            continue
        try:
            q = pd.qcut(
                g["margin_change_ratio"], 4, labels=["q1", "q2", "q3", "q4"], duplicates="drop"
            )
        except ValueError:
            continue
        if q.cat.categories.size < 4:
            continue
        g = g.assign(mf_q=q)
        sums = g.groupby("mf_q", observed=False)["net_amt"].sum()
        counts = g.groupby("mf_q", observed=False)["stock_id"].nunique()
        row = {"trade_date": d}
        for ql in ["q1", "q2", "q3", "q4"]:
            row[f"net_amt_marginflow_{ql}"] = float(sums.get(ql, 0.0))
            row[f"n_stocks_marginflow_{ql}"] = int(counts.get(ql, 0))
        rows.append(row)
    daily = pd.DataFrame(rows)

    full = pd.DataFrame({"trade_date": cal})
    daily = full.merge(daily, on="trade_date", how="left")
    for ql in ["q1", "q2", "q3", "q4"]:
        daily[f"net_amt_marginflow_{ql}"] = daily[f"net_amt_marginflow_{ql}"].fillna(0.0)
        daily[f"n_stocks_marginflow_{ql}"] = daily[f"n_stocks_marginflow_{ql}"].fillna(0).astype(int)

    denom = daily["net_amt_marginflow_q4"].abs() + daily["net_amt_marginflow_q1"].abs()
    daily["marginflow_tilt"] = np.where(
        denom > 0, daily["net_amt_marginflow_q4"] / denom.replace(0.0, np.nan), np.nan
    )
    return daily.sort_values("trade_date").reset_index(drop=True)


def build_aggregate_margin_change_series(
    margin_change_long: pd.DataFrame, cal: list[str]
) -> pd.DataFrame:
    """Standalone market-wide leverage-buildup series — no cohort/branch dependency
    at all (most literal reading of "is broad fresh-leverage buildup itself
    predictive, independent of who's trading it"):
      - agg_margin_change_sum: raw sum of margin_change across the whole
        margin-covered universe each day (same units as margin_balance).
      - agg_margin_change_breadth_pct: % of stocks (among those with margin data
        that day) whose margin_change_ratio is in the TOP DECILE of that stock's
        OWN full-window history — a "breadth of fresh margin buildup" index,
        same self-normalized-then-aggregate idea as the existing buying-breadth
        index, applied to margin flow instead of branch buying.
    """
    wide_ratio = margin_change_long.pivot(
        index="trade_date", columns="stock_id", values="margin_change_ratio"
    )
    pctile_per_stock = wide_ratio.rank(pct=True, na_option="keep")
    is_top_decile = pctile_per_stock >= 0.90
    valid = wide_ratio.notna()
    n_valid = valid.sum(axis=1)
    breadth = (is_top_decile.sum(axis=1) / n_valid.replace(0, np.nan)) * 100.0

    sum_raw = margin_change_long.groupby("trade_date")["margin_change"].sum()

    daily = pd.DataFrame(
        {"trade_date": sum_raw.index, "agg_margin_change_sum": sum_raw.to_numpy()}
    ).merge(
        breadth.rename("agg_margin_change_breadth_pct").reset_index(), on="trade_date", how="left"
    )
    full = pd.DataFrame({"trade_date": cal})
    daily = full.merge(daily, on="trade_date", how="left")
    daily["agg_margin_change_sum"] = daily["agg_margin_change_sum"].fillna(0.0)
    return daily.sort_values("trade_date").reset_index(drop=True)


def append_margin_flow_section_md(
    path: Path,
    *,
    protocol: dict,
    coverage: dict,
    flow_scores: list[dict],
    verdict_lines: list[str],
) -> None:
    """Appends a new section to the EXISTING summary md (does not touch/overwrite
    any prior content in the file — opened in append mode)."""
    lines = [
        "",
        "---",
        "",
        "## Follow-up：融資流量（Margin flow，`margin_change`）vs. 融資餘額（Margin level，`margin_balance`）",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。"
        "使用者回饋：上面「對照基準」用的 margin-based 分位是用融資餘額**水位**"
        "（margin_balance level，「誰現在扛最多槓桿」），不是使用者真正的假設"
        "「誰開始借錢開槓桿（margin_change flow，新增借貸的動能）」。本節用完全"
        "相同的方法論（分位建構→beta_tilt 式傾斜分數→前5日累計→自我百分位→"
        "命中率＋單尾二項檢定），只把分位依據換成 `margin_change`（流量），"
        "並額外測試一個不需要分點資料的最直白版本：市場整體融資流量本身。",
        "",
        "### 方法：為什麼用 `margin_change_ratio`（正規化）而非原始 `margin_change`",
        "",
        f"- 原始 `margin_change` 跨股票規模差異極大（本視窗 std≈1,518、中位數≈-3，"
        "少數大額股主導排序），直接排序等於主要在按「股票規模」排名，不是「新增槓桿"
        "強度」。",
        "- 正規化版本 `margin_change_ratio = margin_change / margin_balance.shift(1)`"
        "（相對前一交易日融資餘額的變動率）分佈穩定（中位數≈-0.001，IQR≈"
        "[-0.018, +0.016]，僅約 0.08% 的（股票,日）組合 |ratio|>1），且直接對應"
        "「誰在快速新增／減少槓桿」這個假設，故採用 `margin_change_ratio` 作為"
        "分位依據。",
        "",
        "### 結果 1：cohort 分點 net_amt 按 margin_change_ratio 分位（q4=新增槓桿最快）",
        "",
        "| 訊號 | 方向 | hit/事件 | 命中率% | 基期% | lift | p值(單尾) |",
        "|------|------|---------:|--------:|------:|-----:|----------:|",
    ]
    for s in flow_scores[:4]:
        lines.append(
            f"| {s['signal']} | {s['direction']} | {s['hit_count']}/{s['n_episodes_scored']} "
            f"(共{s['n_episodes_total']}) | {s['hit_rate_pct']:.1f} | {s['base_rate_pct']:.1f} | "
            f"{s['lift']:.2f} | {s['p_value_one_sided']:.4f} |"
        )
    lines += [
        "",
        "### 結果 2：市場整體融資流量本身（不依賴分點/cohort 資料的最直白版本）",
        "",
        f"- `agg_margin_change_sum`：每日融資覆蓋股票（n={coverage.get('n_margin_stocks_flow', 'NA')}）"
        "`margin_change` 加總。",
        "- `agg_margin_change_breadth_pct`：每日「該股 `margin_change_ratio` 落在自己整個視窗"
        "歷史 top decile」的股票比例（自我常態化後聚合，跟既有買超廣度指數同一手法，"
        "只是套用在融資流量而非分點買超）。",
        "",
        "| 訊號 | 方向 | hit/事件 | 命中率% | 基期% | lift | p值(單尾) |",
        "|------|------|---------:|--------:|------:|-----:|----------:|",
    ]
    for s in flow_scores[4:]:
        lines.append(
            f"| {s['signal']} | {s['direction']} | {s['hit_count']}/{s['n_episodes_scored']} "
            f"(共{s['n_episodes_total']}) | {s['hit_rate_pct']:.1f} | {s['base_rate_pct']:.1f} | "
            f"{s['lift']:.2f} | {s['p_value_one_sided']:.4f} |"
        )
    lines += [
        "",
        "### 結論（flow vs. level）",
        "",
    ]
    lines += verdict_lines
    lines += [
        "",
        "### 限制（本節新增，補充原限制清單）",
        "",
        "- `margin_change_ratio` 用 `margin_balance.shift(1)` 正規化，前一天融資餘額=0"
        "（極少數新上市／全額還款次日）時該筆比率設為 NaN，不參與分位建構。",
        "- `agg_margin_change_breadth_pct` 的「自己整個視窗歷史 top decile」用全樣本"
        "（含未來日）排百分位，跟既有 `compute_lookback_percentiles` 手法一致，是"
        "回溯性研究的既定做法，非即時可用的信號定義；且需要每檔股票在融資覆蓋視窗內"
        "有足夠天數才具統計意義，覆蓋率限制同前述 margin level 版本。",
        "- 樣本事件數（大跌11／大漲9）未變，本節新增訊號同樣是探索性、非嚴謹"
        "walk-forward 驗證。",
        "",
    ]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 3. Self-normalization (same style as compute_lookback_percentiles, applied
#    to a single aggregate series instead of a per-branch groupby).
# ---------------------------------------------------------------------------
def compute_lookback_percentiles_series(
    daily_df: pd.DataFrame, cols: list[str], *, lookback_days: int
) -> pd.DataFrame:
    out = daily_df.sort_values("trade_date").copy()
    for col in cols:
        lb_sum = out[col].shift(1).rolling(lookback_days).sum()
        out[f"{col}_lb_sum"] = lb_sum
        out[f"{col}_lb_pctile"] = lb_sum.rank(pct=True, na_option="keep")
    return out


# ---------------------------------------------------------------------------
# Scoring — cross-episode hit-rate + one-sided binomial test, n=1 series.
# ---------------------------------------------------------------------------
def score_series_vs_episodes(
    daily_df: pd.DataFrame,
    episodes: pd.DataFrame,
    pctile_col: str,
    *,
    tail_pct: float,
    direction: str,
    signal_label: str,
) -> dict:
    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    n_total = len(ep_dates)
    sub = daily_df[daily_df["trade_date"].isin(ep_dates)].dropna(subset=[pctile_col])
    n_scored = int(len(sub))
    if direction == "crash":
        hit_mask = sub[pctile_col] <= tail_pct
    elif direction == "rally":
        hit_mask = sub[pctile_col] >= (1.0 - tail_pct)
    else:
        raise ValueError(f"unknown direction {direction!r}")
    hit_count = int(hit_mask.sum())
    hit_rate = hit_count / n_scored if n_scored else 0.0
    p_value = (
        stats.binomtest(hit_count, n_scored, tail_pct, alternative="greater").pvalue
        if n_scored
        else float("nan")
    )
    return {
        "signal": signal_label,
        "direction": direction,
        "n_episodes_scored": n_scored,
        "n_episodes_total": n_total,
        "hit_count": hit_count,
        "hit_rate_pct": round(hit_rate * 100.0, 1),
        "base_rate_pct": round(tail_pct * 100.0, 1),
        "lift": round(hit_rate / tail_pct, 2) if tail_pct else None,
        "p_value_one_sided": round(float(p_value), 4) if n_scored else None,
    }


# ---------------------------------------------------------------------------
# Summary md
# ---------------------------------------------------------------------------
def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    coverage: dict,
    scores: list[dict],
    baseline_scores: list[dict],
    crash_branch_baseline: dict | None,
    rally_branch_baseline: dict | None,
) -> None:
    lines = [
        "# Beta 分位買賣超 precursor 研究 · 高 beta（高槓桿）股票是否比分點身分更有效",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "",
        "延伸既有分點 precursor 研究線（詳見腳本 docstring）：把訊號維度從",
        "「哪個分點在買賣」換成「買賣的是哪種股票（beta 分位／槓桿特性）」，",
        "測試「高 beta（High-beta，高槓桿特性股票）買賣超」是否比分點身分",
        "或單純市場整體買超廣度（buying breadth）更能領先大漲／大跌。",
        "",
        "## 凍結協議",
        "",
        f"- 視窗：{protocol['start']}..{protocol['end']}（與既有 crash/rally episode 檔案一致，直接沿用，未重新計算事件）",
        f"- 分點宇宙：與既有 pipeline 同一 cohort（2 年內有效交易日 ≥ {protocol['min_branch_dates']}，n={coverage['n_cohort']}）",
        f"- 個股宇宙：cohort 分點實際有交易且有 `stock_daily_bars`（finmind）收盤價的股票，n={coverage['n_stock_universe']}",
        f"- Beta：trailing {protocol['beta_window']} 個交易日 rolling beta（cov(個股報酬,大盤報酬)/var(大盤報酬)），"
        f"視窗嚴格早於當日（shift(1) 後再取 rolling，避免 lookahead）；歷史不足 {protocol['beta_window']} 個交易日留 NaN",
        "- 分位建構：每個交易日，把當日 cohort 全體分點對每檔股票的 net_amt（股數×收盤價）加總，"
        "用當日最近可得（forward-fill）的 beta 值分成四分位 q1（最低beta）..q4（最高beta），"
        "加總各分位當日 net_amt；beta_tilt = net_amt_q4 / (|net_amt_q4|+|net_amt_q1|)",
        f"- 自我常態化：`net_amt_beta_q4` 與 `beta_tilt` 各自算前 {protocol['lookback_days']} 個交易日累計，"
        "對序列自身整個視窗歷史取百分位排名（同既有 `compute_lookback_percentiles` 手法，n=1 series）",
        f"- 命中：大漲事件 = 百分位 ≥ {(1-protocol['tail_pct'])*100:.0f}%；大跌事件 = 百分位 ≤ {protocol['tail_pct']*100:.0f}%；"
        f"命中率 vs. 基期 {protocol['tail_pct']*100:.0f}% 單尾二項檢定",
        "",
        "## 覆蓋率與資料狀況",
        "",
        f"- cohort 分點數：{coverage['n_cohort']}",
        f"- 個股宇宙（有 net_amt 且有收盤價）：{coverage['n_stock_universe']}",
        f"- 有效 beta 的（股票,日）組合數：{coverage['n_beta_obs']:,}",
        f"- 融資（margin）資料覆蓋：{coverage['n_margin_stocks']} 檔股票（同視窗內），"
        f"資料最後日期 {coverage['margin_last_date']}"
        + (
            f"（比分析視窗結束日 {protocol['end']} 早，尾端 {coverage['margin_gap_days']} 個交易日無融資資料）"
            if coverage.get("margin_gap_days", 0) > 0
            else ""
        ),
        f"- 大跌事件數：{coverage['n_crash_episodes']}；大漲事件數：{coverage['n_rally_episodes']}（沿用既有 episode 檔案，未重新計算）",
        "",
        "## 主要結果：高 beta 買賣超聚合訊號 vs. 大漲／大跌事件",
        "",
        "| 訊號 | 方向 | hit/事件 | 命中率% | 基期% | lift | p值(單尾) |",
        "|------|------|---------:|--------:|------:|-----:|----------:|",
    ]
    for s in scores:
        lines.append(
            f"| {s['signal']} | {s['direction']} | {s['hit_count']}/{s['n_episodes_scored']} "
            f"(共{s['n_episodes_total']}) | {s['hit_rate_pct']:.1f} | {s['base_rate_pct']:.1f} | "
            f"{s['lift']:.2f} | {s['p_value_one_sided']:.4f} |"
        )

    lines += [
        "",
        "## 對照基準 1：既有市場整體買超廣度指數（rally_buying_breadth_dashboard.md 的 breadth_pct／composite_score）",
        "",
        "用跟本研究**完全相同**的「前5日累計→序列自身歷史百分位→top decile 命中率＋單尾二項檢定」流程，"
        "直接套用在既有 `rally_buying_breadth_timeseries.csv` 的 `breadth_pct`／`composite_score` 上"
        "（該 CSV 只涵蓋大漲方向，讀取現有檔案未重算）：",
        "",
        "| 訊號 | 方向 | hit/事件 | 命中率% | 基期% | lift | p值(單尾) |",
        "|------|------|---------:|--------:|------:|-----:|----------:|",
    ]
    for s in baseline_scores:
        lines.append(
            f"| {s['signal']} | {s['direction']} | {s['hit_count']}/{s['n_episodes_scored']} "
            f"(共{s['n_episodes_total']}) | {s['hit_rate_pct']:.1f} | {s['base_rate_pct']:.1f} | "
            f"{s['lift']:.2f} | {s['p_value_one_sided']:.4f} |"
        )
    lines += [
        "",
        "注意：`rally_buying_breadth_dashboard.md` 另外報告過「82.9% 平均 walk-forward 判別百分位排名」"
        "（見 `walkforward_precursor_summary.md`），那是嚴格 expanding-window walk-forward + AUC-like"
        "百分位排名方法，跟本研究／上表用的「in-sample 前5日累計→單尾二項檢定」命中率方法**不是同一套"
        "檢定嚴謹度**，兩者數字不可直接互換比較，只能定性參考「廣度型聚合訊號在兩種方法下都顯示中等以上"
        "判別力」這個方向一致的結論。",
        "",
        "## 對照基準 2：既有分點層級大跌 precursor 訊號（in-sample 單一最佳分點）",
        "",
    ]
    if crash_branch_baseline:
        b = crash_branch_baseline
        lines.append(
            f"`market_crash_precursor_branch_scores.csv` in-sample 最佳分點：hit "
            f"{b['hit_count']}/{b['n_episodes_scored']}，命中率 {b['hit_rate_pct']:.1f}%，"
            f"lift {b['lift']:.2f}，p={b['p_value_one_sided']:.4f}（分點："
            f"{b['branch_name']} `{b['branch_id']}`）。"
        )
    else:
        lines.append("（找不到既有分點層級大跌分數檔，略過）")
    lines += [
        "",
        "## 對照基準 3：既有分點層級大漲 precursor 訊號（in-sample 單一最佳分點，僅供對照，"
        "**四路驗證已判定對大漲方向不成立**）",
        "",
    ]
    if rally_branch_baseline:
        b = rally_branch_baseline
        lines.append(
            f"`market_rally_precursor_branch_scores.csv` in-sample 最佳分點：hit "
            f"{b['hit_count']}/{b['n_episodes_scored']}，命中率 {b['hit_rate_pct']:.1f}%，"
            f"lift {b['lift']:.2f}，p={b['p_value_one_sided']:.4f}（分點："
            f"{b['branch_name']} `{b['branch_id']}`）。in-sample 數字看似顯著，但這正是"
            "背景研究裡「具名分點站不住腳」的那個現象——本研究不把它當作真實可比較的基準，只列出"
            "數字讓讀者看到「單一分點 in-sample 命中率」本身可以多高、多不可靠。",
        )
    else:
        lines.append("（找不到既有分點層級大漲分數檔，略過）")

    lines += [
        "",
        "## 結論（verdict）",
        "",
    ]
    lines.append(protocol["verdict_text"])

    lines += [
        "",
        "## 限制與注意事項",
        "",
        "- 事件數少（大跌 11 個、大漲 9 個 episode），單尾二項檢定檢定力有限，本研究為**探索性**分析，"
        "不作為可下單訊號，也不是嚴謹的 walk-forward 驗證（見上方對照基準 1 的方法論差異說明）。",
        "- Beta 用 60 個交易日 rolling window、shift(1) 避免 lookahead；早期視窗（分析起始前 ~60 個"
        "交易日內）beta 幾乎必然是 NaN，該期間內的事件命中判斷樣本量會相應變少（見「n_episodes_scored」"
        "vs.「n_episodes_total」）。",
        "- 個股若在某段時間停牌／無收盤價，beta 計算的 rolling cov／var 視窗內含 NaN 報酬可能造成"
        "該股 beta 短暫失真或變成 NaN，本研究未特別處理停牌股票的插補，直接沿用 pandas rolling 的"
        "預設 NaN 傳播行為。",
        "- 融資餘額（`stock_margin_daily`）覆蓋率遠低於股價／分點資料（見上方覆蓋率），且資料有",
        "  尾端延遲，margin-based 對照結果的統計檢定力比 beta-based 更弱，僅供交叉檢查，不是本研究",
        "  主要交付物。",
        "- 分位建構要求當日至少有一定數量的股票同時具備「cohort 淨額」與「beta／融資餘額」才分四分位，"
        "不足者當日整體視為中性（net_amt=0），非缺值——與既有分點版本 pipeline 的處理慣例一致。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Beta-bucketed market precursor study")
    ap.add_argument("--start", default="2024-07-21")
    ap.add_argument("--end", default="2026-07-20")
    ap.add_argument("--beta-window", type=int, default=60)
    ap.add_argument("--beta-warmup-calendar-days", type=int, default=130)
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--min-branch-dates", type=int, default=450)
    ap.add_argument("--min-stocks-per-day", type=int, default=8)
    ap.add_argument(
        "--crash-episodes-csv",
        type=Path,
        default=OUT / "market_crash_precursor_episodes.csv",
    )
    ap.add_argument(
        "--rally-episodes-csv",
        type=Path,
        default=OUT / "market_rally_precursor_episodes.csv",
    )
    ap.add_argument(
        "--breadth-timeseries-csv",
        type=Path,
        default=OUT / "rally_buying_breadth_timeseries.csv",
    )
    ap.add_argument(
        "--crash-branch-scores-csv",
        type=Path,
        default=OUT / "market_crash_precursor_branch_scores.csv",
    )
    ap.add_argument(
        "--rally-branch-scores-csv",
        type=Path,
        default=OUT / "market_rally_precursor_branch_scores.csv",
    )
    ap.add_argument("--skip-margin", action="store_true", help="skip optional margin-based leverage cross-check")
    ap.add_argument(
        "--skip-margin-flow",
        action="store_true",
        help="skip follow-up margin_change (flow/momentum) cross-check appended to the summary md",
    )
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)

    print("building calendar …", flush=True)
    cal = build_calendar(conn, args.start, args.end)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    ext_start = (pd.Timestamp(args.start) - pd.Timedelta(days=args.beta_warmup_calendar_days)).strftime(
        "%Y-%m-%d"
    )
    ext_cal = build_calendar(conn, ext_start, args.end)
    print(f"extended (beta warmup) calendar n={len(ext_cal)} {ext_cal[0]}..{ext_cal[-1]}", flush=True)

    crash_episodes = pd.read_csv(args.crash_episodes_csv, dtype={"trade_date": str})
    rally_episodes = pd.read_csv(args.rally_episodes_csv, dtype={"trade_date": str})
    print(
        f"episodes: crash={int(crash_episodes['episode_first'].sum())} "
        f"rally={int(rally_episodes['episode_first'].sum())} (reused as-is)",
        flush=True,
    )

    print("loading branch cohort (2y deep coverage) — full-table scan, may take ~1-2min …", flush=True)
    t0 = time.time()
    cohort = load_cohort(conn, start=args.start, end=args.end, min_dates=args.min_branch_dates)
    ids = cohort["id"].tolist()
    print(f"cohort n={len(ids)} ({time.time()-t0:.1f}s)", flush=True)

    print("building stock-day net_amt panel (month-chunked) …", flush=True)
    stock_day_panel = build_stock_day_panel(conn, ids, start=args.start, end=args.end)
    universe = sorted(stock_day_panel["stock_id"].unique().tolist())
    print(f"stock-day panel rows={len(stock_day_panel):,} universe n={len(universe)}", flush=True)

    print("loading price panel for beta computation …", flush=True)
    price_long = load_price_long(conn, universe, start=ext_start, end=args.end)
    print(f"price rows={len(price_long):,} stocks={price_long['stock_id'].nunique()}", flush=True)

    bench_close = load_benchmark_close(conn, code=BENCHMARK).sort_index()
    mkt_ret = bench_close.pct_change()

    print(f"computing rolling {args.beta_window}d beta (no lookahead) …", flush=True)
    beta_long = compute_rolling_beta(price_long, mkt_ret, window=args.beta_window)
    print(f"beta obs (non-NaN)={len(beta_long):,}", flush=True)
    beta_asof = ffill_beta_asof(beta_long, cal)

    print("building beta-quartile daily series …", flush=True)
    beta_daily = build_beta_quartile_daily_series(
        stock_day_panel, beta_asof, cal, min_stocks_per_day=args.min_stocks_per_day
    )
    beta_daily = compute_lookback_percentiles_series(
        beta_daily, ["net_amt_beta_q4", "beta_tilt"], lookback_days=args.lookback_days
    )

    margin_daily = None
    n_margin_stocks = 0
    margin_last_date = "NA"
    margin_gap_days = 0
    if not args.skip_margin:
        print("loading margin_balance panel (optional leverage proxy) …", flush=True)
        margin_long = load_margin_long(conn, start=args.start, end=args.end)
        n_margin_stocks = margin_long["stock_id"].nunique()
        margin_last_date = margin_long["trade_date"].max() if not margin_long.empty else "NA"
        margin_gap_days = sum(1 for d in cal if d > str(margin_last_date))
        margin_daily = build_margin_quartile_daily_series(
            stock_day_panel, margin_long, cal, min_stocks_per_day=args.min_stocks_per_day
        )
        margin_daily = compute_lookback_percentiles_series(
            margin_daily, ["net_amt_margin_q4", "margin_tilt"], lookback_days=args.lookback_days
        )

    conn.close()

    daily = beta_daily
    if margin_daily is not None:
        daily = daily.merge(margin_daily, on="trade_date", how="left")

    out_csv = OUT / "beta_group_precursor_daily_series.csv"
    daily.to_csv(out_csv, index=False)
    print(f"wrote {out_csv} rows={len(daily):,}", flush=True)

    print("scoring beta-bucket signal vs crash/rally episodes …", flush=True)
    scores = [
        score_series_vs_episodes(
            daily, crash_episodes, "net_amt_beta_q4_lb_pctile",
            tail_pct=args.tail_pct, direction="crash", signal_label="net_amt_beta_q4（高beta買賣超累計）",
        ),
        score_series_vs_episodes(
            daily, rally_episodes, "net_amt_beta_q4_lb_pctile",
            tail_pct=args.tail_pct, direction="rally", signal_label="net_amt_beta_q4（高beta買賣超累計）",
        ),
        score_series_vs_episodes(
            daily, crash_episodes, "beta_tilt_lb_pctile",
            tail_pct=args.tail_pct, direction="crash", signal_label="beta_tilt（beta傾斜分數）",
        ),
        score_series_vs_episodes(
            daily, rally_episodes, "beta_tilt_lb_pctile",
            tail_pct=args.tail_pct, direction="rally", signal_label="beta_tilt（beta傾斜分數）",
        ),
    ]
    if margin_daily is not None:
        scores += [
            score_series_vs_episodes(
                daily, crash_episodes, "net_amt_margin_q4_lb_pctile",
                tail_pct=args.tail_pct, direction="crash", signal_label="net_amt_margin_q4（高融資餘額買賣超累計）",
            ),
            score_series_vs_episodes(
                daily, rally_episodes, "net_amt_margin_q4_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="net_amt_margin_q4（高融資餘額買賣超累計）",
            ),
            score_series_vs_episodes(
                daily, crash_episodes, "margin_tilt_lb_pctile",
                tail_pct=args.tail_pct, direction="crash", signal_label="margin_tilt（融資餘額傾斜分數）",
            ),
            score_series_vs_episodes(
                daily, rally_episodes, "margin_tilt_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="margin_tilt（融資餘額傾斜分數）",
            ),
        ]
    for s in scores:
        print(f"  {s}", flush=True)

    baseline_scores: list[dict] = []
    if args.breadth_timeseries_csv.exists():
        breadth = pd.read_csv(args.breadth_timeseries_csv, dtype={"trade_date": str})
        breadth = compute_lookback_percentiles_series(
            breadth, ["breadth_pct", "composite_score"], lookback_days=args.lookback_days
        )
        baseline_scores = [
            score_series_vs_episodes(
                breadth, rally_episodes, "breadth_pct_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="breadth_pct（既有買超廣度指數）",
            ),
            score_series_vs_episodes(
                breadth, rally_episodes, "composite_score_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="composite_score（既有複合分數）",
            ),
        ]

    crash_branch_baseline = None
    if args.crash_branch_scores_csv.exists():
        cb = pd.read_csv(args.crash_branch_scores_csv)
        if not cb.empty:
            crash_branch_baseline = cb.iloc[0].to_dict()

    rally_branch_baseline = None
    if args.rally_branch_scores_csv.exists():
        rb = pd.read_csv(args.rally_branch_scores_csv)
        if not rb.empty:
            rally_branch_baseline = rb.iloc[0].to_dict()

    tilt_rally = next(s for s in scores if s["signal"].startswith("beta_tilt") and s["direction"] == "rally")
    tilt_crash = next(s for s in scores if s["signal"].startswith("beta_tilt") and s["direction"] == "crash")
    q4_rally = next(s for s in scores if s["signal"].startswith("net_amt_beta_q4") and s["direction"] == "rally")
    q4_crash = next(s for s in scores if s["signal"].startswith("net_amt_beta_q4") and s["direction"] == "crash")
    best_rally = max(tilt_rally, q4_rally, key=lambda s: s["lift"] or 0)
    best_crash = max(tilt_crash, q4_crash, key=lambda s: s["lift"] or 0)
    verdict_lines = [
        f"- 大漲方向：本研究最佳高-beta 聚合訊號（{best_rally['signal']}）命中率 "
        f"{best_rally['hit_rate_pct']:.1f}%、lift {best_rally['lift']:.2f}、"
        f"p={best_rally['p_value_one_sided']:.4f}（n={best_rally['n_episodes_scored']}）。",
        f"- 大跌方向：本研究最佳高-beta 聚合訊號（{best_crash['signal']}）命中率 "
        f"{best_crash['hit_rate_pct']:.1f}%、lift {best_crash['lift']:.2f}、"
        f"p={best_crash['p_value_one_sided']:.4f}（n={best_crash['n_episodes_scored']}）。",
        "- 「換維度」（從分點身分換成股票 beta 特性）本身不是萬能解方："
        "把整個 cohort 的買賣超按股票 beta 分四份，本質上仍是一種「廣度型聚合」指標"
        "（只是分組依據換成股票特性而非分點身分），跟既有買超廣度指數屬同一類方法論，"
        "而不是回到「具名精英」思路——這點與既有四路驗證「廣度型聚合優於具名清單」的"
        "結論方向一致。",
        "- 相對既有訊號設計：見上方對照基準 1-3 表格逐項數字；整體判斷（不只是最佳單點數字）"
        "應以上表逐行比較，而非只看本段摘要的最高分。",
    ]

    protocol = {
        "start": args.start,
        "end": args.end,
        "beta_window": args.beta_window,
        "lookback_days": args.lookback_days,
        "tail_pct": args.tail_pct,
        "min_branch_dates": args.min_branch_dates,
        "verdict_text": "\n".join(verdict_lines),
    }
    coverage = {
        "n_cohort": len(ids),
        "n_stock_universe": len(universe),
        "n_beta_obs": len(beta_long),
        "n_margin_stocks": n_margin_stocks,
        "margin_last_date": margin_last_date,
        "margin_gap_days": margin_gap_days,
        "n_crash_episodes": int(crash_episodes["episode_first"].sum()),
        "n_rally_episodes": int(rally_episodes["episode_first"].sum()),
    }

    summary_path = OUT / "beta_group_precursor_summary.md"
    write_summary_md(
        summary_path,
        protocol=protocol,
        coverage=coverage,
        scores=scores,
        baseline_scores=baseline_scores,
        crash_branch_baseline=crash_branch_baseline,
        rally_branch_baseline=rally_branch_baseline,
    )
    print(f"wrote {summary_path}", flush=True)

    if not args.skip_margin and not args.skip_margin_flow:
        print("[follow-up] loading margin_change (flow) panel …", flush=True)
        conn2 = connect_ro(args.db)
        margin_change_long = load_margin_change_long(conn2, start=args.start, end=args.end)
        conn2.close()
        n_margin_stocks_flow = margin_change_long["stock_id"].nunique()
        print(f"[follow-up] margin_change rows={len(margin_change_long):,} stocks={n_margin_stocks_flow}", flush=True)

        marginflow_daily = build_margin_change_quartile_daily_series(
            stock_day_panel, margin_change_long, cal, min_stocks_per_day=args.min_stocks_per_day
        )
        marginflow_daily = compute_lookback_percentiles_series(
            marginflow_daily, ["net_amt_marginflow_q4", "marginflow_tilt"], lookback_days=args.lookback_days
        )

        agg_daily = build_aggregate_margin_change_series(margin_change_long, cal)
        agg_daily = compute_lookback_percentiles_series(
            agg_daily,
            ["agg_margin_change_sum", "agg_margin_change_breadth_pct"],
            lookback_days=args.lookback_days,
        )

        flow_daily = marginflow_daily.merge(agg_daily, on="trade_date", how="left")
        flow_csv = OUT / "beta_group_precursor_margin_flow_daily_series.csv"
        flow_daily.to_csv(flow_csv, index=False)
        print(f"wrote {flow_csv} rows={len(flow_daily):,} (new file, does not touch {out_csv.name})", flush=True)

        print("[follow-up] scoring margin-flow signals vs crash/rally episodes …", flush=True)
        flow_scores = [
            score_series_vs_episodes(
                flow_daily, crash_episodes, "net_amt_marginflow_q4_lb_pctile",
                tail_pct=args.tail_pct, direction="crash",
                signal_label="net_amt_marginflow_q4（新增槓桿最快股票買賣超累計）",
            ),
            score_series_vs_episodes(
                flow_daily, rally_episodes, "net_amt_marginflow_q4_lb_pctile",
                tail_pct=args.tail_pct, direction="rally",
                signal_label="net_amt_marginflow_q4（新增槓桿最快股票買賣超累計）",
            ),
            score_series_vs_episodes(
                flow_daily, crash_episodes, "marginflow_tilt_lb_pctile",
                tail_pct=args.tail_pct, direction="crash", signal_label="marginflow_tilt（融資流量傾斜分數）",
            ),
            score_series_vs_episodes(
                flow_daily, rally_episodes, "marginflow_tilt_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="marginflow_tilt（融資流量傾斜分數）",
            ),
            score_series_vs_episodes(
                flow_daily, crash_episodes, "agg_margin_change_sum_lb_pctile",
                tail_pct=args.tail_pct, direction="crash", signal_label="agg_margin_change_sum（市場整體融資流量加總）",
            ),
            score_series_vs_episodes(
                flow_daily, rally_episodes, "agg_margin_change_sum_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="agg_margin_change_sum（市場整體融資流量加總）",
            ),
            score_series_vs_episodes(
                flow_daily, crash_episodes, "agg_margin_change_breadth_pct_lb_pctile",
                tail_pct=args.tail_pct, direction="crash", signal_label="agg_margin_change_breadth_pct（融資新增廣度）",
            ),
            score_series_vs_episodes(
                flow_daily, rally_episodes, "agg_margin_change_breadth_pct_lb_pctile",
                tail_pct=args.tail_pct, direction="rally", signal_label="agg_margin_change_breadth_pct（融資新增廣度）",
            ),
        ]
        for s in flow_scores:
            print(f"  {s}", flush=True)

        level_best_lift = max(
            (s["lift"] or 0)
            for s in scores
            if s["signal"].startswith(("net_amt_margin_q4", "margin_tilt"))
        )
        flow_best = max(flow_scores, key=lambda s: s["lift"] or 0)
        flow_verdict = [
            f"- Flow 版本最佳結果：`{flow_best['signal']}`（{flow_best['direction']} 方向）"
            f"命中率 {flow_best['hit_rate_pct']:.1f}%、lift {flow_best['lift']:.2f}、"
            f"p={flow_best['p_value_one_sided']:.4f}（n={flow_best['n_episodes_scored']}）。"
            f" Level 版本（`net_amt_margin_q4`／`margin_tilt`）最佳 lift 為 "
            f"{level_best_lift:.2f}（見本檔前段對照基準表格）。",
        ]
        if flow_best["lift"] and flow_best["lift"] > level_best_lift and (flow_best["p_value_one_sided"] or 1) < 0.10:
            flow_verdict.append(
                "- **Flow 優於 level，且達到探索性顯著門檻（p<0.10）**：把融資訊號從"
                "「餘額水位」換成「新增借貸流量」確實改善了判別力，值得列入後續更嚴謹"
                "（walk-forward）驗證的候選。"
            )
        elif flow_best["lift"] and flow_best["lift"] > level_best_lift:
            flow_verdict.append(
                "- Flow 版本 lift 數字上優於 level 版本，但 p 值未達 0.10 探索性門檻——"
                "方向上支持使用者假設（新增槓桿流量比餘額水位更有訊息），但證據仍弱，"
                "樣本事件數過少（大跌11／大漲9）難以下定論。"
            )
        else:
            flow_verdict.append(
                "- Flow 版本並未優於 level 版本：兩者同樣落在基期附近（lift≈1），"
                "把融資訊號換成「新增槓桿流量」並未挽救 margin-based leverage proxy 的"
                "無效結論——不論用水位還是流量，融資資料在本研究的方法論下都沒有"
                "看到領先大漲／大跌的證據。"
            )
        flow_verdict.append(
            "- 不論 flow 或 level，融資資料的樣本覆蓋率（cohort 淨額+融資雙重要求後"
            "每日可用股票數更少）都比 beta 版本弱，統計檢定力本身就偏低，"
            "任何『不顯著』的結論都應該解讀為『證據不足』而非『確定無效』。"
        )

        append_margin_flow_section_md(
            summary_path,
            protocol=protocol,
            coverage={**coverage, "n_margin_stocks_flow": n_margin_stocks_flow},
            flow_scores=flow_scores,
            verdict_lines=flow_verdict,
        )
        print(f"appended follow-up section to {summary_path}", flush=True)

    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
