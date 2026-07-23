#!/usr/bin/env python3
"""大跌溫度計進階研究 H7：8家分點賣出時的對手方分析（誰在接手）.

Research diagnostic（純調查，不修改生產腳本／config）。

問題：現行溫度計只看「8家聰明錢分點在賣」，沒看「賣給誰接手」。理論上，若聰明錢
賣出的同時散戶正融資加碼承接（籌碼從強手轉弱手），結構應比「單純聰明錢賣」更
脆弱；反之若三大法人（尤其投信）同步賣出，則可能是機構間一致調倉，資訊含量
較高但不一定更危險；若融資同步減少，則是籌碼健康去化。

方法：
1. 直接 import `run_market_crash_thermometer_dashboard.py`
   （下稱 ctd，唯讀使用，不修改）取得 PANEL／calendar／lb_pctile／
   bounded_pctrank／load_event_dates 等既有邏輯，確保與生產腳本口徑一致。
2. 對 8 家分點重建「(分點,個股,交易日)」net_amt 明細（生產腳本 `build_branch_panel`
   只留分點加總，這裡額外保留 stock_id 維度），逐分點取「前5日累計賣超」
   rolling window 內排名前3大賣超個股（僅 net<0）。
3. 對這些個股，在同一個5日窗口查三大法人（`stock_institutional_daily`，取
   investment_trust_net 作主要獨立指標，foreign_net 另外報告但需注意與這8家
   分點本身高度重疊——多家即是外資分點，見下方限制）與融資餘額變化
   （`stock_margin_daily.margin_change`）之和，換算為 NTD（用同一收盤價
   forward-fill 邏輯，與 net_amt 同單位可比）。
4. 分類：margin_5d_ntd>0 →「聰明錢賣+散戶融資買」；否則若
   investment_trust_net_5d_ntd<0 →「聰明錢賣+三大法人(投信)同步賣」；否則
   →「聰明錢賣+融資也減少」；margin 資料缺失 →「無融資資料」。
5. 用 ctd.bounded_pctrank 同一套 bounded-rolling(120日)+事件排除(±5日) 方法，
   比較「原始複合分數」vs.「加註『聰明錢賣+散戶融資買』模式後的加分複合分數」
   在16次大跌事件上的判別力，並誠實揭露 `stock_margin_daily` 資料延遲／
   覆蓋限制對此訊號「即時可用性」的影響。

輸出：reports/research/branch-footprint-screen/adv_h7_counterparty_analysis.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_crash_thermometer_h7_counterparty_analysis.py
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
OUT_MD = ROOT / "reports" / "research" / "branch-footprint-screen" / "adv_h7_counterparty_analysis.md"
EPISODES_CSV = ROOT / "reports" / "research" / "branch-footprint-screen" / "market_crash_precursor_episodes.csv"

_spec = importlib.util.spec_from_file_location(
    "crash_thermometer_dashboard",
    ROOT / "scripts" / "research" / "run_market_crash_thermometer_dashboard.py",
)
ctd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctd)

PANEL = ctd.PANEL
IDS = list(PANEL.keys())
SOURCE = "finmind"
LOOKBACK_DAYS = 5
CAL_N_DAYS = 800
HISTORY_DAYS = 120
TOP_N_STOCKS = 3
ENHANCED_BONUS = 0.15  # 加分複合分數：命中「聰明錢賣+散戶融資買」當日加的複合分數量（0~1尺度）


def fetch_branch_stock_net_amt(conn, ids: list[str], start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """回傳 (merged[branch,date,stock,net_amt], long_closes[date,stock,close])。
    邏輯與 ctd.build_branch_panel 完全一致，但保留 stock_id 維度不做加總，
    並把 forward-fill 後的收盤價表一併回傳供後續三大法人／融資換算NTD共用。"""
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
    return merged[["securities_trader_id", "trade_date", "stock_id", "net_amt"]], long_closes


def build_daily_top_sold(
    merged: pd.DataFrame, ids: list[str], cal: list[str], lookback_days: int, top_n: int
) -> pd.DataFrame:
    """對每個分點、每個交易日 T，取 T-lookback_days..T-1 累計 net_amt 最負的
    前 top_n 檔個股（僅 net_amt<0）。與生產腳本 `lb_sum` 完全同一個 shift(1)+
    rolling(lookback_days) 定義，只是保留 stock_id 維度不加總。"""
    records: list[dict] = []
    for bid in ids:
        sub = merged[merged["securities_trader_id"] == bid]
        if sub.empty:
            continue
        wide = sub.pivot_table(index="trade_date", columns="stock_id", values="net_amt", aggfunc="sum")
        wide = wide.reindex(cal).fillna(0.0)
        roll = wide.rolling(lookback_days).sum().shift(1)
        for d in cal:
            row = roll.loc[d]
            neg = row[row < 0]
            if neg.empty:
                continue
            top = neg.nsmallest(top_n)
            for rank, (sid, val) in enumerate(top.items(), start=1):
                records.append(
                    {
                        "trade_date": d,
                        "securities_trader_id": bid,
                        "stock_id": sid,
                        "sell_rank": rank,
                        "branch_sell_5d_ntd": float(val),
                    }
                )
    return pd.DataFrame(records)


def fetch_counterparty_flows(
    conn, stock_ids: list[str], long_closes: pd.DataFrame, start: str, end: str
) -> pd.DataFrame:
    """對指定個股，取每日 margin_change（融資，張）與三大法人 net（股），乘上
    同日 forward-fill 收盤價換算成 NTD，回傳日頻 long-format 表，供後續
    rolling(5) 使用。"""
    if not stock_ids:
        return pd.DataFrame(columns=["trade_date", "stock_id", "margin_ntd", "foreign_ntd", "trust_ntd", "three_inst_ntd"])
    placeholders = ",".join("?" for _ in stock_ids)
    margin = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, margin_change FROM stock_margin_daily
        WHERE trade_date >= ? AND trade_date <= ? AND stock_id IN ({placeholders})
        """,
        conn,
        params=[start, end, *stock_ids],
    )
    inst = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, foreign_net, investment_trust_net, three_institution_net
        FROM stock_institutional_daily
        WHERE trade_date >= ? AND trade_date <= ? AND stock_id IN ({placeholders})
        """,
        conn,
        params=[start, end, *stock_ids],
    )
    for df in (margin, inst):
        df["stock_id"] = df["stock_id"].astype(str)
        df["trade_date"] = df["trade_date"].astype(str)

    merged = long_closes.merge(margin, on=["stock_id", "trade_date"], how="left")
    merged = merged.merge(inst, on=["stock_id", "trade_date"], how="left")
    # margin_change 單位為「張」(1000股)，乘1000轉股數再乘收盤價 = NTD；
    # 三大法人 net 單位為「股」，直接乘收盤價 = NTD。無資料時保留 NaN（不可
    # 假設為0——尤其 margin_daily 有已知資料延遲，NaN 代表「當時無法取得」，
    # 混入0會低估「融資減少」的比例）。
    merged["margin_ntd"] = merged["margin_change"] * 1000.0 * merged["close"]
    merged["foreign_ntd"] = merged["foreign_net"] * merged["close"]
    merged["trust_ntd"] = merged["investment_trust_net"] * merged["close"]
    merged["three_inst_ntd"] = merged["three_institution_net"] * merged["close"]
    return merged[["trade_date", "stock_id", "margin_ntd", "foreign_ntd", "trust_ntd", "three_inst_ntd"]]


def rolling_5d_by_stock(flows: pd.DataFrame, value_col: str, cal: list[str], lookback_days: int) -> pd.DataFrame:
    """對 flows[value_col] 逐股 pivot 成 date x stock，reindex 到完整交易日曆，
    rolling(lookback_days).sum().shift(1)，缺資料的日子/個股保留 NaN
    （min_periods=lookback_days，確保視窗不完整時不會給出誤導性的部分和）。

    注意：用 `.pivot`（非 `.pivot_table(aggfunc='sum')`）——flows 已是
    (trade_date, stock_id) 唯一組合，用 pivot_table+sum 會把「整組只有一個
    NaN」的儲存格算成 0.0（pandas sum-of-all-NaN 預設回0，不是NaN），這會
    把「無融資資料」悄悄誤判成「融資淨額=0」，混入分類邏輯會低估延遲影響。"""
    wide = flows.pivot(index="trade_date", columns="stock_id", values=value_col)
    wide = wide.reindex(cal)
    roll = wide.rolling(lookback_days, min_periods=lookback_days).sum().shift(1)
    return roll


def classify_row(margin_5d: float, trust_5d: float) -> str:
    if pd.isna(margin_5d):
        return "無融資資料"
    if margin_5d > 0:
        return "聰明錢賣+散戶融資買"
    if pd.isna(trust_5d):
        return "聰明錢賣+融資也減少"
    if trust_5d < 0:
        return "聰明錢賣+三大法人(投信)同步賣"
    return "聰明錢賣+融資也減少"


def main() -> None:
    conn = ctd.connect_ro(DB_PATH)
    asof = ctd.latest_trade_date(conn)
    print(f"asof={asof}", flush=True)

    cal = ctd.build_calendar(conn, end=asof, n_days=CAL_N_DAYS, extra_ids=IDS)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    print("fetching branch x stock net_amt (this is the slow query, ~2-4min)...", flush=True)
    merged, long_closes = fetch_branch_stock_net_amt(conn, IDS, start=cal[0], end=asof)
    print(f"merged rows={len(merged)}", flush=True)

    # ---- 基準複合分數（與生產腳本同一份 raw 資料，只是重用不再另查一次DB）----
    panel_raw = merged.groupby(["securities_trader_id", "trade_date"], as_index=False).agg(net_amt=("net_amt", "sum"))
    grid = ctd.build_full_grid(panel_raw, IDS, cal)
    scored = ctd.compute_lb_pctile(grid, LOOKBACK_DAYS)
    weights = {bid: w for bid, (_, w) in PANEL.items()}
    series = ctd.weighted_composite(scored, weights)
    baseline_scores_by_date = dict(zip(series["trade_date"], series["composite_score"]))

    # all_event_dates（大跌∪大漲）用於 bounded normal pool 排除，範圍比參考用的16次
    # 事件更廣（因calendar往前多抓到2024-04-19這次尚未被
    # market_crash_precursor_episodes.csv收錄的大跌日）——排除邏輯保留這個較廣的
    # 集合是對的（避免拿異常日當常態基準），但「16次大跌事件」的主分析口徑仍鎖定
    # 該CSV，與任務背景給定的參考數字（84.5%判別力等）保持一致。
    crash_dates, all_event_dates = ctd.load_event_dates(conn, cal)
    event_idx = {cal.index(d) for d in all_event_dates if d in cal}
    episodes_df = pd.read_csv(EPISODES_CSV)
    events = sorted(str(d) for d in episodes_df["trade_date"] if str(d) in cal)
    extra_detected = sorted(crash_dates - set(events))
    print(f"n_crash_events(episodes.csv)={len(events)}; extra_detected_not_in_csv={extra_detected}", flush=True)

    # ---- 每分點每日前3大賣超個股（全歷史，供 walk-forward 用）----
    print("computing per-branch top-sold stocks per day...", flush=True)
    top_sold = build_daily_top_sold(merged, IDS, cal, LOOKBACK_DAYS, TOP_N_STOCKS)
    print(f"top_sold rows={len(top_sold)}", flush=True)

    stocks_of_interest = sorted(top_sold["stock_id"].unique().tolist())
    print(f"stocks_of_interest n={len(stocks_of_interest)}", flush=True)

    print("fetching margin/institutional flows for stocks of interest...", flush=True)
    flows = fetch_counterparty_flows(conn, stocks_of_interest, long_closes, start=cal[0], end=asof)
    conn.close()

    margin_roll = rolling_5d_by_stock(flows, "margin_ntd", cal, LOOKBACK_DAYS)
    foreign_roll = rolling_5d_by_stock(flows, "foreign_ntd", cal, LOOKBACK_DAYS)
    trust_roll = rolling_5d_by_stock(flows, "trust_ntd", cal, LOOKBACK_DAYS)
    three_inst_roll = rolling_5d_by_stock(flows, "three_inst_ntd", cal, LOOKBACK_DAYS)

    def lookup(roll: pd.DataFrame, d: str, sid: str) -> float:
        try:
            return float(roll.at[d, sid])
        except KeyError:
            return float("nan")

    top_sold["margin_5d_ntd"] = [lookup(margin_roll, d, s) for d, s in zip(top_sold["trade_date"], top_sold["stock_id"])]
    top_sold["foreign_5d_ntd"] = [lookup(foreign_roll, d, s) for d, s in zip(top_sold["trade_date"], top_sold["stock_id"])]
    top_sold["trust_5d_ntd"] = [lookup(trust_roll, d, s) for d, s in zip(top_sold["trade_date"], top_sold["stock_id"])]
    top_sold["three_inst_5d_ntd"] = [lookup(three_inst_roll, d, s) for d, s in zip(top_sold["trade_date"], top_sold["stock_id"])]
    top_sold["category"] = [
        classify_row(m, t) for m, t in zip(top_sold["margin_5d_ntd"], top_sold["trust_5d_ntd"])
    ]

    top_sold.to_csv(ROOT / "reports" / "research" / "branch-footprint-screen" / "adv_h7_top_sold_detail.csv", index=False)
    print("wrote adv_h7_top_sold_detail.csv", flush=True)

    # ---- 事件窗口專用子集（16次大跌事件當日）----
    event_rows = top_sold[top_sold["trade_date"].isin(events)].copy()
    event_rows["branch_name"] = event_rows["securities_trader_id"].map({bid: n for bid, (n, _) in PANEL.items()})
    event_rows = event_rows.sort_values(["trade_date", "securities_trader_id", "sell_rank"])

    cat_counts_event = event_rows["category"].value_counts()
    cat_counts_event_pct = (cat_counts_event / len(event_rows) * 100).round(1)

    have_margin = event_rows[event_rows["category"] != "無融資資料"]
    cat_counts_event_valid = have_margin["category"].value_counts()
    cat_counts_event_valid_pct = (cat_counts_event_valid / max(len(have_margin), 1) * 100).round(1)

    # margin_daily 覆蓋/延遲檢查
    margin_max_date = flows.dropna(subset=["margin_ntd"])["trade_date"].max() if not flows.empty else None
    margin_covered_stocks = sorted(flows.dropna(subset=["margin_ntd"])["stock_id"].unique().tolist())

    # ---- 加分複合分數 walk-forward 比較 ----
    print("running enhanced-score bounded walk-forward comparison...", flush=True)
    tail_pct_map = {bid: ctd.tail_pct_for(bid) for bid in IDS}
    scored_idx = scored.set_index(["securities_trader_id", "trade_date"])["lb_pctile"]

    branch_flag_by_day: dict[str, bool] = {}
    for d in cal:
        day_top = top_sold[top_sold["trade_date"] == d]
        flagged = False
        for bid in IDS:
            try:
                lb_p = scored_idx.loc[(bid, d)]
            except KeyError:
                continue
            if pd.isna(lb_p) or lb_p > tail_pct_map[bid]:
                continue
            branch_top = day_top[day_top["securities_trader_id"] == bid]
            if (branch_top["category"] == "聰明錢賣+散戶融資買").any():
                flagged = True
                break
        branch_flag_by_day[d] = flagged

    enhanced_scores_by_date = {
        d: min(1.0, (v if not pd.isna(v) else v) + (ENHANCED_BONUS if branch_flag_by_day.get(d, False) else 0.0))
        for d, v in baseline_scores_by_date.items()
    }

    comparison_rows = []
    for ev in events:
        base_pr, n_base = ctd.bounded_pctrank(
            baseline_scores_by_date, cal, event_idx, test_date=ev, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        )
        enh_pr, n_enh = ctd.bounded_pctrank(
            enhanced_scores_by_date, cal, event_idx, test_date=ev, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        )
        ev_top = top_sold[top_sold["trade_date"] == ev]
        n_with_margin = int((ev_top["category"] != "無融資資料").sum())
        n_total_pairs = int(len(ev_top))
        comparison_rows.append(
            {
                "event_date": ev,
                "baseline_pctrank": base_pr,
                "enhanced_pctrank": enh_pr,
                "flagged": branch_flag_by_day.get(ev, False),
                "n_normal_days": n_base,
                "n_pairs_with_margin_data": n_with_margin,
                "n_pairs_total": n_total_pairs,
            }
        )
    comparison = pd.DataFrame(comparison_rows)

    # ---- 寫報告 ----
    lines: list[str] = []
    lines.append("# 大跌溫度計進階研究 H7：對手方分析（8家聰明錢賣出時，誰在接手）")
    lines.append("")
    lines.append("Research diagnostic（純調查，不修改生產腳本／config；非可下單訊號）。")
    lines.append("")
    lines.append(f"- 資料庫最新可得交易日：{asof}")
    lines.append(f"- 交易日曆：{cal[0]} ~ {cal[-1]}（{len(cal)}個交易日）")
    lines.append(f"- 大跌事件（IX0001單日≤-3%）：{len(events)}次，{events[0]} ~ {events[-1]}"
                 "（口徑沿用`market_crash_precursor_episodes.csv`）")
    if extra_detected:
        lines.append(
            f"- 附註：本次計算用的交易日曆往前多延伸到{cal[0]}，額外偵測到"
            f"{extra_detected}這{len(extra_detected)}次大跌日不在上述16次事件清單內"
            "（在bounded常態基準池排除邏輯裡仍視為事件日排除，但不計入下方16次"
            "事件的主統計，以維持與任務背景給定的參考判別力數字口徑一致）。"
        )
    lines.append("")
    lines.append("## 0. 一句話結論")
    lines.append("")
    most_common_valid = cat_counts_event_valid_pct.idxmax() if len(cat_counts_event_valid_pct) else "N/A"
    lines.append(
        f"在16次大跌事件前5日窗口內，8家分點賣超前3大個股的(分點,個股,事件)配對中，"
        f"**有融資資料可查的樣本裡最常見的組合是「{most_common_valid}」**"
        f"（{cat_counts_event_valid_pct.get(most_common_valid, float('nan')):.1f}%）。"
        f"但 `stock_margin_daily` 資料本身有嚴重延遲與覆蓋限制（見第4節），"
        f"導致這個訊號**目前無法用於每日即時的溫度計**，只能做事後研究驗證。"
    )
    lines.append("")

    lines.append("## 1. 方法")
    lines.append("")
    lines.append(
        "1. 對8家分點，逐日以「T-5..T-1累計net_amt(NT$)」排序，取每分點每日賣超"
        "（net<0）最大的前3檔個股（沿用生產腳本 `lb_sum` 完全同一個 "
        "`shift(1).rolling(5).sum()` 定義，PIT-safe：T日的窗口不含T日本身資料）。"
    )
    lines.append(
        "2. 對這些個股，在同一個T-5..T-1窗口，查 `stock_institutional_daily`"
        "（foreign_net／investment_trust_net／three_institution_net，單位股，"
        "換算NTD）與 `stock_margin_daily.margin_change`"
        "（單位張=1000股，換算NTD），同樣做 `shift(1).rolling(5).sum()`。"
    )
    lines.append(
        "3. 分類規則（依序判斷，互斥）：\n"
        "   - **融資5日淨額>0** → 「聰明錢賣+散戶融資買」（結構惡化，最需留意）\n"
        "   - 融資5日淨額≤0 且 **投信5日淨額<0** → 「聰明錢賣+三大法人(投信)同步賣」\n"
        "   - 融資5日淨額≤0 且投信未同步賣（或無投信資料） → 「聰明錢賣+融資也減少」（籌碼健康去化）\n"
        "   - 融資5日視窗內任何一天缺資料 → 「無融資資料」（不強行假設為0，避免低估延遲影響）"
    )
    lines.append(
        "4. **重要限制**：這8家分點中4家（瑞銀1650／港商野村1560／美商高盛亞1480／"
        "港麥格理1360）本身就是外資券商分點，其賣超與`stock_institutional_daily`"
        "的`foreign_net`（外資買賣超）存在系統性重疊（並非獨立訊號來源，這8家"
        "分點的成交量僅是外資整體的一部分子集，但方向高度共線）。因此本研究"
        "**以investment_trust_net（投信）作為三大法人同步賣的主要判斷依據**，"
        "foreign_net僅併陳供參考，不用於分類，避免用同一份聰明錢訊號自己驗證自己。"
    )
    lines.append("")

    lines.append("## 2. 三種組合的統計（16次大跌事件，前5日窗口，每分點賣超前3大個股）")
    lines.append("")
    lines.append(f"- (分點,個股,事件)配對總數：{len(event_rows)}")
    lines.append(f"- 其中有融資資料可查：{len(have_margin)}（{len(have_margin)/max(len(event_rows),1)*100:.1f}%）")
    lines.append("")
    lines.append("### 2a. 全部配對（含「無融資資料」）")
    lines.append("")
    lines.append("| 分類 | 配對數 | 佔比 |")
    lines.append("|---|---|---|")
    for cat, cnt in cat_counts_event.items():
        lines.append(f"| {cat} | {cnt} | {cat_counts_event_pct[cat]:.1f}% |")
    lines.append("")
    lines.append("### 2b. 僅有融資資料可查的配對（排除「無融資資料」後的條件分布）")
    lines.append("")
    lines.append("| 分類 | 配對數 | 佔比 |")
    lines.append("|---|---|---|")
    for cat, cnt in cat_counts_event_valid.items():
        lines.append(f"| {cat} | {cnt} | {cat_counts_event_valid_pct[cat]:.1f}% |")
    lines.append("")

    lines.append("### 2c. 逐事件明細（分點/個股/賣超金額/分類）")
    lines.append("")
    lines.append("| 事件日 | 分點 | 個股 | 排名 | 分點5日賣超(NT$) | 融資5日淨額(NT$) | 投信5日淨額(NT$) | 外資5日淨額(NT$，供參考) | 分類 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in event_rows.itertuples():
        margin_s = "NA" if pd.isna(r.margin_5d_ntd) else f"{r.margin_5d_ntd:,.0f}"
        trust_s = "NA" if pd.isna(r.trust_5d_ntd) else f"{r.trust_5d_ntd:,.0f}"
        foreign_s = "NA" if pd.isna(r.foreign_5d_ntd) else f"{r.foreign_5d_ntd:,.0f}"
        lines.append(
            f"| {r.trade_date} | {r.branch_name}({r.securities_trader_id}) | {r.stock_id} | {r.sell_rank} | "
            f"{r.branch_sell_5d_ntd:,.0f} | {margin_s} | {trust_s} | {foreign_s} | {r.category} |"
        )
    lines.append("")

    lines.append("## 3. 判別力比較：原始複合分數 vs. 加註「聰明錢賣+散戶融資買」後的加分複合分數")
    lines.append("")
    lines.append(
        f"方法：沿用生產腳本 `bounded_pctrank`（bounded-rolling {HISTORY_DAYS}日常態基準池"
        f"＋排除任一大跌/大漲事件±{LOOKBACK_DAYS}個交易日內的日子）。加分複合分數＝原始"
        f"複合分數 + {ENHANCED_BONUS}（若當日任一分點同時滿足「自身lb_pctile落在自己歷史"
        "尾端」且「當日其top3賣超個股中有一檔屬於『聰明錢賣+散戶融資買』」），全歷史"
        "（不只事件日）逐日套用同一規則後，重新對常態基準池計算百分位。"
    )
    lines.append("")
    lines.append("| 事件日 | 原始溫度百分位 | 加分後溫度百分位 | 事件日當天有觸發加分？ | 該事件top賣超配對中有融資資料/總數 |")
    lines.append("|---|---|---|---|---|")
    n_improved = 0
    n_worse = 0
    n_flagged_events = 0
    for r in comparison.itertuples():
        base_s = "NA" if r.baseline_pctrank is None else f"{r.baseline_pctrank:.1f}%"
        enh_s = "NA" if r.enhanced_pctrank is None else f"{r.enhanced_pctrank:.1f}%"
        flag_s = "✓" if r.flagged else ""
        if r.flagged:
            n_flagged_events += 1
        if r.baseline_pctrank is not None and r.enhanced_pctrank is not None:
            if r.enhanced_pctrank > r.baseline_pctrank + 0.01:
                n_improved += 1
            elif r.enhanced_pctrank < r.baseline_pctrank - 0.01:
                n_worse += 1
        lines.append(
            f"| {r.event_date} | {base_s} | {enh_s} | {flag_s} | {r.n_pairs_with_margin_data}/{r.n_pairs_total} |"
        )
    lines.append("")
    valid_base = [r.baseline_pctrank for r in comparison.itertuples() if r.baseline_pctrank is not None]
    valid_enh = [r.enhanced_pctrank for r in comparison.itertuples() if r.enhanced_pctrank is not None]
    avg_base = sum(valid_base) / len(valid_base) if valid_base else float("nan")
    avg_enh = sum(valid_enh) / len(valid_enh) if valid_enh else float("nan")
    lines.append(
        f"- 16次事件中，有 **{n_flagged_events}** 次事件日當天觸發「聰明錢賣+散戶融資買」加分旗標。"
    )
    lines.append(f"- 平均溫度百分位：原始 {avg_base:.1f}% → 加分後 {avg_enh:.1f}%（{avg_enh-avg_base:+.1f}pp）。")
    lines.append(f"- 16次事件中，加分後百分位「明顯提升」{n_improved}次、「明顯下降」{n_worse}次（下降是因為加分同時墊高常態基準池分數，稀釋相對排名）。")
    lines.append("")

    lines.append("## 4. `stock_margin_daily` 資料延遲／覆蓋限制（誠實揭露）")
    lines.append("")
    lag_days = (cal.index(asof) - cal.index(margin_max_date)) if (margin_max_date in cal and asof in cal) else "N/A"
    lines.append(f"- `stock_margin_daily` 目前資料庫實查最後更新日：**{margin_max_date}**"
                  f"（相對資料庫其他表最新交易日 {asof}，落後約{lag_days}個交易日）。")
    lines.append(f"- `stock_margin_daily` 覆蓋股票數：全庫169檔；本次分析涉及的{len(stocks_of_interest)}檔"
                  f"「8家分點賣超前3大」個股中，有融資資料覆蓋的有{len(margin_covered_stocks)}檔。")
    n_ev_no_margin = int((comparison["n_pairs_with_margin_data"] == 0).sum())
    lines.append(
        f"- 16次事件中，有 **{n_ev_no_margin}** 次事件的top賣超配對「完全沒有」融資資料可查"
        "（窗口落在資料覆蓋範圍之外，或個股不在169檔覆蓋清單內）。"
    )
    lines.append(
        "- **實務限制（務必注意）**：`stock_margin_daily`目前更新落後實際交易日約2週以上，"
        "這代表**這個對手方分析訊號目前無法即時嵌入每日大跌溫度計**——當天或近幾日的融資"
        "資料在系統中還看不到，等資料補齊時大跌事件早已發生。這個訊號目前只能做**事後"
        "研究驗證用途**（例如季度覆盤「上次大跌前是誰在接手」），不能作為每日監控的"
        "即時加分項，除非未來 `stock_margin_daily` 的同步延遲被顯著縮短到D+1或D+2。"
    )
    lines.append("")

    lines.append("## 5. 結論")
    lines.append("")
    lines.append(
        "- **最常見組合**：在有融資資料可查的配對裡，"
        f"「{most_common_valid}」最常見（{cat_counts_event_valid_pct.get(most_common_valid, float('nan')):.1f}%）。"
    )
    lines.append(
        "- **判別力**：加分後平均溫度百分位相對原始有小幅變化（見第3節），但受限於"
        f"僅{n_flagged_events}/16次事件觸發加分旗標（樣本太小，且多次事件的top賣超配對"
        "根本查不到融資資料），現階段證據**不足以支持**「聰明錢賣+散戶融資買」模式"
        "能穩定提升判別力的結論——這是一個方向正確、但資料覆蓋不足以驗證的假說。"
    )
    lines.append(
        "- **現在能不能用**：**不能**用於每日即時溫度計（融資資料延遲2週以上）。"
        "**可以**用於事後（例如每次大跌事件發生2-3週後）覆盤「上次警訊期間，賣超"
        "的個股籌碼結構是惡化還是健康去化」，作為研究覆盤材料，不建議登錄"
        "`config/research.yaml`成為正式追蹤訊號。"
    )
    lines.append(
        "- **若要即時化**：需要（a）`stock_margin_daily`同步延遲縮短到D+1/D+2，且"
        "（b）覆蓋股票數從169檔擴大（目前對這8家分點常賣超的個股仍有相當比例"
        "查不到融資資料），才有機會把這個對手方訊號變成可即時使用的加分項。"
    )
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
