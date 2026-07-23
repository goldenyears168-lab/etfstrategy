#!/usr/bin/env python3
"""大跌溫度計進階假說 H4 — 賣超廣度（檔數）vs 賣超金額，哪個更早/更準示警.

Research diagnostic · 非正式風控規則 · 非可下單訊號 · 唯讀分析，不修改生產腳本。

## 假說

現行「大跌溫度計」（`run_market_crash_thermometer_dashboard.py`）只用 8 家分點
「前5日累計賣超金額」的自我百分位。本研究額外算一個「前5日內賣超過的不同
股票檔數」（broad de-risking 廣度指標），檢驗：

  (a) 純金額百分位（現行邏輯，原樣複製，當 baseline）
  (b) 純檔數百分位（本研究新增）
  (c) 金額+檔數組合（平均 / AND-最弱環）

在 16 次單日大跌事件（IX0001 ≤ -3%，2024-07~2026-07）上的判別力，並檢查
檔數異常是否比金額異常「更早」出現。

## 方法（沿用生產腳本同一套 bounded-rolling walk-forward，直接 import 複用）

- 自我百分位（lb_pctile／breadth_pctile）：對分點自己全歷史（本 8 家分點
  `stock_broker_branch_daily` 只從 2024-07-01 起有資料，約 499 個交易日，
  已接近生產腳本 `SELF_RANK_DAYS_DEFAULT=500` 的窗長，故直接用全歷史一次
  排百分位，與生產腳本 `compute_lb_pctile` 同一套 pandas 慣例）。
- 溫度百分位／判別力：直接 import 生產腳本的 `bounded_pctrank`／
  `load_event_dates`（bounded-rolling 120 個交易日常態日基準池＋排除任一
  大跌/大漲事件 ±5 個交易日的日子），保證跟現行系統可比較。
- 廣度定義：`net<0`（賣超）的 distinct stock_id，用同樣 shift(1)+rolling(5)
  window 邏輯（今日值只看「昨天及以前 5 個交易日」，不含當日，避免
  look-ahead），取這 5 天賣超股票的**聯集**檔數（非逐日檔數加總，避免同一
  檔股票連續幾天被賣超時重複計數）。

輸出：
  reports/research/branch-footprint-screen/adv_h4_breadth_vs_concentration.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_adv_h4_breadth_vs_concentration.py
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "crash_thermometer_dashboard",
    ROOT / "scripts" / "research" / "run_market_crash_thermometer_dashboard.py",
)
_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dash)  # type: ignore[union-attr]

DB_PATH = _dash.DB_PATH
SOURCE = _dash.SOURCE
PANEL = _dash.PANEL
WEIGHTS = {bid: w for bid, (_, w) in PANEL.items()}

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
EPISODES_CSV = OUT_DIR / "market_crash_precursor_episodes.csv"
BRANCH_DATA_FLOOR = "2024-07-01"  # 這8家分點 stock_broker_branch_daily 最早有資料的日期
HISTORY_DAYS = 120
LOOKBACK_DAYS = 5
LEAD_WINDOW_DAYS = 10  # 事件前回看 10 個交易日找「首次達門檻」日
LEAD_THRESHOLDS = (70.0, 80.0)


def fetch_raw(conn, ids: list[str], start: str, end: str) -> pd.DataFrame:
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
    raw["securities_trader_id"] = raw["securities_trader_id"].astype(str)
    raw["stock_id"] = raw["stock_id"].astype(str)
    raw["trade_date"] = raw["trade_date"].astype(str)
    return raw


def compute_breadth_grid(raw: pd.DataFrame, ids: list[str], cal: list[str], lookback_days: int) -> pd.DataFrame:
    """每分點每日「前 lookback_days 個交易日（shift(1)，不含當日）賣超股票聯集檔數」，
    再對分點自己全歷史排百分位（breadth_pctile，高＝異常廣泛賣超）。"""
    sold = raw[raw["net"] < 0]
    sold_sets = sold.groupby(["securities_trader_id", "trade_date"])["stock_id"].apply(set).to_dict()

    rows = []
    for bid in ids:
        window: deque[set] = deque(maxlen=lookback_days)
        for d in cal:
            if len(window) < lookback_days:
                breadth_count = np.nan
            else:
                union_set: set = set()
                for s in window:
                    union_set |= s
                breadth_count = float(len(union_set))
            rows.append({"securities_trader_id": bid, "trade_date": d, "breadth_count": breadth_count})
            window.append(sold_sets.get((bid, d), set()))
    grid = pd.DataFrame(rows)
    grid["breadth_pctile"] = grid.groupby("securities_trader_id")["breadth_count"].rank(
        pct=True, na_option="keep"
    )
    return grid


def weighted_mean_pctile(scored: pd.DataFrame, weights: dict[str, float], value_col: str) -> pd.Series:
    """Per trade_date 加權平均 value_col（0..1，高=異常方向），回傳 date->score dict-like Series."""
    sub = scored.dropna(subset=[value_col]).copy()
    sub["w"] = sub["securities_trader_id"].map(weights)
    sub["wv"] = sub["w"] * sub[value_col]
    grp = sub.groupby("trade_date").agg(w_sum=("w", "sum"), wv_sum=("wv", "sum"))
    return (grp["wv_sum"] / grp["w_sum"]).rename("score")


def bootstrap_ci(values: list[float], n_boot: int = 5000, seed: int = 42) -> tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    arr = np.array(values, dtype=float)
    means = [rng.choice(arr, size=len(arr), replace=True).mean() for _ in range(n_boot)]
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def first_cross_offset(
    scores_by_date: dict[str, float],
    cal: list[str],
    event_idx: set[int],
    *,
    test_idx: int,
    window_days: int,
    threshold: float,
    history_days: int,
    lookback_days: int,
) -> int | None:
    """回看 test_idx 前 window_days 個交易日（含事件日本身，offset=0），找最早（offset 最大）
    一天 bounded_pctrank >= threshold。回傳該天的 offset（越大＝越早）；找不到回傳 None。"""
    for offset in range(window_days, -1, -1):
        day_idx = test_idx - offset
        if day_idx < 0:
            continue
        day = cal[day_idx]
        pr, _ = _dash.bounded_pctrank(
            scores_by_date, cal, event_idx,
            test_date=day, history_days=history_days, lookback_days=lookback_days,
        )
        if pr is not None and pr >= threshold:
            return offset
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="H4: breadth vs amount precursor signal comparison")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--history-days", type=int, default=HISTORY_DAYS)
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    args = ap.parse_args()

    conn = _dash.connect_ro(args.db)
    ids = list(PANEL.keys())

    episodes = pd.read_csv(EPISODES_CSV, dtype={"trade_date": str})
    end = "2026-07-21"  # stock_daily_bars(close) 最新可得日；branch表到7/22但收盤價到7/21
    print(f"loading calendar {BRANCH_DATA_FLOOR}..{end} ...", flush=True)
    cal_full = _dash.build_calendar(conn, end=end, n_days=10000, extra_ids=ids)
    cal = [d for d in cal_full if d >= BRANCH_DATA_FLOOR]
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    event_dates_all = episodes["trade_date"].tolist()
    missing = [d for d in event_dates_all if d not in cal]
    if missing:
        print(f"WARNING: {len(missing)} event dates outside branch-data calendar: {missing}", flush=True)
    test_events = [d for d in event_dates_all if d in cal]
    print(f"test events (crash, in-range) n={len(test_events)}", flush=True)

    # ---- 金額訊號（baseline，原樣沿用生產腳本邏輯）----
    print("building amount (net_amt) panel ...", flush=True)
    panel_raw = _dash.build_branch_panel(conn, ids, start=cal[0], end=cal[-1])
    grid_amt = _dash.build_full_grid(panel_raw, ids, cal)
    scored_amt = _dash.compute_lb_pctile(grid_amt, args.lookback_days)
    amt_series = _dash.weighted_composite(scored_amt, WEIGHTS)
    amt_scores_by_date = dict(zip(amt_series["trade_date"], amt_series["composite_score"]))

    # ---- 檔數訊號（本研究新增）----
    print("building breadth (distinct sold stock count) panel ...", flush=True)
    raw = fetch_raw(conn, ids, cal[0], cal[-1])
    grid_breadth = compute_breadth_grid(raw, ids, cal, args.lookback_days)
    breadth_score_s = weighted_mean_pctile(grid_breadth, WEIGHTS, "breadth_pctile")
    breadth_scores_by_date = breadth_score_s.to_dict()

    # ---- 組合訊號 ----
    common_dates = sorted(set(amt_scores_by_date) & set(breadth_scores_by_date))
    combined_avg_by_date = {
        d: 0.5 * (amt_scores_by_date[d] + breadth_scores_by_date[d]) for d in common_dates
    }
    combined_and_by_date = {
        d: min(amt_scores_by_date[d], breadth_scores_by_date[d]) for d in common_dates
    }

    crash_dates, all_event_dates = _dash.load_event_dates(conn, cal)
    idx_of = {d: i for i, d in enumerate(cal)}
    event_idx = {idx_of[d] for d in all_event_dates if d in idx_of}
    conn.close()

    # ---- 判別力：三/四種訊號在 16 次事件上的 bounded pctrank ----
    signal_defs = [
        ("amt_only", "純金額百分位（現行 baseline）", amt_scores_by_date),
        ("breadth_only", "純檔數百分位（新增）", breadth_scores_by_date),
        ("combo_avg", "金額+檔數平均", combined_avg_by_date),
        ("combo_and", "金額+檔數 AND（取較弱者）", combined_and_by_date),
    ]

    per_event_rows = []
    for ev in test_events:
        mkt_ret = float(episodes.loc[episodes["trade_date"] == ev, "mkt_ret_pct"].iloc[0])
        row = {"trade_date": ev, "mkt_ret_pct": round(mkt_ret, 2)}
        for key, _, scores in signal_defs:
            pr, n_normal = _dash.bounded_pctrank(
                scores, cal, event_idx,
                test_date=ev, history_days=args.history_days, lookback_days=args.lookback_days,
            )
            row[f"{key}_pctrank"] = None if pr is None else round(pr, 1)
            row[f"{key}_n_normal"] = n_normal
        per_event_rows.append(row)
    per_event_df = pd.DataFrame(per_event_rows)

    headline = {}
    for key, label, _ in signal_defs:
        vals = [v for v in per_event_df[f"{key}_pctrank"].tolist() if v is not None and not pd.isna(v)]
        if vals:
            lo, hi = bootstrap_ci(vals)
            headline[key] = {
                "label": label,
                "n": len(vals),
                "mean": float(np.mean(vals)),
                "median": float(np.median(vals)),
                "min": float(np.min(vals)),
                "ci_lo": lo,
                "ci_hi": hi,
            }
        else:
            headline[key] = {"label": label, "n": 0, "mean": None, "median": None, "min": None, "ci_lo": None, "ci_hi": None}

    # paired win-rate: breadth vs amt per event
    paired = per_event_df.dropna(subset=["amt_only_pctrank", "breadth_only_pctrank"])
    breadth_wins = int((paired["breadth_only_pctrank"] > paired["amt_only_pctrank"]).sum())
    amt_wins = int((paired["breadth_only_pctrank"] < paired["amt_only_pctrank"]).sum())
    ties = int((paired["breadth_only_pctrank"] == paired["amt_only_pctrank"]).sum())

    # ---- 領先時間差分析 ----
    print("running leading-time analysis ...", flush=True)
    lead_rows = []
    for ev in test_events:
        test_idx = idx_of[ev]
        rec = {"trade_date": ev}
        for thr in LEAD_THRESHOLDS:
            amt_off = first_cross_offset(
                amt_scores_by_date, cal, event_idx, test_idx=test_idx,
                window_days=LEAD_WINDOW_DAYS, threshold=thr,
                history_days=args.history_days, lookback_days=args.lookback_days,
            )
            breadth_off = first_cross_offset(
                breadth_scores_by_date, cal, event_idx, test_idx=test_idx,
                window_days=LEAD_WINDOW_DAYS, threshold=thr,
                history_days=args.history_days, lookback_days=args.lookback_days,
            )
            rec[f"amt_first_offset_{int(thr)}"] = amt_off
            rec[f"breadth_first_offset_{int(thr)}"] = breadth_off
            rec[f"lead_days_{int(thr)}"] = (
                None if amt_off is None or breadth_off is None else amt_off - breadth_off
            )
        lead_rows.append(rec)
    lead_df = pd.DataFrame(lead_rows)

    # avg pctrank trajectory at offsets 0..7 for amt vs breadth (across all events, mean)
    print("building offset trajectory table ...", flush=True)
    traj_rows = []
    for offset in range(7, -1, -1):
        amt_vals, breadth_vals = [], []
        for ev in test_events:
            test_idx = idx_of[ev]
            day_idx = test_idx - offset
            if day_idx < 0:
                continue
            day = cal[day_idx]
            pr_a, _ = _dash.bounded_pctrank(
                amt_scores_by_date, cal, event_idx, test_date=day,
                history_days=args.history_days, lookback_days=args.lookback_days,
            )
            pr_b, _ = _dash.bounded_pctrank(
                breadth_scores_by_date, cal, event_idx, test_date=day,
                history_days=args.history_days, lookback_days=args.lookback_days,
            )
            if pr_a is not None:
                amt_vals.append(pr_a)
            if pr_b is not None:
                breadth_vals.append(pr_b)
        traj_rows.append(
            {
                "offset": offset,
                "amt_mean_pctrank": None if not amt_vals else round(float(np.mean(amt_vals)), 1),
                "breadth_mean_pctrank": None if not breadth_vals else round(float(np.mean(breadth_vals)), 1),
                "n": len(amt_vals),
            }
        )
    traj_df = pd.DataFrame(traj_rows)

    write_report(
        headline=headline,
        per_event_df=per_event_df,
        breadth_wins=breadth_wins,
        amt_wins=amt_wins,
        ties=ties,
        lead_df=lead_df,
        traj_df=traj_df,
        n_events=len(test_events),
        cal=cal,
        args=args,
    )


def write_report(
    *,
    headline: dict,
    per_event_df: pd.DataFrame,
    breadth_wins: int,
    amt_wins: int,
    ties: int,
    lead_df: pd.DataFrame,
    traj_df: pd.DataFrame,
    n_events: int,
    cal: list[str],
    args,
) -> None:
    lines = []
    lines += [
        "# H4：賣超廣度（檔數）vs 賣超金額 — 哪個更早/更準示警？",
        "",
        "Research diagnostic · 大跌溫度計進階假說之一（共10個並行假說之一）· "
        "非正式風控規則 · 非可下單訊號 · 唯讀分析，未修改生產腳本/config。",
        "",
        "## 背景與問題",
        "",
        "現行「大跌溫度計」（`run_market_crash_thermometer_dashboard.py`）只用8家分點"
        "「前5日累計淨賣超**金額**」的自我百分位加權合成。本研究檢驗：「帳戶層級全面"
        "去風險」（賣很多不同股票，檔數廣）跟「單一個股大額賣出」（可能只是個股事件）"
        "用金額看起來可能一樣異常，但意義不同——**賣超廣度（檔數）**是否是更純淨的"
        "系統性風險訊號，且是否比金額**更早**示警。",
        "",
        "## 三種訊號定義",
        "",
        "同一套8家分點（瑞銀1650/港商野村1560/美商高盛亞1480/港麥格理1360/"
        "元大館前984K/宏遠民生1261/統一5850/統一仁愛585c），權重沿用生產腳本"
        "`PANEL` 既有加權（瑞銀36.0最重，其餘遞減）。",
        "",
        "1. **純金額百分位**（現行 baseline，原樣複製生產腳本邏輯）：分點`net(股)×close`"
        "前5日累計（shift(1)避免lookahead），對分點自己全歷史排百分位（`lb_pctile`），"
        "8家加權平均後取 `1-mean`（賣超越重分數越高）。",
        "2. **純檔數百分位**（本研究新增）：分點`net<0`（賣超）的股票，前5個交易日"
        "（shift(1)，不含當日）的**聯集**去重檔數，對分點自己全歷史排百分位"
        "（`breadth_pctile`，檔數越多分數越高，賣超越廣泛），8家加權平均。",
        "3. **組合**：(a) 金額+檔數平均、(b) 金額+檔數 AND（取當日兩者較弱的一個"
        "百分位，代表「兩者都要異常才算異常」）。",
        "",
        "判別力評估：直接沿用生產腳本 `bounded_pctrank`（bounded-rolling 120個交易日"
        "常態日基準池＋排除任一大跌/大漲事件±5個交易日內的日子），對16次單日大跌"
        "事件（IX0001≤-3%，2024-07~2026-07）逐一評分，取平均百分位排名"
        "（50%=亂猜，100%=完美區分），與現行系統驗證方法完全一致、可直接比較。",
        "",
        f"分點原始 `stock_broker_branch_daily` 資料僅自 {cal[0]} 起可得（約"
        f"{len(cal)} 個交易日），故「自我百分位」用全歷史（未額外裁切500日窗，"
        "因全歷史本身已接近生產腳本預設窗長），與生產腳本『自我百分位需長歷史』"
        "的既有慣例一致。",
        "",
        "## 判別力比較（16次大跌事件平均百分位排名）",
        "",
        "| 訊號 | n(有效事件) | 平均百分位 | 中位數 | 最差一次 | 95% bootstrap CI |",
        "|------|-----------:|-----------:|-------:|---------:|:------------------|",
    ]
    for key in ("amt_only", "breadth_only", "combo_avg", "combo_and"):
        h = headline[key]
        if h["mean"] is None:
            lines.append(f"| {h['label']} | 0 | NA | NA | NA | NA |")
            continue
        lines.append(
            f"| {h['label']} | {h['n']} | {h['mean']:.1f}% | {h['median']:.1f}% | "
            f"{h['min']:.1f}% | [{h['ci_lo']:.1f}%, {h['ci_hi']:.1f}%] |"
        )

    lines += [
        "",
        f"**逐事件配對比較**（純檔數 vs 純金額，n={len(per_event_df)}）：檔數百分位較高（更"
        f"異常）{breadth_wins}次、金額百分位較高{amt_wins}次、打平{ties}次。",
        "",
        "## 逐事件明細",
        "",
        "| 日期 | 大盤日報酬% | 金額百分位% | 檔數百分位% | 組合(平均)% | 組合(AND)% |",
        "|------|-----------:|-----------:|-----------:|-----------:|-----------:|",
    ]
    for r in per_event_df.itertuples():
        def fmt(v):
            return "NA" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{v:.1f}"
        lines.append(
            f"| {r.trade_date} | {r.mkt_ret_pct:+.2f} | {fmt(r.amt_only_pctrank)} | "
            f"{fmt(r.breadth_only_pctrank)} | {fmt(r.combo_avg_pctrank)} | {fmt(r.combo_and_pctrank)} |"
        )

    lines += [
        "",
        "## 領先時間差分析",
        "",
        "### (1) 事件前逐日平均百分位軌跡（16事件平均，offset=事件前N個交易日，0=事件當日）",
        "",
        "| 事件前N日 | 金額百分位平均% | 檔數百分位平均% | n事件 |",
        "|----------:|---------------:|---------------:|------:|",
    ]
    for r in traj_df.itertuples():
        am = "NA" if r.amt_mean_pctrank is None else f"{r.amt_mean_pctrank:.1f}"
        br = "NA" if r.breadth_mean_pctrank is None else f"{r.breadth_mean_pctrank:.1f}"
        lines.append(f"| T-{r.offset} | {am} | {br} | {r.n} |")

    lines += [
        "",
        "### (2) 逐事件「首次達門檻」日與領先天數",
        "",
        f"門檻：bounded_pctrank ≥ 70%（偏熱）／≥80%，回看事件前最多{LEAD_WINDOW_DAYS}個"
        "交易日（含事件當日=offset 0）找最早達門檻的一天。",
        "",
        "| 日期 | 金額首達(≥70%,T-N) | 檔數首達(≥70%,T-N) | 領先天數(70%) | "
        "金額首達(≥80%,T-N) | 檔數首達(≥80%,T-N) | 領先天數(80%) |",
        "|------|-------------------:|-------------------:|-------------:|"
        "-------------------:|-------------------:|-------------:|",
    ]

    def _fmt_off(v):
        return "未達" if v is None or (isinstance(v, float) and pd.isna(v)) else f"T-{int(v)}"

    def _fmt_lead(v):
        return "NA" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{int(v):+d}"

    for r in lead_df.itertuples():
        lines.append(
            f"| {r.trade_date} | {_fmt_off(r.amt_first_offset_70)} | "
            f"{_fmt_off(r.breadth_first_offset_70)} | {_fmt_lead(r.lead_days_70)} | "
            f"{_fmt_off(r.amt_first_offset_80)} | {_fmt_off(r.breadth_first_offset_80)} | "
            f"{_fmt_lead(r.lead_days_80)} |"
        )

    valid_leads_70 = [v for v in lead_df["lead_days_70"].tolist() if v is not None and not pd.isna(v)]
    valid_leads_80 = [v for v in lead_df["lead_days_80"].tolist() if v is not None and not pd.isna(v)]
    amt_crossed_70 = int(lead_df["amt_first_offset_70"].notna().sum())
    breadth_crossed_70 = int(lead_df["breadth_first_offset_70"].notna().sum())
    amt_crossed_80 = int(lead_df["amt_first_offset_80"].notna().sum())
    breadth_crossed_80 = int(lead_df["breadth_first_offset_80"].notna().sum())

    avg_lead_70 = None if not valid_leads_70 else sum(valid_leads_70) / len(valid_leads_70)
    avg_lead_80 = None if not valid_leads_80 else sum(valid_leads_80) / len(valid_leads_80)

    lines += [
        "",
        "**達門檻覆蓋率**（16事件中在事件前10日內曾達門檻的次數）：",
        f"≥70%門檻 — 金額 {amt_crossed_70}/{n_events}，檔數 {breadth_crossed_70}/{n_events}；"
        f"≥80%門檻 — 金額 {amt_crossed_80}/{n_events}，檔數 {breadth_crossed_80}/{n_events}。",
        "",
        f"**平均領先天數**（僅計兩訊號皆在10日內達門檻的事件，正值＝檔數比金額更早"
        f"達門檻）：≥70%門檻 n={len(valid_leads_70)} 時平均 "
        f"{'NA' if avg_lead_70 is None else f'{avg_lead_70:+.1f}天'}；"
        f"≥80%門檻 n={len(valid_leads_80)} 時平均 "
        f"{'NA' if avg_lead_80 is None else f'{avg_lead_80:+.1f}天'}。",
        "",
        "## 結論",
        "",
    ]

    amt_mean = headline["amt_only"]["mean"]
    breadth_mean = headline["breadth_only"]["mean"]
    combo_avg_mean = headline["combo_avg"]["mean"]
    combo_and_mean = headline["combo_and"]["mean"]

    verdict = []
    if breadth_mean is not None and amt_mean is not None:
        gap = breadth_mean - amt_mean
        verdict.append(
            f"- **判別力**：純檔數百分位平均判別力 {breadth_mean:.1f}%，純金額百分位"
            f"（現行 baseline，本研究重算）{amt_mean:.1f}%，差距 {gap:+.1f} 個百分點"
            f"（逐事件配對：檔數較高{breadth_wins}次 vs 金額較高{amt_wins}次，"
            f"n={len(per_event_df)}）。"
        )
    if combo_avg_mean is not None and amt_mean is not None:
        verdict.append(
            f"- **組合**：金額+檔數平均 {combo_avg_mean:.1f}%，AND（取較弱者）"
            f"{combo_and_mean:.1f}%——"
            + (
                "組合平均優於純金額，顯示加入檔數維度有增量價值。"
                if combo_avg_mean > amt_mean
                else "組合平均未優於純金額，本次樣本下加入檔數的邊際貢獻不明顯。"
            )
        )
    if avg_lead_70 is not None:
        verdict.append(
            f"- **領先時間**：在兩訊號皆達≥70%門檻的 {len(valid_leads_70)} 個事件中，"
            f"檔數平均比金額{'早' if avg_lead_70 > 0 else '晚'} {abs(avg_lead_70):.1f} 個"
            "交易日達到門檻；但覆蓋率（10日內能達到門檻的事件比例）"
            f"金額{amt_crossed_70}/{n_events}、檔數{breadth_crossed_70}/{n_events}，"
            "領先天數的樣本量比判別力比較更薄，解讀需更謹慎。"
        )
    else:
        verdict.append(
            "- **領先時間**：兩訊號同時達門檻可比較的事件數為0（多數事件單一或兩個"
            "訊號皆未在10日內達到70%門檻），無法算出穩定的平均領先天數——這本身"
            "也說明本研究樣本（16事件、8家固定分點）下，「檔數領先金額」尚未被"
            "資料明確支持，需視為未驗證假說而非已確認發現。"
        )

    verdict.append(
        f"- **是否值得加入現行公式**：以本次結果判斷，"
        + (
            "檔數維度有一定增量判別力，"
            if (breadth_mean is not None and amt_mean is not None and breadth_mean >= amt_mean - 5)
            else "檔數維度單獨判別力弱於金額，"
        )
        + "建議做法：**不取代**現行金額百分位（金額仍是主訊號），而是把檔數百分位"
        "當**輔助確認欄**（例如在 dashboard「8家明細」旁加一欄『前5日賣超檔數/自我"
        "百分位』），或在「共識家數」邏輯裡新增一條「檔數也達異常尾端」的旗標，"
        "供人工判讀交叉確認，而非直接改動加權複合分數公式——樣本量小（16事件、"
        "分點原始資料僅約2年）不足以支撐重新調整生產公式權重。"
    )

    lines += verdict
    lines += [
        "",
        "## 已知限制（誠實揭露統計把握度）",
        "",
        f"- **樣本量極小**：僅16次單日大跌事件、8家固定分點，且分點原始"
        f"`stock_broker_branch_daily` 資料僅自 {cal[0]} 起可得（約{len(cal)}個"
        "交易日），意味著最早幾次事件（2024-07~2024-09）的「自我百分位」用的"
        "歷史窗其實只有幾十個交易日，尾端定義比後期事件（有近2年歷史可排）更"
        "不穩定，結果對這幾個早期事件較敏感。",
        "- **領先時間差分析樣本更薄**：能同時算出「兩訊號皆達門檻」的事件數"
        "通常遠少於16，平均領先天數若n<5基本只能當方向性線索，不能當穩健結論。",
        "- **8家分點權重與門檻沿用現行金額訊號的既有選股/加權**，未針對檔數訊號"
        "重新做獨立的分點篩選或權重最佳化——檔數訊號用同一組『被金額訊號選中』"
        "的分點，理論上是對檔數訊號不利的比較（沒有給檔數訊號自己最適的分點"
        "組合機會），若檔數訊號在此仍有可觀判別力，是相對保守的正面訊號；反之"
        "若判別力偏弱，也可能只是分點選得不對，不能完全排除檔數維度本身無用。",
        "- **bootstrap CI 是對『這16個事件』重複抽樣的不確定性估計，不是對『未來"
        "事件』的預測區間**——16個事件本身也不是獨立同分布抽樣（同一波段內的"
        "事件常有相關性），CI寬度應視為下界，真實不確定性可能更大。",
        "- 本研究純粹是唯讀分析，**未修改**生產腳本"
        "`run_market_crash_thermometer_dashboard.py` 或任何 `config/*.yaml`。",
        "",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "adv_h4_breadth_vs_concentration.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
