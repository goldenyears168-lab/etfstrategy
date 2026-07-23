#!/usr/bin/env python3
"""買超／賣超廣度指數 · 嚴格 walk-forward 驗證（大跌 + 大漲，同一套方法）.

Research diagnostic · 非正式風控規則 · 非可下單訊號。

`rally_buying_breadth_dashboard.md` 目前引用的「82.9% walk-forward 判別力」
其實是 `run_market_precursor_walkforward_score.py`（具名分點 panel 法）算出來
的數字，不是廣度／複合分數本身的 walk-forward 結果——廣度指數過去只做過
「用同一批事件回測門檻 recall/false-alarm」的**in-sample**檢驗，從未真正
做過 no-lookahead 驗證。本腳本補上這一步，用擴大版滾動事件（Track C，16個
大跌 + 24個大漲）對廣度／複合分數做跟 `run_market_precursor_walkforward_score.py`
同一套邏輯的 walk-forward：測試事件的分數，只跟「嚴格早於它、且不落在任何
事件 ±lookback_days 交易日窗內」的常態日分數比百分位排名。

廣度分數本身不需要「訓練」（是全 cohort 固定公式，不像具名 panel 需要用
過去事件選名單），所以這裡的 walk-forward 主要是在確保「常態日基準」不
偷看未來、不被鄰近事件污染，跟具名 panel 版本的驗證邏輯一致，方便直接比較
兩種方法在同一組事件上的判別力差異。

輸出：
  reports/research/branch-footprint-screen/breadth_walkforward_results.csv
  reports/research/branch-footprint-screen/breadth_walkforward_summary.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_breadth_walkforward.py
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


def build_breadth_series(grid: pd.DataFrame, *, direction: str) -> pd.DataFrame:
    scored = grid.dropna(subset=["lb_pctile"]).copy()
    if direction == "crash":
        scored["lb_pctile_signed"] = 1.0 - scored["lb_pctile"]
        extreme_mask = scored["lb_pctile"] <= 0.10
    else:
        scored["lb_pctile_signed"] = scored["lb_pctile"]
        extreme_mask = scored["lb_pctile"] >= 0.90
    scored["extreme"] = extreme_mask
    grp = scored.groupby("trade_date")
    out = grp.apply(
        lambda g: pd.Series(
            {
                "breadth_pct": float(g["extreme"].mean() * 100.0),
                "composite_score": float(g["lb_pctile_signed"].mean()),
                "n_branches": int(len(g)),
            }
        ),
        include_groups=False,
    ).reset_index()
    return out.sort_values("trade_date").reset_index(drop=True)


def walk_forward_breadth(
    series: pd.DataFrame,
    episodes: pd.DataFrame,
    *,
    full_event_dates: list[str],
    lookback_days: int,
    normal_pool_window: int = 0,
) -> pd.DataFrame:
    """normal_pool_window=0 → 全歷史 expanding pool（原始版，易受長期趨勢污染）；
    >0 → 只取測試日前最近 N 個交易日（rolling window），控制長期趨勢干擾。"""
    dates = series["trade_date"].tolist()
    idx_of = {d: i for i, d in enumerate(dates)}
    event_idx = sorted(idx_of[d] for d in full_event_dates if d in idx_of)

    def is_far_from_any_event(day_idx: int) -> bool:
        return all(abs(day_idx - ei) > lookback_days for ei in event_idx)

    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    ep_dates = [d for d in ep_dates if d in idx_of]

    rows = []
    for i, test_date in enumerate(ep_dates):
        test_idx = idx_of[test_date]
        floor_idx = max(0, test_idx - normal_pool_window) if normal_pool_window > 0 else 0
        normal_pool_dates = [
            d for d in dates
            if floor_idx <= idx_of[d] < test_idx and is_far_from_any_event(idx_of[d])
        ]
        test_row = series[series["trade_date"] == test_date].iloc[0]
        mkt_ret = float(
            episodes.loc[episodes["trade_date"] == test_date, "mkt_ret_pct"].iloc[0]
        )
        for metric in ("breadth_pct", "composite_score"):
            normal_vals = series[series["trade_date"].isin(normal_pool_dates)][metric].tolist()
            pct_rank = None
            if normal_vals:
                combined = normal_vals + [float(test_row[metric])]
                pct_rank = float(
                    stats.percentileofscore(combined, float(test_row[metric]), kind="mean")
                )
            rows.append(
                {
                    "episode_index": i + 1,
                    "trade_date": test_date,
                    "mkt_ret_pct": round(mkt_ret, 3),
                    "metric": metric,
                    "value": round(float(test_row[metric]), 4),
                    "n_normal_days_compared": len(normal_pool_dates),
                    "percentile_rank_vs_normal_days": (
                        None if pct_rank is None else round(pct_rank, 1)
                    ),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Breadth index walk-forward (no-lookahead) validation")
    ap.add_argument("--crash-grid", type=Path, default=CRASH_GRID)
    ap.add_argument("--rally-grid", type=Path, default=RALLY_GRID)
    ap.add_argument("--crash-episodes", type=Path, default=CRASH_EPISODES)
    ap.add_argument("--rally-episodes", type=Path, default=RALLY_EPISODES)
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument(
        "--normal-pool-window",
        type=int,
        default=0,
        help="0=全歷史expanding pool；>0=只取測試日前最近N個交易日的rolling window"
        "（控制長期趨勢污染，例如120）",
    )
    ap.add_argument("--out-suffix", default="")
    args = ap.parse_args()
    suf = args.out_suffix

    crash_grid = pd.read_parquet(args.crash_grid)
    rally_grid = pd.read_parquet(args.rally_grid)
    for g in (crash_grid, rally_grid):
        g["trade_date"] = g["trade_date"].astype(str)

    crash_episodes = pd.read_csv(args.crash_episodes, dtype={"trade_date": str})
    rally_episodes = pd.read_csv(args.rally_episodes, dtype={"trade_date": str})
    full_event_dates = sorted(
        set(crash_episodes.loc[crash_episodes["episode_first"], "trade_date"])
        | set(rally_episodes.loc[rally_episodes["episode_first"], "trade_date"])
    )
    print(f"combined event-date superset n={len(full_event_dates)}", flush=True)

    crash_series = build_breadth_series(crash_grid, direction="crash")
    rally_series = build_breadth_series(rally_grid, direction="rally")
    print(f"crash breadth series rows={len(crash_series)}", flush=True)
    print(f"rally breadth series rows={len(rally_series)}", flush=True)

    crash_results = walk_forward_breadth(
        crash_series, crash_episodes, full_event_dates=full_event_dates,
        lookback_days=args.lookback_days, normal_pool_window=args.normal_pool_window,
    )
    crash_results.insert(0, "direction", "crash")
    rally_results = walk_forward_breadth(
        rally_series, rally_episodes, full_event_dates=full_event_dates,
        lookback_days=args.lookback_days, normal_pool_window=args.normal_pool_window,
    )
    rally_results.insert(0, "direction", "rally")

    combined = pd.concat([crash_results, rally_results], ignore_index=True)
    out_csv = OUT / f"breadth_walkforward_results{suf}.csv"
    combined.to_csv(out_csv, index=False)
    print(f"wrote {out_csv} rows={len(combined)}", flush=True)

    headline = (
        combined.dropna(subset=["percentile_rank_vs_normal_days"])
        .groupby(["direction", "metric"])["percentile_rank_vs_normal_days"]
        .agg(n="count", avg_percentile_rank="mean")
        .reset_index()
    )
    headline["avg_percentile_rank"] = headline["avg_percentile_rank"].round(1)
    print(headline.to_string(index=False), flush=True)

    def hl(direction: str, metric: str) -> str:
        row = headline[(headline["direction"] == direction) & (headline["metric"] == metric)]
        if row.empty:
            return "NA"
        return f"{row['avg_percentile_rank'].iloc[0]:.1f}% (n={int(row['n'].iloc[0])})"

    lines = [
        "# 買超／賣超廣度指數 · 嚴格 walk-forward 驗證",
        "",
        "Research diagnostic · 非正式風控規則 · 非可下單訊號。",
        "",
        "修正既有 `rally_buying_breadth_dashboard.md` 的一個引用錯誤：該檔案",
        "「限制與注意事項」引用的 82.9% walk-forward 判別力，其實是**具名分點",
        "panel 法**（`run_market_precursor_walkforward_score.py`）算出來的數字",
        "（且只用 5 個小樣本測試事件），不是廣度／複合分數本身的 walk-forward",
        "結果——廣度指數過去只做過 in-sample 門檻回測（用同一批事件回測 recall/",
        "false-alarm），從未做過真正 no-lookahead 驗證。本次補上，並改用擴大版",
        "滾動事件（16個大跌 + 24個大漲，Track C 定義）以取得較穩定的估計。",
        "",
        "## 方法",
        "",
        "- 廣度分數本身不需訓練（全 cohort 固定公式：大跌=`1-mean(lb_pctile)`，"
        "大漲=`mean(lb_pctile)`，`breadth_pct`=極端尾部比例），跟具名 panel 版本"
        "最大差異是**不需要用過去事件選分點名單**。",
        "- Walk-forward 的重點在「常態日基準」：測試事件當天的分數，只跟"
        f"**嚴格早於**它、且不落在任何大跌+大漲事件 ±{args.lookback_days} 交易日"
        "窗內的常態日分數比百分位排名，跟具名 panel 版本用同一套常態日排除規則，"
        "確保兩種方法在同一組事件、同一套基準下可直接比較。",
        "",
        "## Headline：walk-forward 判別力（平均百分位排名，50%=亂猜）",
        "",
        "| 方向 | 指標 | 平均百分位排名 |",
        "|------|------|---------------:|",
        f"| 大跌（crash） | composite_score | {hl('crash', 'composite_score')} |",
        f"| 大跌（crash） | breadth_pct | {hl('crash', 'breadth_pct')} |",
        f"| 大漲（rally） | composite_score | {hl('rally', 'composite_score')} |",
        f"| 大漲（rally） | breadth_pct | {hl('rally', 'breadth_pct')} |",
        "",
        "## 對照：具名分點 panel 法（同一組擴大版事件，`walkforward_precursor_summary_rollingevt.md`）",
        "",
        "| 方向 | 平均百分位排名 |",
        "|------|---------------:|",
        "| 大跌 full panel | 58.4% (n=9) |",
        "| 大漲 full panel | 53.0% (n=17) |",
        "",
    ]

    for direction, results, label in (("crash", crash_results, "大跌"), ("rally", rally_results, "大漲")):
        lines += [
            f"## {label}方向 · composite_score 逐事件明細",
            "",
            "| 事件序 | 日期 | 大盤日報酬% | composite_score | 常態日百分位排名% |",
            "|------:|------|-----------:|-----------------:|-------------------:|",
        ]
        sub = results[results["metric"] == "composite_score"].sort_values("episode_index")
        for r in sub.itertuples():
            pr = "NA" if pd.isna(r.percentile_rank_vs_normal_days) else f"{r.percentile_rank_vs_normal_days:.1f}"
            lines.append(f"| {r.episode_index} | {r.trade_date} | {r.mkt_ret_pct:+.2f} | {r.value:.4f} | {pr} |")
        lines.append("")

    lines += [
        "## 限制與注意事項",
        "",
        "- 事件數仍有限（16個大跌／24個大漲），平均百分位排名可能被少數極端",
        "事件拉動，請對照逐事件明細判斷是否為穩定的跨事件現象。",
        "- `lb_pctile` 是分點對自己 2 年歷史的 self-normalized 百分位，本驗證",
        "沿用既有 grid cache 的計算方式，未重新對 `lb_pctile` 本身做因果排序",
        "限制（該限制在原始兩份 precursor scan summary 已說明）。",
        "",
    ]

    summary_path = OUT / f"breadth_walkforward_summary{suf}.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {summary_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
