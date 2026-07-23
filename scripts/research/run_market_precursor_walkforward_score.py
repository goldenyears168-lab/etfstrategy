#!/usr/bin/env python3
"""Market crash/rally branch precursor — expanding-window walk-forward 連續複合分數驗證.

Research diagnostic。修正 `run_market_crash_precursor_loocv.py` /
`run_market_rally_precursor_loocv.py` 既有 leave-one-episode-out（LOOCV）驗證
的一個 look-ahead 問題：那兩支腳本排除事件 e 之後，仍用「其他全部事件」
（包含在 e 之後才發生的事件）重新選分點名單——這代表用未來事件幫過去
的訊號打分，不是真實可部署的早期預警系統會有的資訊集合（上線那天只
看得到過去的事件）。

做法（expanding-window walk-forward，嚴格依時間序）：
  1. 事件（大跌｜大漲）依時間排序（episodes csv 已是時間序）。
  2. 從第 `--min-train-episodes + 1` 個事件開始（預設 min-train-episodes=4，
     即從第 5 個事件起）當作 test 事件；每個 test 事件 e_i 只用「嚴格早於
     e_i」的事件 e_1..e_{i-1} 重新跑既有 `score_branches`／`score_branches_
     rally`（hit_count>=3 且 p<0.10）選出 walk-forward panel，e_i 及更晚的
     事件完全看不到。
  3. 連續複合分數（composite score）：panel 分點在 e_i 當天各自的
     `lb_pctile`（0..1，既有 grid cache 已算好，self-normalized 對分點自己
     2 年歷史）取平均，再依方向轉成「分數越高＝precursor 訊號越強」：
       - 大跌方向：composite = 1 - mean(lb_pctile)（原始低百分位＝重賣超）
       - 大漲方向：composite = mean(lb_pctile)（原始高百分位＝重買超）
     同一計算套用在「常態日」比較樣本：取 e_i 之前（嚴格早於 e_i，避免
     用到未來資料）、且不落在大跌+大漲合併 20 個事件（episode_first）任一
     個 ±`--lookback-days` 交易日索引窗內的所有交易日（避免常態日基準
     被鄰近事件的訊號污染）。
  4. AUC-like 指標：e_i 的 composite 分數，在「自己 + 該期常態日」全體中的
     百分位排名（0~100%，50%=與亂猜無異，100%=完美區分）。跨所有
     test 事件取平均，即為該方向「walk-forward 判別力」headline 指標。
  5. 同時比較兩種 panel：hit_count>=3&p<0.10 全 panel，vs 其中依訓練期
     hit_count 排序前 `--top-n-panel`（預設5）家的高信心子集。

依賴（已快取，全部 pandas，不需 DB）：
  reports/research/branch-footprint-screen/_market_crash_precursor_grid_cache.parquet
  reports/research/branch-footprint-screen/_market_rally_precursor_grid_cache.parquet
  reports/research/branch-footprint-screen/market_crash_precursor_episodes.csv
  reports/research/branch-footprint-screen/market_rally_precursor_episodes.csv

`score_branches`／`score_branches_rally` 直接從既有兩支 precursor scan
腳本 import（`importlib.util`，與 rally scan 沿用同一 pattern），避免
重寫命中率／p 值方法。

輸出：
  reports/research/branch-footprint-screen/walkforward_precursor_results_crash.csv
  reports/research/branch-footprint-screen/walkforward_precursor_results_rally.csv
  reports/research/branch-footprint-screen/walkforward_precursor_summary.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_walkforward_score.py --direction both
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_spec_crash = importlib.util.spec_from_file_location(
    "run_market_crash_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_crash_branch_precursor_scan.py",
)
_crash_mod = importlib.util.module_from_spec(_spec_crash)
_spec_crash.loader.exec_module(_crash_mod)  # type: ignore[union-attr]

_spec_rally = importlib.util.spec_from_file_location(
    "run_market_rally_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_rally_branch_precursor_scan.py",
)
_rally_mod = importlib.util.module_from_spec(_spec_rally)
_spec_rally.loader.exec_module(_rally_mod)  # type: ignore[union-attr]

score_branches_crash = _crash_mod.score_branches
score_branches_rally = _rally_mod.score_branches_rally

OUT = ROOT / "reports/research/branch-footprint-screen"
CRASH_GRID = OUT / "_market_crash_precursor_grid_cache.parquet"
RALLY_GRID = OUT / "_market_rally_precursor_grid_cache.parquet"
CRASH_EPISODES = OUT / "market_crash_precursor_episodes.csv"
RALLY_EPISODES = OUT / "market_rally_precursor_episodes.csv"

DIRECTIONS = ("crash", "rally")


def load_full_event_dates(crash_episodes: pd.DataFrame, rally_episodes: pd.DataFrame) -> list[str]:
    """大跌 + 大漲合併的 20 個事件（episode_first）日期，用於常態日排除窗。"""
    dates = (
        crash_episodes.loc[crash_episodes["episode_first"], "trade_date"].tolist()
        + rally_episodes.loc[rally_episodes["episode_first"], "trade_date"].tolist()
    )
    return sorted(set(dates))


def composite_score_for_date(
    grid: pd.DataFrame,
    date: str,
    panel_ids: list[str],
    *,
    direction: str,
) -> tuple[float | None, int]:
    if not panel_ids:
        return None, 0
    sub = grid[
        (grid["trade_date"] == date) & (grid["securities_trader_id"].isin(panel_ids))
    ]
    sub = sub.dropna(subset=["lb_pctile"])
    if sub.empty:
        return None, 0
    mean_pctile = float(sub["lb_pctile"].mean())
    score = (1.0 - mean_pctile) if direction == "crash" else mean_pctile
    return score, int(len(sub))


def walk_forward_direction(
    *,
    direction: str,
    grid: pd.DataFrame,
    episodes: pd.DataFrame,
    full_event_dates: list[str],
    min_train_episodes: int,
    min_hit_count: int,
    max_p_value: float,
    tail_pct: float,
    lookback_days: int,
    top_n_panel: int,
) -> pd.DataFrame:
    score_fn = score_branches_crash if direction == "crash" else score_branches_rally
    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()

    cal = sorted(grid["trade_date"].unique().tolist())
    idx_of = {d: i for i, d in enumerate(cal)}
    event_idx = sorted(idx_of[d] for d in full_event_dates if d in idx_of)

    def is_far_from_any_event(day_idx: int) -> bool:
        return all(abs(day_idx - ei) > lookback_days for ei in event_idx)

    rows: list[dict] = []
    for i, test_date in enumerate(ep_dates):
        if i < min_train_episodes:
            continue
        train_dates = set(ep_dates[:i])
        train_eps = episodes.copy()
        train_eps["episode_first"] = train_eps["trade_date"].isin(train_dates)

        scores = score_fn(grid, train_eps, {}, tail_pct=tail_pct)
        if scores.empty:
            sig = scores
        else:
            sig = scores[
                (scores["hit_count"] >= min_hit_count) & (scores["p_value_one_sided"] < max_p_value)
            ]
        panel_full_ids = sig["branch_id"].tolist() if not sig.empty else []
        panel_top5_ids = panel_full_ids[:top_n_panel]

        mkt_ret = float(
            episodes.loc[episodes["trade_date"] == test_date, "mkt_ret_pct"].iloc[0]
        )
        test_idx = idx_of[test_date]
        normal_pool_dates = [
            d for d in cal if idx_of[d] < test_idx and is_far_from_any_event(idx_of[d])
        ]

        print(
            f"  [{direction}] test={test_date} (episode {i+1}/{len(ep_dates)}, "
            f"train_n={i}) panel_full={len(panel_full_ids)} panel_top5={len(panel_top5_ids)} "
            f"normal_pool={len(normal_pool_dates)}",
            flush=True,
        )

        for panel_variant, panel_ids in (("full", panel_full_ids), ("top5", panel_top5_ids)):
            composite_test, n_valid = composite_score_for_date(
                grid, test_date, panel_ids, direction=direction
            )
            normal_scores: list[float] = []
            for d in normal_pool_dates:
                cs, _ = composite_score_for_date(grid, d, panel_ids, direction=direction)
                if cs is not None:
                    normal_scores.append(cs)

            pct_rank = None
            if composite_test is not None and normal_scores:
                combined = normal_scores + [composite_test]
                pct_rank = float(
                    stats.percentileofscore(combined, composite_test, kind="mean")
                )

            rows.append(
                {
                    "direction": direction,
                    "panel_variant": panel_variant,
                    "episode_index": i + 1,
                    "n_train_episodes": i,
                    "trade_date": test_date,
                    "mkt_ret_pct": round(mkt_ret, 3),
                    "panel_size": len(panel_ids),
                    "n_panel_valid_on_date": n_valid,
                    "composite_score": (
                        None if composite_test is None else round(composite_test, 4)
                    ),
                    "n_normal_days_compared": len(normal_scores),
                    "percentile_rank_vs_normal_days": (
                        None if pct_rank is None else round(pct_rank, 1)
                    ),
                }
            )

    return pd.DataFrame(rows)


def headline_metrics(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame(
            columns=["direction", "panel_variant", "n_test_episodes", "avg_percentile_rank"]
        )
    valid = results.dropna(subset=["percentile_rank_vs_normal_days"])
    agg = (
        valid.groupby(["direction", "panel_variant"])["percentile_rank_vs_normal_days"]
        .agg(n_test_episodes="count", avg_percentile_rank="mean")
        .reset_index()
    )
    agg["avg_percentile_rank"] = agg["avg_percentile_rank"].round(1)
    return agg


def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    results_crash: pd.DataFrame,
    results_rally: pd.DataFrame,
    headline: pd.DataFrame,
) -> None:
    def _headline_val(direction: str, panel_variant: str) -> str:
        row = headline[
            (headline["direction"] == direction) & (headline["panel_variant"] == panel_variant)
        ]
        if row.empty:
            return "NA"
        return f"{row['avg_percentile_rank'].iloc[0]:.1f}% (n={int(row['n_test_episodes'].iloc[0])})"

    lines = [
        "# Expanding-window walk-forward · 分點 precursor 連續複合分數驗證",
        "",
        "Research diagnostic · 非正式風控規則 · 非可下單訊號。",
        "",
        "修正 `market_crash_precursor_loocv.md` / `market_rally_precursor_loocv.md` 的",
        "look-ahead 問題：那兩份 leave-one-episode-out 驗證排除事件 e 後，仍用「包含",
        "e 之後才發生的未來事件」重新選分點名單，不是真實上線那天能取得的資訊集合。",
        "本驗證改為嚴格依時間序的 expanding-window walk-forward：test 事件 e_i 只用",
        "**嚴格早於 e_i** 的過去事件選 panel，且用一個連續複合分數（非二元命中／未命中）",
        "評估判別力，而非只看 consensus_k 是否觸發。",
        "",
        "## 方法",
        "",
        f"- **Panel 選取**：從第 {protocol['min_train_episodes']+1} 個事件起當 test，"
        f"只用該事件之前（不含自己）的事件重跑既有 `score_branches`／`score_branches_rally`"
        f"（hit_count ≥ {protocol['min_hit_count']} 且 p < {protocol['max_p_value']}），"
        "選出 walk-forward panel；同時取該 panel 依訓練期 hit_count 排序前 "
        f"{protocol['top_n_panel']} 家為「top5 高信心」子集做對照。",
        "- **複合分數**：panel 內分點在測試日各自的 `lb_pctile`（既有 grid cache，"
        "self-normalized 對分點自身 2 年歷史）取平均；大跌方向轉成 "
        "`1 - mean(lb_pctile)`，大漲方向直接用 `mean(lb_pctile)`，兩者統一為"
        "「分數越高＝precursor 訊號越強」。",
        "- **常態日基準**：測試日**嚴格早於**的所有交易日中，排除落在大跌＋大漲"
        f"合併 20 個事件（episode_first）任一事件 ±{protocol['lookback_days']} 交易日索引"
        "窗內的天數（避免常態日基準被鄰近事件污染），對剩下的日子套用同一套"
        "複合分數計算，作為「這天不是事件日」的比較樣本。",
        "- **AUC-like 判別力指標**：測試事件的複合分數，在「自己 + 該期所有常態日」"
        "分數中的百分位排名（0~100%）；50% = 與亂猜無異，100% = 完美區分"
        "（優於全部常態日）。跨所有 walk-forward test 事件取平均，為該方向"
        "的 headline 指標，分 full panel / top5 高信心 panel 兩種變體分別計算。",
        "",
        "## Headline：walk-forward 判別力（平均百分位排名）",
        "",
        "| 方向 | panel 變體 | 平均百分位排名 |",
        "|------|-----------|---------------:|",
        f"| 大跌（crash） | full（hit_count≥3&p<0.10） | {_headline_val('crash', 'full')} |",
        f"| 大跌（crash） | top5 高信心 | {_headline_val('crash', 'top5')} |",
        f"| 大漲（rally） | full（hit_count≥3&p<0.10） | {_headline_val('rally', 'full')} |",
        f"| 大漲（rally） | top5 高信心 | {_headline_val('rally', 'top5')} |",
        "",
    ]

    for direction, results, label in (
        ("crash", results_crash, "大跌"),
        ("rally", results_rally, "大漲"),
    ):
        lines += [
            f"## {label}方向 · 逐事件明細",
            "",
            "| 事件序 | 日期 | 大盤日報酬% | panel變體 | panel家數 | 複合分數 | 常態日百分位排名% | 比較常態日數 |",
            "|------:|------|-----------:|-----------|---------:|---------:|-----------------:|-----------:|",
        ]
        if results.empty:
            lines.append("（無可測試事件）")
        else:
            for r in results.sort_values(["episode_index", "panel_variant"]).itertuples():
                cs = "NA" if r.composite_score is None or pd.isna(r.composite_score) else f"{r.composite_score:.4f}"
                pr = (
                    "NA"
                    if r.percentile_rank_vs_normal_days is None or pd.isna(r.percentile_rank_vs_normal_days)
                    else f"{r.percentile_rank_vs_normal_days:.1f}"
                )
                lines.append(
                    f"| {r.episode_index} | {r.trade_date} | {r.mkt_ret_pct:+.2f} | "
                    f"{r.panel_variant} | {r.panel_size} | {cs} | {pr} | {r.n_normal_days_compared} |"
                )
        lines.append("")

    crash_full = headline[(headline["direction"] == "crash") & (headline["panel_variant"] == "full")]
    rally_full = headline[(headline["direction"] == "rally") & (headline["panel_variant"] == "full")]
    crash_top5 = headline[(headline["direction"] == "crash") & (headline["panel_variant"] == "top5")]
    rally_top5 = headline[(headline["direction"] == "rally") & (headline["panel_variant"] == "top5")]

    def _val(df: pd.DataFrame) -> float | None:
        return None if df.empty else float(df["avg_percentile_rank"].iloc[0])

    def _n(df: pd.DataFrame) -> int:
        return 0 if df.empty else int(df["n_test_episodes"].iloc[0])

    cf, rf, ct, rt = _val(crash_full), _val(rally_full), _val(crash_top5), _val(rally_top5)
    n_crash, n_rally = _n(crash_full), _n(rally_full)

    verdict_lines = [
        "## 驗證結論",
        "",
    ]
    if cf is not None and rf is not None:
        gap = cf - rf
        direction_word = "確認" if gap > 0 else "並未確認"
        verdict_lines.append(
            f"- Full panel：大跌 walk-forward 平均百分位排名 {cf:.1f}%（n={n_crash} 個 test 事件）"
            f" vs 大漲 {rf:.1f}%（n={n_rally} 個 test 事件），差距 {gap:+.1f} 個百分點。"
            f"在嚴格 no-lookahead 的 expanding-window 驗證下，{direction_word}"
            "「大跌 precursor 訊號比大漲更乾淨／更具判別力」的質化發現。"
        )
    else:
        verdict_lines.append("- 資料不足以計算 full panel 的跨方向比較（某方向無可測試事件或 panel 皆為空）。")

    if ct is not None and rt is not None and cf is not None and rf is not None:
        top5_better_crash = ct > cf
        top5_better_rally = rt > rf
        verdict_lines.append(
            f"- Top5 高信心 panel：大跌 {ct:.1f}% vs 大漲 {rt:.1f}%。"
            f"相對各自 full panel（大跌 {cf:.1f}%／大漲 {rf:.1f}%），"
            f"大跌方向 top5 {'優於' if top5_better_crash else '未優於'} full panel，"
            f"大漲方向 top5 {'優於' if top5_better_rally else '未優於'} full panel——"
            + (
                "顯示縮小到高信心子集在兩個方向都能提升（或至少不損害）判別力。"
                if top5_better_crash and top5_better_rally
                else "結果不一致，縮小 panel 並非穩定有效的改善方向，需視方向而定。"
            )
        )
    else:
        verdict_lines.append("- 資料不足以完整比較 top5 vs full panel。")

    n_crash_empty = int(
        (
            (results_crash["panel_variant"] == "full")
            & (results_crash["panel_size"] == 0)
        ).sum()
    ) if not results_crash.empty else 0
    n_crash_attempted = int((results_crash["panel_variant"] == "full").sum()) if not results_crash.empty else 0
    if n_crash_empty:
        verdict_lines.append(
            f"- **重要限制**：大跌方向 {n_crash_attempted} 個 test 事件中，"
            f"有 {n_crash_empty} 個（train_n=4~8，即前 5 個 test 事件）walk-forward "
            "panel 為**空**（no lookahead 條件下，用那麼少的過去事件重選，沒有任何"
            "分點能同時滿足 hit_count≥3 且 p<0.10）——panel 要到 train_n=9（第10個"
            "事件）才首度非空。因此大跌方向 headline 數字實際只由最後 2 個事件撐"
            "出來，樣本極薄，比大漲方向（5 個事件、panel 從 train_n=4 起就非空）"
            "脆弱得多；不宜直接拿來反駁「大跌訊號更乾淨」，但也不能說已經驗證。"
        )
    verdict_lines += [
        "- 本驗證仍受限於事件數少（大跌 test 事件數個位數、大漲更少），"
        "平均百分位排名可能被 1-2 個極端事件拉動，請對照上方逐事件明細判斷"
        "是否為少數事件撐出來的結果，而非穩定的跨事件現象。",
        "- 常態日基準本身也用「該分點 2 年全歷史」算 `lb_pctile`（既有 grid cache "
        "設計，非本腳本重算），並非對常態日再做因果排序上的因果限制；本驗證"
        "修正的是「panel 選取時的未來事件洩漏」，而非既有 `lb_pctile` 計算方式"
        "本身的其他限制（見兩份原始 precursor scan summary 的「限制與注意事項」）。",
        "",
    ]
    lines += verdict_lines
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Expanding-window walk-forward precursor composite-score validation"
    )
    ap.add_argument("--direction", choices=["crash", "rally", "both"], default="both")
    ap.add_argument("--min-train-episodes", type=int, default=4)
    ap.add_argument("--min-hit-count", type=int, default=3)
    ap.add_argument("--max-p-value", type=float, default=0.10)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--top-n-panel", type=int, default=5)
    ap.add_argument("--crash-grid", type=Path, default=CRASH_GRID)
    ap.add_argument("--rally-grid", type=Path, default=RALLY_GRID)
    ap.add_argument("--crash-episodes", type=Path, default=CRASH_EPISODES)
    ap.add_argument("--rally-episodes", type=Path, default=RALLY_EPISODES)
    ap.add_argument(
        "--out-suffix",
        default="",
        help="appended to output filenames before extension, e.g. '_gold7ext' — "
        "lets alternate cohort/window runs write to distinct files without clobbering",
    )
    args = ap.parse_args()
    suf = args.out_suffix

    for p in (args.crash_grid, args.rally_grid, args.crash_episodes, args.rally_episodes):
        if not p.exists():
            raise SystemExit(f"missing required cached input: {p}")

    print("loading episode CSVs + grid caches …", flush=True)
    crash_episodes = pd.read_csv(args.crash_episodes, dtype={"trade_date": str})
    rally_episodes = pd.read_csv(args.rally_episodes, dtype={"trade_date": str})
    full_event_dates = load_full_event_dates(crash_episodes, rally_episodes)
    print(f"full 20-event superset (crash+rally) n={len(full_event_dates)}", flush=True)

    protocol = {
        "min_train_episodes": args.min_train_episodes,
        "min_hit_count": args.min_hit_count,
        "max_p_value": args.max_p_value,
        "tail_pct": args.tail_pct,
        "lookback_days": args.lookback_days,
        "top_n_panel": args.top_n_panel,
    }

    results_crash = pd.DataFrame()
    results_rally = pd.DataFrame()

    if args.direction in ("crash", "both"):
        print("loading crash grid cache …", flush=True)
        crash_grid = pd.read_parquet(args.crash_grid)
        crash_grid["securities_trader_id"] = crash_grid["securities_trader_id"].astype(str)
        crash_grid["trade_date"] = crash_grid["trade_date"].astype(str)
        print("running crash walk-forward …", flush=True)
        results_crash = walk_forward_direction(
            direction="crash",
            grid=crash_grid,
            episodes=crash_episodes,
            full_event_dates=full_event_dates,
            min_train_episodes=args.min_train_episodes,
            min_hit_count=args.min_hit_count,
            max_p_value=args.max_p_value,
            tail_pct=args.tail_pct,
            lookback_days=args.lookback_days,
            top_n_panel=args.top_n_panel,
        )
        out_path = OUT / f"walkforward_precursor_results_crash{suf}.csv"
        results_crash.to_csv(out_path, index=False)
        print(f"wrote {out_path} rows={len(results_crash)}", flush=True)

    if args.direction in ("rally", "both"):
        print("loading rally grid cache …", flush=True)
        rally_grid = pd.read_parquet(args.rally_grid)
        rally_grid["securities_trader_id"] = rally_grid["securities_trader_id"].astype(str)
        rally_grid["trade_date"] = rally_grid["trade_date"].astype(str)
        print("running rally walk-forward …", flush=True)
        results_rally = walk_forward_direction(
            direction="rally",
            grid=rally_grid,
            episodes=rally_episodes,
            full_event_dates=full_event_dates,
            min_train_episodes=args.min_train_episodes,
            min_hit_count=args.min_hit_count,
            max_p_value=args.max_p_value,
            tail_pct=args.tail_pct,
            lookback_days=args.lookback_days,
            top_n_panel=args.top_n_panel,
        )
        out_path = OUT / f"walkforward_precursor_results_rally{suf}.csv"
        results_rally.to_csv(out_path, index=False)
        print(f"wrote {out_path} rows={len(results_rally)}", flush=True)

    if args.direction == "both":
        combined = pd.concat([results_crash, results_rally], ignore_index=True)
        headline = headline_metrics(combined)
        print("headline metrics:", flush=True)
        print(headline.to_string(index=False), flush=True)

        summary_path = OUT / f"walkforward_precursor_summary{suf}.md"
        write_summary_md(
            summary_path,
            protocol=protocol,
            results_crash=results_crash,
            results_rally=results_rally,
            headline=headline,
        )
        print(f"wrote {summary_path}", flush=True)
    else:
        print(
            "note: --direction != both, skipped walkforward_precursor_summary.md "
            "(需要 crash+rally 兩邊資料才能寫跨方向比較，請用 --direction both 產出完整報告)",
            flush=True,
        )

    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
