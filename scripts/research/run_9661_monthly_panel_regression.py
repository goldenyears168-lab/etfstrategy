#!/usr/bin/env python3
"""9661（富邦-新店/陳族元）月度淨部位變化訊號 panel 迴歸 + 系統性產業指數 + walk-forward IS/OOS 驗證.

方法論（見任務說明，修正前兩輪的偽複製與產業共同因子問題）：

1. 訊號重新定義為「月度淨部位變化」：對每個 (stock_id, year_month)，
   signal_amt = sum(daily net shares in that month) x 該月最後交易日收盤價。
   一個月只有一個觀察值，天然消除日內自相關/偽複製問題。

2. Panel: fwd_1m_return ~ signal + sector_return + market_return
   - fwd_1m_return：下一個月的個股報酬（避免用當月同期報酬 look-ahead）
   - sector_return：系統性 PCB載板/CCL/半導體材料供應鏈 peer group 等權重下月報酬
     （不含9661的9檔核心持股本身）
   - market_return：IX0001 下月報酬
   - 標準誤：panel 迴歸用 cluster-robust（依 year_month 分群）；
     單股時間序列迴歸用 Newey-West (HAC)。

3. Walk-forward：依月份時間軸切前60% IS / 後40% OOS，兩段分開報告，
   OOS只能跑一次、不能回頭依結果調整方法論。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_9661_monthly_panel_regression.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9661"

# 9661 核心觀察持股（訊號股票，也是候選對照組必須排除的對象）
CORE_HOLDINGS = ["3189", "2344", "2408", "8358", "8046", "4958", "1303", "1815", "3006"]

# 系統性半導體材料/PCB載板供應鏈 peer group（排除核心持股本身）。
# 篩選原則：finmind 收盤價資料連續（2015或2019起至今無大缺口），
# 涵蓋 PCB本板(3037/2313/3044/2316/8213)、軟板FPC(6269/6141/2313)、
# CCL/銅箔基板(2383/6274)、IC載板(3037/6191)、記憶體封測/驅動IC封裝(6239/2449/6147/2367)。
# 曾評估但因資料過短/有缺口而排除：2461太陽(僅2026起)、6213聯茂(僅2025起)、
# 8039台虹(僅2025起)、3413/5471(僅2025起)、3260/2451(2077列，較完整2812列短約700個交易日，
# 疑有缺口)、3583(與此供應鏈關聯不足)。
# 2316楠梓電子、6191精成科：continuity_check 掃出 2020-01-01~2023-01-02 之間有 1099 個日曆天
# 的缺口（finmind 該區間完全沒有收盤價資料），會在月度報酬序列中產生一筆橫跨3年的假「單月」
# 報酬並污染整個 sector index，故排除。（6191 另外在 2024-12-26 還有一筆 close=0 的髒資料，
# 已在資料載入層一併過濾 close<=0，但缺口問題本身無法用過濾解決，仍需整檔排除。）
SECTOR_PEERS = [
    "3037",  # 欣興 (Unimicron, PCB/IC substrate)
    "2313",  # 華通 (Compeq, PCB/FPC)
    "2383",  # 台光電 (EMC, CCL)
    "6274",  # 台燿 (Elite Material, CCL)
    "3044",  # 健鼎 (Unitech, PCB)
    "2367",  # 燿華電子
    "6269",  # 台郡 (Flexium, FPC)
    "6141",  # 柏承 (FPC)
    "8213",  # 志超 (PCB drilling/substrate)
    "6239",  # 力成 (Powertech, memory packaging/test)
    "2449",  # 京元電子 (KYEC, IC testing)
    "6147",  # 頎邦 (Chipbond, driver IC packaging/COF)
]

MIN_BUY_DAYS = 20
IS_FRACTION = 0.60
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_trader_daily(conn) -> pd.DataFrame:
    log("loading 9661 daily broker-branch data (full table scan, ~1min)...")
    q = """
        SELECT trade_date, stock_id, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = 'finmind'
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID,))
    log(f"  -> {len(df):,} rows")
    return df


def load_all_bars(conn) -> pd.DataFrame:
    log("loading all finmind stock_daily_bars...")
    q = "SELECT stock_id, trade_date, close FROM stock_daily_bars WHERE source = 'finmind'"
    df = pd.read_sql_query(q, conn)
    n_bad = int((df["close"] <= 0).sum())
    if n_bad:
        log(f"  -> {len(df):,} rows, dropping {n_bad} rows with close<=0 (known data glitches)")
        df = df[df["close"] > 0].copy()
    else:
        log(f"  -> {len(df):,} rows")
    return df


def month_end_close(bars: pd.DataFrame) -> pd.DataFrame:
    """Per (stock_id, year_month): close on the last trading day of the month."""
    b = bars.copy()
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b["year_month"] = b["trade_date"].dt.to_period("M")
    b = b.sort_values("trade_date")
    last = b.groupby(["stock_id", "year_month"], as_index=False).last()[
        ["stock_id", "year_month", "close"]
    ]
    return last


def monthly_return_by_stock(month_close: pd.DataFrame) -> pd.DataFrame:
    m = month_close.sort_values(["stock_id", "year_month"]).copy()
    m["ret"] = m.groupby("stock_id")["close"].pct_change()
    m["ret"] = m["ret"].replace([np.inf, -np.inf], np.nan)
    # forward 1-month return: the return realized during month t+1, attached to row t
    m["fwd_1m_return"] = m.groupby("stock_id")["ret"].shift(-1)
    return m


def continuity_check(bars: pd.DataFrame, stock_ids: list[str], max_gap_days: int = 10) -> pd.DataFrame:
    """夠年份沒有 > max_gap_days 個交易日缺口 (per user's data-landmine warning)."""
    rows = []
    b = bars[bars["stock_id"].isin(stock_ids)].copy()
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    for sid, g in b.groupby("stock_id"):
        g = g.sort_values("trade_date")
        gaps = g["trade_date"].diff().dt.days.dropna()
        # trading-day gap proxy: count consecutive calendar-day gaps > ~14 (covers long weekends/holidays)
        max_gap = gaps.max() if len(gaps) else np.nan
        n_big_gaps = int((gaps > 20).sum())
        rows.append(
            {
                "stock_id": sid,
                "first_date": g["trade_date"].min().date(),
                "last_date": g["trade_date"].max().date(),
                "n_rows": len(g),
                "max_calendar_gap_days": max_gap,
                "n_gaps_gt20d": n_big_gaps,
            }
        )
    return pd.DataFrame(rows).sort_values("stock_id")


# ---------------------------------------------------------------------------
# Signal + panel construction
# ---------------------------------------------------------------------------

def build_monthly_signal(trader_daily: pd.DataFrame, month_close: pd.DataFrame) -> pd.DataFrame:
    d = trader_daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    d["year_month"] = d["trade_date"].dt.to_period("M")
    monthly = d.groupby(["stock_id", "year_month"], as_index=False).agg(
        monthly_net_shares=("net", "sum"),
        monthly_buy_days=("buy", lambda s: int((s > 0).sum())),
        monthly_trade_days=("trade_date", "nunique"),
    )
    monthly = monthly.merge(month_close, on=["stock_id", "year_month"], how="left")
    monthly["monthly_net_change_amt"] = monthly["monthly_net_shares"] * monthly["close"]
    return monthly


def eligible_stock_list(trader_daily: pd.DataFrame) -> pd.DataFrame:
    g = trader_daily[trader_daily["buy"] > 0].groupby("stock_id")["trade_date"].nunique()
    g = g[g >= MIN_BUY_DAYS].sort_values(ascending=False)
    return g.reset_index().rename(columns={"trade_date": "total_buy_days"})


def zscore_signal(panel: pd.DataFrame) -> pd.Series:
    """每個分點自己歷史分布的 z-score（依 stock_id 分組），用來標準化跨股票量級差異。"""
    def _z(s: pd.Series) -> pd.Series:
        mu, sd = s.mean(), s.std(ddof=0)
        if not sd or np.isnan(sd) or sd == 0:
            return pd.Series(0.0, index=s.index)
        return (s - mu) / sd

    return panel.groupby("stock_id")["monthly_net_change_amt"].transform(_z)


# ---------------------------------------------------------------------------
# Regression helpers
# ---------------------------------------------------------------------------

def run_cluster_panel_ols(df: pd.DataFrame, label: str) -> dict:
    d = df.dropna(subset=["fwd_1m_return", "signal", "sector_return", "market_return"]).copy()
    if len(d) < 30:
        return {"label": label, "n": len(d), "note": "insufficient observations (<30)"}
    d["ym_str"] = d["year_month"].astype(str)
    model = smf.ols("fwd_1m_return ~ signal + sector_return + market_return", data=d)
    res = model.fit(cov_type="cluster", cov_kwds={"groups": d["ym_str"]})
    return {
        "label": label,
        "n": len(d),
        "n_months": d["ym_str"].nunique(),
        "n_stocks": d["stock_id"].nunique(),
        "signal_coef": res.params.get("signal"),
        "signal_se": res.bse.get("signal"),
        "signal_t": res.tvalues.get("signal"),
        "signal_p": res.pvalues.get("signal"),
        "sector_coef": res.params.get("sector_return"),
        "sector_p": res.pvalues.get("sector_return"),
        "market_coef": res.params.get("market_return"),
        "market_p": res.pvalues.get("market_return"),
        "const": res.params.get("Intercept"),
        "r2": res.rsquared,
        "res": res,
    }


def run_hac_timeseries_ols(df: pd.DataFrame, label: str, maxlags: int = 3) -> dict:
    """單股時間序列迴歸（每月一列，同一股票），用 Newey-West HAC 標準誤."""
    d = df.dropna(subset=["fwd_1m_return", "signal", "sector_return", "market_return"]).copy()
    if len(d) < 20:
        return {"label": label, "n": len(d), "note": "insufficient observations (<20)"}
    d = d.sort_values("year_month")
    model = smf.ols("fwd_1m_return ~ signal + sector_return + market_return", data=d)
    res = model.fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    return {
        "label": label,
        "n": len(d),
        "n_months": d["year_month"].nunique(),
        "signal_coef": res.params.get("signal"),
        "signal_se": res.bse.get("signal"),
        "signal_t": res.tvalues.get("signal"),
        "signal_p": res.pvalues.get("signal"),
        "sector_coef": res.params.get("sector_return"),
        "sector_p": res.pvalues.get("sector_return"),
        "market_coef": res.params.get("market_return"),
        "market_p": res.pvalues.get("market_return"),
        "const": res.params.get("Intercept"),
        "r2": res.rsquared,
        "res": res,
    }


def fmt_row(r: dict) -> str:
    if "note" in r:
        return f"  {r['label']}: n={r['n']} -- {r['note']}"
    return (
        f"  {r['label']}: n={r['n']} ({r.get('n_months','?')} months"
        f"{', ' + str(r.get('n_stocks')) + ' stocks' if 'n_stocks' in r else ''}) "
        f"signal_coef={r['signal_coef']:.6f} se={r['signal_se']:.6f} "
        f"t={r['signal_t']:.3f} p={r['signal_p']:.4f} | "
        f"sector_coef={r['sector_coef']:.4f}(p={r['sector_p']:.3f}) "
        f"market_coef={r['market_coef']:.4f}(p={r['market_p']:.3f}) "
        f"R2={r['r2']:.4f}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = connect(DEFAULT_DB_PATH)

    trader_daily = load_trader_daily(conn)
    all_bars = load_all_bars(conn)
    bench = load_benchmark_close(conn, code="IX0001")

    # ---- eligible universe ----
    eligible = eligible_stock_list(trader_daily)
    eligible.to_csv(OUT_DIR / "whale_9661_panel_eligible_stocks.csv", index=False)
    log(f"eligible stocks (total buy_days >= {MIN_BUY_DAYS}): {len(eligible)}")
    for c in CORE_HOLDINGS:
        if c not in set(eligible["stock_id"]):
            log(f"  WARNING: core holding {c} did NOT meet eligibility threshold")

    # ---- continuity check on peers + core holdings ----
    cont = continuity_check(all_bars, SECTOR_PEERS + CORE_HOLDINGS)
    cont.to_csv(OUT_DIR / "whale_9661_panel_continuity_check.csv", index=False)
    bad = cont[cont["n_gaps_gt20d"] > 0]
    if len(bad):
        log("continuity WARNING (gaps > 20 calendar days found):")
        log(bad.to_string(index=False))
    else:
        log("continuity check: no >20-calendar-day gaps found in peers/core holdings.")

    # ---- monthly closes & returns for full stock universe ----
    month_close_all = month_end_close(all_bars)
    stock_monthly_ret = monthly_return_by_stock(month_close_all)

    # ---- sector index (equal-weight peer group, excludes core holdings) ----
    sector_month = stock_monthly_ret[stock_monthly_ret["stock_id"].isin(SECTOR_PEERS)]
    sector_series = sector_month.groupby("year_month")["ret"].mean().rename("sector_ret_contemp")
    sector_series = sector_series.sort_index()
    sector_fwd = sector_series.shift(-1).rename("sector_return")

    # ---- market index ----
    bench_df = bench.to_frame("close").reset_index()
    bench_df.columns = ["trade_date", "close"]
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"])
    bench_df["year_month"] = bench_df["trade_date"].dt.to_period("M")
    bench_df = bench_df.sort_values("trade_date")
    bench_month = bench_df.groupby("year_month", as_index=False).last()[["year_month", "close"]]
    bench_month = bench_month.sort_values("year_month")
    bench_month["mkt_ret_contemp"] = bench_month["close"].pct_change()
    bench_month["mkt_ret_contemp"] = bench_month["mkt_ret_contemp"].replace([np.inf, -np.inf], np.nan)
    bench_month = bench_month.set_index("year_month")
    market_fwd = bench_month["mkt_ret_contemp"].shift(-1).rename("market_return")

    # ---- monthly signal (9661) ----
    monthly_signal = build_monthly_signal(trader_daily, month_close_all)
    monthly_signal.to_csv(OUT_DIR / "whale_9661_panel_monthly_signal_raw.csv", index=False)

    # keep only eligible stocks
    elig_ids = set(eligible["stock_id"])
    panel_ids = elig_ids  # full qualifying universe per task instructions

    sig = monthly_signal[monthly_signal["stock_id"].isin(panel_ids)].copy()

    # ---- build full (stock, year_month) grid for eligible stocks over data-covered months ----
    all_months = sorted(stock_monthly_ret["year_month"].unique())
    # restrict to branch-data-backfilled window: 2019-01 onward
    all_months = [m for m in all_months if m >= pd.Period("2019-01", freq="M")]

    grid = pd.MultiIndex.from_product([sorted(panel_ids), all_months], names=["stock_id", "year_month"]).to_frame(
        index=False
    )
    panel = grid.merge(
        sig[["stock_id", "year_month", "monthly_net_change_amt", "monthly_buy_days", "monthly_trade_days"]],
        on=["stock_id", "year_month"],
        how="left",
    )
    # months with no trading by 9661 in that stock => net change = 0 (a valid "no change" signal)
    panel["monthly_net_change_amt"] = panel["monthly_net_change_amt"].fillna(0.0)
    panel["monthly_buy_days"] = panel["monthly_buy_days"].fillna(0)
    panel["monthly_trade_days"] = panel["monthly_trade_days"].fillna(0)

    # attach fwd_1m_return (per stock)
    panel = panel.merge(
        stock_monthly_ret[["stock_id", "year_month", "fwd_1m_return"]],
        on=["stock_id", "year_month"],
        how="left",
    )
    # attach sector_return / market_return (same value across stocks, indexed by year_month)
    panel = panel.merge(sector_fwd.reset_index(), on="year_month", how="left")
    panel = panel.merge(market_fwd.reset_index(), on="year_month", how="left")

    # signal choice: raw amount vs within-stock z-score. IS decision documented below.
    panel["signal_raw_amt"] = panel["monthly_net_change_amt"]
    panel["signal_z"] = zscore_signal(panel)

    panel = panel.sort_values(["year_month", "stock_id"]).reset_index(drop=True)
    panel.to_csv(OUT_DIR / "whale_9661_panel_full.csv", index=False)
    log(f"full panel rows: {len(panel):,} ({panel['stock_id'].nunique()} stocks x {panel['year_month'].nunique()} months)")

    # ---------------------------------------------------------------
    # IS/OOS split by month (chronological, 60/40)
    # ---------------------------------------------------------------
    months_sorted = sorted(panel["year_month"].dropna().unique())
    n_months = len(months_sorted)
    n_is = int(round(n_months * IS_FRACTION))
    is_months = set(months_sorted[:n_is])
    oos_months = set(months_sorted[n_is:])
    log(f"IS months: {months_sorted[0]}..{sorted(is_months)[-1]} ({len(is_months)} months)")
    log(f"OOS months: {sorted(oos_months)[0]}..{months_sorted[-1]} ({len(oos_months)} months)")

    panel_is = panel[panel["year_month"].isin(is_months)].copy()
    panel_oos = panel[panel["year_month"].isin(oos_months)].copy()

    # ---------------------------------------------------------------
    # IS-only model-choice decision: raw amount vs z-score signal
    # (decided ONCE on IS data, then locked in for OOS -- no re-peeking)
    # ---------------------------------------------------------------
    log("\n=== IS-only signal-form comparison (raw amount vs within-stock z-score) ===")
    is_raw = panel_is.assign(signal=panel_is["signal_raw_amt"])
    is_z = panel_is.assign(signal=panel_is["signal_z"])
    r_raw = run_cluster_panel_ols(is_raw, "IS full-panel, signal=raw amt")
    r_z = run_cluster_panel_ols(is_z, "IS full-panel, signal=z-score")
    log(fmt_row(r_raw))
    log(fmt_row(r_z))

    # Mechanical IS-only decision rule (locked in before ever touching OOS):
    # pick whichever signal form has the smaller IS p-value on the signal coefficient.
    # This keeps the choice tied to what IS actually shows rather than a prior belief.
    p_raw = r_raw.get("signal_p", np.nan)
    p_z = r_z.get("signal_p", np.nan)
    if np.isnan(p_z) or (not np.isnan(p_raw) and p_raw <= p_z):
        chosen_form, chosen_label = "signal_raw_amt", "raw monthly net-change amount (NT$)"
    else:
        chosen_form, chosen_label = "signal_z", "within-stock z-score"
    log(
        f"\n-> IS decision rule: pick the signal form with the smaller IS p-value on the signal "
        f"coefficient (raw amt p={p_raw:.4f} vs z-score p={p_z:.4f}). "
        f"Chosen: {chosen_label}. This choice is locked in before running any OOS regression."
    )

    panel["signal"] = panel[chosen_form]
    panel_is = panel[panel["year_month"].isin(is_months)].copy()
    panel_oos = panel[panel["year_month"].isin(oos_months)].copy()

    # ---------------------------------------------------------------
    # Full-panel regression: IS then OOS (OOS run exactly once)
    # ---------------------------------------------------------------
    log("\n=== FULL PANEL (all eligible stocks) -- signal + sector_return + market_return ===")
    full_is = run_cluster_panel_ols(panel_is, "FULL PANEL - IS")
    log(fmt_row(full_is))
    full_oos = run_cluster_panel_ols(panel_oos, "FULL PANEL - OOS (run once, as-is)")
    log(fmt_row(full_oos))

    # ---------------------------------------------------------------
    # Core-holding single-stock regressions (with sector_return control)
    # ---------------------------------------------------------------
    CORE_FOCUS = ["3189", "2408", "8358", "8046", "4958"]
    log("\n=== CORE-HOLDING single-stock panels (signal + sector_return + market_return, HAC/Newey-West SE) ===")
    core_results = []
    for sid in CORE_FOCUS:
        d = panel[panel["stock_id"] == sid].copy()
        d_is = d[d["year_month"].isin(is_months)]
        d_oos = d[d["year_month"].isin(oos_months)]
        r_is = run_hac_timeseries_ols(d_is, f"{sid} IS")
        r_oos = run_hac_timeseries_ols(d_oos, f"{sid} OOS")
        log(fmt_row(r_is))
        log(fmt_row(r_oos))
        core_results.append(r_is)
        core_results.append(r_oos)

    # ---------------------------------------------------------------
    # Save summary CSV
    # ---------------------------------------------------------------
    def strip_res(r: dict) -> dict:
        return {k: v for k, v in r.items() if k != "res"}

    summary_rows = [
        strip_res(r_raw) | {"stage": "IS_signal_form_compare"},
        strip_res(r_z) | {"stage": "IS_signal_form_compare"},
        strip_res(full_is) | {"stage": "full_panel"},
        strip_res(full_oos) | {"stage": "full_panel"},
    ] + [strip_res(r) | {"stage": "core_holding"} for r in core_results]
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_DIR / "whale_9661_panel_regression_summary.csv", index=False)

    # full statsmodels summary text dump for the two headline models
    def dump(r: dict) -> str:
        res = r.get("res")
        if res is None:
            return r.get("note", "N/A")
        return str(res.summary())

    with open(OUT_DIR / "whale_9661_panel_regression_full_summary.txt", "w") as f:
        f.write("=== FULL PANEL - IS ===\n")
        f.write(dump(full_is))
        f.write("\n\n=== FULL PANEL - OOS ===\n")
        f.write(dump(full_oos))
        f.write("\n\n=== IS signal-form compare: raw amt ===\n")
        f.write(dump(r_raw))
        f.write("\n\n=== IS signal-form compare: z-score ===\n")
        f.write(dump(r_z))
        for r in core_results:
            f.write(f"\n\n=== {r['label']} ===\n")
            f.write(dump(r))

    log(f"\nSaved: {OUT_DIR}/whale_9661_panel_regression_summary.csv")
    log(f"Saved: {OUT_DIR}/whale_9661_panel_regression_full_summary.txt")
    log(f"Saved: {OUT_DIR}/whale_9661_panel_full.csv")
    log("DONE")


if __name__ == "__main__":
    main()
