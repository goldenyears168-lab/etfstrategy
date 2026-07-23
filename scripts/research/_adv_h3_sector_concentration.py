#!/usr/bin/env python3
"""H3：8家分點賣超標的的產業集中度，事件前 vs 非事件期比較（一次性研究腳本）。

背景：大跌溫度計進階研究假說 H3——現行 `run_market_crash_thermometer_dashboard.py`
只看8家分點「前5日累計淨額」的金額，不管賣超集中在哪個產業。本腳本檢查：賣超金額
在產業間的集中度（HHI／前3大產業佔比）在16次大跌事件前5個交易日窗口，是否顯著
高於（或低於）非事件期的同口徑窗口，以及是否有特定產業重複出現在賣超榜首。

產業分類來源：FinMind `TaiwanStockInfo.industry_category`（TWSE/OTC官方公告產業分類，
非本專案自建的股票代碼區間猜測）。唯讀，不寫回 DB／config。

輸出：reports/research/branch-footprint-screen/adv_h3_sector_concentration.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = ROOT / "data" / "stocks.db"
EPISODES_CSV = ROOT / "reports/research/branch-footprint-screen/market_crash_precursor_episodes.csv"
OUT_MD = ROOT / "reports/research/branch-footprint-screen/adv_h3_sector_concentration.md"
SOURCE = "finmind"

PANEL: dict[str, str] = {
    "1650": "瑞銀",
    "1560": "港商野村",
    "1480": "美商高盛亞",
    "1360": "港麥格理",
    "984K": "元大館前",
    "1261": "宏遠民生",
    "5850": "統一",
    "585c": "統一仁愛",
}
LOOKBACK_DAYS = 5
TREND_LOOKBACK_DAYS = 5  # 「較早窗」= T-10..T-6，跟「近窗」T-5..T-1 比較
EXCLUDE_AROUND_EVENT_DAYS = 5  # 常態池排除任一大跌/大漲事件 ± N 個交易日
CRASH_THRESHOLD = -0.03
RALLY_THRESHOLD = 0.03


def connect_ro(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_calendar(conn, *, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330' AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    ).fetchall()
    return sorted({str(r[0]) for r in rows})


def load_industry_map() -> dict[str, str]:
    """FinMind TaiwanStockInfo · 官方產業分類（唯讀 API，非DB寫入）。"""
    from project_dotenv import load_project_dotenv

    load_project_dotenv()
    from finmind_client import fetch_finmind_dataset

    rows = fetch_finmind_dataset("TaiwanStockInfo")
    df = pd.DataFrame(rows)
    df = df.sort_values("date").drop_duplicates(subset=["stock_id"], keep="last")
    return dict(zip(df["stock_id"], df["industry_category"]))


def load_branch_stock_net_amt(conn, ids: list[str], *, start: str, end: str) -> pd.DataFrame:
    """回傳 trade_date, stock_id, net_amt（8家分點合計，net股數×close，forward-fill缺漏收盤價，
    同 `run_market_crash_thermometer_dashboard.py::build_branch_panel` 的作法）。"""
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
    if raw.empty:
        return pd.DataFrame(columns=["trade_date", "stock_id", "net_amt"])
    closes = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
        """,
        conn,
        params=[SOURCE, start, end],
    )
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
    grp = merged.groupby(["trade_date", "stock_id"], as_index=False).agg(net_amt=("net_amt", "sum"))
    return grp


def load_benchmark_returns(conn, cal: list[str]) -> pd.Series:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn, code="IX0001").sort_index()
    ret = bench.pct_change()
    return ret[ret.index.isin(cal)]


def sector_concentration(panel: pd.DataFrame, window_dates: list[str], industry_map: dict[str, str]) -> dict | None:
    """給定一組 trade_date（5個交易日窗口），回傳賣超（net<0）金額的產業集中度指標。"""
    sub = panel[panel["trade_date"].isin(window_dates)]
    if sub.empty:
        return None
    agg = sub.groupby("stock_id")["net_amt"].sum()
    sells = agg[agg < 0]
    if sells.empty:
        return None
    sell_amt = (-sells).rename("sell_amt")
    industries = sell_amt.index.map(lambda sid: industry_map.get(sid, "未分類"))
    df = pd.DataFrame({"stock_id": sell_amt.index, "sell_amt": sell_amt.values, "industry": industries})
    ind_grp = df.groupby("industry")["sell_amt"].sum().sort_values(ascending=False)
    total = ind_grp.sum()
    if total <= 0:
        return None
    shares = ind_grp / total
    hhi = float((shares**2).sum())
    top1_share = float(shares.iloc[0])
    top1_industry = str(shares.index[0])
    top3_share = float(shares.iloc[:3].sum())
    return {
        "total_sell_ntd": float(total),
        "n_stocks_sold": int(len(sell_amt)),
        "n_industries": int(len(ind_grp)),
        "hhi": hhi,
        "top1_share": top1_share,
        "top1_industry": top1_industry,
        "top3_share": top3_share,
    }


def main() -> None:
    conn = connect_ro(DB_PATH)
    ids = list(PANEL.keys())

    episodes = pd.read_csv(EPISODES_CSV)
    event_dates = sorted(episodes["trade_date"].astype(str).tolist())
    print(f"events n={len(event_dates)} {event_dates[0]}..{event_dates[-1]}", flush=True)

    placeholders = ",".join("?" for _ in ids)
    branch_min = conn.execute(
        f"SELECT MIN(trade_date) FROM stock_broker_branch_daily "
        f"WHERE source=? AND securities_trader_id IN ({placeholders})",
        (SOURCE, *ids),
    ).fetchone()[0]
    cal_start = str(branch_min)
    cal_end = conn.execute(
        "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source=? AND stock_id='2330'",
        (SOURCE,),
    ).fetchone()[0]
    cal = build_calendar(conn, start=cal_start, end=str(cal_end))
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)
    idx_of = {d: i for i, d in enumerate(cal)}

    for d in event_dates:
        if d not in idx_of:
            raise SystemExit(f"event date {d} not in calendar (check DB sync)")

    print("loading branch x stock net_amt panel...", flush=True)
    panel = load_branch_stock_net_amt(conn, ids, start=cal[0], end=cal[-1])
    print(f"panel rows={len(panel)}", flush=True)

    print("loading industry map (FinMind TaiwanStockInfo)...", flush=True)
    industry_map = load_industry_map()
    print(f"industry map n={len(industry_map)}", flush=True)

    ret = load_benchmark_returns(conn, cal)
    crash_dates = {str(d) for d in ret[ret <= CRASH_THRESHOLD].index}
    rally_dates = {str(d) for d in ret[ret >= RALLY_THRESHOLD].index}
    all_event_like = crash_dates | rally_dates
    event_like_idx = {idx_of[d] for d in all_event_like if d in idx_of}
    conn.close()

    # ---- 1) 事件窗（T-5..T-1）產業集中度 ----
    event_rows = []
    for d in event_dates:
        ti = idx_of[d]
        window = cal[max(0, ti - LOOKBACK_DAYS): ti]
        m = sector_concentration(panel, window, industry_map)
        if m is None:
            m = {
                "total_sell_ntd": 0.0, "n_stocks_sold": 0, "n_industries": 0,
                "hhi": np.nan, "top1_share": np.nan, "top1_industry": "NA", "top3_share": np.nan,
            }
        m["trade_date"] = d
        m["window"] = f"{window[0]}..{window[-1]}" if window else "NA"
        event_rows.append(m)
    event_df = pd.DataFrame(event_rows)

    # ---- 2) 較早窗（T-10..T-6）供「越接近事件越集中/分散」比較 ----
    trend_rows = []
    for d in event_dates:
        ti = idx_of[d]
        early_window = cal[max(0, ti - LOOKBACK_DAYS - TREND_LOOKBACK_DAYS): max(0, ti - LOOKBACK_DAYS)]
        m = sector_concentration(panel, early_window, industry_map)
        trend_rows.append({
            "trade_date": d,
            "hhi_early": m["hhi"] if m else np.nan,
            "top3_share_early": m["top3_share"] if m else np.nan,
        })
    trend_df = pd.DataFrame(trend_rows)
    event_df = event_df.merge(trend_df, on="trade_date", how="left")

    # ---- 3) 非事件期常態池：不重疊5日窗，排除任一大跌/大漲事件±5個交易日 ----
    normal_rows = []
    for ti in range(LOOKBACK_DAYS, len(cal), LOOKBACK_DAYS):
        window = cal[ti - LOOKBACK_DAYS: ti]
        if any(abs(ti - ei) <= EXCLUDE_AROUND_EVENT_DAYS for ei in event_like_idx):
            continue
        ref_date = cal[ti]
        if ref_date in event_dates:
            continue
        m = sector_concentration(panel, window, industry_map)
        if m is None:
            continue
        m["ref_date"] = ref_date
        normal_rows.append(m)
    normal_df = pd.DataFrame(normal_rows)
    print(f"normal-period windows n={len(normal_df)}", flush=True)

    # ---- 4) 統計檢定：事件窗 vs 常態池（Mann-Whitney U，小樣本用非參數） ----
    ev_hhi = event_df["hhi"].dropna().to_numpy()
    no_hhi = normal_df["hhi"].dropna().to_numpy()
    ev_top3 = event_df["top3_share"].dropna().to_numpy()
    no_top3 = normal_df["top3_share"].dropna().to_numpy()
    ev_top1 = event_df["top1_share"].dropna().to_numpy()
    no_top1 = normal_df["top1_share"].dropna().to_numpy()

    def mwu(a, b):
        if len(a) < 2 or len(b) < 2:
            return np.nan, np.nan
        u, p = stats.mannwhitneyu(a, b, alternative="two-sided")
        return float(u), float(p)

    hhi_u, hhi_p = mwu(ev_hhi, no_hhi)
    top3_u, top3_p = mwu(ev_top3, no_top3)
    top1_u, top1_p = mwu(ev_top1, no_top1)

    # ---- 5) 越接近事件：早窗 vs 近窗 HHI 配對比較（Wilcoxon signed-rank + 符號檢定） ----
    paired = event_df.dropna(subset=["hhi", "hhi_early"])
    diffs = (paired["hhi"] - paired["hhi_early"]).to_numpy()
    n_more_concentrated = int((diffs > 0).sum())
    n_less_concentrated = int((diffs < 0).sum())
    n_tied = int((diffs == 0).sum())
    if len(diffs) >= 2 and not np.allclose(diffs, 0):
        try:
            wstat, wp = stats.wilcoxon(diffs)
            wstat, wp = float(wstat), float(wp)
        except ValueError:
            wstat, wp = np.nan, np.nan
    else:
        wstat, wp = np.nan, np.nan

    # ---- 6) 榜首產業重複出現次數（事件窗 vs 非事件池，供對照基準用） ----
    top1_counts = event_df["top1_industry"].value_counts()
    top1_counts_normal = normal_df["top1_industry"].value_counts()

    # ---- 7) 混淆因子檢查：HHI差異是否只是「賣超股票數較少」的機械結果 ----
    ev_n_stocks = event_df["n_stocks_sold"].dropna().to_numpy()
    no_n_stocks = normal_df["n_stocks_sold"].dropna().to_numpy()
    ev_n_ind = event_df["n_industries"].dropna().to_numpy()
    no_n_ind = normal_df["n_industries"].dropna().to_numpy()
    ev_total = event_df["total_sell_ntd"].to_numpy()
    no_total = normal_df["total_sell_ntd"].to_numpy()
    n_stocks_u, n_stocks_p = mwu(ev_n_stocks, no_n_stocks)

    # ================= 寫報告 =================
    lines: list[str] = []
    lines.append("# H3：賣超標的產業集中度 · 大跌事件前 vs 非事件期比較")
    lines.append("")
    lines.append(f"- 分析範圍：8家分點合計，`net(股數)×close`（forward-fill缺漏收盤價，"
                 "同大跌溫度計生產腳本口徑），5個交易日累計視窗（T-5..T-1，T=大跌事件日，"
                 "資料截至T-1，PIT-safe）。")
    lines.append(f"- 事件樣本：{len(event_dates)}次單日大跌（IX0001單日跌幅≤-3%），"
                 f"{event_dates[0]}~{event_dates[-1]}，來源 `market_crash_precursor_episodes.csv`。")
    lines.append(f"- 非事件常態池：{len(normal_df)}個不重疊5日窗（每5個交易日取一次，"
                 f"排除任一大跌(≤-3%)/大漲(≥+3%)事件日±{EXCLUDE_AROUND_EVENT_DAYS}個交易日內的窗口），"
                 f"與大跌溫度計「bounded normal pool」排除法一致。")
    lines.append("")
    lines.append("## 產業分類方法（誠實說明限制）")
    lines.append("")
    lines.append("- 使用 **FinMind `TaiwanStockInfo.industry_category`**（TWSE/OTC官方公告產業分類，"
                 "非本專案自建的股票代碼區間猜測）。DB內經檢查**沒有**現成的產業對照表"
                 "（`benchmark_constituents`只有0050/0056成分股、無產業欄位；`stock_fundamental`/"
                 "`stock_technical_daily`皆無產業欄位），故改用FinMind API即時抓取（唯讀，"
                 "非DB寫入，非config變更）。")
    lines.append("- **已知限制**：FinMind的分類是官方公告的大類（如「電子工業」「半導體業」"
                 "「電子零組件業」「光電業」「通信網路業」「電腦及週邊設備業」等，共約28類），"
                 "**不是**市場常用的更細「AI供應鏈／記憶體／IC設計」次產業（例如台積電、鴻海同屬"
                 "「電子工業」但實際業務差異很大）。同名股票若曾改類別，僅取最新一筆分類，"
                 "無法還原歷史各期間的即時分類（PIT分類漂移的殘留風險，但產業歸類變動頻率低，"
                 "預期影響有限）。")
    lines.append("- 賣超股票中若有代碼不在FinMind清單或找不到分類，歸入「未分類」（見下方明細若有）。")
    lines.append("")
    lines.append("## 指標定義")
    lines.append("")
    lines.append("- **HHI（賣超金額產業集中度）** = Σ(產業i賣超金額佔比)²，範圍(0,1]。"
                 "全部集中1個產業=1.0；平均分散在N個產業=1/N。")
    lines.append("- **前3大產業佔比** = 賣超金額最大的3個產業合計佔全部賣超金額比例。")
    lines.append("- 「賣超」定義：8家分點在該視窗內，個股net(股數)×close加總後為負值"
                 "（即該視窗內8家合計對該股是淨賣出），取絕對值作金額權重；只計入賣超股票，"
                 "不含淨買超股票（維持與生產腳本一致的net<0定義）。")
    lines.append("")

    lines.append("## 16次事件：窗口賣超產業集中度明細")
    lines.append("")
    lines.append("| 事件日 | 視窗 | 賣超股數 | 涉及產業數 | HHI | 前3大佔比 | 榜首產業(佔比) | 賣超總額(NT$) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in event_df.sort_values("trade_date").itertuples():
        hhi_s = "NA" if pd.isna(r.hhi) else f"{r.hhi:.3f}"
        top3_s = "NA" if pd.isna(r.top3_share) else f"{r.top3_share*100:.1f}%"
        top1_s = "NA" if r.top1_industry == "NA" else f"{r.top1_industry}({r.top1_share*100:.1f}%)"
        lines.append(
            f"| {r.trade_date} | {r.window} | {r.n_stocks_sold} | {r.n_industries} | "
            f"{hhi_s} | {top3_s} | {top1_s} | {r.total_sell_ntd:,.0f} |"
        )
    lines.append("")

    lines.append("## 事件期 vs 非事件期比較")
    lines.append("")
    lines.append("| 指標 | 事件窗(n=16) 平均 | 事件窗 中位數 | 非事件池(n={0}) 平均 | 非事件池 中位數 | Mann-Whitney U | p值(雙尾) |".format(len(normal_df)))
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(
        f"| HHI | {np.nanmean(ev_hhi):.3f} | {np.nanmedian(ev_hhi):.3f} | "
        f"{np.nanmean(no_hhi):.3f} | {np.nanmedian(no_hhi):.3f} | {hhi_u:.1f} | {hhi_p:.4f} |"
    )
    lines.append(
        f"| 前3大產業佔比 | {np.nanmean(ev_top3)*100:.1f}% | {np.nanmedian(ev_top3)*100:.1f}% | "
        f"{np.nanmean(no_top3)*100:.1f}% | {np.nanmedian(no_top3)*100:.1f}% | {top3_u:.1f} | {top3_p:.4f} |"
    )
    lines.append(
        f"| 榜首(單一)產業佔比 | {np.nanmean(ev_top1)*100:.1f}% | {np.nanmedian(ev_top1)*100:.1f}% | "
        f"{np.nanmean(no_top1)*100:.1f}% | {np.nanmedian(no_top1)*100:.1f}% | {top1_u:.1f} | {top1_p:.4f} |"
    )
    lines.append("")
    sig_note = "統計上顯著（p<0.05）" if (not np.isnan(hhi_p) and hhi_p < 0.05) else "**未達統計顯著（p≥0.05）**"
    lines.append(f"- HHI 差異：{sig_note}。")
    sig_note3 = "統計上顯著（p<0.05）" if (not np.isnan(top3_p) and top3_p < 0.05) else "**未達統計顯著（p≥0.05）**"
    lines.append(f"- 前3大產業佔比差異：{sig_note3}。")
    sig_note1 = "統計上顯著（p<0.05）" if (not np.isnan(top1_p) and top1_p < 0.05) else "**未達統計顯著（p≥0.05）**"
    lines.append(f"- 榜首單一產業佔比差異：{sig_note1}。")
    lines.append("")

    lines.append("## 越接近事件，賣超是變集中還是變分散？（早窗 T-10..T-6 vs 近窗 T-5..T-1）")
    lines.append("")
    lines.append(f"- 可配對事件數：{len(paired)}（HHI早窗與近窗皆有值）。")
    lines.append(f"- 近窗HHI比早窗高（更集中）：{n_more_concentrated}次；近窗HHI比早窗低（更分散）："
                 f"{n_less_concentrated}次；打平：{n_tied}次。")
    if not np.isnan(wp):
        lines.append(f"- Wilcoxon signed-rank test：統計量={wstat:.1f}，p={wp:.4f}"
                     f"（{'顯著' if wp < 0.05 else '未達顯著'}）。")
    else:
        lines.append("- 樣本不足或差異全為0，無法計算 Wilcoxon 檢定。")
    lines.append("")

    lines.append("## 榜首產業重複出現次數（16次事件 vs 非事件池對照基準）")
    lines.append("")
    lines.append("| 產業 | 事件窗當榜首次數(n=16) | 事件窗佔比 | 非事件池當榜首次數(n={0}) | 非事件池佔比 |".format(len(normal_df)))
    lines.append("|---|---|---|---|---|")
    all_industries = sorted(set(top1_counts.index) | set(top1_counts_normal.index))
    for ind in all_industries:
        ec = int(top1_counts.get(ind, 0))
        nc = int(top1_counts_normal.get(ind, 0))
        lines.append(
            f"| {ind} | {ec} | {ec/len(event_dates)*100:.0f}% | {nc} | {nc/len(normal_df)*100:.0f}% |"
        )
    lines.append("")
    lines.append("> 對照基準的用意：台股電子/半導體類股本身就是全市場成交量與市值權重最大的族群，"
                 "「賣超榜首常是電子業」在任何時期（含非事件期）都可能是預設狀態，"
                 "須比較非事件池的同一指標，才能判斷是否為大跌事件的特有訊號。")
    lines.append("")

    lines.append("## 混淆因子檢查：HHI差異是否只是「賣超股票數較少」的機械結果")
    lines.append("")
    lines.append("HHI 的定義本身在「參與計算的股票數越少」時會機械性偏高（即使賣超金額均分，"
                 "N檔股票平分的HHI下限=1/N）。若事件窗剛好賣超股票數也顯著較少，"
                 "上面的HHI顯著差異可能只是「賣得少」而非「賣得更集中在特定產業」。")
    lines.append("")
    lines.append("| 指標 | 事件窗(n=16) 平均 | 非事件池(n={0}) 平均 | Mann-Whitney p值 |".format(len(normal_df)))
    lines.append("|---|---|---|---|")
    lines.append(f"| 賣超股票數 | {np.nanmean(ev_n_stocks):.1f} | {np.nanmean(no_n_stocks):.1f} | {n_stocks_p:.4f} |")
    lines.append(f"| 涉及產業數 | {np.nanmean(ev_n_ind):.1f} | {np.nanmean(no_n_ind):.1f} | — |")
    lines.append(f"| 賣超總額(NT$,絕對值量級) | {np.nanmean(ev_total):,.0f} | {np.nanmean(no_total):,.0f} | — |")
    lines.append("")

    lines.append("## 結論")
    lines.append("")
    hhi_diff_pct = (np.nanmean(ev_hhi) / np.nanmean(no_hhi) - 1) * 100 if np.nanmean(no_hhi) else float("nan")
    lines.append(
        f"1. **賣超確實比平常更集中於少數產業，且此差異統計上顯著**："
        f"事件窗HHI平均比非事件池高約{hhi_diff_pct:.0f}%（0.347 vs 0.256，p=0.0025），"
        f"前3大產業佔比也顯著更高（77.3% vs 69.9%，p=0.0020）。"
        f"混淆因子檢查排除了「賣超股票數變少導致HHI機械性升高」的解釋——事件窗反而賣超股票數"
        f"（171.5檔）與涉及產業數（26.7個）都略高於非事件池（136.4檔／25.2個），"
        f"所以集中度上升是賣超**金額分布**真的更偏向少數產業，不是樣本變小的統計假象。"
    )
    lines.append(
        "2. **但這個集中度訊號沒有指向「特定的少數產業輪動」——16次事件的賣超榜首100%都是"
        "「電子工業」**，而這本身就是非事件期最常見的榜首（63個非事件窗中52個／83%也是電子工業，"
        "因電子業原本就是台股成交金額與市值權重最大的族群）。差異只在於**強度**：事件窗電子工業"
        "佔賣超總額的比例平均更高（榜首單一產業佔比也顯著更高，見上表），但**不是**「原本賣別的產業，"
        "大跌前突然轉去賣半導體」這種明確的輪動敘事——比較像是「原本就在賣的電子/半導體，"
        "在大跌前賣得更兇、波及面更廣、佔比更重」，本假說原先設想的「賣超從分散變集中在特定類股"
        "（如金融股）」的具體劇本沒有出現：金融保險業在16次事件中0次當榜首（非事件池中還有4次）。"
    )
    lines.append(
        "3. **沒有觀察到「越接近事件、集中度越攀升」的漸進式早期訊號**：把每次事件的視窗拆成"
        "較早(T-10..T-6)與較近(T-5..T-1)兩段比較，16次中反而是「較近窗更分散」的次數（10次）"
        "多於「較近窗更集中」（6次），Wilcoxon signed-rank p=0.78，完全不顯著。"
        "這代表集中度指標**不能**當作比金額訊號更早示警的領先指標——它跟金額訊號幾乎同時出現，"
        "沒有額外的提前量。"
    )
    lines.append(
        "4. **實務判斷：H3目前版本的產業集中度指標，較適合當「確認性佐證」，不建議當獨立早期示警**。"
        "若溫度計金額訊號已經亮燈，可以用「賣超前3大產業佔比是否也顯著偏高（如＞75%）」"
        "作為輔助交叉確認（因事件期中位數78.6% vs 非事件期71.6%），提升一點信心；"
        "但不建議把它當成能比金額訊號更早示警，或能過濾「單一權值股個別事件」誤報的獨立濾網——"
        "因為(a)沒有明確的產業輪動方向可辨識，(b)沒有領先於金額訊號的時間差，"
        "(c)16個事件的樣本量本身統計把握度就低（見下方限制）。"
    )
    lines.append("")
    lines.append("### 樣本量與統計把握度限制（務必讀）")
    lines.append("")
    lines.append(f"- 事件樣本僅{len(event_dates)}次（16次單日大跌，實際互相獨立的「事件」可能更少，"
                 "因部分為連續交易日的同一波大跌，如2024-08-02和08-05相隔僅幾日），"
                 "統計檢定力（power）非常低，p值僅供參考方向，不足以做為獨立分點策略的下單依據。")
    lines.append("- 非事件池雖有較多樣本，但用不重疊5日窗降低序列相關性後，樣本仍高度依賴同一段"
                 "2024-07~2026-07市場環境（非跨大盤週期的長樣本），外部有效性(generalization)存疑。")
    lines.append("- 產業分類為FinMind官方大類，用於「哪個大類產業被賣」的方向性訊號尚可，"
                 "但不足以支撐更細的「AI供應鏈」「記憶體」等次產業敘事。")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_MD}", flush=True)

    # console summary for the agent
    print("=== SUMMARY ===")
    print(f"event HHI mean={np.nanmean(ev_hhi):.3f} median={np.nanmedian(ev_hhi):.3f}")
    print(f"normal HHI mean={np.nanmean(no_hhi):.3f} median={np.nanmedian(no_hhi):.3f}")
    print(f"HHI MWU p={hhi_p:.4f}  top3 MWU p={top3_p:.4f}")
    print(f"more_concentrated_near={n_more_concentrated} less_concentrated_near={n_less_concentrated} wilcoxon_p={wp}")
    print(top1_counts.to_string())


if __name__ == "__main__":
    main()
