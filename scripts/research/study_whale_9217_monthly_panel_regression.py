#!/usr/bin/env python3
"""9217（凱基-松山）月度淨部位變化訊號 panel 迴歸 + walk-forward IS/OOS 驗證.

方法論摘要（見對話中的完整規格）：
  1. 訊號重新定義為「月度淨部位變化金額」（不是逐日買進），天然消除日內自相關/偽複製問題。
  2. Panel: (stock_id, year_month) 為一列。signal(當月) -> fwd_1m_return(下個月報酬)，
     避免用同期報酬造成 look-ahead。
  3. 迴歸: fwd_1m_return ~ signal_z + sector_return(若有) + market_return，
     cluster-robust standard errors（依 year_month 分群）。
  4. Walk-forward: 依「整個月份時間軸」切 60% IS / 40% OOS，IS 用來決定訊號標準化參數，
     OOS 只能跑一次、不能回頭改方法論。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_monthly_panel_regression.py
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

warnings.filterwarnings("ignore", category=FutureWarning)

TRADER_ID = "9217"
MIN_BUY_DAYS = 20          # 股票篩選門檻：總買進交易日 >= 20
MIN_IS_MONTHS = 12         # 每檔股票至少要有 12 個 IS 月份資料才能算標準化參數、才納入panel
IS_FRACTION = 0.60
GAP_CALENDAR_DAYS = 16     # 價格資料缺口檢查門檻。注意：台股農曆春節連續假期常達11-16個日曆天
                            # （非資料缺口），若沿用既有腳本的>10天門檻會把幾乎所有股票的每年CNY
                            # 都當成缺口而誤刪。實測本資料集缺口天數分布在11-16天處有清楚的CNY群集
                            # (1492筆)，19天以上才是真正的資料缺失/停牌/來源缺塊，因此改用>16天。
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 明確可辨識同業的核心觀察標的 -> peer 清單（等權重）
SECTOR_PEERS = {
    "2337": {"name": "旺宏(記憶體/NOR Flash)", "peers": ["2344", "2408", "5347"]},  # 華邦電/南亞科/世界先進
    "6147": {"name": "頎邦(封測/COF)", "peers": ["3711", "6239", "2449"]},          # 日月光投控/力成/京元電子
}
FOCUS_STOCKS = ["2337", "6147"]


def section(title: str) -> None:
    print(f"\n{'='*90}\n{title}\n{'='*90}")


# ---------------------------------------------------------------------------
# 1. 資料載入
# ---------------------------------------------------------------------------

def load_branch_daily(conn) -> pd.DataFrame:
    q = """
        SELECT stock_id, trade_date, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = 'finmind'
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID,))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_all_bars(conn) -> pd.DataFrame:
    q = """
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source = 'finmind'
    """
    df = pd.read_sql_query(q, conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


# ---------------------------------------------------------------------------
# 2. 股票清單篩選 + 資料缺口檢查
# ---------------------------------------------------------------------------

def qualifying_stock_list(branch: pd.DataFrame) -> pd.DataFrame:
    g = branch.groupby("stock_id").agg(
        n_buy_days=("buy", lambda x: (x > 0).sum()),
        first_dt=("trade_date", "min"),
        last_dt=("trade_date", "max"),
    ).reset_index()
    qual = g[g["n_buy_days"] >= MIN_BUY_DAYS].copy()
    return qual


def continuity_check(bars: pd.DataFrame, stock_ids: list[str]) -> tuple[list[str], pd.DataFrame]:
    """對每檔股票檢查價格資料是否有 >GAP_CALENDAR_DAYS 的日曆天缺口（在其自身有交易資料的
    活躍區間內）。回傳 (乾淨股票清單, 缺口明細表)。"""
    sub = bars[bars["stock_id"].isin(stock_ids)].sort_values(["stock_id", "trade_date"])
    gap_rows = []
    bad_stocks = set()
    for sid, grp in sub.groupby("stock_id"):
        dates = grp["trade_date"].reset_index(drop=True)
        if len(dates) < 2:
            bad_stocks.add(sid)
            continue
        diffs = dates.diff().dt.days
        big = diffs[diffs > GAP_CALENDAR_DAYS]
        if not big.empty:
            bad_stocks.add(sid)
            for idx in big.index:
                gap_rows.append({
                    "stock_id": sid,
                    "gap_start": str(dates.iloc[idx - 1].date()),
                    "gap_end": str(dates.iloc[idx].date()),
                    "gap_days": int(diffs.iloc[idx]),
                })
    clean = [s for s in stock_ids if s not in bad_stocks]
    gap_df = pd.DataFrame(gap_rows)
    return clean, gap_df


# ---------------------------------------------------------------------------
# 3. 月度序列建構
# ---------------------------------------------------------------------------

def month_end_prices(bars: pd.DataFrame, stock_ids: list[str]) -> pd.DataFrame:
    sub = bars[bars["stock_id"].isin(stock_ids)].copy()
    sub["ym"] = sub["trade_date"].dt.to_period("M")
    sub = sub.sort_values(["stock_id", "trade_date"])
    me = sub.groupby(["stock_id", "ym"], as_index=False).last()[["stock_id", "ym", "trade_date", "close"]]
    me = me.rename(columns={"trade_date": "month_end_date", "close": "month_end_close"})
    return me


def monthly_returns_from_month_end(me: pd.DataFrame, id_col: str = "stock_id") -> pd.DataFrame:
    """依 (id, ym) 排序算 month-over-month return，只有當上個月在該 id 的序列中『日曆上連續』
    （ym - 1）時才計算，否則(缺月/剛上市/停業)為 NaN，避免跨越缺口誤算報酬。"""
    me = me.sort_values([id_col, "ym"]).copy()
    me["prev_ym"] = me.groupby(id_col)["ym"].shift(1)
    me["prev_close"] = me.groupby(id_col)["month_end_close"].shift(1)
    # 注意：Period - Period 回傳 offset 物件而非整數，不能直接 == 1 比較（會永遠False）。
    # 用 prev_ym + 1 == ym 判斷『日曆上緊鄰的下一個月』，NaT + 1 為 NaT，比較自動為 False，正確處理首列。
    is_contiguous = (me["prev_ym"] + 1) == me["ym"]
    me["monthly_return"] = np.where(
        is_contiguous & me["prev_close"].notna() & (me["prev_close"] > 0),
        me["month_end_close"] / me["prev_close"] - 1.0,
        np.nan,
    )
    return me


def monthly_net_signal(branch: pd.DataFrame, stock_ids: list[str]) -> pd.DataFrame:
    sub = branch[branch["stock_id"].isin(stock_ids)].copy()
    sub["ym"] = sub["trade_date"].dt.to_period("M")
    g = sub.groupby(["stock_id", "ym"], as_index=False)["net"].sum()
    g = g.rename(columns={"net": "monthly_net_shares"})
    return g


def build_market_monthly(bench: pd.Series) -> pd.DataFrame:
    b = bench.reset_index()
    b.columns = ["trade_date", "close"]
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b["ym"] = b["trade_date"].dt.to_period("M")
    b = b.sort_values("trade_date")
    me = b.groupby("ym", as_index=False).last()[["ym", "trade_date", "close"]]
    me = me.rename(columns={"trade_date": "month_end_date", "close": "month_end_close"})
    me["id"] = "MARKET"
    mr = monthly_returns_from_month_end(me, id_col="id")
    mr = mr.sort_values("ym")
    mr["market_fwd_return"] = mr["monthly_return"].shift(-1)  # attach NEXT month's return to row M
    return mr[["ym", "market_fwd_return"]]


def build_sector_monthly(bars: pd.DataFrame, peers: list[str]) -> pd.DataFrame:
    me = month_end_prices(bars, peers)
    mr = monthly_returns_from_month_end(me, id_col="stock_id")
    sector = mr.groupby("ym", as_index=False)["monthly_return"].mean()
    sector = sector.rename(columns={"monthly_return": "peer_avg_return"})
    sector = sector.sort_values("ym")
    sector["sector_fwd_return"] = sector["peer_avg_return"].shift(-1)
    return sector[["ym", "sector_fwd_return"]]


# ---------------------------------------------------------------------------
# 4. Panel 組裝
# ---------------------------------------------------------------------------

def build_full_grid(stock_ids: list[str], ym_min, ym_max) -> pd.DataFrame:
    all_yms = pd.period_range(ym_min, ym_max, freq="M")
    idx = pd.MultiIndex.from_product([stock_ids, all_yms], names=["stock_id", "ym"])
    return pd.DataFrame(index=idx).reset_index()


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    section("(1) 載入資料")
    branch = load_branch_daily(conn)
    bars = load_all_bars(conn)
    print(f"分點9217 daily rows: {len(branch)}, 涉及股票數: {branch['stock_id'].nunique()}")
    print(f"stock_daily_bars(finmind) rows: {len(bars)}, 涉及股票數: {bars['stock_id'].nunique()}")

    section("(2) 股票篩選：總買進交易日 >= 20")
    qual = qualifying_stock_list(branch)
    print(f"符合門檻(buy_days>={MIN_BUY_DAYS})股票數: {len(qual)} / 總交易股票數 {branch['stock_id'].nunique()}")

    has_price = set(bars["stock_id"].unique())
    qual_with_price = qual[qual["stock_id"].isin(has_price)].copy()
    print(f"其中同時有 stock_daily_bars 價格資料的股票數: {len(qual_with_price)}")

    section("(3) 價格資料連續性檢查（排除 >10 日曆天缺口的股票，避免資料缺口地雷）")
    clean_ids, gap_df = continuity_check(bars, qual_with_price["stock_id"].tolist())
    print(f"通過連續性檢查（乾淨）股票數: {len(clean_ids)} / {len(qual_with_price)}")
    print(f"被排除股票數（存在>{GAP_CALENDAR_DAYS}日曆天價格缺口）: {len(qual_with_price) - len(clean_ids)}")
    if not gap_df.empty:
        gap_path = OUT_DIR / "whale_9217_monthly_panel_excluded_price_gaps.csv"
        gap_df.to_csv(gap_path, index=False)
        print(f"[OK] 缺口明細寫入 {gap_path}（僅列出前10筆）")
        print(gap_df.head(10).to_string(index=False))
    # 確保核心觀察標的都在最終清單中
    for f in FOCUS_STOCKS:
        if f not in clean_ids:
            print(f"[警告] 核心觀察標的 {f} 未通過篩選/連續性檢查，將從個股專屬分析中略過")

    section("(4) 月度序列建構：月末收盤價、月報酬、月度淨部位變化訊號")
    me_stock = month_end_prices(bars, clean_ids)
    mr_stock = monthly_returns_from_month_end(me_stock, id_col="stock_id")
    mr_stock = mr_stock.sort_values(["stock_id", "ym"])
    mr_stock["fwd_1m_return"] = mr_stock.groupby("stock_id")["monthly_return"].shift(-1)

    net_sig = monthly_net_signal(branch, clean_ids)

    market_monthly = build_market_monthly(bench)

    sector_monthly = {}
    for sid, cfg in SECTOR_PEERS.items():
        sector_monthly[sid] = build_sector_monthly(bars, cfg["peers"])
        print(f"  {sid}({cfg['name']}) peer group = {cfg['peers']}")

    section("(5) Panel 組裝（stock_id x year_month 完整月份格）")
    # 分點交易資料庫只回補到 2019-01-01 起；股價資料則可能更早(部分股票有2015年起的資料)。
    # 若直接用股價資料的最早月份當grid起點，2019年以前完全沒有分點資料的月份會被誤填成
    # monthly_net_shares=0（=「確定沒交易」），但事實是「根本沒有資料可查」，兩者意義不同，
    # 必須用分點資料庫的實際涵蓋起點(2019-01)當作 panel 起點，避免灌入假的zero-signal月份。
    branch_coverage_start = branch["trade_date"].min().to_period("M")
    ym_min = max(mr_stock["ym"].min(), branch_coverage_start)
    ym_max = mr_stock["ym"].max()
    print(f"分點資料庫涵蓋起點: {branch_coverage_start}（panel 起點採用此值，不用股價資料更早的起點）")
    grid = build_full_grid(clean_ids, ym_min, ym_max)

    panel = grid.merge(mr_stock[["stock_id", "ym", "month_end_close", "fwd_1m_return"]],
                        on=["stock_id", "ym"], how="left")
    panel = panel.merge(net_sig, on=["stock_id", "ym"], how="left")
    # 在有月末收盤價的月份（=該股票在市場上有交易）內，無分點交易紀錄 => 淨部位變化視為 0
    panel.loc[panel["month_end_close"].notna() & panel["monthly_net_shares"].isna(), "monthly_net_shares"] = 0.0
    panel["signal_amt"] = panel["monthly_net_shares"] * panel["month_end_close"]

    panel = panel.merge(market_monthly, on="ym", how="left")
    panel = panel.rename(columns={"market_fwd_return": "market_return"})

    panel["sector_return"] = np.nan
    panel["has_sector_control"] = False
    for sid, sm_df in sector_monthly.items():
        mask = panel["stock_id"] == sid
        tmp = panel.loc[mask, ["ym"]].merge(sm_df, on="ym", how="left")
        panel.loc[mask, "sector_return"] = tmp["sector_fwd_return"].values
        panel.loc[mask, "has_sector_control"] = True

    # 只保留有月末收盤價（該股票當月有交易）且能算出 fwd_1m_return 與 market_return 的列
    panel = panel[panel["month_end_close"].notna()].copy()
    panel = panel[panel["fwd_1m_return"].notna() & panel["market_return"].notna()].copy()
    panel["ym_str"] = panel["ym"].astype(str)

    print(f"Panel 總列數（stock x month，具備 fwd_return & market_return）: {len(panel)}")
    print(f"涉及股票數: {panel['stock_id'].nunique()}")
    print(f"月份範圍: {panel['ym_str'].min()} ~ {panel['ym_str'].max()}")

    panel_path = OUT_DIR / "whale_9217_monthly_panel_raw.csv"
    panel.to_csv(panel_path, index=False)
    print(f"[OK] 完整 panel 寫入 {panel_path}")

    section("(6) Walk-forward IS/OOS 切分（依整個月份時間軸的 60/40）")
    all_months = sorted(panel["ym"].unique())
    cutoff_idx = int(len(all_months) * IS_FRACTION)
    is_months = set(all_months[:cutoff_idx])
    oos_months = set(all_months[cutoff_idx:])
    is_range = (str(all_months[0]), str(all_months[cutoff_idx - 1]))
    oos_range = (str(all_months[cutoff_idx]), str(all_months[-1]))
    print(f"總月份數: {len(all_months)}")
    print(f"IS ({len(is_months)}個月): {is_range[0]} ~ {is_range[1]}")
    print(f"OOS({len(oos_months)}個月): {oos_range[0]} ~ {oos_range[1]}")

    panel["split"] = np.where(panel["ym"].isin(is_months), "IS", "OOS")

    section("(7) 訊號標準化 -- 用 IS-only 每檔股票 mean/std 做 z-score（決策說明見下）")
    print(
        "決策說明：由於不同股票的部位規模(股本/股價/9217在該股的活躍程度)差異極大，直接用"
        "原始金額做pooled panel迴歸會被大型股/高頻股主導，不具跨股可比性。因此採用"
        "『每檔股票自己的月度淨部位變化金額 z-score』：mean/std 只用 IS 期間資料估計，"
        "同一組參數套用到 IS 與 OOS（避免用到未來資訊）。若某股票 IS 期間有效月份數"
        f"< {MIN_IS_MONTHS}，視為缺乏足夠校準資料而整檔排除（IS/OOS都不用），並記錄排除清單。"
        "未額外做「排除小額雜訊月份」的門檻篩選——避免在IS/OOS之間製造可被事後調整的"
        "自由度，維持方法論簡單、誠實可重現。"
    )

    is_stats = (
        panel[panel["split"] == "IS"]
        .groupby("stock_id")["signal_amt"]
        .agg(is_n="count", is_mean="mean", is_std="std")
        .reset_index()
    )
    dropped = is_stats[(is_stats["is_n"] < MIN_IS_MONTHS) | (is_stats["is_std"].isna()) | (is_stats["is_std"] == 0)]
    kept_stats = is_stats[~is_stats["stock_id"].isin(dropped["stock_id"])]
    print(f"IS期間有效月份數>={MIN_IS_MONTHS}且std>0 的股票數: {len(kept_stats)} / {len(is_stats)}")
    print(f"被排除股票數（IS校準資料不足）: {len(dropped)}")

    panel = panel.merge(kept_stats[["stock_id", "is_mean", "is_std"]], on="stock_id", how="inner")
    panel["signal_z"] = (panel["signal_amt"] - panel["is_mean"]) / panel["is_std"]

    panel_final_path = OUT_DIR / "whale_9217_monthly_panel_final.csv"
    panel.to_csv(panel_final_path, index=False)
    print(f"[OK] 最終(含 signal_z, split標記) panel 寫入 {panel_final_path}")
    print(f"最終納入迴歸的股票數: {panel['stock_id'].nunique()}, 總列數: {len(panel)}")
    for f in FOCUS_STOCKS:
        if f not in panel["stock_id"].unique():
            print(f"[警告] {f} 因IS校準資料不足或連續性/篩選問題，未進入最終 panel")

    # -----------------------------------------------------------------
    # (8) 迴歸：全樣本 pooled panel（僅市場控制，因大部分股票無明確同業peer）
    # -----------------------------------------------------------------
    section("(8) 主迴歸 A -- 全樣本 pooled panel: fwd_1m_return ~ signal_z + market_return")
    print("(此迴歸涵蓋所有通過篩選的股票；多數股票無明確同業peer，故僅控制大盤報酬。"
          "cluster-robust SE 依 year_month 分群)")

    reg_results = []

    def run_cluster_ols(df: pd.DataFrame, formula: str, cluster_col: str, label: str) -> dict:
        cols = [c.strip() for c in [cluster_col] + formula.replace("~", "+").split("+")]
        cols = [c for c in cols if c in df.columns]
        d = df.dropna(subset=cols)
        if len(d) < 10:
            print(f"[{label}] 樣本數太少(n={len(d)})，略過")
            return {"label": label, "n": len(d)}
        model = smf.ols(formula, data=d).fit(
            cov_type="cluster", cov_kwds={"groups": d[cluster_col]}
        )
        row = {
            "label": label,
            "n": int(model.nobs),
            "n_clusters": d[cluster_col].nunique(),
            "n_stocks": d["stock_id"].nunique() if "stock_id" in d.columns else None,
            "r2": round(model.rsquared, 4),
        }
        for term in model.params.index:
            row[f"coef_{term}"] = round(model.params[term], 6)
            row[f"se_{term}"] = round(model.bse[term], 6)
            row[f"t_{term}"] = round(model.tvalues[term], 3)
            row[f"p_{term}"] = round(model.pvalues[term], 4)
        print(f"\n--- {label} (n={row['n']}, clusters={row['n_clusters']}, stocks={row['n_stocks']}, R2={row['r2']}) ---")
        print(model.summary().tables[1])
        return row

    for split in ("IS", "OOS"):
        sub = panel[panel["split"] == split]
        reg_results.append(
            run_cluster_ols(sub, "fwd_1m_return ~ signal_z + market_return", "ym_str",
                             f"全樣本 pooled {split}")
        )

    # -----------------------------------------------------------------
    # (9) 迴歸 B: 有明確同業peer的子樣本（2337, 6147）pooled，含 sector_return
    # -----------------------------------------------------------------
    section("(9) 主迴歸 B -- 有sector control的子樣本(2337+6147) pooled: "
            "fwd_1m_return ~ signal_z + sector_return + market_return")
    sector_sub = panel[panel["stock_id"].isin(FOCUS_STOCKS) & panel["has_sector_control"]]
    for split in ("IS", "OOS"):
        sub = sector_sub[sector_sub["split"] == split]
        reg_results.append(
            run_cluster_ols(sub, "fwd_1m_return ~ signal_z + sector_return + market_return", "ym_str",
                             f"2337+6147子樣本(sector control) {split}")
        )

    # -----------------------------------------------------------------
    # (10) 個股專屬迴歸: 2337、6147 分別單獨跑（時間序列，用 HAC/Newey-West）
    # -----------------------------------------------------------------
    section("(10) 個股專屬迴歸（2337、6147 各自單獨，不與其他股票混合；HAC/Newey-West SE）")

    def run_single_stock_hac(df: pd.DataFrame, stock_id: str, split: str) -> dict:
        d = df[(df["stock_id"] == stock_id) & (df["split"] == split)].dropna(
            subset=["fwd_1m_return", "signal_z", "sector_return", "market_return"]
        ).sort_values("ym")
        label = f"{stock_id} 單獨 {split}"
        if len(d) < 8:
            print(f"[{label}] 樣本數太少(n={len(d)})，略過")
            return {"label": label, "n": len(d)}
        X = sm.add_constant(d[["signal_z", "sector_return", "market_return"]])
        y = d["fwd_1m_return"]
        maxlags = max(1, int(len(d) ** 0.25))
        model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
        row = {"label": label, "n": int(model.nobs), "r2": round(model.rsquared, 4), "hac_maxlags": maxlags}
        for term in model.params.index:
            row[f"coef_{term}"] = round(model.params[term], 6)
            row[f"se_{term}"] = round(model.bse[term], 6)
            row[f"t_{term}"] = round(model.tvalues[term], 3)
            row[f"p_{term}"] = round(model.pvalues[term], 4)
        print(f"\n--- {label} (n={row['n']}, R2={row['r2']}, HAC maxlags={maxlags}) ---")
        print(model.summary().tables[1])
        return row

    for sid in FOCUS_STOCKS:
        if sid not in panel["stock_id"].unique():
            print(f"[跳過] {sid} 不在最終panel中")
            continue
        for split in ("IS", "OOS"):
            reg_results.append(run_single_stock_hac(panel, sid, split))

    reg_df = pd.DataFrame(reg_results)
    reg_path = OUT_DIR / "whale_9217_monthly_panel_regression_results.csv"
    reg_df.to_csv(reg_path, index=False)
    print(f"\n[OK] 所有迴歸結果彙總寫入 {reg_path}")

    # -----------------------------------------------------------------
    # (11) sector control 涵蓋範圍說明文件
    # -----------------------------------------------------------------
    section("(11) Sector Control 涵蓋範圍說明")
    coverage_rows = []
    for sid, cfg in SECTOR_PEERS.items():
        coverage_rows.append({"stock_id": sid, "name": cfg["name"], "has_sector_control": True,
                               "peers": ",".join(cfg["peers"])})
    other_ids = [s for s in panel["stock_id"].unique() if s not in SECTOR_PEERS]
    for sid in sorted(other_ids):
        coverage_rows.append({"stock_id": sid, "name": "", "has_sector_control": False, "peers": ""})
    cov_df = pd.DataFrame(coverage_rows)
    cov_path = OUT_DIR / "whale_9217_monthly_panel_sector_coverage.csv"
    cov_df.to_csv(cov_path, index=False)
    print(f"[OK] Sector control 涵蓋範圍寫入 {cov_path}")
    print(f"有 sector control 的股票: {list(SECTOR_PEERS.keys())}")
    print(f"僅用市場報酬控制的股票數: {len(other_ids)}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
