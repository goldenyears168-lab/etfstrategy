#!/usr/bin/env python3
"""溫度計候選公式比較 · 同一套嚴謹 walk-forward 方法，換不同複合分數定義.

Research diagnostic · 非正式風控規則 · 非可下單訊號。

前幾輪驗證發現兩個問題：(1) 具名分點 panel 法在大跌/大漲的判別力都偏弱
（58.4%／53.0%）；(2) 全 cohort 自我百分位（self-ts `lb_pctile`）廣度指數
有長期趨勢污染，用全歷史 expanding pool 當基準會虛增判別力（大漲從
乾淨版 ~55-62% 灌水到 70.6%）。

本腳本在同一套「bounded rolling 常態日基準池」（預設120個交易日，天生
不會被長期趨勢污染）下，比較 4 種候選複合分數公式，找出何者判別力最高：

  1. self_ts_full_panel  — 既有動態 hit_count≥3&p<0.10 具名 panel，self-ts lb_pctile 均值
  2. self_ts_breadth      — 全cohort self-ts lb_pctile 廣度／均值（有既知趨勢污染風險）
  3. xsec_breadth         — 全cohort **同儕相對**（cross-sectional，每日跟當天其他分點比，
                            天生不受長期趨勢污染）百分位廣度／均值
  4. curated_fixed_panel  — 固定、經雙重驗證篩選過的分點清單（大跌：美商高盛亞1480+瑞銀1650
                            兩家 whale-filter 全門檻存活；大漲：gold-9 triangulated 9家），
                            self-ts lb_pctile 均值，不重新訓練（測試整段 rolling episode 樣本）

事件：沿用 Track C 擴大版滾動 3 日事件（大跌16個／大漲24個）。

輸出：
  reports/research/branch-footprint-screen/thermometer_candidates_comparison.csv
  reports/research/branch-footprint-screen/thermometer_candidates_summary.md
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen"

CRASH_GRID = OUT / "_market_crash_precursor_grid_cache.parquet"
RALLY_GRID = OUT / "_market_rally_precursor_grid_cache.parquet"
CRASH_EPISODES = OUT / "rolling_event_precursor_episodes_crash_wf.csv"
RALLY_EPISODES = OUT / "rolling_event_precursor_episodes_rally_wf.csv"

CRASH_CURATED = ["1480", "1650"]  # whale-filter 全門檻存活
RALLY_CURATED = ["1030", "1040", "1110", "8883", "8888", "888A", "9623", "9654", "9658"]  # gold-9


def load_grid(path: Path) -> pd.DataFrame:
    g = pd.read_parquet(path)
    g["securities_trader_id"] = g["securities_trader_id"].astype(str)
    g["trade_date"] = g["trade_date"].astype(str)
    g["xsec_pctile"] = g.groupby("trade_date")["lb_sum_ntd"].rank(pct=True, na_option="keep")
    return g


def daily_score(
    grid: pd.DataFrame, date: str, *, method: str, direction: str, curated_ids: list[str]
) -> float | None:
    day = grid[grid["trade_date"] == date]
    if method == "self_ts_breadth":
        col, sub = "lb_pctile", day
    elif method == "xsec_breadth":
        col, sub = "xsec_pctile", day
    elif method == "curated_fixed_panel":
        col = "lb_pctile"
        sub = day[day["securities_trader_id"].isin(curated_ids)]
    else:
        raise ValueError(method)
    sub = sub.dropna(subset=[col])
    if sub.empty:
        return None
    mean_p = float(sub[col].mean())
    return (1.0 - mean_p) if direction == "crash" else mean_p


def walk_forward_candidate(
    grid: pd.DataFrame,
    episodes: pd.DataFrame,
    *,
    direction: str,
    method: str,
    curated_ids: list[str],
    full_event_dates: list[str],
    lookback_days: int,
    normal_pool_window: int,
) -> pd.DataFrame:
    cal = sorted(grid["trade_date"].unique().tolist())
    idx_of = {d: i for i, d in enumerate(cal)}
    event_idx = sorted(idx_of[d] for d in full_event_dates if d in idx_of)

    def is_far(day_idx: int) -> bool:
        return all(abs(day_idx - ei) > lookback_days for ei in event_idx)

    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    ep_dates = [d for d in ep_dates if d in idx_of]

    rows = []
    for i, test_date in enumerate(ep_dates):
        test_idx = idx_of[test_date]
        floor_idx = max(0, test_idx - normal_pool_window) if normal_pool_window > 0 else 0
        normal_pool = [d for d in cal if floor_idx <= idx_of[d] < test_idx and is_far(idx_of[d])]
        test_score = daily_score(grid, test_date, method=method, direction=direction, curated_ids=curated_ids)
        normal_scores = [
            s for d in normal_pool
            if (s := daily_score(grid, d, method=method, direction=direction, curated_ids=curated_ids)) is not None
        ]
        pct_rank = None
        if test_score is not None and normal_scores:
            combined = normal_scores + [test_score]
            pct_rank = float(stats.percentileofscore(combined, test_score, kind="mean"))
        mkt_ret = float(episodes.loc[episodes["trade_date"] == test_date, "mkt_ret_pct"].iloc[0])
        rows.append(
            {
                "direction": direction,
                "method": method,
                "episode_index": i + 1,
                "trade_date": test_date,
                "mkt_ret_pct": round(mkt_ret, 3),
                "score": None if test_score is None else round(test_score, 4),
                "n_normal_days": len(normal_scores),
                "percentile_rank": None if pct_rank is None else round(pct_rank, 1),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare thermometer candidate formulas (same rigor)")
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--normal-pool-window", type=int, default=120)
    args = ap.parse_args()

    crash_grid = load_grid(CRASH_GRID)
    rally_grid = load_grid(RALLY_GRID)
    crash_episodes = pd.read_csv(CRASH_EPISODES, dtype={"trade_date": str})
    rally_episodes = pd.read_csv(RALLY_EPISODES, dtype={"trade_date": str})
    full_event_dates = sorted(
        set(crash_episodes.loc[crash_episodes["episode_first"], "trade_date"])
        | set(rally_episodes.loc[rally_episodes["episode_first"], "trade_date"])
    )

    all_results = []
    for direction, grid, episodes, curated in (
        ("crash", crash_grid, crash_episodes, CRASH_CURATED),
        ("rally", rally_grid, rally_episodes, RALLY_CURATED),
    ):
        for method in ("self_ts_breadth", "xsec_breadth", "curated_fixed_panel"):
            print(f"running {direction}/{method} ...", flush=True)
            res = walk_forward_candidate(
                grid, episodes, direction=direction, method=method, curated_ids=curated,
                full_event_dates=full_event_dates, lookback_days=args.lookback_days,
                normal_pool_window=args.normal_pool_window,
            )
            all_results.append(res)

    combined = pd.concat(all_results, ignore_index=True)
    out_csv = OUT / "thermometer_candidates_comparison.csv"
    combined.to_csv(out_csv, index=False)
    print(f"wrote {out_csv} rows={len(combined)}", flush=True)

    headline = (
        combined.dropna(subset=["percentile_rank"])
        .groupby(["direction", "method"])["percentile_rank"]
        .agg(n="count", avg_percentile_rank="mean")
        .reset_index()
    )
    headline["avg_percentile_rank"] = headline["avg_percentile_rank"].round(1)
    print(headline.to_string(index=False), flush=True)

    lines = [
        "# 溫度計候選公式比較（同一套 bounded-rolling walk-forward）",
        "",
        "Research diagnostic · 非正式風控規則 · 非可下單訊號。",
        "",
        f"常態日基準池：測試日前最近 {args.normal_pool_window} 個交易日（排除任一事件"
        f"±{args.lookback_days} 交易日窗），避免既知的長期趨勢污染問題"
        "（見 `breadth_walkforward_summary.md` 的趨勢污染發現）。",
        "",
        "候選公式：",
        "",
        "1. `self_ts_full_panel`（對照組，來自 `walkforward_precursor_summary_rollingevt.md`，"
        "動態具名 panel，hit_count≥3&p<0.10，逐事件重新訓練）：大跌58.4%／大漲53.0%",
        "2. `self_ts_breadth`：全cohort 自我百分位廣度／均值",
        "3. `xsec_breadth`：全cohort **同儕相對**百分位廣度／均值（每天跟當天其他分點比，"
        "天生不受長期趨勢污染）",
        f"4. `curated_fixed_panel`：固定分點清單，不重新訓練 —— 大跌用 whale-filter 全門檻"
        f"存活的 {'+'.join(CRASH_CURATED)}（美商高盛亞+瑞銀），大漲用 gold-9 triangulated "
        f"{len(RALLY_CURATED)} 家",
        "",
        "## Headline：平均百分位排名（50%=亂猜）",
        "",
        "| 方向 | 方法 | 事件數 | 平均百分位排名 |",
        "|------|------|------:|---------------:|",
    ]
    for r in headline.sort_values(["direction", "avg_percentile_rank"], ascending=[True, False]).itertuples():
        lines.append(f"| {r.direction} | {r.method} | {r.n} | {r.avg_percentile_rank:.1f}% |")
    lines.append("| crash | self_ts_full_panel（對照，n=9，逐事件訓練） | 9 | 58.4% |")
    lines.append("| rally | self_ts_full_panel（對照，n=17，逐事件訓練） | 17 | 53.0% |")

    lines += ["", "## 逐事件明細"]
    for direction, label in (("crash", "大跌"), ("rally", "大漲")):
        for method in ("self_ts_breadth", "xsec_breadth", "curated_fixed_panel"):
            sub = combined[(combined["direction"] == direction) & (combined["method"] == method)].sort_values("episode_index")
            lines += [
                "",
                f"### {label} · {method}",
                "",
                "| 事件序 | 日期 | 大盤日報酬% | 分數 | 常態日百分位排名% | 常態日數 |",
                "|------:|------|-----------:|-----:|-------------------:|--------:|",
            ]
            for r in sub.itertuples():
                sc = "NA" if r.score is None or pd.isna(r.score) else f"{r.score:.4f}"
                pr = "NA" if r.percentile_rank is None or pd.isna(r.percentile_rank) else f"{r.percentile_rank:.1f}"
                lines.append(f"| {r.episode_index} | {r.trade_date} | {r.mkt_ret_pct:+.2f} | {sc} | {pr} | {r.n_normal_days} |")

    lines += [
        "",
        "## 限制與注意事項",
        "",
        "- `curated_fixed_panel` 的分點清單本身是用「全部歷史資料」篩出來的"
        "（whale-filter 全門檻存活 / gold-9 triangulated），清單選取過程本身"
        "看過全部事件，不是嚴格 walk-forward（清單沒有隨測試事件往前推的"
        "訓練期重新選取）；本比較只是誠實檢驗「這份固定清單」在擴大版事件"
        "樣本上的表現，不能宣稱是無look-ahead的完整驗證。",
        "- 常態日基準池限制在120個交易日內，早期事件（訓練期不足120天）的"
        "比較常態日數會偏少，估計較不穩定，請對照常態日數欄位判斷。",
        "",
    ]
    summary_path = OUT / "thermometer_candidates_summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {summary_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
