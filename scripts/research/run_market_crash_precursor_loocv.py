#!/usr/bin/env python3
"""Market crash branch precursor — leave-one-episode-out OOS 驗證.

Research diagnostic。回答一個 `run_market_crash_branch_precursor_scan.py` /
`run_market_crash_branch_risk_dashboard.py` 都沒驗證過的問題：目前的「顯著分點
名單」是用全部 11 個大跌事件（含 2026-07-17 本身）一起算出命中率／p 值，
選出來的分點又拿去對 2026-07-17 做案例展示——這樣同一個事件同時是「訓練
資料」又是「展示成果」，屬於 in-sample、有洩漏風險。

做法：對 11 個大跌事件逐一 leave-one-out——
  1. 扣掉事件 e，只用其他 10 個事件重新跑 `score_branches()`，得到
     「事件 e 訓練時看不到」的顯著分點名單（hit_count>=3, p<0.10）。
  2. 用這個 OOS 名單，檢查事件 e 當天，這些分點各自的 5 日累計賣超百分位
     是否真的落在自己歷史的後 10% 尾端（= 命中）。
  3. 記錄：OOS 命中家數 / OOS 名單家數、以及 consensus_k（>=1/2/3 家命中）
     是否會觸發，逐事件列表 + 整體 OOS recall。

這是對「整套篩選方法」的樣本外檼驗，而不是像 `run_market_crash_branch_risk_
dashboard.py --backtest` 那樣固定用同一份（in-sample 選出的）名單去對其他
事件重新驗證門檻。

依賴：需要先用 `--cache-grid` 跑過一次
  `run_market_crash_branch_precursor_scan.py --lookback-days 5 --cache-grid`
  產生 `_market_crash_precursor_grid_cache.parquet`。

輸出：
  reports/research/branch-footprint-screen/market_crash_precursor_loocv.csv
  reports/research/branch-footprint-screen/market_crash_precursor_loocv.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_crash_precursor_loocv.py
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "run_market_crash_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_crash_branch_precursor_scan.py",
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)  # type: ignore[union-attr]

OUT = ROOT / "reports/research/branch-footprint-screen"
GRID_CACHE = OUT / "_market_crash_precursor_grid_cache.parquet"
EPISODES_CSV = OUT / "market_crash_precursor_episodes.csv"
SCORES_CSV = OUT / "market_crash_precursor_branch_scores.csv"

CONSENSUS_KS = [1, 2, 3]


def run_loocv(
    scored: pd.DataFrame,
    episodes: pd.DataFrame,
    name_map: dict[str, str],
    *,
    min_hit_count: int,
    max_p_value: float,
    tail_pct: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    fold_rows = []
    detail_rows = []

    for held_out in ep_dates:
        train_eps = episodes.copy()
        train_eps["episode_first"] = train_eps["episode_first"] & (
            train_eps["trade_date"] != held_out
        )
        train_scores = m.score_branches(scored, train_eps, name_map, tail_pct=tail_pct)
        if train_scores.empty:
            panel_ids: list[str] = []
        else:
            sig = train_scores[
                (train_scores["hit_count"] >= min_hit_count)
                & (train_scores["p_value_one_sided"] < max_p_value)
            ]
            panel_ids = sig["branch_id"].tolist()

        day_rows = scored[
            (scored["trade_date"] == held_out)
            & (scored["securities_trader_id"].isin(panel_ids))
        ]
        n_hit = int((day_rows["lb_pctile"] <= tail_pct).sum())
        n_panel = len(panel_ids)
        mkt_ret = episodes.loc[episodes["trade_date"] == held_out, "mkt_ret_pct"]
        fold_rows.append(
            {
                "held_out_date": held_out,
                "mkt_ret_pct": float(mkt_ret.iloc[0]) if len(mkt_ret) else None,
                "n_train_episodes": int(train_eps["episode_first"].sum()),
                "oos_panel_size": n_panel,
                "oos_hit_count": n_hit,
                "oos_hit_rate_pct": round(n_hit / n_panel * 100.0, 1) if n_panel else None,
                **{
                    f"triggered_k{k}": bool(n_hit >= k) if n_panel else False
                    for k in CONSENSUS_KS
                },
            }
        )
        for bid in panel_ids:
            row = day_rows[day_rows["securities_trader_id"] == bid]
            pctile = float(row["lb_pctile"].iloc[0]) if len(row) else None
            detail_rows.append(
                {
                    "held_out_date": held_out,
                    "branch_id": bid,
                    "branch_name": name_map.get(bid, ""),
                    "lb_pctile": pctile,
                    "hit": bool(pctile is not None and pctile <= tail_pct),
                }
            )

    fold_df = pd.DataFrame(fold_rows)
    detail_df = pd.DataFrame(detail_rows)
    return fold_df, detail_df


def write_md(path: Path, fold_df: pd.DataFrame, detail_df: pd.DataFrame, *, tail_pct: float) -> None:
    n = len(fold_df)
    recall_by_k = {
        k: int(fold_df[f"triggered_k{k}"].sum()) for k in CONSENSUS_KS
    }
    stable_core = (
        detail_df.groupby(["branch_id", "branch_name"])["held_out_date"]
        .nunique()
        .reset_index(name="n_folds_in_panel")
    )
    hit_counts = (
        detail_df[detail_df["hit"]]
        .groupby(["branch_id", "branch_name"])["held_out_date"]
        .nunique()
        .reset_index(name="n_folds_hit")
    )
    core = stable_core.merge(hit_counts, on=["branch_id", "branch_name"], how="left")
    core["n_folds_hit"] = core["n_folds_hit"].fillna(0).astype(int)
    core = core.sort_values(["n_folds_in_panel", "n_folds_hit"], ascending=False)

    lines = [
        "# Leave-one-episode-out OOS 驗證 — 大跌前賣超分點名單",
        "",
        "Research diagnostic · 非正式風控規則 · 非可下單訊號。",
        f"對 {n} 個大跌事件逐一排除、用剩下的事件重新選名單，再檢查被排除的那個"
        "事件當天，「事件訓練時看不到」的名單是否真的命中，避免 in-sample 循環論證。",
        "",
        "## 逐事件 OOS 結果",
        "",
        "| 排除的事件 | 大盤跌幅% | OOS名單家數 | OOS命中家數 | 命中率% | k≥1觸發 | k≥2觸發 | k≥3觸發 |",
        "|---|---:|---:|---:|---:|:---:|:---:|:---:|",
    ]
    for r in fold_df.to_dict("records"):
        lines.append(
            f"| {r['held_out_date']} | {r['mkt_ret_pct']:.2f} | {r['oos_panel_size']} | "
            f"{r['oos_hit_count']} | {'NA' if r['oos_hit_rate_pct'] is None else r['oos_hit_rate_pct']} | "
            f"{'✓' if r['triggered_k1'] else ''} | {'✓' if r['triggered_k2'] else ''} | "
            f"{'✓' if r['triggered_k3'] else ''} |"
        )
    lines += [
        "",
        "## OOS recall（consensus_k 門檻下，多少事件會被抓到）",
        "",
        "| consensus_k | 觸發事件數/總事件數 | recall% |",
        "|---:|:---:|---:|",
    ]
    for k in CONSENSUS_KS:
        lines.append(f"| {k} | {recall_by_k[k]}/{n} | {recall_by_k[k]/n*100:.1f} |")
    lines += [
        "",
        "## 分點在 OOS 名單中出現的穩定度（跨11折）",
        "",
        "「n_folds_in_panel」= 這個分點在幾折（最多11折）被排除某事件後、"
        "用剩下事件重新選名單時仍入選；「n_folds_hit」= 這些折裡，該分點在"
        "被排除的那個事件當天真的命中賣超尾端的次數。",
        "",
        "| 分點 | 入選折數/11 | 命中折數 |",
        "|---|---:|---:|",
    ]
    for r in core.to_dict("records"):
        lines.append(f"| {r['branch_name']} (`{r['branch_id']}`) | {r['n_folds_in_panel']} | {r['n_folds_hit']} |")
    lines += [
        "",
        "## 解讀",
        "",
        "- 這個 recall 比 `run_market_crash_branch_risk_dashboard.py` 的門檻回測"
        "更嚴格：那邊是固定用「看過全部11個事件（含目標事件本身）」選出的名單"
        "去測其他事件；這裡每一折選名單時**看不到**被測的那個事件，比較接近"
        "真實上線後遇到新事件的情況。",
        "- 若某分點在 11 折裡幾乎每折都入選（n_folds_in_panel 接近11）且命中折數"
        "也高，代表它的訊號不是靠 2026-07-17 這一個事件撐出來的，可信度較高。",
        "- 若 recall 在這裡明顯低於門檻回測算出的 recall，代表原本的名單有一部分"
        "解釋力來自對 11 個事件（尤其 7/17）的過度配適，正式排入風控前應以這裡"
        "的 OOS 數字為準，不要用 in-sample 的數字。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Leave-one-episode-out OOS validation")
    ap.add_argument("--grid-cache", type=Path, default=GRID_CACHE)
    ap.add_argument("--episodes-csv", type=Path, default=EPISODES_CSV)
    ap.add_argument("--scores-csv", type=Path, default=SCORES_CSV)
    ap.add_argument("--min-hit-count", type=int, default=3)
    ap.add_argument("--max-p-value", type=float, default=0.10)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    args = ap.parse_args()

    if not args.grid_cache.exists():
        raise SystemExit(
            f"missing {args.grid_cache} — rerun run_market_crash_branch_precursor_scan.py "
            "with --cache-grid first"
        )

    scored = pd.read_parquet(args.grid_cache)
    episodes = pd.read_csv(args.episodes_csv)
    scores = pd.read_csv(args.scores_csv)
    scores["branch_id"] = scores["branch_id"].astype(str)
    scored["securities_trader_id"] = scored["securities_trader_id"].astype(str)
    name_map = dict(zip(scores["branch_id"], scores["branch_name"]))

    print(f"grid rows={len(scored):,} episodes={int(episodes['episode_first'].sum())}", flush=True)
    fold_df, detail_df = run_loocv(
        scored,
        episodes,
        name_map,
        min_hit_count=args.min_hit_count,
        max_p_value=args.max_p_value,
        tail_pct=args.tail_pct,
    )

    fold_path = OUT / "market_crash_precursor_loocv.csv"
    fold_df.to_csv(fold_path, index=False)
    print(f"wrote {fold_path}", flush=True)

    md_path = OUT / "market_crash_precursor_loocv.md"
    write_md(md_path, fold_df, detail_df, tail_pct=args.tail_pct)
    print(f"wrote {md_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
