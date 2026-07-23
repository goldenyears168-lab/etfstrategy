#!/usr/bin/env python3
"""Market precursor cross-sectional check — 分點提前買/賣超訊號的「同儕相對」驗證.

Research only（Track A，探索性 follow-up，尚未登錄 `config/research.yaml`）。

背景：`run_market_crash_branch_precursor_scan.py` / `run_market_rally_branch_
precursor_scan.py` 用的是「自我常態化」百分位（`lb_pctile` = 分點前 N 日累計淨額
相對「自己」2 年歷史分布的分位）。這個方法對規模差異做了控制，但無法區分
「這個分點真的提前領先」跟「這個分點跟大家一樣，在全市場放量的那幾天也跟著放量」
——因為它只跟自己比，不跟同儕比。大跌版本很選（297 檔僅 13 檔通過），大漲版本
極不選（193/297 通過），落差本身就暗示大漲訊號可能只是「全市場一起衝」的共現。

本腳本補一個「同儕相對」（cross-sectional）版本的訊號來檢驗這個假設：

  1. 直接重用兩份已算好的 grid cache（`_market_{crash,rally}_precursor_grid_
     cache.parquet`，內含每個分點每個交易日的 `lb_sum_ntd`），不重新查 DB。
  2. 對每個交易日，把當天所有 297 個分點的 `lb_sum_ntd` 互相排名
     （`groupby(trade_date)["lb_sum_ntd"].rank(pct=True)`），得到
     `xsec_pctile` ——「這個分點今天比同儕極端到什麼程度」，而非「比自己
     過去極端到什麼程度」。
  3. 用同一個方向、同一個門檻重新定義命中（大跌：xsec_pctile ≤ tail_pct；
     大漲：xsec_pctile ≥ 1-tail_pct），對 297 個分點重跑同一套跨事件命中率
     ／單尾二項檢定，輸出跟原始 `*_branch_scores.csv` 同欄位的新分數表。
  4. 比較兩套排名：overlap %、Spearman rank correlation、以及兩邊都通過
     （hit_count≥3 且 p<0.10）的「double-passing」分點清單——這份清單比任一
     單一方法都更站得住腳，因為它同時要求「對自己異常」且「對同儕異常」。

輸出（`reports/research/branch-footprint-screen/`）：
  xsec_precursor_crash_branch_scores.csv
  xsec_precursor_rally_branch_scores.csv
  xsec_precursor_comparison.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_cross_sectional_check.py
  # 或只跑單一方向：--direction crash / --direction rally
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen"

DIRECTIONS = ("crash", "rally")


def load_grid(direction: str) -> pd.DataFrame:
    path = OUT / f"_market_{direction}_precursor_grid_cache.parquet"
    grid = pd.read_parquet(path)
    grid["trade_date"] = grid["trade_date"].astype(str)
    grid["securities_trader_id"] = grid["securities_trader_id"].astype(str)
    return grid


def load_episodes(direction: str) -> pd.DataFrame:
    df = pd.read_csv(OUT / f"market_{direction}_precursor_episodes.csv", dtype={"trade_date": str})
    return df


def load_self_ts_scores(direction: str) -> pd.DataFrame:
    df = pd.read_csv(
        OUT / f"market_{direction}_precursor_branch_scores.csv",
        dtype={"branch_id": str},
    )
    return df


def compute_xsec_pctile(grid: pd.DataFrame) -> pd.DataFrame:
    """每個交易日，對所有分點的 lb_sum_ntd 互相排名（同儕相對百分位）。"""
    grid = grid.copy()
    grid["xsec_pctile"] = grid.groupby("trade_date")["lb_sum_ntd"].rank(
        pct=True, na_option="keep"
    )
    return grid


def score_branches_xsec(
    scored_grid: pd.DataFrame,
    episodes: pd.DataFrame,
    name_map: dict[str, str],
    *,
    tail_pct: float,
    direction: str,
) -> pd.DataFrame:
    ep_dates = episodes.loc[episodes["episode_first"], "trade_date"].tolist()
    n_episodes = len(ep_dates)
    sub = scored_grid[scored_grid["trade_date"].isin(ep_dates)].copy()
    sub = sub.dropna(subset=["xsec_pctile"])
    upper = 1.0 - tail_pct
    rows = []
    for bid, g in sub.groupby("securities_trader_id"):
        hit_mask = g["xsec_pctile"] <= tail_pct if direction == "crash" else g["xsec_pctile"] >= upper
        hit_count = int(hit_mask.sum())
        n_scored = int(len(g))
        if n_scored == 0:
            continue
        hit_rate = hit_count / n_scored
        p_value = stats.binomtest(hit_count, n_scored, tail_pct, alternative="greater").pvalue
        avg_hit_ntd = float(g.loc[hit_mask, "lb_sum_ntd"].mean()) if hit_count else None
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


def run_direction(direction: str, *, tail_pct: float, min_hit_count: int, p_threshold: float, suf: str) -> dict:
    print(f"[{direction}] loading grid cache …", flush=True)
    grid = load_grid(direction)
    print(f"[{direction}] grid rows={len(grid):,} branches={grid['securities_trader_id'].nunique()}", flush=True)

    episodes = load_episodes(direction)
    n_ep = int(episodes["episode_first"].sum())
    print(f"[{direction}] episodes={n_ep}", flush=True)

    self_scores = load_self_ts_scores(direction)
    name_map = dict(zip(self_scores["branch_id"], self_scores["branch_name"]))

    print(f"[{direction}] computing cross-sectional (peer-relative) percentile …", flush=True)
    scored = compute_xsec_pctile(grid)

    print(f"[{direction}] scoring branches vs episodes (xsec method) …", flush=True)
    xsec_scores = score_branches_xsec(
        scored, episodes, name_map, tail_pct=tail_pct, direction=direction
    )
    out_path = OUT / f"xsec_precursor_{direction}_branch_scores{suf}.csv"
    xsec_scores.to_csv(out_path, index=False)
    print(f"[{direction}] wrote {out_path} rows={len(xsec_scores)}", flush=True)

    merged = self_scores.merge(
        xsec_scores, on="branch_id", suffixes=("_self", "_xsec"), how="outer"
    )
    merged["branch_name"] = merged["branch_name_self"].fillna(merged["branch_name_xsec"])
    merged["hit_count_self"] = merged["hit_count_self"].fillna(0).astype(int)
    merged["hit_count_xsec"] = merged["hit_count_xsec"].fillna(0).astype(int)
    merged["p_value_self"] = merged["p_value_one_sided_self"]
    merged["p_value_xsec"] = merged["p_value_one_sided_xsec"]

    self_sig = set(
        merged.loc[
            (merged["hit_count_self"] >= min_hit_count) & (merged["p_value_self"] < p_threshold),
            "branch_id",
        ]
    )
    xsec_sig = set(
        merged.loc[
            (merged["hit_count_xsec"] >= min_hit_count) & (merged["p_value_xsec"] < p_threshold),
            "branch_id",
        ]
    )
    both = self_sig & xsec_sig
    only_self = self_sig - xsec_sig
    only_xsec = xsec_sig - self_sig

    valid = merged.dropna(subset=["hit_count_self", "hit_count_xsec"])
    rho, rho_p = (
        stats.spearmanr(valid["hit_count_self"], valid["hit_count_xsec"])
        if len(valid) >= 2
        else (float("nan"), float("nan"))
    )

    overlap_pct = (len(both) / len(self_sig) * 100.0) if self_sig else float("nan")

    result = {
        "direction": direction,
        "n_branches": len(merged),
        "n_self_sig": len(self_sig),
        "n_xsec_sig": len(xsec_sig),
        "n_both": len(both),
        "overlap_pct_of_self": overlap_pct,
        "spearman_rho": float(rho),
        "spearman_p": float(rho_p),
        "self_sig_ids": self_sig,
        "xsec_sig_ids": xsec_sig,
        "both_ids": both,
        "only_self_ids": only_self,
        "only_xsec_ids": only_xsec,
        "merged": merged,
        "xsec_scores": xsec_scores,
        "self_scores": self_scores,
    }
    print(
        f"[{direction}] self_sig={len(self_sig)} xsec_sig={len(xsec_sig)} both={len(both)} "
        f"overlap_of_self={overlap_pct:.1f}% spearman_rho={rho:.3f} (p={rho_p:.4f})",
        flush=True,
    )
    return result


def _fmt_branch_row(r: pd.Series) -> str:
    hc_self = int(r["hit_count_self"])
    hc_xsec = int(r["hit_count_xsec"])
    p_self = r["p_value_self"]
    p_xsec = r["p_value_xsec"]
    p_self_s = "NA" if pd.isna(p_self) else f"{p_self:.4f}"
    p_xsec_s = "NA" if pd.isna(p_xsec) else f"{p_xsec:.4f}"
    return (
        f"| {r['branch_name']} (`{r['branch_id']}`) | {hc_self} | {p_self_s} | "
        f"{hc_xsec} | {p_xsec_s} |"
    )


def write_comparison_md(
    path: Path,
    *,
    results: dict[str, dict],
    tail_pct: float,
    min_hit_count: int,
    p_threshold: float,
) -> None:
    lines = [
        "# 分點提前買/賣超訊號 · 同儕相對（cross-sectional）驗證",
        "",
        "Research only · 探索性分析 · Track A follow-up · 尚未登錄 `config/research.yaml` topic ·"
        " 非可下單訊號。",
        "",
        "## 動機",
        "",
        "`market_crash_precursor_branch_scores.csv` / `market_rally_precursor_branch_scores.csv`"
        " 用的百分位是分點「跟自己 2 年歷史比」（self-normalized）。這個方法沒辦法區分"
        "「這個分點真的提前領先大盤」跟「這個分點跟大家一樣，全市場放量的那幾天也跟著放量」。"
        "本研究改用「跟同一天所有其他分點比」的**同儕相對**百分位（`xsec_pctile` = "
        "`groupby(trade_date)[\"lb_sum_ntd\"].rank(pct=True)`），用同一個方向、同一個門檻"
        "（tail_pct=" + f"{tail_pct*100:.0f}%" + "）、同一套跨事件命中率／單尾二項檢定"
        "（hit_count≥" + str(min_hit_count) + " 且 p<" + f"{p_threshold:.2f}" + "）重新篩選，"
        "檢驗兩份既有清單是否經得起「同儕相對」的考驗。",
        "",
        "## 結果總覽",
        "",
        "| 方向 | 分點總數 | self-ts 通過 | xsec 通過 | 兩者皆通過 | overlap%(以 self-ts 為分母) |"
        " Spearman ρ(hit_count) | ρ 的 p 值 |",
        "|------|-----:|-----:|-----:|-----:|-----:|-----:|-----:|",
    ]
    for d in DIRECTIONS:
        r = results[d]
        label = "大跌" if d == "crash" else "大漲"
        lines.append(
            f"| {label}（{d}） | {r['n_branches']} | {r['n_self_sig']} | {r['n_xsec_sig']} | "
            f"{r['n_both']} | {r['overlap_pct_of_self']:.1f}% | {r['spearman_rho']:.3f} | "
            f"{r['spearman_p']:.4f} |"
        )

    for d in DIRECTIONS:
        r = results[d]
        label = "大跌" if d == "crash" else "大漲"
        merged = r["merged"]
        both_ids = r["both_ids"]
        only_self_ids = r["only_self_ids"]

        lines += [
            "",
            f"## {label}版本（{d}）· double-passing 清單",
            "",
            f"self-ts 通過（hit_count≥{min_hit_count}, p<{p_threshold:.2f}）**{r['n_self_sig']}** 檔，"
            f"xsec 通過 **{r['n_xsec_sig']}** 檔，兩者皆通過（double-passing）**{r['n_both']}** 檔"
            f"（= self-ts 通過清單中的 {r['overlap_pct_of_self']:.1f}%）。",
            "",
        ]
        if both_ids:
            sub = merged[merged["branch_id"].isin(both_ids)].sort_values(
                "hit_count_xsec", ascending=False
            )
            lines += [
                "| 分點 | self-ts hit_count | self-ts p值 | xsec hit_count | xsec p值 |",
                "|------|-----:|-----:|-----:|-----:|",
            ]
            for _, row in sub.iterrows():
                lines.append(_fmt_branch_row(row))
        else:
            lines.append("（無分點同時通過兩套檢定）")

        if only_self_ids:
            lines += [
                "",
                f"### self-ts 通過但 xsec 未通過（可能只是「跟大盤一起放量」，不是真正領先）· "
                f"共 {len(only_self_ids)} 檔",
                "",
            ]
            sub2 = merged[merged["branch_id"].isin(only_self_ids)].sort_values(
                "hit_count_self", ascending=False
            )
            lines += [
                "| 分點 | self-ts hit_count | self-ts p值 | xsec hit_count | xsec p值 |",
                "|------|-----:|-----:|-----:|-----:|",
            ]
            for _, row in sub2.iterrows():
                lines.append(_fmt_branch_row(row))

    lines += [
        "",
        "## 具名分點對照（人工核對用）",
        "",
        "檢查兩份原始 précursor scan 中提到的具名分點，在同儕相對檢定下是否依然過關：",
        "",
    ]
    named_crash = ["1480", "585U", "918X", "1261", "1560", "1650", "5850", "585c", "922C", "9289", "9325", "981j", "9833"]
    named_rally = ["962A", "538P", "7005", "700K", "700M", "962B", "9833", "103A"]
    for d, ids in (("crash", named_crash), ("rally", named_rally)):
        label = "大跌" if d == "crash" else "大漲"
        merged = results[d]["merged"]
        sub = merged[merged["branch_id"].isin(ids)].sort_values("hit_count_self", ascending=False)
        lines += [
            f"### {label}版本已知清單",
            "",
            "| 分點 | self-ts hit_count | self-ts p值 | xsec hit_count | xsec p值 | 同時通過門檻 |",
            "|------|-----:|-----:|-----:|-----:|:---:|",
        ]
        both_ids = results[d]["both_ids"]
        for _, row in sub.iterrows():
            mark = "✓" if row["branch_id"] in both_ids else ""
            lines.append(_fmt_branch_row(row).rstrip(" |") + f" | {mark} |")
        lines.append("")

    r_crash = results["crash"]
    r_rally = results["rally"]
    lines += [
        "## 結論",
        "",
        f"**大跌版本**：self-ts 通過 {r_crash['n_self_sig']} 檔中，{r_crash['n_both']} 檔"
        f"（{r_crash['overlap_pct_of_self']:.1f}%）在同儕相對檢定下依然通過，"
        f"hit_count 排名相關性 Spearman ρ={r_crash['spearman_rho']:.3f}。"
        + (
            "大跌版本原本就選（297 檔僅 13 檔），這裡的高存活率驗證了它不是統計噪音，"
            "而是真的抓到了「對同儕也異常」的一小群分點——賣超先行訊號在方法學上站得住腳。"
            if r_crash["overlap_pct_of_self"] >= 50
            else "大跌版本原本以為很選（297 檔僅 13 檔），但這裡的存活率偏低，"
            "說明即使是這個較嚴格的清單，也有相當比例只是「跟大盤一起放量」而非真正對同儕異常，"
            "需要重新檢視 tail_pct 門檻或考慮改用 xsec 版本作為主篩選。"
        ),
        "",
        f"**大漲版本**：self-ts 通過 {r_rally['n_self_sig']} 檔中，{r_rally['n_both']} 檔"
        f"（{r_rally['overlap_pct_of_self']:.1f}%）在同儕相對檢定下依然通過，"
        f"hit_count 排名相關性 Spearman ρ={r_rally['spearman_rho']:.3f}。"
        + (
            "大漲版本原本極不選（297 檔中 193 檔通過），這裡的存活率驗證了假設："
            "大部分通過 self-ts 檢定的分點，在跟同儕比較後就『塌陷』回接近中位數"
            "（xsec_pctile≈0.5），代表它們的訊號只是「全市場放量的共現」，"
            "不是真正搶先進場——大漲 precursor 的噪音問題確有其事，不建議把 self-ts "
            "193 檔清單直接當篩選候選。"
            if r_rally["overlap_pct_of_self"] < 50
            else "大漲版本原本極不選（297 檔中 193 檔通過），但這裡的存活率意外偏高，"
            "說明「全市場一起衝」的假設不能完全解釋大漲訊號的噪音——同儕相對檢定"
            "也大量放行同一批分點，暗示這批分點確實普遍領先（或兩套方法本身高度共線，"
            "需要另一種獨立驗證方式）。"
        ),
        "",
        "**方法互證**：double-passing（同時通過 self-ts 與 xsec 兩套檢定）清單比任一單一方法"
        "更保守、更站得住腳——它同時要求分點「相對自己歷史異常」且「相對當天同儕異常」，"
        "兩種假陽性來源（分點自身規模長期趨勢變化 / 全市場齊漲齊跌的共現）在理論上互相獨立，"
        "同時通過大幅降低誤判機率。建議後續研究若要沿用 précursor 分點清單，"
        "優先採用 double-passing 清單而非任一單一方法的結果。",
        "",
        "## 限制與注意事項",
        "",
        "- xsec_pctile 的計算沿用既有 grid cache 的 `lb_sum_ntd`（前 5 個交易日累計淨額），"
        "分母為當天有效觀察值（NaN 排除，主要是計算視窗前 5 個交易日的暖機期）；"
        "cohort 固定為原始 297 檔深覆蓋分點，未重新篩選宇宙。",
        "- 事件數仍偏少（大跌 11 個 episode／大漲 9 個），Spearman 相關性與二項檢定的"
        "檢定力都有限，本研究維持探索性定性，不作為可下單門檻依據。",
        "- xsec_pctile 是「同一天所有 297 個 cohort 分點互相排名」，並非全市場所有分點，"
        "仍是 cohort 內部的相對排名，非全市場絕對排名。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Market precursor cross-sectional (peer-relative) check")
    ap.add_argument("--direction", choices=["crash", "rally", "both"], default="both")
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--min-hit-count", type=int, default=3)
    ap.add_argument("--p-threshold", type=float, default=0.10)
    ap.add_argument("--out-suffix", default="")
    args = ap.parse_args()
    suf = args.out_suffix

    OUT.mkdir(parents=True, exist_ok=True)
    dirs = DIRECTIONS if args.direction == "both" else (args.direction,)

    results: dict[str, dict] = {}
    for d in dirs:
        results[d] = run_direction(
            d,
            tail_pct=args.tail_pct,
            min_hit_count=args.min_hit_count,
            p_threshold=args.p_threshold,
            suf=suf,
        )

    if set(dirs) == set(DIRECTIONS):
        comparison_path = OUT / f"xsec_precursor_comparison{suf}.md"
        write_comparison_md(
            comparison_path,
            results=results,
            tail_pct=args.tail_pct,
            min_hit_count=args.min_hit_count,
            p_threshold=args.p_threshold,
        )
        print(f"wrote {comparison_path}", flush=True)
    else:
        print("skipped comparison report (need both directions run together)", flush=True)

    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
