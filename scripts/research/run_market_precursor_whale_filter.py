#!/usr/bin/env python3
"""Whale-size filter precursor scan — 大戶（單筆金額≥門檻）過濾版分點 precursor 訊號.

Research only（探索 topic，尚未登錄 `config/research.yaml`）。延伸既有
`run_market_crash_branch_precursor_scan.py` / `run_market_rally_branch_precursor_scan.py`
（分點對「全市場所有個股」net(股)×close 加總的 self-normalized tail-percentile
precursor 訊號），檢驗一個假設：

    分點每日對市場的加總 net_amt 可能混雜了少數「大戶級」（單筆金額≥門檻）
    個股-日部位，與大量「散戶級」（小額）個股-日部位。只保留大額部位再加總
    （whale filter，只參考大戶訊號、排除散戶噪音），訊號會不會比未過濾版
    更乾淨（大跌方向：核心成員是否不變／信心是否提升；大漲方向：入選清單
    是否顯著縮小、降噪）？

方法（凍結協議，與大跌/大漲版本 direction-agnostic 建構函式完全共用，
只在「分點-個股-日」聚合這一步插入 whale filter）：

  1. 分點宇宙 / 交易日曆 / 大跌大漲 episode：與既有 pipeline 完全相同
     （`load_cohort`、`build_calendar`、既有 episode CSV 直接重用、不重算）。
  2. 分點訊號原本 = 分點當日「對全市場所有個股」net(股)×close 加總
     （`net_amt`，見 `build_branch_day_panel`）。whale filter 版在加總前，
     先在「分點-個股-日」這個更細的顆粒度上，丟棄 `abs(net_amt) < threshold`
     的個股-日 row（非 cap/clip，直接排除），只對 `abs(net_amt) >= threshold`
     的大戶級 row 加總，得到 `net_amt_whale`。
  3. 對 `net_amt_whale` 序列跑一模一樣的前 5 日累計 + 自我常態化百分位
     （`compute_lookback_percentiles`），再用一模一樣的跨事件命中率 + 單尾
     二項檢定（`score_branches` / `score_branches_rally`）對既有 episode
     files 打分。
  4. 3 個門檻：NT$ 1億 / 2億（使用者建議基準）/ 5億，分別跑一次，並與原始
     未過濾清單（`market_crash_precursor_branch_scores.csv` /
     `market_rally_precursor_branch_scores.csv`，直接讀取重用、不重算）比較
     overlap % 與 hit_count 排名 Spearman ρ（方法沿用
     `xsec_precursor_comparison.md`）。
  5. 另外統計每個門檻在「金額」與「個股-日 row 數」兩個維度上各保留了多少
     比例（了解每個門檻的激進程度）。

資料來源注意：本次依需求改用 replica DB（`data/replica/stocks.db`）。
該檔案在研究執行當下可能有背景 backfill 程序併發寫入，若用
`mode=ro&immutable=1` 開啟，遇到併發寫入會讀到不一致的頁面而丟出
`database disk image is malformed`；改用 `mode=ro`（仍是唯讀，但走
SQLite 正常的檔案鎖定語意，能正確處理併發讀寫）即可穩定讀取。

輸出（`reports/research/branch-footprint-screen/`）：
  whale_filter_precursor_branch_scores_crash.csv
  whale_filter_precursor_branch_scores_rally.csv
  whale_filter_precursor_summary.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_whale_filter.py \\
    --threshold-ntd 100000000 200000000 500000000
"""
from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "reports/research/branch-footprint-screen"
DEFAULT_DB_PATH = ROOT / "data" / "replica" / "stocks.db"
SOURCE = "finmind"
BENCHMARK = "IX0001"

# --- shared, direction-agnostic building blocks reused as-is from the
# reference crash/rally scripts (avoid re-implementing cohort/calendar/
# rolling-percentile/scoring logic) ---------------------------------------
_crash_spec = importlib.util.spec_from_file_location(
    "run_market_crash_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_crash_branch_precursor_scan.py",
)
_crash = importlib.util.module_from_spec(_crash_spec)
_crash_spec.loader.exec_module(_crash)  # type: ignore[union-attr]

_rally_spec = importlib.util.spec_from_file_location(
    "run_market_rally_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_rally_branch_precursor_scan.py",
)
_rally = importlib.util.module_from_spec(_rally_spec)
_rally_spec.loader.exec_module(_rally)  # type: ignore[union-attr]

build_calendar = _crash.build_calendar
load_cohort = _crash.load_cohort
month_starts = _crash.month_starts
build_full_grid = _crash.build_full_grid
compute_lookback_percentiles = _crash.compute_lookback_percentiles
score_branches = _crash.score_branches
score_branches_rally = _rally.score_branches_rally


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """唯讀連線，不加 immutable=1 — replica DB 執行研究當下可能有背景
    backfill 併發寫入，immutable 會假設檔案內容不變而跳過鎖定，遇到併發
    寫入時會讀到不一致頁面（`database disk image is malformed`）。純
    `mode=ro` 仍是唯讀，但會走正常 SQLite 鎖定語意，可安全與併發寫入共存。
    """
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_benchmark_dates(conn: sqlite3.Connection, *, code: str = BENCHMARK) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_bars WHERE code=? AND close IS NOT NULL",
        (code,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def build_whale_filtered_panels(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    start: str,
    end: str,
    thresholds: list[float],
) -> tuple[dict[float, pd.DataFrame], pd.DataFrame]:
    """Month-chunked single pass over the cohort's (branch, stock, date)
    net_amt lines — mirrors `build_branch_day_panel`'s SQL/price-join
    pattern exactly (same cohort, same price source) for comparability,
    but keeps stock-day granularity long enough to apply an
    ``abs(net_amt) >= threshold`` whale filter *before* summing to
    (branch, date), instead of summing unconditionally.

    Returns:
      panels: {threshold_ntd: DataFrame[securities_trader_id, trade_date,
               net_amt, buy_amt, sell_amt, n_stocks]} (whale-filtered
               aggregate, one entry per requested threshold).
      retention: DataFrame with dollar-amount / line-item retention stats
               per threshold (see step 5 of the module docstring).
    """
    id_set = set(ids)
    months = month_starts(start, end)
    per_threshold_chunks: dict[float, list[pd.DataFrame]] = {t: [] for t in thresholds}
    total_abs_amt = 0.0
    total_line_items = 0
    retained_abs_amt = {t: 0.0 for t in thresholds}
    retained_line_items = {t: 0 for t in thresholds}

    for mi, (a, b) in enumerate(months, 1):
        t0 = time.time()
        raw = pd.read_sql_query(
            """
            SELECT securities_trader_id, trade_date, stock_id, buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND trade_date >= ? AND trade_date <= ?
              AND stock_id != '__EMPTY__'
              AND length(stock_id)=4
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
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
            """
            SELECT stock_id, trade_date, close
            FROM stock_daily_bars
            WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
              AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
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
        merged["buy_amt"] = merged["buy"].astype(float) * merged["close"].astype(float)
        merged["sell_amt"] = merged["sell"].astype(float) * merged["close"].astype(float)

        abs_amt = merged["net_amt"].abs()
        total_abs_amt += float(abs_amt.sum())
        total_line_items += int(len(merged))

        for th in thresholds:
            mask = abs_amt >= th
            retained_abs_amt[th] += float(abs_amt[mask].sum())
            retained_line_items[th] += int(mask.sum())
            sub = merged[mask]
            if sub.empty:
                continue
            grp = sub.groupby(["securities_trader_id", "trade_date"], as_index=False).agg(
                net_amt=("net_amt", "sum"),
                buy_amt=("buy_amt", "sum"),
                sell_amt=("sell_amt", "sum"),
                n_stocks=("stock_id", "nunique"),
            )
            per_threshold_chunks[th].append(grp)

        print(
            f"  [{mi}/{len(months)}] {a}..{b} raw={len(raw):,} merged={len(merged):,} "
            f"({time.time()-t0:.1f}s)",
            flush=True,
        )

    empty_cols = ["securities_trader_id", "trade_date", "net_amt", "buy_amt", "sell_amt", "n_stocks"]
    panels = {
        th: (pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=empty_cols))
        for th, chunks in per_threshold_chunks.items()
    }

    retention = pd.DataFrame(
        [
            {
                "threshold_ntd": th,
                "total_abs_net_amt_ntd": total_abs_amt,
                "retained_abs_net_amt_ntd": retained_abs_amt[th],
                "retained_amt_pct": (
                    round(retained_abs_amt[th] / total_abs_amt * 100.0, 2) if total_abs_amt else None
                ),
                "total_line_items": total_line_items,
                "retained_line_items": retained_line_items[th],
                "retained_line_items_pct": (
                    round(retained_line_items[th] / total_line_items * 100.0, 3)
                    if total_line_items
                    else None
                ),
            }
            for th in thresholds
        ]
    )
    return panels, retention


def compare_to_original(
    whale_scores: pd.DataFrame,
    orig_scores: pd.DataFrame,
    *,
    hit_min: int = 3,
    p_max: float = 0.10,
) -> dict:
    """Overlap % + Spearman rho of hit_count ranking, whale-filtered vs.
    original unfiltered list — same comparison style as
    `xsec_precursor_comparison.md`."""
    if whale_scores.empty or orig_scores.empty:
        return {
            "n_elite_orig": 0,
            "n_elite_whale": 0,
            "n_elite_both": 0,
            "overlap_pct_of_orig": None,
            "spearman_rho": None,
            "spearman_p": None,
            "elite_orig_ids": set(),
            "elite_whale_ids": set(),
        }
    elite_orig = set(
        orig_scores.loc[
            (orig_scores["hit_count"] >= hit_min) & (orig_scores["p_value_one_sided"] < p_max),
            "branch_id",
        ]
    )
    elite_whale = set(
        whale_scores.loc[
            (whale_scores["hit_count"] >= hit_min) & (whale_scores["p_value_one_sided"] < p_max),
            "branch_id",
        ]
    )
    both = elite_orig & elite_whale

    merged = orig_scores[["branch_id", "hit_count"]].merge(
        whale_scores[["branch_id", "hit_count"]],
        on="branch_id",
        how="outer",
        suffixes=("_orig", "_whale"),
    ).fillna(0)
    rho, p = stats.spearmanr(merged["hit_count_orig"], merged["hit_count_whale"])

    return {
        "n_elite_orig": len(elite_orig),
        "n_elite_whale": len(elite_whale),
        "n_elite_both": len(both),
        "overlap_pct_of_orig": round(len(both) / len(elite_orig) * 100.0, 1) if elite_orig else None,
        "spearman_rho": round(float(rho), 4),
        "spearman_p": round(float(p), 4),
        "elite_orig_ids": elite_orig,
        "elite_whale_ids": elite_whale,
    }


def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    coverage: dict,
    retention: pd.DataFrame,
    crash_results: dict[float, pd.DataFrame],
    rally_results: dict[float, pd.DataFrame],
    orig_crash_scores: pd.DataFrame,
    orig_rally_scores: pd.DataFrame,
    crash_comparisons: dict[float, dict],
    rally_comparisons: dict[float, dict],
    name_map: dict[str, str],
) -> None:
    thresholds = protocol["thresholds_ntd"]

    def fmt_ntd(x: float) -> str:
        return f"{x/1e8:.1f}億"

    def elite_table(df: pd.DataFrame) -> list[str]:
        sig = df[(df["hit_count"] >= 3) & (df["p_value_one_sided"] < 0.10)] if not df.empty else df
        lines = [
            "| 分點 | hit/總事件 | 命中率% | lift | p值 |",
            "|------|-----------:|--------:|-----:|----:|",
        ]
        if sig.empty:
            lines.append("| （無） | | | | |")
            return lines
        for r in sig.itertuples():
            lines.append(
                f"| {r.branch_name} (`{r.branch_id}`) | {r.hit_count}/{r.n_episodes_scored} | "
                f"{r.hit_rate_pct:.1f} | {r.lift:.2f} | {r.p_value_one_sided:.4f} |"
            )
        return lines

    lines = [
        "# 大戶過濾（whale filter）分點 precursor 訊號 · 大跌/大漲對照",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "",
        "## 動機",
        "",
        "既有 `market_crash_precursor_summary.md` / `market_rally_precursor_summary.md` 的分點訊號"
        "＝分點當日對全市場所有個股 net(股)×close 加總，可能混雜少數大戶級（單筆金額大）個股-日"
        "部位與大量散戶級（小額）個股-日部位。本研究檢驗：只保留「單筆金額 ≥ 門檻」的個股-日部位"
        "再加總（whale filter，只參考大戶（單筆金額≥門檻）的訊號、排除散戶噪音），訊號是否比未過濾版更乾淨。",
        "",
        "## 凍結協議",
        "",
        f"- 視窗：{protocol['start']}..{protocol['end']}（與既有 pipeline 完全相同）",
        f"- 分點宇宙：2 年內有效交易日 ≥ {protocol['min_branch_dates']}（cohort n={coverage['n_cohort']}，與既有清單相同）",
        f"- Whale 門檻（單筆金額絕對值 ≥ 門檻才計入加總，非 cap/clip）："
        + "、".join(fmt_ntd(t) for t in thresholds),
        f"- 前 {protocol['lookback_days']} 個交易日累計 + 自我常態化百分位 + 跨事件命中率單尾二項檢定："
        "方法與既有 pipeline 完全相同（`compute_lookback_percentiles` / `score_branches` /"
        " `score_branches_rally`，程式碼原樣 import 重用）。",
        f"- Episode 檔案直接重用既有 `market_crash_precursor_episodes.csv`"
        f"（{coverage['n_crash_episodes']} 個事件）／`market_rally_precursor_episodes.csv`"
        f"（{coverage['n_rally_episodes']} 個事件），未重新計算。",
        f"- 資料來源：`{protocol['db']}`（replica，唯讀 `mode=ro`，"
        "非 `immutable=1` — 見下方限制）。",
        "",
        "## 各門檻保留的金額／個股-日 row 數比例",
        "",
        "說明每個門檻的激進程度：門檻越高，保留的金額比例通常遠高於保留的 row 數比例"
        "（因為大額 row 數量少但每筆金額大）。",
        "",
        "| 門檻 | 保留金額(NT$) | 佔全部金額% | 保留 row 數 | 佔全部 row 數% |",
        "|------|-------------:|-----------:|-----------:|--------------:|",
    ]
    for r in retention.itertuples():
        lines.append(
            f"| {fmt_ntd(r.threshold_ntd)} | {r.retained_abs_net_amt_ntd:,.0f} | "
            f"{r.retained_amt_pct:.1f}% | {r.retained_line_items:,} | {r.retained_line_items_pct:.2f}% |"
        )
    lines += [
        f"",
        f"（全部金額基準：{retention['total_abs_net_amt_ntd'].iloc[0]:,.0f} NT$；"
        f"全部 row 數基準：{retention['total_line_items'].iloc[0]:,}）",
        "",
        "## 大跌方向（賣超先行）· 3 門檻 vs. 原始未過濾清單",
        "",
        f"原始未過濾清單（self-ts, hit_count≥3, p<0.10）通過家數："
        f"**{crash_comparisons[thresholds[0]]['n_elite_orig']}**"
        "（見 `market_crash_precursor_branch_scores.csv`）。",
        "",
        "| 門檻 | whale 通過家數 | 與原始重疊家數 | overlap%(以原始為分母) | Spearman ρ(hit_count) | ρ p值 |",
        "|------|-----:|-----:|-----:|-----:|-----:|",
    ]
    for th in thresholds:
        c = crash_comparisons[th]
        lines.append(
            f"| {fmt_ntd(th)} | {c['n_elite_whale']} | {c['n_elite_both']} | "
            f"{c['overlap_pct_of_orig'] if c['overlap_pct_of_orig'] is not None else 'NA'}% | "
            f"{c['spearman_rho']} | {c['spearman_p']} |"
        )

    lines += ["", "### 原始未過濾清單（對照，取自 `market_crash_precursor_branch_scores.csv`）", ""]
    lines += elite_table(orig_crash_scores)

    for th in thresholds:
        lines += [
            "",
            f"### Whale 門檻 {fmt_ntd(th)} · 大跌方向通過清單",
            "",
        ]
        lines += elite_table(crash_results[th])
        new_ids = crash_comparisons[th]["elite_whale_ids"] - crash_comparisons[th]["elite_orig_ids"]
        if new_ids:
            names = "、".join(f"{name_map.get(i,'')}(`{i}`)" for i in sorted(new_ids))
            lines.append("")
            lines.append(f"新增（原始清單未通過，whale 過濾後新通過）：{names}")

    lines += [
        "",
        "## 大漲方向（買超先行）· 3 門檻 vs. 原始未過濾清單",
        "",
        f"原始未過濾清單（self-ts, hit_count≥3, p<0.10）通過家數："
        f"**{rally_comparisons[thresholds[0]]['n_elite_orig']}**"
        "（見 `market_rally_precursor_branch_scores.csv`，訊噪比已知偏高——"
        "見 `xsec_precursor_comparison.md`）。",
        "",
        "| 門檻 | whale 通過家數 | 與原始重疊家數 | overlap%(以原始為分母) | Spearman ρ(hit_count) | ρ p值 |",
        "|------|-----:|-----:|-----:|-----:|-----:|",
    ]
    for th in thresholds:
        c = rally_comparisons[th]
        lines.append(
            f"| {fmt_ntd(th)} | {c['n_elite_whale']} | {c['n_elite_both']} | "
            f"{c['overlap_pct_of_orig'] if c['overlap_pct_of_orig'] is not None else 'NA'}% | "
            f"{c['spearman_rho']} | {c['spearman_p']} |"
        )

    lines += ["", "### 原始未過濾清單 Top20（對照，取自 `market_rally_precursor_branch_scores.csv`）", ""]
    lines += elite_table(orig_rally_scores.head(20)) if not orig_rally_scores.empty else ["（無資料）"]

    for th in thresholds:
        lines += [
            "",
            f"### Whale 門檻 {fmt_ntd(th)} · 大漲方向通過清單",
            "",
        ]
        lines += elite_table(rally_results[th])
        new_ids = rally_comparisons[th]["elite_whale_ids"] - rally_comparisons[th]["elite_orig_ids"]
        if new_ids:
            names = "、".join(f"{name_map.get(i,'')}(`{i}`)" for i in sorted(new_ids))
            lines.append("")
            lines.append(f"新增（原始清單未通過，whale 過濾後新通過）：{names}")

    lines += [
        "",
        "## 結論",
        "",
        "（依上方數字填寫：whale filter 是否縮小/清理大漲清單、是否改變大跌清單核心成員"
        "或僅提升信心、overlap% 與 Spearman ρ 隨門檻升高的走勢。）",
        "",
        "## 限制與注意事項",
        "",
        "- 三個門檻共用同一組 (分點,個股,日) 明細單次月分塊掃描產生，"
        "與既有未過濾 pipeline 用完全相同的 SQL WHERE 條件與收盤價 join 邏輯"
        "（`stock_broker_branch_daily` join `stock_daily_bars`），僅在加總前插入"
        "`abs(net_amt) >= threshold` 篩選，因此三個門檻與原始清單在同一分點宇宙、"
        "同一價格來源、同一事件檔案下可直接比較。",
        "- whale filter 後某些（分點,日）可能完全沒有大額 row（該分點當日全部是小額進出），"
        "此時 `net_amt_whale=0`（視為中性，非缺值），與既有 pipeline「無交易紀錄=0」的慣例一致。",
        "- 資料來源改用 replica DB（`data/replica/stocks.db`），執行研究當下該檔案有背景 backfill"
        "程序併發寫入；唯讀連線改用 `mode=ro`（非 `immutable=1`）以避免併發寫入造成的"
        "`database disk image is malformed` 讀取錯誤（immutable 模式會跳過 SQLite 檔案鎖定，"
        "假設檔案內容在連線期間不變，遇到併發寫入即讀到不一致頁面）。",
        "- 樣本事件數少（大跌 11 個、大漲 9 個），跨事件統計檢定力有限，本研究維持探索性定性，"
        "不作為可下單門檻依據。",
        "- `stock_broker_branch_daily` 為 FinMind 分點進出（非其真實庫存變化），"
        "net_amt_whale 僅代表「當日大額進出方向」，非「持股變化」。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Whale-size filter market precursor scan")
    ap.add_argument("--start", default="2024-07-21")
    ap.add_argument("--end", default="2026-07-20")
    ap.add_argument("--min-branch-dates", type=int, default=450)
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument(
        "--threshold-ntd",
        type=float,
        nargs="+",
        default=[100_000_000.0, 200_000_000.0, 500_000_000.0],
        help="whale 門檻（NT$，單筆|net_amt|絕對值），預設 1億/2億/5億",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
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
        "--orig-crash-scores-csv",
        type=Path,
        default=OUT / "market_crash_precursor_branch_scores.csv",
    )
    ap.add_argument(
        "--orig-rally-scores-csv",
        type=Path,
        default=OUT / "market_rally_precursor_branch_scores.csv",
    )
    args = ap.parse_args()
    thresholds = sorted(args.threshold_ntd)

    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)

    print("building calendar …", flush=True)
    cal = build_calendar(conn, args.start, args.end)
    bench_dates = load_benchmark_dates(conn)
    cal_before = len(cal)
    cal = [d for d in cal if d in bench_dates]
    print(
        f"calendar n={len(cal)} {cal[0]}..{cal[-1]} "
        f"(dropped {cal_before - len(cal)} day(s) with missing {BENCHMARK} close)",
        flush=True,
    )

    print("loading branch cohort (2y deep coverage) — full-table scan …", flush=True)
    t0 = time.time()
    cohort = load_cohort(conn, start=args.start, end=args.end, min_dates=args.min_branch_dates)
    print(f"cohort n={len(cohort)} ({time.time()-t0:.1f}s)", flush=True)
    ids = cohort["id"].tolist()
    name_map = dict(zip(cohort["id"], cohort["name"]))

    print(f"building whale-filtered panels for thresholds={thresholds} (single month-chunked pass) …", flush=True)
    t0 = time.time()
    panels, retention = build_whale_filtered_panels(
        conn, ids, start=args.start, end=args.end, thresholds=thresholds
    )
    conn.close()
    print(f"done ({time.time()-t0:.1f}s)", flush=True)

    crash_episodes = pd.read_csv(args.crash_episodes_csv, dtype={"trade_date": str})
    rally_episodes = pd.read_csv(args.rally_episodes_csv, dtype={"trade_date": str})
    orig_crash_scores = pd.read_csv(args.orig_crash_scores_csv, dtype={"branch_id": str})
    orig_rally_scores = pd.read_csv(args.orig_rally_scores_csv, dtype={"branch_id": str})

    crash_results: dict[float, pd.DataFrame] = {}
    rally_results: dict[float, pd.DataFrame] = {}
    crash_comparisons: dict[float, dict] = {}
    rally_comparisons: dict[float, dict] = {}
    crash_rows: list[pd.DataFrame] = []
    rally_rows: list[pd.DataFrame] = []

    for th in thresholds:
        print(f"scoring threshold={th:,.0f} …", flush=True)
        grid = build_full_grid(panels[th], ids, cal)
        scored = compute_lookback_percentiles(grid, args.lookback_days)

        crash_scores = score_branches(scored, crash_episodes, name_map, tail_pct=args.tail_pct)
        rally_scores = score_branches_rally(scored, rally_episodes, name_map, tail_pct=args.tail_pct)
        crash_results[th] = crash_scores
        rally_results[th] = rally_scores

        crash_comparisons[th] = compare_to_original(crash_scores, orig_crash_scores)
        rally_comparisons[th] = compare_to_original(rally_scores, orig_rally_scores)

        cs = crash_scores.copy()
        cs.insert(0, "threshold_ntd", th)
        crash_rows.append(cs)
        rs = rally_scores.copy()
        rs.insert(0, "threshold_ntd", th)
        rally_rows.append(rs)

    crash_out = pd.concat(crash_rows, ignore_index=True) if crash_rows else pd.DataFrame()
    rally_out = pd.concat(rally_rows, ignore_index=True) if rally_rows else pd.DataFrame()
    crash_out.to_csv(OUT / "whale_filter_precursor_branch_scores_crash.csv", index=False)
    rally_out.to_csv(OUT / "whale_filter_precursor_branch_scores_rally.csv", index=False)
    print(
        f"wrote whale_filter_precursor_branch_scores_crash.csv rows={len(crash_out)}, "
        f"whale_filter_precursor_branch_scores_rally.csv rows={len(rally_out)}",
        flush=True,
    )

    protocol = {
        "start": args.start,
        "end": args.end,
        "min_branch_dates": args.min_branch_dates,
        "lookback_days": args.lookback_days,
        "tail_pct": args.tail_pct,
        "thresholds_ntd": thresholds,
        "db": str(args.db),
    }
    coverage = {
        "n_cohort": len(ids),
        "n_calendar_days": len(cal),
        "n_crash_episodes": int(crash_episodes["episode_first"].sum()),
        "n_rally_episodes": int(rally_episodes["episode_first"].sum()),
    }
    summary_path = OUT / "whale_filter_precursor_summary.md"
    write_summary_md(
        summary_path,
        protocol=protocol,
        coverage=coverage,
        retention=retention,
        crash_results=crash_results,
        rally_results=rally_results,
        orig_crash_scores=orig_crash_scores,
        orig_rally_scores=orig_rally_scores,
        crash_comparisons=crash_comparisons,
        rally_comparisons=rally_comparisons,
        name_map=name_map,
    )
    print(f"wrote {summary_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
