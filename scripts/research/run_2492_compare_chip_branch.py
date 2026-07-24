#!/usr/bin/env python3
"""2492: chip vs branch vs fusion — which predicts T+1 / T+5 better?

Branch buy window: any net-buy (or top10) within last 5 trading days counts.
Research only. Writes surge_branch/COMPARE_CHIP_BRANCH.md
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "reports/research/chip-overlays/2492_knowledge/surge_branch"
DB = ROOT / "data" / "stocks.db"
PANEL = ROOT / "reports/research/chip-overlays/2492_knowledge/panel_daily.csv"
SID = "2492"
START = "2025-07-01"
OOS = "2026-01-01"
TOP_N = 10
BUY_WIN = 5  # 五天內買超都算

FOREIGN_KW = (
    "美商", "摩根", "瑞銀", "美林", "港商", "野村", "瑞士", "花旗", "渣打",
    "香港", "新加坡", "大和", "法銀", "德意志", "巴克萊", "高盛", "美銀",
)

DOM_A = ["9239", "983Z", "5850", "9654", "9666"]  # hi-lift lo-base
# expand with other high-value domestic from prior rerun
DOM_EXTRA = ["980w", "8888", "700B", "984K", "779D", "585H", "9268", "9227", "918X", "981j"]
WATCH = sorted(set(DOM_A + DOM_EXTRA))


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def rolling_any(s: pd.Series, win: int) -> pd.Series:
    return s.astype(float).rolling(win, min_periods=1).max().fillna(0).astype(bool)


def score_split(df: pd.DataFrame, mask: pd.Series, y: str, split_mask: pd.Series) -> dict:
    yv = df.loc[split_mask & mask, y].dropna()
    n = int(len(yv))
    if n == 0:
        return {"n": 0, "hit": np.nan, "med": np.nan}
    return {
        "n": n,
        "hit": float((yv > 0).mean()),
        "med": float(yv.median()),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    panel = pd.read_csv(PANEL)
    panel["date"] = pd.to_datetime(panel["date"])

    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    br = pd.read_sql_query(
        """
        SELECT trade_date AS date, securities_trader_id AS tid,
               securities_trader AS tname, net
        FROM stock_broker_branch_daily
        WHERE source='finmind' AND stock_id=? AND trade_date>=?
        """,
        conn,
        params=[SID, START],
    )
    bars = pd.read_sql_query(
        """
        SELECT trade_date AS date, close FROM stock_daily_bars
        WHERE source='finmind' AND stock_id=? AND trade_date>=?
        """,
        conn,
        params=[SID, START],
    )
    conn.close()

    br["date"] = pd.to_datetime(br["date"])
    bars["date"] = pd.to_datetime(bars["date"])
    br = br.merge(bars, on="date", how="left")
    br["net_amt"] = br["net"] * br["close"]
    br["tid"] = br["tid"].astype(str)
    name_map = (
        br.dropna(subset=["tname"])
        .groupby("tid")["tname"]
        .agg(lambda s: s.mode().iloc[0] if len(s.mode()) else s.iloc[0])
        .to_dict()
    )

    # daily top10 membership
    buyers = br[br["net"] > 0]
    top_rows = []
    for d, day in buyers.groupby("date"):
        top = day.nlargest(TOP_N, "net_amt")
        for _, r in top.iterrows():
            top_rows.append({"date": d, "tid": str(r["tid"])})
    top_df = pd.DataFrame(top_rows)

    cal = sorted(panel["date"].unique())
    # wide daily flags for watch seats
    net_buy = (
        br[br["tid"].isin(WATCH) & (br["net"] > 0)]
        .groupby(["date", "tid"])
        .size()
        .unstack(fill_value=0)
        .reindex(cal, fill_value=0)
    )
    net_buy = (net_buy > 0).astype(int)
    for tid in WATCH:
        if tid not in net_buy.columns:
            net_buy[tid] = 0

    in_top = (
        top_df[top_df["tid"].isin(WATCH)]
        .groupby(["date", "tid"])
        .size()
        .unstack(fill_value=0)
        .reindex(cal, fill_value=0)
    )
    in_top = (in_top > 0).astype(int)
    for tid in WATCH:
        if tid not in in_top.columns:
            in_top[tid] = 0

    # 5d any-buy / any-top10
    buy5 = net_buy.apply(lambda c: rolling_any(c, BUY_WIN))
    top5 = in_top.apply(lambda c: rolling_any(c, BUY_WIN))

    df = panel.set_index("date").reindex(cal).sort_index()
    # chip features
    df["scoop"] = ((df["net_FT"] < 0) & (df["FT_ntd"] > 0) & (df["FT_resid_z"] >= 0.5)).fillna(False)
    df["dump_rs"] = (
        (df["net_FT"] > 0) & (df["FT_ntd"] < 0) & (df["RS_mom10"] <= 0)
    ).fillna(False)
    df["aligned_buy"] = (df["aligned_buy"] == True) | (df["aligned_buy"] == 1)  # noqa: E712
    df["part_hi"] = (df["FT_part_ok"] == True) & (df["FT_part_pct"] >= 0.8)  # noqa: E712
    df["part_lo"] = (df["FT_part_ok"] == True) & (df["FT_part_pct"] <= 0.2)  # noqa: E712
    df["chip_bull"] = df["scoop"] | (df["aligned_buy"] & df["part_hi"])
    df["chip_bear"] = df["dump_rs"] | df["part_lo"]
    df["ft_z_pos"] = (df["FT_z"] >= 1.0).fillna(False)
    df["ft_z_neg"] = (df["FT_z"] <= -1.0).fillna(False)

    # branch aggregates (5d)
    df["domA_buy5_n"] = buy5[DOM_A].sum(axis=1).reindex(df.index).fillna(0)
    df["domA_top5_n"] = top5[DOM_A].sum(axis=1).reindex(df.index).fillna(0)
    df["watch_buy5_n"] = buy5[WATCH].sum(axis=1).reindex(df.index).fillna(0)
    df["watch_top5_n"] = top5[WATCH].sum(axis=1).reindex(df.index).fillna(0)
    for tid in DOM_A + ["9268", "9654", "9239", "5850"]:
        df[f"buy5_{tid}"] = buy5[tid].reindex(df.index).fillna(False).astype(bool)
        df[f"top5_{tid}"] = top5[tid].reindex(df.index).fillna(False).astype(bool)

    df = df.reset_index().rename(columns={"index": "date"})
    if "date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "date"})

    is_m = df["date"] < pd.Timestamp(OOS)
    oos_m = df["date"] >= pd.Timestamp(OOS)
    base_oos = {
        "fwd1_r": float((df.loc[oos_m, "fwd1_r"] > 0).mean()),
        "fwd5_r": float((df.loc[oos_m, "fwd5_r"] > 0).mean()),
    }

    arms: list[tuple[str, str, pd.Series]] = []
    # family, name, mask
    arms += [
        ("chip", "C_scoop_resid", df["scoop"]),
        ("chip", "C_dump_rs", df["dump_rs"]),
        ("chip", "C_aligned_hi_part", df["aligned_buy"] & df["part_hi"]),
        ("chip", "C_part_lo", df["part_lo"]),
        ("chip", "C_ft_z_ge1", df["ft_z_pos"]),
        ("chip", "C_ft_z_le_m1", df["ft_z_neg"]),
        ("chip", "C_bull_any", df["chip_bull"]),
        ("chip", "C_bear_any", df["chip_bear"]),
    ]
    arms += [
        ("branch", "B_domA_buy5_k1", df["domA_buy5_n"] >= 1),
        ("branch", "B_domA_buy5_k2", df["domA_buy5_n"] >= 2),
        ("branch", "B_domA_buy5_k3", df["domA_buy5_n"] >= 3),
        ("branch", "B_domA_top5_k1", df["domA_top5_n"] >= 1),
        ("branch", "B_domA_top5_k2", df["domA_top5_n"] >= 2),
        ("branch", "B_watch_buy5_k3", df["watch_buy5_n"] >= 3),
        ("branch", "B_watch_top5_k2", df["watch_top5_n"] >= 2),
        ("branch", "B_seat_9239_buy5", df["buy5_9239"]),
        ("branch", "B_seat_9654_buy5", df["buy5_9654"]),
        ("branch", "B_seat_5850_buy5", df["buy5_5850"]),
        ("branch", "B_seat_9268_buy5", df["buy5_9268"]),
        ("branch", "B_seat_9239_top5", df["top5_9239"]),
        ("branch", "B_seat_9654_top5", df["top5_9654"]),
    ]
    # fusion
    arms += [
        ("fusion", "F_scoop_OR_domA_top5_k2", df["scoop"] | (df["domA_top5_n"] >= 2)),
        ("fusion", "F_scoop_AND_domA_buy5_k1", df["scoop"] & (df["domA_buy5_n"] >= 1)),
        ("fusion", "F_scoop_AND_domA_top5_k1", df["scoop"] & (df["domA_top5_n"] >= 1)),
        ("fusion", "F_bull_OR_domA_buy5_k2", df["chip_bull"] | (df["domA_buy5_n"] >= 2)),
        ("fusion", "F_bear_OR_not_domA_buy5", df["chip_bear"] & (df["domA_buy5_n"] < 1)),
        # soft: chip bull or (branch strong without requiring chip)
        ("fusion", "F_scoop_OR_9654_buy5", df["scoop"] | df["buy5_9654"]),
        ("fusion", "F_scoop_OR_domA_buy5_k2", df["scoop"] | (df["domA_buy5_n"] >= 2)),
        # confirm: branch then require not chip dump
        ("fusion", "F_domA_top5_k2_not_dump", (df["domA_top5_n"] >= 2) & ~df["dump_rs"]),
        ("fusion", "F_9654_buy5_not_dump", df["buy5_9654"] & ~df["dump_rs"]),
        ("fusion", "F_domA_buy5_k2_not_dump", (df["domA_buy5_n"] >= 2) & ~df["dump_rs"]),
    ]

    rows = []
    for fam, name, mask in arms:
        mask = mask.fillna(False)
        for y in ("fwd1_r", "fwd5_r"):
            s_is = score_split(df, mask, y, is_m)
            s_oos = score_split(df, mask, y, oos_m)
            base = base_oos[y]
            lift = s_oos["hit"] - base if np.isfinite(s_oos["hit"]) else np.nan
            # stability: both sides not terrible
            stable = (
                s_is["n"] >= 8
                and s_oos["n"] >= 8
                and np.isfinite(s_is["hit"])
                and np.isfinite(s_oos["hit"])
                and s_is["hit"] >= 0.50
                and s_oos["hit"] >= 0.55
                and s_oos["med"] > 0
            )
            # strength score (OOS primary, IS gate soft)
            if s_oos["n"] < 8 or not np.isfinite(s_oos["hit"]):
                strength = -1.0
            else:
                is_pen = 1.0
                if s_is["n"] >= 5 and np.isfinite(s_is["hit"]):
                    if s_is["hit"] < 0.45:
                        is_pen = 0.35  # flash OOS
                    elif s_is["hit"] < 0.50:
                        is_pen = 0.7
                strength = (
                    (s_oos["hit"] - base)
                    * np.sqrt(s_oos["n"])
                    * is_pen
                    * (1.0 if s_oos["med"] > 0 else 0.5)
                )
            rows.append(
                {
                    "family": fam,
                    "arm": name,
                    "target": y,
                    "n_is": s_is["n"],
                    "hit_is": s_is["hit"],
                    "med_is": s_is["med"],
                    "n_oos": s_oos["n"],
                    "hit_oos": s_oos["hit"],
                    "med_oos": s_oos["med"],
                    "base_oos": base,
                    "lift_oos": lift,
                    "stable": stable,
                    "strength": strength,
                }
            )

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "compare_chip_branch_scores.csv", index=False)

    # best per family per target
    lines = [
        "# 華新科(2492) · 籌碼 vs 分點 vs 搭配（徹底對打）",
        "",
        f"- Window: panel `{panel['date'].min().date()}`→`{panel['date'].max().date()}` · OOS `{OOS}`",
        f"- 分點買超窗：**近 {BUY_WIN} 個交易日任一淨買／進 top{TOP_N} 都算**",
        f"- DOM_A: " + ", ".join(f"{name_map.get(t,t)}(`{t}`)" for t in DOM_A),
        f"- 基線 OOS: P(fwd1>0)={base_oos['fwd1_r']:.1%} · P(fwd5>0)={base_oos['fwd5_r']:.1%}",
        "",
        "## 總判決",
        "",
    ]

    verdict_bits = []
    for y in ("fwd1_r", "fwd5_r"):
        sub = res[res["target"] == y].copy()
        # best stable
        stab = sub[sub["stable"]].sort_values("strength", ascending=False)
        best_any = sub[sub["n_oos"] >= 8].sort_values("strength", ascending=False)
        fam_best = {}
        for fam in ("chip", "branch", "fusion"):
            fsub = sub[(sub["family"] == fam) & (sub["n_oos"] >= 8)].sort_values(
                "strength", ascending=False
            )
            fam_best[fam] = fsub.iloc[0] if len(fsub) else None
        horizon = "T+1" if y == "fwd1_r" else "T+5"
        lines.append(f"### {horizon}")
        lines.append("")
        if len(stab):
            b = stab.iloc[0]
            lines.append(
                f"- **最強穩定臂**: `{b.arm}`（{b.family}）OOS hit={b.hit_oos:.0%} "
                f"med={b.med_oos*100:+.2f}% n={int(b.n_oos)} · IS hit={b.hit_is:.0%} n={int(b.n_is)}"
            )
            verdict_bits.append((horizon, "stable", b))
        else:
            lines.append("- **無同時過 IS≥50% 且 OOS≥55% 的穩定臂**")
        if len(best_any):
            b = best_any.iloc[0]
            lines.append(
                f"- OOS 強度最高（含 IS 懲罰）: `{b.arm}` hit={b.hit_oos:.0%} "
                f"strength={b.strength:.2f} · IS hit={b.hit_is:.0%}"
            )
        lines.append("")
        lines.append("| 家族 | 最佳臂 | OOS hit | med | n | IS hit | strength |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for fam in ("chip", "branch", "fusion"):
            b = fam_best[fam]
            if b is None:
                lines.append(f"| {fam} | — | — | — | — | — | — |")
            else:
                lines.append(
                    f"| {fam} | `{b.arm}` | {b.hit_oos:.0%} | {b.med_oos*100:+.2f}% | "
                    f"{int(b.n_oos)} | {b.hit_is:.0%} | {b.strength:.2f} |"
                )
        lines.append("")

        # who wins
        scores = {fam: (fam_best[fam].strength if fam_best[fam] is not None else -9) for fam in ("chip", "branch", "fusion")}
        winner = max(scores, key=scores.get)
        lines.append(f"- **{horizon} 家族勝出（依 strength）: `{winner}`** → " + ", ".join(f"{k}={v:.2f}" for k, v in scores.items()))
        lines.append("")

    lines += [
        "## 穩定臂清單（IS≥50% · OOS≥55% · n≥8 · med>0）",
        "",
    ]
    stab_all = res[res["stable"]].sort_values(["target", "strength"], ascending=[True, False])
    if stab_all.empty:
        lines.append("_無_（多數高 OOS 臂在 IS 翻車）")
    else:
        lines.append("| target | family | arm | IS hit | OOS hit | OOS med | n_oos | strength |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|")
        for _, r in stab_all.iterrows():
            lines.append(
                f"| {r.target} | {r.family} | `{r.arm}` | {r.hit_is:.0%} | {r.hit_oos:.0%} | "
                f"{r.med_oos*100:+.2f}% | {int(r.n_oos)} | {r.strength:.2f} |"
            )

    lines += [
        "",
        "## OOS Top10（各 horizon · 含 IS 懲罰後 strength）",
        "",
    ]
    for y in ("fwd1_r", "fwd5_r"):
        lines.append(f"### {y}")
        lines.append("")
        lines.append("| family | arm | n_oos | hit_oos | med | hit_is | strength |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        top = res[(res.target == y) & (res.n_oos >= 8)].sort_values("strength", ascending=False).head(10)
        for _, r in top.iterrows():
            lines.append(
                f"| {r.family} | `{r.arm}` | {int(r.n_oos)} | {r.hit_oos:.0%} | "
                f"{r.med_oos*100:+.2f}% | {r.hit_is:.0%} | {r.strength:.2f} |"
            )
        lines.append("")

    lines += [
        "## 解讀規則",
        "",
        "1. **穩定 > 漂亮**：IS 很差、OOS 很美 = 可能樣本切段運氣，不稱「最強」。",
        "2. **五天買超窗**會提高覆蓋、降低尖銳度；k≥2／top5 通常比單日淨買穩。",
        "3. **搭配**只有在 fusion strength 明顯高於 chip／branch 單做時才算「1+1>2」。",
        "4. FT 籌碼 = 外資+投信；分點 DOM_A = 本土高 lift 席。",
        "",
        f"機器分數：`compare_chip_branch_scores.csv`",
        "",
    ]

    # JSON summary for knowledge
    summary = {
        "buy_win_days": BUY_WIN,
        "oos_cut": OOS,
        "base_oos": base_oos,
        "dom_a": [{"tid": t, "name": name_map.get(t, t)} for t in DOM_A],
        "stable_arms": stab_all.to_dict(orient="records") if len(stab_all) else [],
        "best_by_target": {},
    }
    for y in ("fwd1_r", "fwd5_r"):
        sub = res[(res.target == y) & (res.n_oos >= 8)].sort_values("strength", ascending=False)
        summary["best_by_target"][y] = sub.head(5).to_dict(orient="records")

    (OUT / "COMPARE_CHIP_BRANCH.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "compare_chip_branch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print("\n".join(lines[:80]))
    print("wrote", OUT / "COMPARE_CHIP_BRANCH.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
