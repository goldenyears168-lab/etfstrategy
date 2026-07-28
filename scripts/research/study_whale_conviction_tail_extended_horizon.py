#!/usr/bin/env python3
"""分點大戶「重壓月」尾部事件 + 長天期 forward return 延伸驗證.

動機：既有月度 panel regression（study_whale_9217_monthly_panel_regression.py /
run_9661_monthly_panel_regression.py）用 fwd_1m_return 做 pooled 迴歸，結論是
signal_z 係數不顯著。使用者質疑：知名大戶不可能沒有 edge，要求檢查盲點。本腳本
專門補測三個既有方法論完全沒測過的角度（不是重跑同一件事换個門檻）：

  (A) 長天期 forward return（1/3/6/12個月）：既有規格只測下個月，如果大戶的
      獲利模式是建倉後抱長波段，1個月窗口本來就測不到。
  (B) Conviction 分位（依 IS 期校準的 signal_z 切 quintile）：既有迴歸把所有
      月份（含雜訊小額調整）跟大額重壓月一視同仁地丟進同一條迴歸線估一個線性
      係數，若真實效果只集中在最大額的重壓月，線性迴歸會被大量小額雜訊月稀釋
      到不顯著。
  (C) 尾部事件檢定：專門檢驗「最大重壓月（Q5, top quintile）」的 forward
      return 是否顯著異於其餘月份 —— 這是財富集中在少數大注的假說（大戶的
      名聲/財富常來自少數幾筆生涯級重壓，不是穩定可重複的統計 edge）。

分位切點用 IS 期資料估計、套用到 OOS（避免用未來資訊），與既有 z-score 校準
原則一致。長天期 forward return 用「月末收盤價序列 shift(-H) 且中間 H 個月
在月曆上連續」判斷有效，避免跨越資料缺口誤算報酬（沿用既有 continuity_check
邏輯的精神）。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_conviction_tail_extended_horizon.py --trader-id 9217
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_conviction_tail_extended_horizon.py --trader-id 9661
"""

from __future__ import annotations

import argparse
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

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MIN_BUY_DAYS = 20
MIN_IS_MONTHS = 12
IS_FRACTION = 0.60
GAP_CALENDAR_DAYS = 16  # 見既有 monthly panel 腳本註解：CNY連假11-16天群集，>16天才是真缺口
HORIZONS = (1, 3, 6, 12)
N_QUINTILES = 5

CONFIG = {
    "9217": {
        "core_holdings": ["2337", "6147"],
        "sector_peers": ["2344", "2408", "5347", "3711", "6239", "2449"],
    },
    "9661": {
        "core_holdings": ["3189", "2344", "2408", "8358", "8046", "4958", "1303", "1815", "3006"],
        "sector_peers": [
            "3037", "2313", "2383", "6274", "3044", "2367",
            "6269", "6141", "8213", "6239", "2449", "6147",
        ],
    },
}


def section(title: str) -> None:
    print(f"\n{'='*90}\n{title}\n{'='*90}")


def log(msg: str) -> None:
    print(f"[{pd.Timestamp.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 資料載入（與既有月度 panel 腳本邏輯一致）
# ---------------------------------------------------------------------------

def load_branch_daily(conn, trader_id: str) -> pd.DataFrame:
    q = """
        SELECT stock_id, trade_date, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = 'finmind'
    """
    df = pd.read_sql_query(q, conn, params=(trader_id,))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_all_bars(conn) -> pd.DataFrame:
    q = "SELECT stock_id, trade_date, close FROM stock_daily_bars WHERE source = 'finmind' AND close > 0"
    df = pd.read_sql_query(q, conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def qualifying_stock_list(branch: pd.DataFrame) -> list[str]:
    g = branch.groupby("stock_id").agg(n_buy_days=("buy", lambda x: (x > 0).sum())).reset_index()
    return g[g["n_buy_days"] >= MIN_BUY_DAYS]["stock_id"].tolist()


def continuity_check(bars: pd.DataFrame, stock_ids: list[str]) -> list[str]:
    sub = bars[bars["stock_id"].isin(stock_ids)].sort_values(["stock_id", "trade_date"])
    bad = set()
    for sid, grp in sub.groupby("stock_id"):
        dates = grp["trade_date"].reset_index(drop=True)
        if len(dates) < 2:
            bad.add(sid)
            continue
        if (dates.diff().dt.days > GAP_CALENDAR_DAYS).any():
            bad.add(sid)
    return [s for s in stock_ids if s not in bad]


def month_end_prices(bars: pd.DataFrame, stock_ids: list[str]) -> pd.DataFrame:
    sub = bars[bars["stock_id"].isin(stock_ids)].copy()
    sub["ym"] = sub["trade_date"].dt.to_period("M")
    sub = sub.sort_values(["stock_id", "trade_date"])
    me = sub.groupby(["stock_id", "ym"], as_index=False).last()[["stock_id", "ym", "trade_date", "close"]]
    return me.rename(columns={"trade_date": "month_end_date", "close": "month_end_close"})


def multi_horizon_fwd_returns(me: pd.DataFrame, id_col: str, horizons: tuple[int, ...]) -> pd.DataFrame:
    """對每個 (id, ym) 列，計算未來 H 個月的累積報酬 close(t+H)/close(t)-1，
    只有當 t..t+H 之間月份序列在月曆上連續（無缺口跳月）時才有效，否則 NaN。"""
    me = me.sort_values([id_col, "ym"]).reset_index(drop=True)
    out = me.copy()
    for h in horizons:
        close_h = me.groupby(id_col)["month_end_close"].shift(-h)
        ym_h = me.groupby(id_col)["ym"].shift(-h)
        contiguous = (ym_h == me["ym"] + h)
        fwd = np.where(
            contiguous & close_h.notna() & (me["month_end_close"] > 0),
            close_h / me["month_end_close"] - 1.0,
            np.nan,
        )
        out[f"fwd_{h}m_return"] = fwd
    return out


def build_market_monthly(bench: pd.Series, horizons: tuple[int, ...]) -> pd.DataFrame:
    b = bench.reset_index()
    b.columns = ["trade_date", "close"]
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b["ym"] = b["trade_date"].dt.to_period("M")
    me = b.sort_values("trade_date").groupby("ym", as_index=False).last()[["ym", "close"]]
    me = me.rename(columns={"close": "month_end_close"})
    me["id"] = "MARKET"
    mr = multi_horizon_fwd_returns(me, id_col="id", horizons=horizons)
    cols = ["ym"] + [f"fwd_{h}m_return" for h in horizons]
    return mr[cols].rename(columns={f"fwd_{h}m_return": f"market_fwd_{h}m" for h in horizons})


def build_sector_monthly(bars: pd.DataFrame, peers: list[str], horizons: tuple[int, ...]) -> pd.DataFrame:
    me = month_end_prices(bars, peers)
    mr = multi_horizon_fwd_returns(me, id_col="stock_id", horizons=horizons)
    agg = {f"fwd_{h}m_return": "mean" for h in horizons}
    sector = mr.groupby("ym", as_index=False).agg(agg)
    return sector.rename(columns={f"fwd_{h}m_return": f"sector_fwd_{h}m" for h in horizons})


def monthly_net_signal(branch: pd.DataFrame, stock_ids: list[str]) -> pd.DataFrame:
    sub = branch[branch["stock_id"].isin(stock_ids)].copy()
    sub["ym"] = sub["trade_date"].dt.to_period("M")
    g = sub.groupby(["stock_id", "ym"], as_index=False)["net"].sum()
    return g.rename(columns={"net": "monthly_net_shares"})


def build_full_grid(stock_ids: list[str], ym_min, ym_max) -> pd.DataFrame:
    all_yms = pd.period_range(ym_min, ym_max, freq="M")
    idx = pd.MultiIndex.from_product([stock_ids, all_yms], names=["stock_id", "ym"])
    return pd.DataFrame(index=idx).reset_index()


def run_cluster_ols(df: pd.DataFrame, formula: str, cluster_col: str, label: str) -> dict:
    cols = [c.strip() for c in [cluster_col] + formula.replace("~", "+").split("+")]
    cols = [c for c in cols if c in df.columns]
    d = df.dropna(subset=cols)
    if len(d) < 10 or d[cluster_col].nunique() < 3:
        print(f"[{label}] 樣本數或群數太少(n={len(d)}, clusters={d[cluster_col].nunique() if len(d) else 0})，略過")
        return {"label": label, "n": len(d)}
    model = smf.ols(formula, data=d).fit(cov_type="cluster", cov_kwds={"groups": d[cluster_col]})
    row = {"label": label, "n": int(model.nobs), "n_clusters": d[cluster_col].nunique(), "r2": round(model.rsquared, 4)}
    for term in model.params.index:
        row[f"coef_{term}"] = round(model.params[term], 6)
        row[f"p_{term}"] = round(model.pvalues[term], 4)
    print(f"--- {label} (n={row['n']}, clusters={row['n_clusters']}, R2={row['r2']}) ---")
    for term in model.params.index:
        print(f"    {term:22s} coef={row[f'coef_{term}']:+.5f}  p={row[f'p_{term}']:.4f}")
    return row


def bootstrap_mean_diff_ci(a: np.ndarray, b: np.ndarray, n_boot: int = 5000, seed: int = 42) -> tuple[float, float, float]:
    """回傳 (mean_diff, ci_low, ci_high)，diff = mean(a) - mean(b)。用固定 seed 保證可重現。"""
    rng = np.random.default_rng(seed)
    if len(a) < 2 or len(b) < 2:
        return float(np.mean(a) - np.mean(b)) if len(a) and len(b) else float("nan"), float("nan"), float("nan")
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        sa = rng.choice(a, size=len(a), replace=True)
        sb = rng.choice(b, size=len(b), replace=True)
        diffs[i] = sa.mean() - sb.mean()
    return float(np.mean(a) - np.mean(b)), float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader-id", required=True, choices=list(CONFIG.keys()))
    args = ap.parse_args()
    trader_id = args.trader_id
    cfg = CONFIG[trader_id]
    core_holdings = cfg["core_holdings"]
    sector_peers = cfg["sector_peers"]

    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    section(f"(1) 載入資料 -- trader_id={trader_id}")
    branch = load_branch_daily(conn, trader_id)
    bars = load_all_bars(conn)
    log(f"分點{trader_id} daily rows: {len(branch)}, 涉及股票數: {branch['stock_id'].nunique()}")

    section("(2)(3) 股票篩選 + 連續性檢查")
    qual = qualifying_stock_list(branch)
    has_price = set(bars["stock_id"].unique())
    qual_with_price = [s for s in qual if s in has_price]
    clean_ids = continuity_check(bars, qual_with_price)
    log(f"符合門檻且價格資料乾淨的股票數: {len(clean_ids)} / {len(qual)}")
    for f in core_holdings:
        if f not in clean_ids:
            log(f"[警告] 核心觀察標的 {f} 未通過篩選，將從結果中缺席")

    section(f"(4) 月度序列建構：多天期 forward return {HORIZONS}")
    me_stock = month_end_prices(bars, clean_ids)
    mr_stock = multi_horizon_fwd_returns(me_stock, id_col="stock_id", horizons=HORIZONS)
    net_sig = monthly_net_signal(branch, clean_ids)
    market_monthly = build_market_monthly(bench, HORIZONS)
    clean_peers = continuity_check(bars, [p for p in sector_peers if p in has_price])
    log(f"Sector peer 清單({len(sector_peers)}檔)中價格資料乾淨可用: {len(clean_peers)}")
    sector_monthly = build_sector_monthly(bars, clean_peers, HORIZONS)

    section("(5) Panel 組裝")
    branch_coverage_start = branch["trade_date"].min().to_period("M")
    ym_min = max(mr_stock["ym"].min(), branch_coverage_start)
    ym_max = mr_stock["ym"].max()
    grid = build_full_grid(clean_ids, ym_min, ym_max)

    keep_cols = ["stock_id", "ym", "month_end_close"] + [f"fwd_{h}m_return" for h in HORIZONS]
    panel = grid.merge(mr_stock[keep_cols], on=["stock_id", "ym"], how="left")
    panel = panel.merge(net_sig, on=["stock_id", "ym"], how="left")
    panel.loc[panel["month_end_close"].notna() & panel["monthly_net_shares"].isna(), "monthly_net_shares"] = 0.0
    panel["signal_amt"] = panel["monthly_net_shares"] * panel["month_end_close"]
    panel = panel.merge(market_monthly, on="ym", how="left")
    panel = panel.merge(sector_monthly, on="ym", how="left")
    panel["has_sector_control"] = panel["stock_id"].isin(sector_peers) == False  # noqa: E712 (股票本身不在peer清單才有意義的sector對照)
    panel = panel[panel["month_end_close"].notna()].copy()
    panel["ym_str"] = panel["ym"].astype(str)
    log(f"Panel 總列數: {len(panel)}, 股票數: {panel['stock_id'].nunique()}, 月份: {panel['ym_str'].min()}~{panel['ym_str'].max()}")

    section("(6) Walk-forward IS/OOS 切分 + signal_z / conviction quintile 校準（僅用IS資料估參數）")
    all_months = sorted(panel["ym"].unique())
    cutoff_idx = int(len(all_months) * IS_FRACTION)
    is_months = set(all_months[:cutoff_idx])
    panel["split"] = np.where(panel["ym"].isin(is_months), "IS", "OOS")
    log(f"IS: {all_months[0]}~{all_months[cutoff_idx-1]} ({cutoff_idx}個月)  "
        f"OOS: {all_months[cutoff_idx]}~{all_months[-1]} ({len(all_months)-cutoff_idx}個月)")

    is_stats = (panel[panel["split"] == "IS"].groupby("stock_id")["signal_amt"]
                .agg(is_n="count", is_mean="mean", is_std="std").reset_index())
    kept = is_stats[(is_stats["is_n"] >= MIN_IS_MONTHS) & is_stats["is_std"].notna() & (is_stats["is_std"] > 0)]
    panel = panel.merge(kept[["stock_id", "is_mean", "is_std"]], on="stock_id", how="inner")
    panel["signal_z"] = (panel["signal_amt"] - panel["is_mean"]) / panel["is_std"]
    log(f"進入最終panel的股票數: {panel['stock_id'].nunique()} (IS校準資料不足被排除: {len(is_stats)-len(kept)})")

    # conviction quintile 切點：只用 IS 期 signal_z 分布估計（pooled across stocks，因z已跨股標準化）
    is_z = panel.loc[panel["split"] == "IS", "signal_z"].dropna()
    quintile_edges = np.quantile(is_z, np.linspace(0, 1, N_QUINTILES + 1))
    quintile_edges[0], quintile_edges[-1] = -np.inf, np.inf
    panel["conviction_q"] = pd.cut(panel["signal_z"], bins=quintile_edges, labels=[f"Q{i+1}" for i in range(N_QUINTILES)])
    log(f"Conviction quintile 切點(IS估計，signal_z): {[round(x,3) for x in quintile_edges[1:-1]]}")

    panel_path = OUT_DIR / f"whale_{trader_id}_conviction_tail_panel.csv"
    panel.to_csv(panel_path, index=False)
    log(f"[OK] panel 寫入 {panel_path}")

    reg_rows = []
    quintile_rows = []
    tail_test_rows = []

    for h in HORIZONS:
        fwd_col = f"fwd_{h}m_return"
        mkt_col = f"market_fwd_{h}m"
        sec_col = f"sector_fwd_{h}m"

        section(f"(7) 天期 H={h}個月 -- 全樣本 pooled 迴歸: {fwd_col} ~ signal_z + market")
        for split in ("IS", "OOS"):
            sub = panel[panel["split"] == split]
            reg_rows.append(run_cluster_ols(sub, f"{fwd_col} ~ signal_z + {mkt_col}", "ym_str",
                                             f"H{h}m 全樣本 {split}"))

        core_sub = panel[panel["stock_id"].isin(core_holdings)]
        if core_sub["has_sector_control"].any():
            section(f"(7b) 天期 H={h}個月 -- 核心持股({core_holdings}) + sector control")
            for split in ("IS", "OOS"):
                sub = core_sub[core_sub["split"] == split]
                reg_rows.append(run_cluster_ols(sub, f"{fwd_col} ~ signal_z + {sec_col} + {mkt_col}", "ym_str",
                                                 f"H{h}m 核心持股(sector) {split}"))

        section(f"(8) 天期 H={h}個月 -- Conviction quintile 分組 (Q5=最大重壓月)")
        for split in ("IS", "OOS"):
            sub = panel[(panel["split"] == split) & panel[fwd_col].notna() & panel["conviction_q"].notna()]
            g = sub.groupby("conviction_q", observed=True)[fwd_col].agg(n="count", mean="mean", median="median",
                                                                          win_rate=lambda x: (x > 0).mean())
            for q, row in g.iterrows():
                quintile_rows.append({"trader_id": trader_id, "horizon_m": h, "split": split,
                                       "quintile": q, "n": int(row["n"]), "mean": round(row["mean"], 5),
                                       "median": round(row["median"], 5), "win_rate": round(row["win_rate"], 4)})
            print(g.round(4).to_string())

        section(f"(9) 天期 H={h}個月 -- 尾部檢定：Q5(最大重壓月) vs 其餘(Q1-Q4)")
        for split in ("IS", "OOS"):
            sub = panel[(panel["split"] == split) & panel[fwd_col].notna() & panel["conviction_q"].notna()]
            q5 = sub[sub["conviction_q"] == "Q5"][fwd_col].to_numpy()
            rest = sub[sub["conviction_q"] != "Q5"][fwd_col].to_numpy()
            if len(q5) >= 5 and len(rest) >= 5:
                diff, lo, hi = bootstrap_mean_diff_ci(q5, rest)
                sig = "顯著(CI不含0)" if (lo > 0 or hi < 0) else "不顯著(CI含0)"
                print(f"  [{split}] H{h}m Q5(n={len(q5)}, mean={q5.mean():+.4f}) vs "
                      f"Q1-4(n={len(rest)}, mean={rest.mean():+.4f})  diff={diff:+.4f}  "
                      f"95%CI=[{lo:+.4f},{hi:+.4f}]  {sig}")
                tail_test_rows.append({"trader_id": trader_id, "horizon_m": h, "split": split,
                                        "n_q5": len(q5), "n_rest": len(rest), "mean_q5": round(float(q5.mean()), 5),
                                        "mean_rest": round(float(rest.mean()), 5), "diff": round(diff, 5),
                                        "ci_low": round(lo, 5), "ci_high": round(hi, 5), "verdict": sig})
            else:
                print(f"  [{split}] H{h}m 樣本不足(Q5 n={len(q5)}, rest n={len(rest)})，略過")
                tail_test_rows.append({"trader_id": trader_id, "horizon_m": h, "split": split,
                                        "n_q5": len(q5), "n_rest": len(rest), "verdict": "insufficient_n"})

    reg_path = OUT_DIR / f"whale_{trader_id}_conviction_tail_regression_results.csv"
    pd.DataFrame(reg_rows).to_csv(reg_path, index=False)
    quintile_path = OUT_DIR / f"whale_{trader_id}_conviction_tail_quintile_results.csv"
    pd.DataFrame(quintile_rows).to_csv(quintile_path, index=False)
    tail_path = OUT_DIR / f"whale_{trader_id}_conviction_tail_test_results.csv"
    pd.DataFrame(tail_test_rows).to_csv(tail_path, index=False)
    log(f"[OK] 迴歸結果 -> {reg_path}")
    log(f"[OK] quintile 結果 -> {quintile_path}")
    log(f"[OK] 尾部檢定結果 -> {tail_path}")

    section("(10) 摘要：各天期 Q5 vs 其餘 尾部檢定 verdict 總覽")
    print(pd.DataFrame(tail_test_rows).to_string(index=False))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
