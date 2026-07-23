#!/usr/bin/env python3
"""Market rally buying-breadth dashboard — 分點買超廣度指數（v2 大漲雷達）.

Research / monitoring diagnostic. 非正式訊號 · 非可下單策略 ·
尚未登錄 `config/research.yaml`。

## 為什麼不再用「具名分點清單」

`run_market_rally_branch_opportunity_dashboard.py`（v1）沿用大跌版本的手法，
挑出跨事件命中顯著的「精英分點」。四路延伸研究（Track A 同儕相對百分位、
Track B 券商母公司層級、Track C 滾動事件擴大樣本、Track D 嚴格 walk-forward）
一致指出：v1 的具名精英分點在大漲方向站不住腳——

- Track A：v1 具名的 8 家精英，同儕相對檢定下**全部落榜**（xsec hit_count 0-2，
  對比 self-ts hit_count 5-6），代表牠們只是「跟大家一起放量」，不是真的領先。
- Track C：換一個（一樣合理的）事件定義，精英名單幾乎整批換人（Top20 重疊
  僅 5/20，Spearman ρ=0.157），代表原本的名單不是穩定核心，是換一批雜訊。
- Track B：把分點加總到券商母公司層級，訊號沒有變乾淨（lift 反而下降）。

但 Track D 的嚴格 walk-forward（只用測試事件之前的歷史選 panel、算連續複合
分數）發現：即使 panel 本身很大（14~160 家、隨訓練期擴大而擴大)，「panel
內分點 lb_pctile 的平均值」這個**廣度型**指標，在測試事件當天對常態日的
判別力還是有 82.9% 的平均百分位排名（同一套方法在大跌方向反而因為歷史
事件太少、panel 常常選不出來而站不住腳）。

**結論／v2 設計**：大漲方向真正有用的不是「哪幾家分點神」，而是「多少
比例的分點同時異常買超」這個廣度現象本身——因為大漲前的機構買盤本來就
是廣泛參與，把它當成一個連續的市場廣度指標來監控，比硬套大跌版本的
「找出幾個神分點」框架更誠實、更貼近實際的訊號結構。

## 指標定義

- 分點宇宙：既有 297 檔深覆蓋分點 cohort（`_market_rally_precursor_grid_cache.parquet`，
  lookback=5，`lb_pctile` = 每個分點前5日累計買超金額，對自己 2 年歷史的
  self-normalized 百分位）。
- **買超廣度 breadth_pct**：當日 cohort 中 `lb_pctile >= 0.90`（異常重買超）
  的分點比例。
- **複合分數 composite_score**：當日 cohort 全體 `lb_pctile` 平均值（Track D
  同一套定義）。
- 兩者皆為連續指標，逐日追蹤；用既有 9 個大漲事件（`market_rally_precursor_episodes.csv`）
  回測門檻的 recall／false-alarm rate。

輸出（`reports/research/branch-footprint-screen/`）：
  rally_buying_breadth_timeseries.csv
  rally_buying_breadth_threshold_backtest.csv
  rally_buying_breadth_dashboard.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_rally_buying_breadth_dashboard.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen"
GRID_CACHE = OUT / "_market_rally_precursor_grid_cache.parquet"
EPISODES_CSV = OUT / "market_rally_precursor_episodes.csv"

BREADTH_THRESHOLDS_PCT = [10, 15, 20, 25, 30, 40]
COMPOSITE_THRESHOLDS_PCTILE = [60, 70, 75, 80, 85, 90]


def build_breadth_series(scored: pd.DataFrame) -> pd.DataFrame:
    scored = scored.dropna(subset=["lb_pctile"])
    grp = scored.groupby("trade_date")
    out = grp["lb_pctile"].agg(
        breadth_pct=lambda s: float((s >= 0.90).mean() * 100.0),
        composite_score=lambda s: float(s.mean()),
        n_branches=lambda s: int(s.shape[0]),
    ).reset_index()
    return out.sort_values("trade_date").reset_index(drop=True)


def backtest_thresholds(
    series: pd.DataFrame, episodes: pd.DataFrame, *, lookback_days: int
) -> pd.DataFrame:
    dates = series["trade_date"].tolist()
    idx_of = {d: i for i, d in enumerate(dates)}
    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    ep_dates = [d for d in ep_dates if d in idx_of]

    near_ep: set[str] = set()
    for d in ep_dates:
        i = idx_of[d]
        for j in range(max(0, i - lookback_days), i + 1):
            near_ep.add(dates[j])
    normal_mask = ~series["trade_date"].isin(near_ep)
    normal = series[normal_mask]

    rows = []
    for thr in BREADTH_THRESHOLDS_PCT:
        fired = set(series.loc[series["breadth_pct"] >= thr, "trade_date"])
        recall_hits = sum(
            1 for d in ep_dates if any(dd in fired for dd in dates[max(0, idx_of[d] - lookback_days): idx_of[d] + 1])
        )
        fp_days = sum(1 for d in normal["trade_date"] if d in fired)
        rows.append(
            {
                "metric": "breadth_pct",
                "threshold": thr,
                "recall_n": f"{recall_hits}/{len(ep_dates)}",
                "recall_pct": round(recall_hits / len(ep_dates) * 100.0, 1) if ep_dates else None,
                "false_alarm_pct": round(fp_days / len(normal) * 100.0, 1) if len(normal) else None,
            }
        )
    for pctile in COMPOSITE_THRESHOLDS_PCTILE:
        thr_val = series["composite_score"].quantile(pctile / 100.0)
        fired = set(series.loc[series["composite_score"] >= thr_val, "trade_date"])
        recall_hits = sum(
            1 for d in ep_dates if any(dd in fired for dd in dates[max(0, idx_of[d] - lookback_days): idx_of[d] + 1])
        )
        fp_days = sum(1 for d in normal["trade_date"] if d in fired)
        rows.append(
            {
                "metric": "composite_score",
                "threshold": f"P{pctile}(={thr_val:.3f})",
                "recall_n": f"{recall_hits}/{len(ep_dates)}",
                "recall_pct": round(recall_hits / len(ep_dates) * 100.0, 1) if ep_dates else None,
                "false_alarm_pct": round(fp_days / len(normal) * 100.0, 1) if len(normal) else None,
            }
        )
    return pd.DataFrame(rows)


def write_dashboard_md(
    path: Path,
    *,
    series: pd.DataFrame,
    backtest: pd.DataFrame,
    episodes: pd.DataFrame,
    lookback_days: int,
) -> None:
    latest = series.iloc[-1]
    breadth_rank = float((series["breadth_pct"] <= latest["breadth_pct"]).mean() * 100.0)
    composite_rank = float((series["composite_score"] <= latest["composite_score"]).mean() * 100.0)

    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    ep_view = series[series["trade_date"].isin(ep_dates)].merge(
        episodes[["trade_date", "mkt_ret_pct"]], on="trade_date", how="left"
    )

    lines = [
        "# 分點買超廣度指數 · 大漲雷達 v2",
        "",
        "Research / monitoring diagnostic · 非正式訊號 · 非可下單策略 ·",
        "尚未登錄 `config/research.yaml`。取代具名分點清單版本"
        "（`run_market_rally_branch_opportunity_dashboard.py`），改用連續的",
        "「分點買超廣度」指標——理由見本檔開頭 docstring（四路延伸研究一致",
        "顯示具名精英分點站不住腳，但廣度型聚合指標在嚴格 walk-forward 驗證",
        "下仍有判別力）。",
        "",
        f"## 目前狀態（最新資料日：{latest['trade_date']}）",
        "",
        f"- 買超廣度 breadth_pct：**{latest['breadth_pct']:.1f}%**"
        f"（cohort {int(latest['n_branches'])} 家中，前5日累計買超進自己歷史"
        f"前10%的比例；歷史百分位排名 {breadth_rank:.1f}%）",
        f"- 複合分數 composite_score：**{latest['composite_score']:.3f}**"
        f"（cohort 全體 lb_pctile 平均值；歷史百分位排名 {composite_rank:.1f}%）",
        "",
        "## 門檻回測（用既有 9 個大漲事件，前5日視窗內是否觸發）",
        "",
        "recall = 事件前5日視窗內有觸發門檻的事件比例；"
        "false_alarm = 非事件視窗的一般交易日中誤報的比例。",
        "",
        "| 指標 | 門檻 | recall | recall% | false_alarm% |",
        "|------|-----:|:------:|--------:|--------------:|",
    ]
    for r in backtest.itertuples():
        recall_pct = "NA" if r.recall_pct is None else f"{r.recall_pct:.1f}"
        fpr = "NA" if r.false_alarm_pct is None else f"{r.false_alarm_pct:.1f}"
        lines.append(f"| {r.metric} | {r.threshold} | {r.recall_n} | {recall_pct} | {fpr} |")

    lines += [
        "",
        "## 9 個既有大漲事件當天的廣度／複合分數（事後對照）",
        "",
        "| 日期 | 大盤日報酬% | breadth_pct(%) | composite_score |",
        "|------|-----------:|---------------:|-----------------:|",
    ]
    for r in ep_view.itertuples():
        lines.append(
            f"| {r.trade_date} | {r.mkt_ret_pct:+.2f} | {r.breadth_pct:.1f} | {r.composite_score:.3f} |"
        )

    lines += [
        "",
        "## 限制與注意事項",
        "",
        "- breadth_pct／composite_score 用的 `lb_pctile` 仍是「分點跟自己歷史比」，"
        "Track A 已證實這個指標在跨切面（同儕相對）驗證下對「單一分點」是否"
        "領先大盤沒有鑑別力——但在**聚合**成廣度／複合分數後，Track D 的"
        "walk-forward 驗證顯示仍有中等判別力（平均百分位排名 82.9%），"
        "解讀為「廣泛參與程度」訊號，不是「哪家分點神」訊號。",
        "- 事件數仍少（9 個），門檻回測的統計檢定力有限，屬探索性分析。",
        "- 買超廣度絕大部分時間偏高（大漲日本身就是廣泛參與的極端值），"
        "門檻設定越低越容易被平日正常波動誤報，請搭配 false_alarm% 一起看，"
        "不要只看 recall%。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Rally buying-breadth dashboard (v2)")
    ap.add_argument("--grid-cache", type=Path, default=GRID_CACHE)
    ap.add_argument("--episodes-csv", type=Path, default=EPISODES_CSV)
    ap.add_argument("--lookback-days", type=int, default=5)
    args = ap.parse_args()

    if not args.grid_cache.exists():
        raise SystemExit(f"missing {args.grid_cache}")

    scored = pd.read_parquet(args.grid_cache)
    episodes = pd.read_csv(args.episodes_csv)
    print(f"grid rows={len(scored):,}", flush=True)

    series = build_breadth_series(scored)
    print(f"breadth series rows={len(series):,} {series['trade_date'].iloc[0]}..{series['trade_date'].iloc[-1]}", flush=True)

    backtest = backtest_thresholds(series, episodes, lookback_days=args.lookback_days)

    ts_path = OUT / "rally_buying_breadth_timeseries.csv"
    series.to_csv(ts_path, index=False)
    bt_path = OUT / "rally_buying_breadth_threshold_backtest.csv"
    backtest.to_csv(bt_path, index=False)
    print(f"wrote {ts_path}", flush=True)
    print(f"wrote {bt_path}", flush=True)

    md_path = OUT / "rally_buying_breadth_dashboard.md"
    write_dashboard_md(md_path, series=series, backtest=backtest, episodes=episodes, lookback_days=args.lookback_days)
    print(f"wrote {md_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
