#!/usr/bin/env python3
"""Market crash branch precursor scan — 大盤大跌日·分點提前賣超研究.

Research only（探索 topic，尚未登錄 `config/research.yaml`）。回答：

  過去 2 年（預設 2024-07-21..2026-07-20）大盤（IX0001）單日跌幅 ≤ -3% 的
  「大跌日」，前 5 個交易日內，哪些券商分點（securities_trader_id）已經在
  「淨賣超」——且賣超規模相對自己歷史屬於後段（自我常態化 tail 分位）。
  跨多次大跌事件重複出現、命中率顯著高於基期（tail_pct）的分點，視為對
  大盤下跌具有領先／風控訊號的候選（非個股 alpha，非可下單策略）。

方法（凍結協議）：
  1. 大跌日：IX0001 close-to-close 日報酬 ≤ --crash-threshold（預設 -3%）。
  2. episode dedup：交易日索引差 ≤ --episode-gap-days 的大跌日視為同一事件，
     僅取事件內第一天（避免連跌多日重複計數同一段賣超）。
  3. 分點宇宙：`stock_broker_branch_daily` 2 年內有效交易日數
     ≥ --min-branch-dates（預設 450／~490 個交易日）的深度覆蓋分點
     （與 branch-follow prefer2y 同一 cohort 概念）。
  4. 分點訊號 = 該分點「當日對全市場所有個股」net(股) × close 加總的
     NT$ 淨額（net_amt）；net_amt<0 = 當日對市場整體是淨賣方。
  5. 每個分點、每個交易日，計算「前 5 個交易日 net_amt 累計」，
     再對該分點自己的歷史分布取百分位（self-normalized，控制分點規模差異）。
     百分位 ≤ --tail-pct（預設 10%）= 該分點「異常重賣超」視窗。
  6. 對每個大跌事件，命中 = 該分點在大跌日的「前 5 日百分位」≤ tail_pct。
     跨事件命中率 vs. 基期 tail_pct 做單尾二項檢定（H1: 命中率 > 基期）。

輸出（`reports/research/branch-footprint-screen/`）：
  market_crash_precursor_episodes.csv
  market_crash_precursor_branch_scores.csv
  market_crash_precursor_20260717_case.csv（或 --case-date 指定事件）
  market_crash_precursor_summary.md
  _market_crash_precursor_meta.json

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_crash_branch_precursor_scan.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen"
DB_PATH = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
BENCHMARK = "IX0001"


def connect_ro(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """Trading calendar aligned to the branch tape's own date grain."""
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def find_crash_episodes(
    conn: sqlite3.Connection,
    cal: list[str],
    *,
    start: str,
    end: str,
    crash_threshold: float,
    episode_gap_days: int,
) -> pd.DataFrame:
    bench = load_benchmark_close(conn, code=BENCHMARK).sort_index()
    bench = bench[(bench.index >= start) & (bench.index <= end)]
    ret = bench.pct_change()
    crash = ret[ret <= crash_threshold]

    idx = {d: i for i, d in enumerate(cal)}
    rows = []
    for d, r in crash.items():
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


def load_cohort(
    conn: sqlite3.Connection, *, start: str, end: str, min_dates: int
) -> pd.DataFrame:
    """Deep-coverage branch cohort (mirrors branch-follow prefer2y logic)."""
    rows = conn.execute(
        """
        SELECT securities_trader_id AS id, MAX(securities_trader) AS name,
               COUNT(DISTINCT trade_date) AS n_dates
        FROM stock_broker_branch_daily
        WHERE source=? AND stock_id != '__EMPTY__'
          AND trade_date >= ? AND trade_date <= ?
        GROUP BY securities_trader_id
        HAVING COUNT(DISTINCT trade_date) >= ?
        ORDER BY n_dates DESC
        """,
        (SOURCE, start, end, min_dates),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    df["id"] = df["id"].astype(str)
    df["name"] = df["name"].fillna("")
    return df


def month_starts(start: str, end: str) -> list[tuple[str, str]]:
    cur = pd.Timestamp(start).to_period("M").to_timestamp()
    end_ts = pd.Timestamp(end)
    out: list[tuple[str, str]] = []
    while cur <= end_ts:
        m_end = (cur + pd.offsets.MonthEnd(0)).normalize()
        a = max(cur, pd.Timestamp(start)).strftime("%Y-%m-%d")
        b = min(m_end, end_ts).strftime("%Y-%m-%d")
        out.append((a, b))
        cur = (cur + pd.offsets.MonthBegin(1)).normalize()
        if cur.day != 1:
            cur = cur.to_period("M").to_timestamp()
    return out


def build_branch_day_panel(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Month-chunked (branch × date) aggregate: net_amt/buy_amt/sell_amt (NT$), n_stocks."""
    id_set = set(ids)
    months = month_starts(start, end)
    chunks: list[pd.DataFrame] = []
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
        grp = (
            merged.groupby(["securities_trader_id", "trade_date"], as_index=False)
            .agg(
                net_amt=("net_amt", "sum"),
                buy_amt=("buy_amt", "sum"),
                sell_amt=("sell_amt", "sum"),
                n_stocks=("stock_id", "nunique"),
            )
        )
        chunks.append(grp)
        print(
            f"  [{mi}/{len(months)}] {a}..{b} raw={len(raw):,} rows "
            f"({time.time()-t0:.1f}s)",
            flush=True,
        )
    if not chunks:
        return pd.DataFrame(
            columns=["securities_trader_id", "trade_date", "net_amt", "buy_amt", "sell_amt", "n_stocks"]
        )
    return pd.concat(chunks, ignore_index=True)


def build_full_grid(
    panel: pd.DataFrame, ids: list[str], cal: list[str]
) -> pd.DataFrame:
    """Reindex (branch × calendar date); missing days -> net_amt=0 (no observed trade)."""
    full_idx = pd.MultiIndex.from_product([ids, cal], names=["securities_trader_id", "trade_date"])
    p = panel.set_index(["securities_trader_id", "trade_date"])
    grid = p.reindex(full_idx).fillna(0.0)
    grid = grid.reset_index()
    grid["trade_date"] = grid["trade_date"].astype(str)
    return grid


def compute_lookback_percentiles(grid: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Per branch: rolling sum of net_amt over the `lookback_days` days BEFORE each row,
    then self-normalized percentile rank (0..1, low = heavy relative selling).

    2026-07-27 修正（look-ahead bug，與 `run_market_crash_thermometer_dashboard.py`
    `compute_lb_pctile()` 同一個問題）：舊版用 `lb_sum.rank(pct=True)` 對整個
    grid 一次性排名——任何一天的自我百分位都拿「載入視窗內全部日子」（含當天
    之後的未來資料）去排序。改成 **expanding window**：每一天只用「當天為止」
    已發生的資料排名，不看未來。這會連帶影響下游 `score_branches()` 算出的
    hit_count／p_value（用來篩分點、決定 PANEL 權重），因為早期事件的
    「異常重賣超」判定，舊版其實部分借用了事件發生後才出現的資料。"""
    grid = grid.sort_values(["securities_trader_id", "trade_date"]).copy()

    def _per_branch(g: pd.DataFrame) -> pd.DataFrame:
        lb_sum = g["net_amt"].shift(1).rolling(lookback_days).sum()
        vals = lb_sum.to_numpy()
        n = len(vals)
        pct = np.full(n, np.nan)
        for i in range(n):
            if np.isnan(vals[i]):
                continue
            window = vals[: i + 1]
            window = window[~np.isnan(window)]
            if len(window) == 0:
                continue
            pct[i] = stats.percentileofscore(window, vals[i], kind="mean") / 100.0
        return pd.DataFrame({"lb_sum_ntd": lb_sum, "lb_pctile": pct}, index=g.index)

    out = grid.groupby("securities_trader_id", group_keys=False).apply(
        _per_branch, include_groups=False
    )
    return pd.concat(
        [grid.reset_index(drop=True), out.reset_index(drop=True)],
        axis=1,
    )


def score_branches(
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
    rows = []
    for bid, g in sub.groupby("securities_trader_id"):
        hit_mask = g["lb_pctile"] <= tail_pct
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


def case_study(
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
    row["flagged_tail"] = row["lb_pctile"] <= tail_pct
    row["is_serial_precursor"] = row["branch_id"].isin(sig_ids)
    row["window_dates"] = ",".join(window)

    day_net = scored_grid[scored_grid["trade_date"].isin(window)][
        ["securities_trader_id", "trade_date", "net_amt"]
    ].rename(columns={"securities_trader_id": "branch_id"})
    day_pivot = day_net.pivot(index="branch_id", columns="trade_date", values="net_amt")
    day_pivot = day_pivot.add_prefix("net_")
    row = row.merge(day_pivot, left_on="branch_id", right_index=True, how="left")
    return row.sort_values("lb_sum_ntd").reset_index(drop=True)


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
        "# 大盤大跌日 · 分點提前賣超 precursor scan",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "",
        "## 凍結協議",
        "",
        f"- 大跌日：IX0001 close-to-close ≤ {protocol['crash_threshold']*100:.1f}%",
        f"- episode dedup：交易日索引差 ≤ {protocol['episode_gap_days']} 天視為同一事件",
        f"- 視窗：{protocol['start']}..{protocol['end']}",
        f"- 分點宇宙：2 年內有效交易日 ≥ {protocol['min_branch_dates']}（cohort n={coverage['n_cohort']}）",
        f"- 訊號：分點當日全市場 net(股)×close 加總 → 前 {protocol['lookback_days']} 個交易日累計，"
        f"對分點自身歷史分布取百分位；≤ {protocol['tail_pct']*100:.0f}% 視為「異常重賣超」",
        f"- 統計檢定：命中率 vs. 基期 {protocol['tail_pct']*100:.0f}% 單尾二項檢定",
        "",
        "## 大跌事件（episode 首日）",
        "",
        f"共 {n_ep} 個事件（原始大跌日 {len(episodes)} 天，dedup 後 {n_ep} 個事件）。",
        "",
        "| 日期 | 大盤日報酬% |",
        "|------|-----:|",
    ]
    for r in episodes[episodes["episode_first"]].itertuples():
        lines.append(f"| {r.trade_date} | {r.mkt_ret_pct:+.2f} |")

    lines += [
        "",
        "## 分點提前賣超排行（跨事件命中）",
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
        f"### 全部分點 Top20（依 hit_count 排序，診斷用）",
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
        f"## 案例：{case_date} 大跌日 · 前 {protocol['lookback_days']} 交易日賣超明細",
        "",
    ]
    if case_df.empty:
        lines.append("（案例日不在資料視窗內，略過）")
    else:
        day_cols = sorted(c for c in case_df.columns if c.startswith("net_"))
        day_labels = [c.replace("net_", "") for c in day_cols]
        header = (
            "| 分點 | " + " | ".join(f"{d} 淨額" for d in day_labels)
            + " | 前5日累計淨額(NT$) | 自我百分位 | 異常重賣超 | 曾為跨事件顯著提前賣超分點 |"
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
        "- 樣本事件數少（2 年內 3% 大跌約 10 餘個事件），跨事件統計檢定力有限，"
        "本研究為**探索性**，不作為可下單風控規則。",
        "- 訊號定義為分點「對全市場所有個股 net(股)×close 加總」，非個股層級；"
        "分點若集中在特定產業／權值股，訊號解讀需搭配持股內容。",
        "- 分點在某交易日若無任何交易紀錄，視為 net_amt=0（中性），"
        "而非缺值；覆蓋率 <100% 的分點可能低估其賣超頻率。",
        "- `stock_broker_branch_daily` 為 FinMind 分點進出（非其真實庫存變化），"
        "net<0 僅代表「當日賣出多於買入」，非「持股清空」。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Market crash branch precursor scan")
    ap.add_argument("--start", default="2024-07-21")
    ap.add_argument("--end", default="2026-07-20")
    ap.add_argument("--crash-threshold", type=float, default=-0.03)
    ap.add_argument("--episode-gap-days", type=int, default=3)
    ap.add_argument("--min-branch-dates", type=int, default=450)
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--case-date", default="2026-07-17")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument(
        "--out-suffix",
        default="",
        help="appended to output filenames before extension, e.g. '_lb4' — lets "
        "window-length sweeps (3/4/5/6/7 天) write to distinct files without clobbering",
    )
    ap.add_argument(
        "--cache-grid",
        action="store_true",
        help="也把完整 branch-day scored grid（lb_sum_ntd/lb_pctile，未依事件聚合）"
        "存成 parquet cache，供 leave-one-episode-out OOS 驗證等後續分析重用，"
        "避免每次都要重跑 ~4-5 分鐘的 SQL 全表掃描",
    )
    args = ap.parse_args()
    suf = args.out_suffix

    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)

    print("building calendar …", flush=True)
    cal = build_calendar(conn, args.start, args.end)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    print("finding crash episodes …", flush=True)
    episodes = find_crash_episodes(
        conn,
        cal,
        start=args.start,
        end=args.end,
        crash_threshold=args.crash_threshold,
        episode_gap_days=args.episode_gap_days,
    )
    n_ep = int(episodes["episode_first"].sum()) if not episodes.empty else 0
    print(f"crash days={len(episodes)} episodes={n_ep}", flush=True)
    episodes.to_csv(OUT / f"market_crash_precursor_episodes{suf}.csv", index=False)

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
        cache_path = OUT / f"_market_crash_precursor_grid_cache{suf}.parquet"
        scored.to_parquet(cache_path, index=False)
        print(f"wrote grid cache {cache_path} rows={len(scored):,}", flush=True)

    print("scoring branches vs crash episodes …", flush=True)
    branch_scores = score_branches(scored, episodes, name_map, tail_pct=args.tail_pct)
    branch_scores.to_csv(OUT / f"market_crash_precursor_branch_scores{suf}.csv", index=False)
    print(f"wrote branch_scores rows={len(branch_scores)}", flush=True)

    case_df = case_study(
        scored,
        branch_scores,
        name_map,
        case_date=args.case_date,
        cal=cal,
        lookback_days=args.lookback_days,
        tail_pct=args.tail_pct,
    )
    case_slug = args.case_date.replace("-", "")
    case_path = OUT / f"market_crash_precursor_{case_slug}_case{suf}.csv"
    case_df.to_csv(case_path, index=False)
    print(f"wrote {case_path} rows={len(case_df)}", flush=True)

    protocol = {
        "start": args.start,
        "end": args.end,
        "crash_threshold": args.crash_threshold,
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
        "n_crash_days": len(episodes),
        "n_episodes": n_ep,
        "panel_rows": len(panel),
    }
    summary_path = OUT / f"market_crash_precursor_summary{suf}.md"
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

    (OUT / f"_market_crash_precursor_meta{suf}.json").write_text(
        json.dumps({"protocol": protocol, "coverage": coverage}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
