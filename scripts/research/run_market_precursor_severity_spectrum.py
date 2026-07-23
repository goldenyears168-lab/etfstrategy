#!/usr/bin/env python3
"""Precursor severity spectrum — 訊號強度是否隨漲跌幅度漸進變化，還是±3%斷崖.

Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。

既有 `market_crash_precursor_scan.py` / `market_rally_precursor_scan.py` 用固定
±3% 門檻把交易日切成「事件／非事件」二元標籤。本研究改用**連續**視角：對
視窗內**每一個**交易日（不只是超過門檻的），都算出當天的 precursor 複合分數
與同日大盤報酬，檢驗兩者是否呈現單調（monotonic）梯度關係，還是只在±3%
附近才有訊號、其餘幅度都是雜訊（斷崖 vs 光譜）。

指標：
  - crash_composite_score(day) = 1 - mean(lb_pctile of 金牌7家)，用延伸版
    2022-01~2026-07-20 grid cache（`_market_crash_precursor_grid_cache_gold7ext.parquet`），
    數值越高＝金牌7家前5日累計賣超越極端。
  - rally_composite_score(day) = mean(lb_pctile) across 297 cohort，直接沿用既有
    `rally_buying_breadth_timeseries.csv` 的 composite_score 欄位（Track D 同一套
    定義），數值越高＝全市場買超廣度越極端。

分桶：依當日大盤報酬% 切 12 個區間（<=-5%, -5~-4%, ..., +4~+5%, >=+5%），
每桶算 crash/rally 複合分數的平均值、中位數、樣本數；並算全樣本 Spearman
相關係數（crash_composite_score vs -mkt_ret_pct；rally_composite_score vs
mkt_ret_pct）量化「是否為連續光譜」。

輸出：
  reports/research/branch-footprint-screen/precursor_severity_spectrum_daily.csv
  reports/research/branch-footprint-screen/precursor_severity_spectrum_summary.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_severity_spectrum.py --db data/replica/stocks.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen"
CRASH_GRID_EXT = OUT / "_market_crash_precursor_grid_cache_gold7ext.parquet"
RALLY_BREADTH_TS = OUT / "rally_buying_breadth_timeseries.csv"
BENCHMARK = "IX0001"

BINS = [-np.inf, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, np.inf]
BIN_LABELS = [
    "<=-5%", "-5~-4%", "-4~-3%", "-3~-2%", "-2~-1%", "-1~0%",
    "0~1%", "1~2%", "2~3%", "3~4%", "4~5%", ">=5%",
]


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_crash_composite(grid_path: Path) -> pd.DataFrame:
    grid = pd.read_parquet(grid_path)
    grid["securities_trader_id"] = grid["securities_trader_id"].astype(str)
    daily = (
        grid.dropna(subset=["lb_pctile"])
        .groupby("trade_date")["lb_pctile"]
        .mean()
        .rename("crash_composite_score")
    )
    daily = (1.0 - daily).rename("crash_composite_score")
    return daily.reset_index()


def build_rally_composite(ts_path: Path) -> pd.DataFrame:
    df = pd.read_csv(ts_path)
    return df[["trade_date", "composite_score"]].rename(
        columns={"composite_score": "rally_composite_score"}
    )


def bucket_table(df: pd.DataFrame, score_col: str) -> pd.DataFrame:
    sub = df.dropna(subset=[score_col, "mkt_ret_pct"]).copy()
    sub["bucket"] = pd.cut(sub["mkt_ret_pct"], bins=BINS, labels=BIN_LABELS, right=False)
    agg = (
        sub.groupby("bucket", observed=False)[score_col]
        .agg(mean="mean", median="median", n="count")
        .reindex(BIN_LABELS)
    )
    return agg.reset_index()


def main() -> None:
    ap = argparse.ArgumentParser(description="Precursor severity spectrum (dose-response check)")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "stocks.db")
    ap.add_argument("--crash-grid", type=Path, default=CRASH_GRID_EXT)
    ap.add_argument("--rally-breadth-ts", type=Path, default=RALLY_BREADTH_TS)
    args = ap.parse_args()

    conn = connect_ro(args.db)
    bench = load_benchmark_close(conn, code=BENCHMARK).sort_index()
    conn.close()
    ret = (bench.pct_change() * 100.0).rename("mkt_ret_pct")
    ret_df = ret.reset_index().rename(columns={"index": "trade_date"})
    ret_df["trade_date"] = ret_df["trade_date"].astype(str)

    print("building crash composite (gold-7 extended, 2022-01~2026-07-20) …", flush=True)
    crash_daily = build_crash_composite(args.crash_grid)
    print(f"  rows={len(crash_daily)}", flush=True)

    print("loading rally composite (297-cohort breadth index, 2024-07~2026-07) …", flush=True)
    rally_daily = build_rally_composite(args.rally_breadth_ts)
    print(f"  rows={len(rally_daily)}", flush=True)

    merged = ret_df.merge(crash_daily, on="trade_date", how="left").merge(
        rally_daily, on="trade_date", how="left"
    )
    merged = merged.dropna(subset=["mkt_ret_pct"])
    out_path = OUT / "precursor_severity_spectrum_daily.csv"
    merged.to_csv(out_path, index=False)
    print(f"wrote {out_path} rows={len(merged)}", flush=True)

    crash_bt = bucket_table(merged, "crash_composite_score")
    rally_bt = bucket_table(merged, "rally_composite_score")

    crash_valid = merged.dropna(subset=["crash_composite_score"])
    rally_valid = merged.dropna(subset=["rally_composite_score"])
    crash_rho, crash_p = stats.spearmanr(
        crash_valid["crash_composite_score"], -crash_valid["mkt_ret_pct"]
    )
    rally_rho, rally_p = stats.spearmanr(
        rally_valid["rally_composite_score"], rally_valid["mkt_ret_pct"]
    )

    lines = [
        "# Precursor 訊號強度 vs. 大盤漲跌幅度 · 連續光譜檢驗",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "",
        "既有大跌/大漲 precursor scan 用固定 ±3% 門檻把交易日切成事件／非事件二元",
        "標籤。本研究對**每一個**交易日（不限於超過門檻的），比對 precursor 複合",
        "分數與當日大盤報酬，檢驗訊號是否隨漲跌幅度連續變化（光譜），還是只在",
        "±3% 附近才有反應、其餘幅度都跟雜訊差不多（斷崖）。",
        "",
        "## 指標定義",
        "",
        "- `crash_composite_score`：金牌7家（`1650`瑞銀/`585U`統一南京/`1480`美商高盛亞/"
        "`1560`港商野村/`5850`統一/`918X`群益金鼎-台北/`9833`元大敦化）當日 `lb_pctile`"
        "（前5日累計賣超自我百分位）平均值，取 `1-mean`，數值越高＝賣超越極端；"
        "視窗 2022-01-03..2026-07-20（延伸回填後的完整history）。",
        "- `rally_composite_score`：297檔cohort全體當日 `lb_pctile` 平均值（沿用既有"
        "`rally_buying_breadth_dashboard.md`／Track D 同一套複合分數定義），數值越高"
        "＝全市場買超廣度越極端；視窗 2024-07-21..2026-07-20。",
        "- 兩個指標視窗長度不同（金牌7家有回填至2022年，大漲廣度指數目前只到"
        "2024-07），分開檢視，不直接比較兩者絕對數值。",
        "",
        "## 全樣本相關係數（是否為連續單調關係）",
        "",
        f"- crash_composite_score vs -當日報酬%：Spearman ρ={crash_rho:.3f}, "
        f"p={crash_p:.2e}（n={len(crash_valid)}）",
        f"- rally_composite_score vs 當日報酬%：Spearman ρ={rally_rho:.3f}, "
        f"p={rally_p:.2e}（n={len(rally_valid)}）",
        "",
        "## 依當日大盤報酬分桶：precursor 分數是否隨幅度遞增",
        "",
        "### 大跌方向（金牌7家 crash_composite_score）",
        "",
        "| 報酬區間 | 平均分數 | 中位數 | 樣本天數 |",
        "|---|---:|---:|---:|",
    ]
    for r in crash_bt.itertuples():
        mean_s = "NA" if pd.isna(r.mean) else f"{r.mean:.4f}"
        med_s = "NA" if pd.isna(r.median) else f"{r.median:.4f}"
        lines.append(f"| {r.bucket} | {mean_s} | {med_s} | {int(r.n)} |")

    lines += [
        "",
        "### 大漲方向（297cohort rally_composite_score／買超廣度）",
        "",
        "| 報酬區間 | 平均分數 | 中位數 | 樣本天數 |",
        "|---|---:|---:|---:|",
    ]
    for r in rally_bt.itertuples():
        mean_s = "NA" if pd.isna(r.mean) else f"{r.mean:.4f}"
        med_s = "NA" if pd.isna(r.median) else f"{r.median:.4f}"
        lines.append(f"| {r.bucket} | {mean_s} | {med_s} | {int(r.n)} |")

    lines += [
        "",
        "## 解讀指引",
        "",
        "- 若分桶表從「<=-5%」到「0~1%」（或大漲方向從「>=5%」到「0~1%」）呈現"
        "**單調遞減**，代表訊號是連續光譜——±2%、±1%的溫和波動也能用同一套規則",
        "偵測到「較弱但方向一致」的訊號，只是強度隨幅度縮小；",
        "- 若只有最極端的1-2桶（如<=-5%、-5~-4%）分數明顯偏高，中間幅度"
        "（-2%~-3%、-1%~-2%）幾乎跟0附近持平，代表訊號其實是**斷崖式**——"
        "±3%不是任意選的，是訊號真正開始跟雜訊分離的轉折點，往下擴大到±2%"
        "可能引入大量假陽性。",
        "- Spearman ρ 是全樣本單調性的量化版本：ρ 越接近1（大跌方向已取負號"
        "方向調整）代表越連續；ρ 接近0但分桶表兩端仍有落差，代表關係集中在"
        "極端尾部而非全樣本連續遞增。",
        "",
        "## 限制與注意事項",
        "",
        "- 這是探索性的描述統計（分桶平均/中位數＋Spearman相關），不是嚴謹的"
        "跨事件命中率假設檢定；中間幅度區間（±1%~±2%）樣本天數雖多，但"
        "「非事件日」本身没有一個對照的虛無假設基準線，解讀需保守。",
        "- crash 用延伸版金牌7家（2022-2026），rally 用297cohort廣度指數"
        "（2024-2026）——兩者視窗、cohort大小都不同，只能分開看各自的梯度"
        "形狀，不能直接比較兩個方向的絕對數值高低。",
        "- 若後續要把這個「光譜」做成正式分級警示（如：紅/橙/黃燈），建議先"
        "把本研究的分桶結果換算成「這個分數在此視窗歷史上的百分位」，而非"
        "直接用原始分數比大小。",
        "",
    ]
    summary_path = OUT / "precursor_severity_spectrum_summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {summary_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
