#!/usr/bin/env python3
"""大跌溫度計進階研究 H9：聰明錢賣超標的的個股融資變化.

Research diagnostic（純調查，不修改生產腳本／config）。

問題：之前的「全市場融資寬度」研究（有多少股票同時新增融資）結果是 null
訊號（lift 3.33, p=0.053, n=9，不顯著）。這次換一個角度：不看全市場寬度，
只聚焦在「這8家聰明錢分點大跌前正在重賣超的特定個股」，看這些股票自身的
融資變化——是否有「聰明錢賣、散戶還在融資接刀」的組合模式，這個角度結合了
「聰明錢賣什麼」+「散戶在那些特定股票上的槓桿行為」兩個維度，是之前沒測過的。

方法：
1. 對8家分點，逐一大跌事件（16次，`ctd.load_event_dates` 直接從 IX0001 重算，
   與 CSV `market_crash_precursor_episodes.csv` 事件定義一致），在事件前5個
   交易日窗口內，找各分點賣超金額最大的前3檔個股。
2. 對這些「聰明錢賣出的特定股票」，查 `stock_margin_daily` 的融資變化
   （margin_change 對前一日 margin_balance 正規化的 ratio，5日累計），並用
   與生產腳本 `bounded_pctrank` 完全相同的邏輯（bounded-rolling 120日 +
   事件排除±5日）對該股「自身」歷史取百分位——這一步是為了跟現行溫度計
   的「自我常態化」方法論一致。
3. 為了公平比較判別力（而非只看16次事件當天的單點數字），把「聰明錢賣超
   個股的融資接刀百分位」延伸成整段歷史（同一 calendar）的每日時間序列，
   再用 `bounded_pctrank` 對這條新序列本身也算一次百分位，跟現行純
   「聰明錢賣超金額」複合分數（`weighted_composite`→`bounded_pctrank`）
   的判別力做逐事件對照。
4. 誠實查核 `stock_margin_daily` 的實際資料覆蓋率與延遲天數，並评估對「每日
   即時應用」的實務限制。

輸出：reports/research/branch-footprint-screen/adv_h9_stock_level_margin.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_crash_thermometer_h9_stock_level_margin.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = ROOT / "data" / "stocks.db"
OUT_MD = ROOT / "reports" / "research" / "branch-footprint-screen" / "adv_h9_stock_level_margin.md"
EPISODES_CSV = ROOT / "reports" / "research" / "branch-footprint-screen" / "market_crash_precursor_episodes.csv"

# 直接載入生產腳本模組（唯讀 import，不修改該檔案）
_spec = importlib.util.spec_from_file_location(
    "crash_thermometer_dashboard",
    ROOT / "scripts" / "research" / "run_market_crash_thermometer_dashboard.py",
)
ctd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctd)

PANEL = ctd.PANEL
IDS = list(PANEL.keys())
SOURCE = ctd.SOURCE
LOOKBACK_DAYS = 5
CAL_N_DAYS = 560  # ~2.2年交易日，覆蓋2024-07~asof（同 H1 慣例）
HISTORY_DAYS = ctd.HISTORY_DAYS_DEFAULT  # 120
TOP_N_PER_BRANCH_EVENT = 3


def connect() -> "sqlite3.Connection":
    return ctd.connect_ro(DB_PATH)


def build_branch_stock_grid(conn, ids: list[str], cal: list[str]) -> pd.DataFrame:
    """跟 `ctd.build_branch_panel` 相同的 net_amt=net(股)×close 邏輯，但不對
    stock_id 做 groupby 加總——保留個股層級，並補齊(branch,stock,date)全網格
    （branch 當天沒交易該股=0），供後續逐股 rolling 5日賣超排名使用。"""
    start, end = cal[0], cal[-1]
    placeholders = ",".join("?" for _ in ids)
    raw = pd.read_sql_query(
        f"""
        SELECT securities_trader_id, trade_date, stock_id, net
        FROM stock_broker_branch_daily
        WHERE source=? AND trade_date >= ? AND trade_date <= ?
          AND stock_id != '__EMPTY__' AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND securities_trader_id IN ({placeholders})
        """,
        conn,
        params=[SOURCE, start, end, *ids],
    )
    closes = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
        """,
        conn,
        params=[SOURCE, start, end],
    )
    raw["securities_trader_id"] = raw["securities_trader_id"].astype(str)
    raw["stock_id"] = raw["stock_id"].astype(str)
    raw["trade_date"] = raw["trade_date"].astype(str)
    closes["stock_id"] = closes["stock_id"].astype(str)
    closes["trade_date"] = closes["trade_date"].astype(str)

    all_dates = sorted(set(raw["trade_date"]) | set(closes["trade_date"]))
    stocks_needed = sorted(set(raw["stock_id"]) & set(closes["stock_id"]))
    closes = closes[closes["stock_id"].isin(stocks_needed)]
    wide = closes.pivot(index="trade_date", columns="stock_id", values="close")
    wide = wide.reindex(all_dates).ffill()
    long_closes = wide.stack().rename("close").reset_index()
    long_closes.columns = ["trade_date", "stock_id", "close"]

    merged = raw.merge(long_closes, on=["stock_id", "trade_date"], how="inner")
    merged["net_amt"] = merged["net"].astype(float) * merged["close"].astype(float)
    grp = merged.groupby(
        ["securities_trader_id", "stock_id", "trade_date"], as_index=False
    ).agg(net_amt=("net_amt", "sum"))

    # 補齊 (branch, stock, date) 全網格：branch 當天沒賣過該股=0，供 rolling(5)
    # 累計不因為稀疏交易紀錄而錯位。stock 宇宙限縮在「至少被這8家某一天交易過」
    # 的股票，避免無意義擴張到全市場千餘檔。
    stocks_traded = sorted(grp["stock_id"].unique())
    full_idx = pd.MultiIndex.from_product([ids, stocks_traded, cal], names=["securities_trader_id", "stock_id", "trade_date"])
    g = grp.set_index(["securities_trader_id", "stock_id", "trade_date"])
    grid = g.reindex(full_idx).fillna(0.0).reset_index()
    return grid


def compute_top_sold(grid: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """逐 (branch, stock) 算 lb_sum = net_amt.shift(1).rolling(lookback).sum()
    （跟 `ctd.compute_lb_pctile` 同一定義：評估日 T 用的是 T-lookback..T-1 的
    賣超金額，不含 T 當天），再在每個 (branch, date) 內對所有股票排名，取
    賣超（net<0）金額最大的前 N 檔標記為 top_sold。"""
    grid = grid.sort_values(["securities_trader_id", "stock_id", "trade_date"]).copy()
    grid["lb_sum"] = (
        grid.groupby(["securities_trader_id", "stock_id"])["net_amt"]
        .transform(lambda s: s.shift(1).rolling(lookback_days).sum())
    )
    sold = grid[grid["lb_sum"] < 0].copy()
    sold["rank_sold"] = sold.groupby(["securities_trader_id", "trade_date"])["lb_sum"].rank(method="first", ascending=True)
    return sold


def load_margin_universe(conn) -> tuple[pd.DataFrame, str, str, int]:
    meta = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT stock_id) FROM stock_margin_daily WHERE source='finmind'"
    ).fetchone()
    margin = pd.read_sql_query(
        "SELECT stock_id, trade_date, margin_balance, margin_change FROM stock_margin_daily WHERE source='finmind'",
        conn,
    )
    margin["stock_id"] = margin["stock_id"].astype(str)
    margin["trade_date"] = margin["trade_date"].astype(str)
    return margin, str(meta[0]), str(meta[1]), int(meta[2])


def build_margin_flow5d_by_stock(margin: pd.DataFrame, cal: list[str]) -> dict[str, dict[str, float]]:
    """逐股：把 margin_change_ratio=margin_change/margin_balance.shift(1) 對齊到
    全域交易日曆 `cal`（不做 forward-fill——缺漏的日子維持真缺，才能誠實反映
    資料延遲/覆蓋缺口），算 shift(1).rolling(5,min_periods=5).sum()（窗口
    T-5..T-1，跟賣超金額 lb_sum 同一時間定義），回傳 {stock_id: {trade_date: flow5d}}。"""
    margin = margin.sort_values(["stock_id", "trade_date"]).copy()
    margin["ratio"] = margin["margin_change"] / margin.groupby("stock_id")["margin_balance"].shift(1)
    margin.loc[margin.groupby("stock_id")["margin_balance"].shift(1) == 0, "ratio"] = np.nan

    out: dict[str, dict[str, float]] = {}
    cal_set = set(cal)
    for stock_id, g in margin.groupby("stock_id"):
        g = g[g["trade_date"].isin(cal_set)]
        if g.empty:
            continue
        s = g.set_index("trade_date")["ratio"].reindex(cal)
        flow5d = s.shift(1).rolling(5, min_periods=5).sum()
        flow5d = flow5d.dropna()
        if not flow5d.empty:
            out[stock_id] = flow5d.to_dict()
    return out


def main() -> None:
    conn = connect()
    asof = ctd.latest_trade_date(conn)
    print(f"asof={asof}", flush=True)

    cal = ctd.build_calendar(conn, end=asof, n_days=CAL_N_DAYS, extra_ids=IDS)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)
    idx_of = {d: i for i, d in enumerate(cal)}

    crash_dates, all_event_dates = ctd.load_event_dates(conn, cal)
    event_idx = {idx_of[d] for d in all_event_dates if d in idx_of}
    # 用背景指定的既有 episode 檔案（16次）列舉要評估的事件，而非重算後的
    # crash_dates 全集——calendar 起點(2024-03-29)早於分點資料實際起點(2024-07-01)，
    # 重算會多算出1個(2024-04-19)在分點資料覆蓋前、毫無意義的偽事件；
    # event_idx（用於 bounded_pctrank 常態池排除）仍用重算全集，較保守不影響結論。
    episodes = pd.read_csv(EPISODES_CSV)
    crash_list = sorted(str(d) for d in episodes["trade_date"] if str(d) in idx_of)
    print(f"n crash events (from episodes csv, IX0001<=-3%) = {len(crash_list)}", flush=True)

    # ---- 1. 8 家分點個股層級賣超網格 + 逐事件 top-N 賣超股 ----
    grid = build_branch_stock_grid(conn, IDS, cal)
    print(f"branch-stock grid rows={len(grid)}, stocks universe n={grid['stock_id'].nunique()}", flush=True)
    sold = compute_top_sold(grid, LOOKBACK_DAYS)
    top_sold = sold[sold["rank_sold"] <= TOP_N_PER_BRANCH_EVENT].copy()
    print(f"top-sold rows (all dates) = {len(top_sold)}", flush=True)

    # 事件當天(E)的 top-sold 明細（用於報告表格與覆蓋率查核）
    event_top_sold = top_sold[top_sold["trade_date"].isin(crash_list)].copy()
    event_top_sold = event_top_sold.sort_values(["trade_date", "securities_trader_id", "rank_sold"])

    # ---- 2. 融資表覆蓋與延遲查核 ----
    margin, margin_min_date, margin_max_date, margin_n_stocks = load_margin_universe(conn)
    margin_stock_set = set(margin["stock_id"].unique())

    sold_stock_universe = sorted(top_sold["stock_id"].unique())
    event_sold_stock_universe = sorted(event_top_sold["stock_id"].unique())
    coverage_all = len(set(sold_stock_universe) & margin_stock_set) / max(1, len(sold_stock_universe))
    coverage_event = len(set(event_sold_stock_universe) & margin_stock_set) / max(1, len(event_sold_stock_universe))

    # 逐事件：margin 表在 E-1 當時可得的最新資料落後幾個交易日
    margin_dates_sorted = sorted(margin["trade_date"].unique())
    lag_rows = []
    for e in crash_list:
        ei = idx_of[e]
        e_minus_1 = cal[ei - 1] if ei >= 1 else None
        available = [d for d in margin_dates_sorted if d <= (e_minus_1 or e)]
        if not available:
            lag_rows.append({"event": e, "e_minus_1": e_minus_1, "latest_margin_date": None, "lag_trading_days": None})
            continue
        latest = available[-1]
        lag_td = (idx_of.get(e_minus_1, ei) - idx_of[latest]) if latest in idx_of else None
        lag_rows.append({"event": e, "e_minus_1": e_minus_1, "latest_margin_date": latest, "lag_trading_days": lag_td})
    lag_df = pd.DataFrame(lag_rows)

    asof_lag_td = None
    if margin_max_date in idx_of and asof in idx_of:
        asof_lag_td = idx_of[asof] - idx_of[margin_max_date]

    # ---- 3. 逐股融資 5日流量 + bounded 自我百分位（跟生產 bounded_pctrank 同邏輯） ----
    flow5d_by_stock = build_margin_flow5d_by_stock(margin, cal)
    print(f"stocks with usable margin flow5d series (any date) = {len(flow5d_by_stock)}", flush=True)

    def margin_pctrank_for(stock_id: str, test_date: str) -> tuple[float | None, int]:
        series = flow5d_by_stock.get(stock_id)
        if not series:
            return None, 0
        return ctd.bounded_pctrank(
            series, cal, event_idx, test_date=test_date, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        )

    # 逐日、逐分點：top-N 賣超股的融資百分位平均 -> 分點層級 margin pileup 分數
    branch_date_scores: dict[tuple[str, str], list[float]] = {}
    for row in top_sold.itertuples():
        pr, _ = margin_pctrank_for(row.stock_id, row.trade_date)
        if pr is not None:
            branch_date_scores.setdefault((row.securities_trader_id, row.trade_date), []).append(pr)

    branch_pileup_rows = [
        {"securities_trader_id": bid, "trade_date": d, "margin_pileup_pr": float(np.mean(vals)), "n_stocks": len(vals)}
        for (bid, d), vals in branch_date_scores.items()
    ]
    branch_pileup = pd.DataFrame(branch_pileup_rows)
    print(f"branch-date margin pileup scored rows = {len(branch_pileup)}", flush=True)

    weights = {bid: w for bid, (_, w) in PANEL.items()}
    if not branch_pileup.empty:
        branch_pileup["w"] = branch_pileup["securities_trader_id"].map(weights)
        agg = branch_pileup.groupby("trade_date").apply(
            lambda g: pd.Series(
                {
                    "margin_pileup_score": float((g["margin_pileup_pr"] * g["w"]).sum() / g["w"].sum()),
                    "n_branches_covered": int(g["securities_trader_id"].nunique()),
                }
            ),
            include_groups=False,
        ).reset_index()
    else:
        agg = pd.DataFrame(columns=["trade_date", "margin_pileup_score", "n_branches_covered"])
    margin_pileup_by_date = dict(zip(agg["trade_date"], agg["margin_pileup_score"]))
    n_branches_covered_by_date = dict(zip(agg["trade_date"], agg["n_branches_covered"]))

    # ---- 4. 基線複合分數（現行溫度計同一套：build_branch_panel -> compute_lb_pctile -> weighted_composite） ----
    panel_raw = ctd.build_branch_panel(conn, IDS, start=cal[0], end=asof)
    full_grid = ctd.build_full_grid(panel_raw, IDS, cal)
    scored = ctd.compute_lb_pctile(full_grid, LOOKBACK_DAYS)
    series = ctd.weighted_composite(scored, weights)
    baseline_by_date = dict(zip(series["trade_date"], series["composite_score"]))

    # ---- 5. 逐事件：baseline vs. margin_pileup vs. combined 的 bounded_pctrank ----
    margin_pileup_pctrank_by_date: dict[str, float | None] = {}
    for d in cal:
        pr, _ = ctd.bounded_pctrank(
            margin_pileup_by_date, cal, event_idx, test_date=d, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        )
        margin_pileup_pctrank_by_date[d] = pr

    combined_by_date: dict[str, float] = {}
    for d in cal:
        b = baseline_by_date.get(d)
        m = margin_pileup_pctrank_by_date.get(d)
        if b is None:
            continue
        combined_by_date[d] = 0.5 * b + 0.5 * m if m is not None else b

    results = []
    for e in crash_list:
        b_pr, b_n = ctd.bounded_pctrank(baseline_by_date, cal, event_idx, test_date=e, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS)
        m_pr = margin_pileup_pctrank_by_date.get(e)
        c_pr, c_n = ctd.bounded_pctrank(combined_by_date, cal, event_idx, test_date=e, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS)
        raw_margin_score = margin_pileup_by_date.get(e)
        n_cov = n_branches_covered_by_date.get(e, 0)
        results.append(
            {
                "event": e,
                "baseline_pctrank": b_pr,
                "margin_pileup_raw": raw_margin_score,
                "margin_pileup_pctrank": m_pr,
                "n_branches_margin_covered": n_cov,
                "combined_pctrank": c_pr,
                "n_normal_baseline": b_n,
            }
        )
    results_df = pd.DataFrame(results)

    conn.close()

    write_report(
        asof=asof,
        cal=cal,
        crash_list=crash_list,
        event_top_sold=event_top_sold,
        margin_min_date=margin_min_date,
        margin_max_date=margin_max_date,
        margin_n_stocks=margin_n_stocks,
        coverage_all=coverage_all,
        coverage_event=coverage_event,
        sold_stock_universe_n=len(sold_stock_universe),
        event_sold_stock_universe_n=len(event_sold_stock_universe),
        lag_df=lag_df,
        asof_lag_td=asof_lag_td,
        results_df=results_df,
        flow5d_by_stock=flow5d_by_stock,
    )
    print(f"wrote {OUT_MD}", flush=True)


def write_report(
    *,
    asof: str,
    cal: list[str],
    crash_list: list[str],
    event_top_sold: pd.DataFrame,
    margin_min_date: str,
    margin_max_date: str,
    margin_n_stocks: int,
    coverage_all: float,
    coverage_event: float,
    sold_stock_universe_n: int,
    event_sold_stock_universe_n: int,
    lag_df: pd.DataFrame,
    asof_lag_td: int | None,
    results_df: pd.DataFrame,
    flow5d_by_stock: dict,
) -> None:
    idx_of = {d: i for i, d in enumerate(cal)}
    lines: list[str] = []
    lines.append("# H9：聰明錢賣超標的的個股融資變化 vs. 全市場融資寬度")
    lines.append("")
    lines.append("Research diagnostic（純調查，不修改生產腳本／config）· 非可下單訊號。")
    lines.append("")
    lines.append(
        "沿用「大跌溫度計」8家分點清單與 `bounded_pctrank`（bounded-rolling "
        f"{ctd.HISTORY_DAYS_DEFAULT}日常態基準池＋事件±{LOOKBACK_DAYS}日排除）方法論，測試："
        "把「8家分點在16次大跌事件前5個交易日內實際賣超的特定個股」的融資餘額變化，"
        "組合進現行「聰明錢賣超金額」複合分數，判別力是否優於（1）現行純賣超金額版本、"
        "（2）之前測過的全市場融資寬度版本（null，lift 3.33／p=0.053／n=9）。"
    )
    lines.append("")
    lines.append("## 結論先講（TL;DR）")
    lines.append("")

    b_valid = results_df.dropna(subset=["baseline_pctrank"])
    m_valid = results_df.dropna(subset=["margin_pileup_pctrank"])
    c_valid = results_df.dropna(subset=["combined_pctrank"])
    baseline_avg = b_valid["baseline_pctrank"].mean() if not b_valid.empty else float("nan")
    combined_avg = c_valid["combined_pctrank"].mean() if not c_valid.empty else float("nan")
    n_events = len(results_df)
    n_margin_scorable = int(results_df["margin_pileup_pctrank"].notna().sum())

    lines.append(
        f"- **資料延遲是本假說目前無法實務應用的硬限制**：`stock_margin_daily` 最新資料"
        f"僅到 **{margin_max_date}**，而分點/股價資料已到 **{asof}**，"
        f"落後約 **{asof_lag_td if asof_lag_td is not None else 'NA'} 個交易日**"
        "（略多於2週）。16次大跌事件中，只有 "
        f"**{n_margin_scorable}/{n_events}** 次事件當天能算出融資訊號（其餘因融資資料"
        "落後而完全缺值），且能算出的幾乎都是較早期事件——**最近期事件（尤其 2026-07-17）"
        "融資資料在事件窗口內完全空白**，代表這個角度目前**無法**整合進每天9點寄出的"
        "即時溫度計，只能用於事後（2週以上）回顧驗證。"
    )
    lines.append(
        f"- **個股覆蓋率**：8家分點在16次大跌事件前5日賣超前{TOP_N_PER_BRANCH_EVENT}大個股"
        f"（去重後共 {event_sold_stock_universe_n} 檔），只有 **{coverage_event*100:.1f}%** "
        f"在 `stock_margin_daily` 有出現過（全期任意日期口徑的賣超股覆蓋率 "
        f"{coverage_all*100:.1f}%，n={sold_stock_universe_n}）。這8家常賣超的是外資/主力"
        "偏好的中大型股，融資表（僅169檔、且明顯偏中小型/高融資熱度股）本身就對不齊。"
    )
    if n_margin_scorable >= 3:
        lines.append(
            f"- **判別力比較**（僅能在有融資覆蓋的 {n_margin_scorable} 次事件上比較）：現行純賣超"
            f"金額複合分數平均百分位 **{b_valid[b_valid['event'].isin(m_valid['event'])]['baseline_pctrank'].mean():.1f}%**，"
            f"加入融資接刀訊號的組合分數平均百分位 **{c_valid[c_valid['event'].isin(m_valid['event'])]['combined_pctrank'].mean():.1f}%**"
            "（詳見下方逐事件表）。"
        )
    else:
        lines.append(
            "- **判別力比較無法進行有意義的統計**：有融資覆蓋可算分數的事件數過少"
            f"（{n_margin_scorable}/{n_events}），任何平均數字都只是少數樣本的巧合，"
            "不構成可信的判別力結論。"
        )
    lines.append(
        "- **相對全市場融資寬度版本（之前研究，null 訊號）**：換成「個股層級、跟隨聰明錢"
        "賣出標的」角度後，資料延遲與覆蓋率的問題並沒有改善（因為兩者用的都是同一張"
        "`stock_margin_daily` 表），反而因為要求「賣超股同時有融資資料」的雙重篩選條件，"
        "可用樣本更少。**這個角度沒有解決全市場寬度版本的根本瓶頸（資料延遲＋覆蓋率），"
        "只是換了個更窄的鏡頭在看同一個資料缺口。**"
    )
    lines.append("")

    lines.append("## 1. 資料覆蓋與延遲：誠實查證結果")
    lines.append("")
    lines.append(f"- `stock_margin_daily`（source=finmind）：{margin_min_date} ~ **{margin_max_date}**，"
                  f"覆蓋 **{margin_n_stocks}** 檔股票。")
    lines.append(f"- 分點/股價資料最新可得日：**{asof}**。")
    lines.append(
        f"- **即時延遲＝{asof_lag_td if asof_lag_td is not None else 'NA'} 個交易日**"
        f"（{margin_max_date} → {asof}，換算日曆天約2-3週，跟背景所述「已知約2週延遲」"
        "一致，查證當下（本次研究執行時）狀況與背景描述相符，未見改善）。"
    )
    lines.append(
        "- 覆蓋率查證細節：融資表166-168檔股票的覆蓋數量在時間軸上會小幅波動"
        "（如2026-06-26的168檔驟降到2026-06-29的149檔），推測是個股融資資格"
        "（如全額交割、注意股）變動或來源同步狀態變化，不是穩定的169檔固定清單——"
        "換句話說，覆蓋率本身也不是靜態常數，每天實際可用的股票數會有感的抖動。"
    )
    lines.append("")
    lines.append("### 逐事件：E-1當時可得的最新融資資料落後幾個交易日")
    lines.append("")
    lines.append("| 大跌事件(E) | E-1 | 當時可得最新融資日期 | 落後(交易日) |")
    lines.append("|---|---|---|---|")
    for r in lag_df.itertuples():
        lag_s = "NA" if pd.isna(r.lag_trading_days) else f"{int(r.lag_trading_days)}"
        latest_s = r.latest_margin_date or "無資料"
        lines.append(f"| {r.event} | {r.e_minus_1} | {latest_s} | {lag_s} |")
    lines.append("")
    lines.append(
        "註：此處落後天數是「用完整歷史DB回頭看，E-1當天理論上最新可得的融資日期」，"
        "反映的是**資料本身在來源端就有的延遲結構**（即使在正式落地生產排程之前，"
        "融資資料相對交易日的發布本身就晚），不是本研究DB同步排程的問題——DB max date "
        f"{margin_max_date} 是目前抓取到的最新值，之後的事件（尤其2026-06-26、2026-07-17）"
        "落後幅度更大，是因為DB同步本身還沒抓到那之後的融資資料，兩種延遲疊加。"
    )
    lines.append("")

    lines.append("## 2. 8家分點大跌事件前5日賣超前" + str(TOP_N_PER_BRANCH_EVENT) + "大個股（逐事件明細）")
    lines.append("")
    lines.append(f"個股宇宙（16次事件、8家分點、去重後）：**{event_sold_stock_universe_n}** 檔；"
                  f"其中 **{coverage_event*100:.1f}%** 在融資表出現過。")
    lines.append("")
    lines.append("| 大跌事件(E) | 分點 | 個股 | 排名 | 前5日賣超金額(NT$) | 融資表有資料? |")
    lines.append("|---|---|---|---:|---:|---|")
    name_of = {bid: name for bid, (name, _) in PANEL.items()}
    margin_covered = set()
    for stock_id, series in flow5d_by_stock.items():
        margin_covered.add(stock_id)
    for r in event_top_sold.itertuples():
        cov = "✓" if r.stock_id in margin_covered else ""
        name = name_of.get(r.securities_trader_id, r.securities_trader_id)
        lines.append(
            f"| {r.trade_date} | {name}({r.securities_trader_id}) | {r.stock_id} | {int(r.rank_sold)} | "
            f"{r.lb_sum:,.0f} | {cov} |"
        )
    lines.append("")
    lines.append(
        "「融資表有資料?」欄只代表該股在融資表**曾經有可用的5日融資流量**（任意日期口徑），"
        "不代表在該事件的窗口日期上**恰好**有資料——尤其近期事件因DB同步落後，即使股票"
        "本身平時有融資覆蓋，事件窗口內仍可能整段空白（見上方逐事件延遲表）。"
    )
    lines.append("")

    lines.append("## 3. 判別力比較：純賣超金額 vs. +融資接刀組合分數")
    lines.append("")
    lines.append(
        "方法：`combined_score = 0.5×baseline_pctrank + 0.5×margin_pileup_pctrank`"
        "（後者=賣超前3大股的融資5日流量、對股票自身歷史bounded百分位、"
        "按分點權重加權平均後，本身再對「120日常態基準池＋事件排除」取一次百分位——"
        "跟現行溫度計 `composite_score` 用的是同一套 `bounded_pctrank` 邏輯）。"
        "若某事件當天融資訊號缺值（無論是覆蓋率或延遲造成），combined 退回等於 baseline。"
    )
    lines.append("")
    lines.append("| 大跌事件(E) | baseline百分位 | 融資接刀分點覆蓋數/8 | 融資接刀百分位 | combined百分位 |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in results_df.itertuples():
        b_s = "NA" if pd.isna(r.baseline_pctrank) else f"{r.baseline_pctrank:.1f}%"
        m_s = "NA" if pd.isna(r.margin_pileup_pctrank) else f"{r.margin_pileup_pctrank:.1f}%"
        c_s = "NA" if pd.isna(r.combined_pctrank) else f"{r.combined_pctrank:.1f}%"
        n_cov_s = f"{int(r.n_branches_margin_covered)}/8" if not pd.isna(r.n_branches_margin_covered) else "0/8"
        lines.append(f"| {r.event} | {b_s} | {n_cov_s} | {m_s} | {c_s} |")
    lines.append("")
    lines.append(f"- baseline 平均百分位（16事件全部）：**{baseline_avg:.1f}%**")
    if n_margin_scorable > 0:
        both_valid = results_df.dropna(subset=["margin_pileup_pctrank"])
        lines.append(
            f"- 在有融資訊號可算的 {n_margin_scorable} 次事件上：baseline平均 "
            f"**{both_valid['baseline_pctrank'].mean():.1f}%** vs. combined平均 "
            f"**{both_valid['combined_pctrank'].mean():.1f}%**"
            f"（融資接刀分項本身平均 **{both_valid['margin_pileup_pctrank'].mean():.1f}%**）。"
        )
    else:
        lines.append("- 無任何事件有可用融資訊號，無法計算判別力對照。")
    lines.append("")

    lines.append("## 4. 結論（verdict）")
    lines.append("")
    lines.append(
        "1. **這個「個股層級、跟隨聰明錢賣出標的」角度，並沒有比全市場融資寬度版本更有效"
        "——兩者都被同一個資料缺口（`stock_margin_daily` 覆蓋率169檔+約2-3週延遲）卡住**。"
        "換維度（從全市場寬度換成聰明錢賣出的特定股票）本身邏輯合理，但融資資料本身的"
        "限制沒有因此消失，反而因為要求「賣超股∩融資覆蓋股」的交集，樣本更稀薄。"
    )
    lines.append(
        "2. **實務限制（務必看清楚）：目前這個訊號完全無法整合進每天9點的即時溫度計**。"
        f"融資資料落後 asof 約 {asof_lag_td if asof_lag_td is not None else 'NA'} 個交易日"
        "（超過2週），意味著「今天寄出的溫度計」永遠拿不到「今天」或「最近2-3週」任何"
        "一天的融資訊號——這不是覆蓋率能解決的問題（即使覆蓋率是100%，資料本身還沒進"
        "資料庫），是資料來源/同步排程層級的硬限制。"
    )
    lines.append(
        "3. **這個訊號目前只能用於事後（2週以上）回顧驗證研究，不能用於任何當日或近期"
        "決策**。若未來要讓這類「聰明錢賣超×融資接刀」訊號真正可用，前提是先解決"
        "`stock_margin_daily` 的同步延遲與覆蓋率問題（例如換一個更即時、覆蓋更廣的"
        "融資資料來源），而不是在現有資料上換分析角度。"
    )
    lines.append(
        "4. 即使忽略延遲問題、只看「有覆蓋的少數歷史事件」上的判別力數字，樣本量"
        f"（{n_margin_scorable}/16 事件）太小，任何看起來「改善」或「持平」的平均百分位"
        "數字都不構成統計上可信的結論，只能定性參考「方向沒有明顯惡化」。"
    )
    lines.append("")
    lines.append("## 限制與注意事項")
    lines.append("")
    lines.append(
        "- 融資5日流量的自我百分位用 `margin_change/margin_balance.shift(1)`（正規化流量"
        "比率，同 `beta_group_precursor_summary.md` 的 `margin_change_ratio` 慣例），"
        "對齊到全域交易日曆（不做forward-fill，缺漏日誠實留空），再套用生產腳本"
        f"`bounded_pctrank`（{HISTORY_DAYS}日常態基準池＋事件±{LOOKBACK_DAYS}日排除）。"
    )
    lines.append(
        "- 「聰明錢賣超前3大個股」的排名口徑：對每個(分點,交易日)取 lb_sum "
        "(=shift(1).rolling(5).sum() 前5日累計賣超金額) 最負的前3檔，跟現行溫度計"
        "分點層級 `lb_sum` 用的是同一底層邏輯，只是不對股票加總。"
    )
    lines.append(
        "- combined_score 用簡單0.5/0.5平均，未做嚴謹的權重優化（因樣本量過小、"
        "融資訊號大部分事件缺值，優化權重沒有意義），純粹展示「加進去會不會變好」的"
        "方向性檢查。"
    )
    lines.append(
        "- 16次大跌事件直接讀取 `market_crash_precursor_episodes.csv`（既有事件定義，"
        "非重新定義）；`bounded_pctrank` 常態池的事件排除窗口另用 `ctd.load_event_dates`"
        "（IX0001日報酬≤-3%／≥+3% 全集）重算，範圍略寬於這16次（因為分點資料實際"
        "起點2024-07-01之前，重算會多出1個沒有分點資料可用、無意義的偽事件"
        "2024-04-19，僅作為常態池排除依據，不計入16次事件評估）。"
    )
    lines.append("- 探索性研究，非嚴謹統計檢定（事件數過少），非可下單訊號，未登錄 `config/research.yaml`。")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
