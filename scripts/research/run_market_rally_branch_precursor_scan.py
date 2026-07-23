#!/usr/bin/env python3
"""Market rally branch precursor scan — 大盤大漲日·分點提前買超研究.

Research only（探索 topic，尚未登錄 `config/research.yaml`）。與
`run_market_crash_branch_precursor_scan.py`（大跌·賣超）方向相反、方法對稱：

  過去 2 年大盤（IX0001）單日漲幅 ≥ --rally-threshold（預設 +3%）的「大漲
  日」，前 N 個交易日內，哪些券商分點已經在「淨買超」——且買超規模相對
  自己歷史屬於前段（自我常態化 tail 分位，高分位＝異常重買超）。跨多次
  大漲事件重複出現、命中率顯著高於基期的分點，視為對大盤上漲具有領先
  訊號的候選（非個股 alpha，非可下單策略）。

方法（凍結協議，與大跌版本對稱，只反轉方向）：
  1. 大漲日：IX0001 close-to-close 日報酬 ≥ --rally-threshold（預設 +3%）。
  2. episode dedup：交易日索引差 ≤ --episode-gap-days 的大漲日視為同一事件，
     僅取事件內第一天。
  3. 分點宇宙：`stock_broker_branch_daily` 2 年內有效交易日數
     ≥ --min-branch-dates 的深度覆蓋分點（與大跌版本同一 cohort 概念）。
  4. 分點訊號 = 該分點「當日對全市場所有個股」net(股) × close 加總的
     NT$ 淨額（net_amt）；net_amt>0 = 當日對市場整體是淨買方。
  5. 每個分點、每個交易日，計算「前 N 個交易日 net_amt 累計」，再對該
     分點自己的歷史分布取百分位。百分位 ≥ 1 - --tail-pct（預設 90%）=
     該分點「異常重買超」視窗。
  6. 對每個大漲事件，命中 = 該分點在大漲日的「前 N 日百分位」≥ 1-tail_pct。
     跨事件命中率 vs. 基期 tail_pct 做單尾二項檢定（H1: 命中率 > 基期）。

與大跌版本共用（direction-agnostic）的建構函式直接從
`run_market_crash_branch_precursor_scan.py` import，避免重複維護：
connect_ro / build_calendar / load_cohort / month_starts /
build_branch_day_panel / build_full_grid / compute_lookback_percentiles。

輸出（`reports/research/branch-footprint-screen/`）：
  market_rally_precursor_episodes.csv
  market_rally_precursor_branch_scores.csv
  market_rally_precursor_{case_date}_case.csv
  market_rally_precursor_summary.md
  _market_rally_precursor_meta.json

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_rally_branch_precursor_scan.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "run_market_crash_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_crash_branch_precursor_scan.py",
)
c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c)  # type: ignore[union-attr]

connect_ro = c.connect_ro
build_calendar = c.build_calendar
load_cohort = c.load_cohort
month_starts = c.month_starts
build_branch_day_panel = c.build_branch_day_panel
build_full_grid = c.build_full_grid
compute_lookback_percentiles = c.compute_lookback_percentiles

OUT = ROOT / "reports/research/branch-footprint-screen"
DB_PATH = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
BENCHMARK = "IX0001"


def find_rally_episodes(
    conn: sqlite3.Connection,
    cal: list[str],
    *,
    start: str,
    end: str,
    rally_threshold: float,
    episode_gap_days: int,
) -> pd.DataFrame:
    bench = load_benchmark_close(conn, code=BENCHMARK).sort_index()
    bench = bench[(bench.index >= start) & (bench.index <= end)]
    ret = bench.pct_change()
    rally = ret[ret >= rally_threshold]

    idx = {d: i for i, d in enumerate(cal)}
    rows = []
    for d, r in rally.items():
        if d not in idx:
            continue
        rows.append({"trade_date": d, "mkt_ret_pct": float(r) * 100.0, "cal_idx": idx[d]})
    df = pd.DataFrame(rows).sort_values("cal_idx").reset_index(drop=True)
    if df.empty:
        df["episode_first"] = pd.Series(dtype=bool)
        return df

    kept = [True]
    prev_idx = int(df.loc[0, "cal_idx"])
    for i in range(1, len(df)):
        cur_idx = int(df.loc[i, "cal_idx"])
        is_first = (cur_idx - prev_idx) > episode_gap_days
        kept.append(is_first)
        if is_first:
            prev_idx = cur_idx
    df["episode_first"] = kept
    return df


def score_branches_rally(
    scored_grid: pd.DataFrame,
    episodes: pd.DataFrame,
    name_map: dict[str, str],
    *,
    tail_pct: float,
) -> pd.DataFrame:
    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    n_episodes = len(ep_dates)
    sub = scored_grid[scored_grid["trade_date"].isin(ep_dates)].copy()
    sub = sub.dropna(subset=["lb_pctile"])
    upper = 1.0 - tail_pct
    rows = []
    for bid, g in sub.groupby("securities_trader_id"):
        hit_mask = g["lb_pctile"] >= upper
        hit_count = int(hit_mask.sum())
        n_scored = int(len(g))
        hit_rate = hit_count / n_scored if n_scored else 0.0
        if n_scored == 0:
            continue
        p_value = stats.binomtest(hit_count, n_scored, tail_pct, alternative="greater").pvalue
        avg_hit_ntd = (
            float(g.loc[hit_mask, "lb_sum_ntd"].mean()) if hit_count else None
        )
        rows.append(
            {
                "branch_id": bid,
                "branch_name": name_map.get(bid, ""),
                "n_episodes_scored": n_scored,
                "n_episodes_total": n_episodes,
                "hit_count": hit_count,
                "hit_rate_pct": round(hit_rate * 100.0, 1),
                "base_rate_pct": round(tail_pct * 100.0, 1),
                "lift": round(hit_rate / tail_pct, 2) if tail_pct else None,
                "p_value_one_sided": round(float(p_value), 4),
                "avg_hit_window_net_ntd": (
                    None if avg_hit_ntd is None else round(avg_hit_ntd, 0)
                ),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(
        ["hit_count", "hit_rate_pct", "p_value_one_sided"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def case_study_rally(
    scored_grid: pd.DataFrame,
    branch_scores: pd.DataFrame,
    name_map: dict[str, str],
    *,
    case_date: str,
    cal: list[str],
    lookback_days: int,
    tail_pct: float,
) -> pd.DataFrame:
    if case_date not in cal:
        return pd.DataFrame()
    i = cal.index(case_date)
    window = cal[max(0, i - lookback_days) : i]
    upper = 1.0 - tail_pct
    sig_ids = set(
        branch_scores.loc[
            (branch_scores["hit_count"] >= 3) & (branch_scores["p_value_one_sided"] < 0.10),
            "branch_id",
        ]
    )
    row = scored_grid[scored_grid["trade_date"] == case_date][
        ["securities_trader_id", "lb_sum_ntd", "lb_pctile"]
    ].rename(columns={"securities_trader_id": "branch_id"})
    row["branch_name"] = row["branch_id"].map(name_map).fillna("")
    row["flagged_tail"] = row["lb_pctile"] >= upper
    row["is_serial_precursor"] = row["branch_id"].isin(sig_ids)
    row["window_dates"] = ",".join(window)

    day_net = scored_grid[scored_grid["trade_date"].isin(window)][
        ["securities_trader_id", "trade_date", "net_amt"]
    ].rename(columns={"securities_trader_id": "branch_id"})
    day_pivot = day_net.pivot(index="branch_id", columns="trade_date", values="net_amt")
    day_pivot = day_pivot.add_prefix("net_")
    row = row.merge(day_pivot, left_on="branch_id", right_index=True, how="left")
    return row.sort_values("lb_sum_ntd", ascending=False).reset_index(drop=True)


def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    coverage: dict,
    episodes: pd.DataFrame,
    branch_scores: pd.DataFrame,
    case_df: pd.DataFrame,
    case_date: str,
) -> None:
    n_ep = int(episodes["episode_first"].sum()) if not episodes.empty else 0
    sig = branch_scores[
        (branch_scores["hit_count"] >= 3) & (branch_scores["p_value_one_sided"] < 0.10)
    ] if not branch_scores.empty else branch_scores

    lines = [
        "# 大盤大漲日 · 分點提前買超 precursor scan",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "與大跌版本（`market_crash_precursor_summary.md`）方法對稱，方向相反。",
        "",
        "## 凍結協議",
        "",
        f"- 大漲日：IX0001 close-to-close ≥ {protocol['rally_threshold']*100:.1f}%",
        f"- episode dedup：交易日索引差 ≤ {protocol['episode_gap_days']} 天視為同一事件",
        f"- 視窗：{protocol['start']}..{protocol['end']}",
        f"- 分點宇宙：2 年內有效交易日 ≥ {protocol['min_branch_dates']}（cohort n={coverage['n_cohort']}）",
        f"- 訊號：分點當日全市場 net(股)×close 加總 → 前 {protocol['lookback_days']} 個交易日累計，"
        f"對分點自身歷史分布取百分位；≥ {(1-protocol['tail_pct'])*100:.0f}% 視為「異常重買超」",
        f"- 統計檢定：命中率 vs. 基期 {protocol['tail_pct']*100:.0f}% 單尾二項檢定",
        "",
        "## 大漲事件（episode 首日）",
        "",
        f"共 {n_ep} 個事件（原始大漲日 {len(episodes)} 天，dedup 後 {n_ep} 個事件）。",
        "",
        "| 日期 | 大盤日報酬% |",
        "|------|-----:|",
    ]
    for r in episodes[episodes["episode_first"]].itertuples():
        lines.append(f"| {r.trade_date} | {r.mkt_ret_pct:+.2f} |")

    lines += [
        "",
        "## 分點提前買超排行（跨事件命中）",
        "",
        f"篩選：hit_count ≥ 3 且 p < 0.10（探索性門檻，非正式 graduation gate）。"
        f" 通過家數：**{len(sig)}**。",
        "",
    ]
    if not sig.empty:
        lines += [
            "| 分點 | hit/總事件 | 命中率% | 基期% | lift | p值 | 命中窗均額(NT$) |",
            "|------|-----------:|--------:|------:|-----:|----:|---------------:|",
        ]
        for r in sig.itertuples():
            amt = "NA" if r.avg_hit_window_net_ntd is None else f"{r.avg_hit_window_net_ntd:,.0f}"
            lines.append(
                f"| {r.branch_name} (`{r.branch_id}`) | {r.hit_count}/{r.n_episodes_scored} | "
                f"{r.hit_rate_pct:.1f} | {r.base_rate_pct:.1f} | {r.lift:.2f} | "
                f"{r.p_value_one_sided:.4f} | {amt} |"
            )
    else:
        lines.append("（無分點通過門檻 — 樣本事件數少，屬預期內結果，見下方限制）")

    lines += [
        "",
        "### 全部分點 Top20（依 hit_count 排序，診斷用）",
        "",
        "| rank | 分點 | hit/總事件 | 命中率% | lift | p值 |",
        "|-----:|------|-----------:|--------:|-----:|----:|",
    ]
    for i, r in enumerate(branch_scores.head(20).itertuples(), 1):
        lines.append(
            f"| {i} | {r.branch_name} (`{r.branch_id}`) | {r.hit_count}/{r.n_episodes_scored} | "
            f"{r.hit_rate_pct:.1f} | {r.lift:.2f} | {r.p_value_one_sided:.4f} |"
        )

    lines += [
        "",
        f"## 案例：{case_date} 大漲日 · 前 {protocol['lookback_days']} 交易日買超明細",
        "",
    ]
    if case_df.empty:
        lines.append("（案例日不在資料視窗內，略過）")
    else:
        day_cols = sorted(col for col in case_df.columns if col.startswith("net_"))
        day_labels = [c.replace("net_", "") for c in day_cols]
        header = (
            "| 分點 | " + " | ".join(f"{d} 淨額" for d in day_labels)
            + " | 前N日累計淨額(NT$) | 自我百分位 | 異常重買超 | 曾為跨事件顯著提前買超分點 |"
        )
        sep = (
            "|------|" + "|".join(["-----------:"] * len(day_labels))
            + "|-------------------:|-----------:|:----------:|:--------------------------:|"
        )
        lines += [header, sep]
        for r in case_df.head(20).to_dict("records"):
            day_vals = " | ".join(
                ("NA" if pd.isna(r[c]) else f"{r[c]:,.0f}") for c in day_cols
            )
            lines.append(
                f"| {r['branch_name']} (`{r['branch_id']}`) | {day_vals} | "
                f"{r['lb_sum_ntd']:,.0f} | {r['lb_pctile']*100:.1f}% | "
                f"{'✓' if r['flagged_tail'] else ''} | "
                f"{'✓' if r['is_serial_precursor'] else ''} |"
            )

    lines += [
        "",
        "## 限制與注意事項",
        "",
        "- 樣本事件數少，跨事件統計檢定力有限，本研究為**探索性**，不作為可下單策略。",
        "- 訊號定義為分點「對全市場所有個股 net(股)×close 加總」，非個股層級；"
        "分點若集中在特定產業／權值股，訊號解讀需搭配持股內容。",
        "- 分點在某交易日若無任何交易紀錄，視為 net_amt=0（中性），"
        "而非缺值；覆蓋率 <100% 的分點可能低估其買超頻率。",
        "- `stock_broker_branch_daily` 為 FinMind 分點進出（非其真實庫存變化），"
        "net>0 僅代表「當日買入多於賣出」，非「新建倉位」。",
        "- 與大跌版本一樣：買超先行 ≠ 因果，可能只是同一批機構資金輪動的共現現象。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Market rally branch precursor scan")
    ap.add_argument("--start", default="2024-07-21")
    ap.add_argument("--end", default="2026-07-21")
    ap.add_argument("--rally-threshold", type=float, default=0.03)
    ap.add_argument("--episode-gap-days", type=int, default=3)
    ap.add_argument("--min-branch-dates", type=int, default=450)
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--case-date", default="2026-07-21")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out-suffix", default="")
    ap.add_argument("--cache-grid", action="store_true")
    args = ap.parse_args()
    suf = args.out_suffix

    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)

    print("building calendar …", flush=True)
    cal = build_calendar(conn, args.start, args.end)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    print("finding rally episodes …", flush=True)
    episodes = find_rally_episodes(
        conn,
        cal,
        start=args.start,
        end=args.end,
        rally_threshold=args.rally_threshold,
        episode_gap_days=args.episode_gap_days,
    )
    n_ep = int(episodes["episode_first"].sum()) if not episodes.empty else 0
    print(f"rally days={len(episodes)} episodes={n_ep}", flush=True)
    episodes.to_csv(OUT / f"market_rally_precursor_episodes{suf}.csv", index=False)

    print("loading branch cohort (2y deep coverage) — full-table scan, may take ~1-2min …", flush=True)
    t0 = time.time()
    cohort = load_cohort(conn, start=args.start, end=args.end, min_dates=args.min_branch_dates)
    print(f"cohort n={len(cohort)} ({time.time()-t0:.1f}s)", flush=True)
    ids = cohort["id"].tolist()
    name_map = dict(zip(cohort["id"], cohort["name"]))

    print("building branch-day panel (month-chunked) …", flush=True)
    panel = build_branch_day_panel(conn, ids, start=args.start, end=args.end)
    conn.close()
    print(f"panel rows={len(panel):,}", flush=True)

    print("building full grid + lookback percentiles …", flush=True)
    grid = build_full_grid(panel, ids, cal)
    scored = compute_lookback_percentiles(grid, args.lookback_days)

    if args.cache_grid:
        cache_path = OUT / f"_market_rally_precursor_grid_cache{suf}.parquet"
        scored.to_parquet(cache_path, index=False)
        print(f"wrote grid cache {cache_path} rows={len(scored):,}", flush=True)

    print("scoring branches vs rally episodes …", flush=True)
    branch_scores = score_branches_rally(scored, episodes, name_map, tail_pct=args.tail_pct)
    branch_scores.to_csv(OUT / f"market_rally_precursor_branch_scores{suf}.csv", index=False)
    print(f"wrote branch_scores rows={len(branch_scores)}", flush=True)

    case_df = case_study_rally(
        scored,
        branch_scores,
        name_map,
        case_date=args.case_date,
        cal=cal,
        lookback_days=args.lookback_days,
        tail_pct=args.tail_pct,
    )
    case_slug = args.case_date.replace("-", "")
    case_path = OUT / f"market_rally_precursor_{case_slug}_case{suf}.csv"
    case_df.to_csv(case_path, index=False)
    print(f"wrote {case_path} rows={len(case_df)}", flush=True)

    protocol = {
        "start": args.start,
        "end": args.end,
        "rally_threshold": args.rally_threshold,
        "episode_gap_days": args.episode_gap_days,
        "min_branch_dates": args.min_branch_dates,
        "lookback_days": args.lookback_days,
        "tail_pct": args.tail_pct,
        "case_date": args.case_date,
        "benchmark": BENCHMARK,
    }
    coverage = {
        "n_cohort": len(ids),
        "n_calendar_days": len(cal),
        "n_rally_days": len(episodes),
        "n_episodes": n_ep,
        "panel_rows": len(panel),
    }
    summary_path = OUT / f"market_rally_precursor_summary{suf}.md"
    write_summary_md(
        summary_path,
        protocol=protocol,
        coverage=coverage,
        episodes=episodes,
        branch_scores=branch_scores,
        case_df=case_df,
        case_date=args.case_date,
    )
    print(f"wrote {summary_path}", flush=True)

    (OUT / f"_market_rally_precursor_meta{suf}.json").write_text(
        json.dumps({"protocol": protocol, "coverage": coverage}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
