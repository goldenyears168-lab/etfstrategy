#!/usr/bin/env python3
"""2492 華新科 · 大漲前 T−1/T−2 分點買超常客（Track P）.

Research only. See SURGE_BRANCH_RESEARCH_PLANS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "reports/research/chip-overlays/2492_knowledge/surge_branch"
DB = ROOT / "data" / "stocks.db"
SID = "2492"
START = "2024-07-01"
SURGE = 0.05
EP_GAP = 5
OOS = "2026-01-01"
TOP_N = 10


def main() -> int:
    import sqlite3

    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    bars = pd.read_sql_query(
        """
        SELECT trade_date AS date, close FROM stock_daily_bars
        WHERE source='finmind' AND stock_id=? AND trade_date>=?
        ORDER BY 1
        """,
        conn,
        params=[SID, START],
    )
    br = pd.read_sql_query(
        """
        SELECT trade_date AS date, securities_trader_id AS tid,
               securities_trader AS tname, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE source='finmind' AND stock_id=? AND trade_date>=?
        ORDER BY trade_date
        """,
        conn,
        params=[SID, START],
    )
    conn.close()

    bars["date"] = pd.to_datetime(bars["date"])
    br["date"] = pd.to_datetime(br["date"])
    bars = bars.sort_values("date").drop_duplicates("date")
    bars["r"] = bars["close"].pct_change()
    cal = bars["date"].tolist()
    idx = {d: i for i, d in enumerate(cal)}

    surge_days = bars.loc[bars["r"] >= SURGE, ["date", "r", "close"]].copy()
    surge_days["cal_idx"] = surge_days["date"].map(idx)
    surge_days = surge_days.dropna(subset=["cal_idx"]).sort_values("cal_idx")
    kept: list[bool] = []
    prev = -10**9
    for _, row in surge_days.iterrows():
        i = int(row.cal_idx)
        if i - prev > EP_GAP:
            kept.append(True)
            prev = i
        else:
            kept.append(False)
    surge_days["episode_first"] = kept
    eps = surge_days[surge_days.episode_first].copy()
    eps["surge_ret_pct"] = eps["r"] * 100
    eps["split"] = np.where(eps["date"] >= pd.Timestamp(OOS), "OOS", "IS")
    n_ep = len(eps)
    n_is = int((eps["split"] == "IS").sum())
    n_oos = int((eps["split"] == "OOS").sum())
    print(f"episodes={n_ep} IS={n_is} OOS={n_oos}")

    eps_out = eps[["date", "surge_ret_pct", "close", "split"]].rename(
        columns={"date": "surge_date"}
    )
    eps_out.to_csv(OUT / "surge_episodes.csv", index=False)

    br = br.merge(bars[["date", "close"]], on="date", how="left")
    br["net_amt"] = br["net"] * br["close"]
    buyers = br[br["net"] > 0].copy()

    rows = []
    for _, ep in eps.iterrows():
        si = int(ep.cal_idx)
        for lag in (1, 2):
            if si - lag < 0:
                continue
            d = cal[si - lag]
            day = buyers[buyers["date"] == d]
            if day.empty:
                continue
            top = day.nlargest(TOP_N, "net_amt").copy()
            top["rank"] = range(1, len(top) + 1)
            for _, r in top.iterrows():
                rows.append(
                    {
                        "surge_date": ep["date"].date().isoformat(),
                        "split": ep["split"],
                        "lag": lag,
                        "precursor_date": d.date().isoformat(),
                        "tid": r["tid"],
                        "tname": r["tname"],
                        "net": r["net"],
                        "net_amt": r["net_amt"],
                        "rank": int(r["rank"]),
                    }
                )
    prec = pd.DataFrame(rows)
    prec.to_csv(OUT / "precursor_branch_day.csv", index=False)

    scores = []
    for tid, g in prec.groupby("tid"):
        name = g["tname"].dropna().mode().iloc[0] if g["tname"].notna().any() else tid
        scores.append(
            {
                "tid": tid,
                "tname": name,
                "episodes_hit": g["surge_date"].nunique(),
                "hit_rate": g["surge_date"].nunique() / n_ep if n_ep else np.nan,
                "hit_lag1": g.loc[g["lag"] == 1, "surge_date"].nunique(),
                "hit_lag2": g.loc[g["lag"] == 2, "surge_date"].nunique(),
                "is_hit": g.loc[g["split"] == "IS", "surge_date"].nunique(),
                "oos_hit": g.loc[g["split"] == "OOS", "surge_date"].nunique(),
                "is_hit_rate": (
                    g.loc[g["split"] == "IS", "surge_date"].nunique() / n_is
                    if n_is
                    else np.nan
                ),
                "oos_hit_rate": (
                    g.loc[g["split"] == "OOS", "surge_date"].nunique() / n_oos
                    if n_oos
                    else np.nan
                ),
                "med_net_amt": g["net_amt"].median(),
                "med_rank": g["rank"].median(),
                "n_top_appearances": len(g),
            }
        )
    sc = pd.DataFrame(scores).sort_values(
        ["episodes_hit", "med_net_amt"], ascending=[False, False]
    )

    day_tops = []
    for d, day in buyers.groupby("date"):
        day_tops.append(day.nlargest(TOP_N, "net_amt").assign(date=d))
    all_top = pd.concat(day_tops, ignore_index=True)
    days_total = buyers["date"].nunique()
    base = (all_top.groupby("tid").size() / days_total).rename("top10_days")
    sc = sc.merge(base.reset_index(), on="tid", how="left")
    sc["top10_days"] = sc["top10_days"].fillna(0.0)
    sc["lift_vs_base"] = sc["hit_rate"] - sc["top10_days"]
    sc.to_csv(OUT / "precursor_branch_scores.csv", index=False)

    thr = max(3, int(np.ceil(0.3 * n_ep)))
    stable = sc[sc["episodes_hit"] >= thr]
    oos_cov = sc[(sc["is_hit"] >= 2) & (sc["oos_hit"] >= 1)]

    lines = [
        "# 華新科(2492) 大漲前分點常客（Track P）",
        "",
        f"- Window: `{START}` → `{bars['date'].max().date()}` · surge ≥ +{SURGE*100:.0f}% · episode gap {EP_GAP}d",
        f"- Episodes: **{n_ep}**（IS {n_is} / OOS {n_oos}）",
        f"- Precursor: T−1 / T−2 當日該股淨買金額 top{TOP_N}（不含大漲當日）",
        f"- Baseline: 任意交易日進入 top{TOP_N} 的日頻比例",
        "",
        "## 常客榜（按跨事件命中次數）",
        "",
        "| 分點 | id | 命中事件 | hit | IS | OOS | 基期top10 | lift | 中位淨買額 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in sc.head(15).iterrows():
        lines.append(
            f"| {r.tname} | `{r.tid}` | {int(r.episodes_hit)}/{n_ep} | {r.hit_rate:.0%} | "
            f"{int(r.is_hit)} | {int(r.oos_hit)} | {r.top10_days:.1%} | "
            f"{r.lift_vs_base:+.0%} | {r.med_net_amt/1e8:.2f}億 |"
        )
    lines += [
        "",
        "## 穩定席（暫定）",
        "",
        f"- 命中 ≥ {thr}（max(3, 30% episodes)）: **{len(stable)}** 席",
        f"- IS≥2 且 OOS≥1: **{len(oos_cov)}** 席",
        "",
        "### 建議列入融合觀察的席（前 8）",
        "",
    ]
    for _, r in sc.head(8).iterrows():
        lines.append(
            f"- **{r.tname}** (`{r.tid}`) · 事件命中 {int(r.episodes_hit)}/{n_ep} · "
            f"OOS {int(r.oos_hit)} · lift {r.lift_vs_base:+.0%}"
        )
    lines += [
        "",
        "## 注意",
        "",
        "- 2492 專家池為 `skipped_hard0`；此榜≠已採納專家。",
        "- hit 高但基期也高 → 看 **lift**。",
        "- 下一步：與 chip Tier A 做 AND 融合（`SURGE_BRANCH_RESEARCH_PLANS.md`）。",
        "",
    ]
    (OUT / "SURGE_BRANCH_TOP.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(sc.head(8)[["tname", "tid", "episodes_hit", "hit_rate", "oos_hit", "lift_vs_base"]])
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
