#!/usr/bin/env python3
"""Market precursor house-level aggregation — 分點 → 券商 house 聚合研究.

Research only（探索 topic，尚未登錄 `config/research.yaml`）。動機：`
run_market_crash_branch_precursor_scan.py`／`run_market_rally_branch_precursor_scan.py`
的 297 分點 cohort 中，通過 hit_count≥3 & p<0.10 門檻的「精英」分點名單裡，
同一券商（house）常常貢獻多個分點（例如兆豐、土銀、玉山都出現一長串分點）。
本研究檢驗：這是否代表訊號其實主要來自「該券商自營／集團資金流」透過旗下
多個分點同時進出，而非真正獨立的分點層級行為——若是，house 層級聚合訊號
應該比分點層級更乾淨（lift 更高／p 值更低／通過名單更精簡），尤其對雜訊
較多的大漲方向。

方法（沿用大跌／大漲版本凍結協議，僅將聚合單位從「分點」換成「券商」）：
  1. house 對照表：解析 297 分點 cohort 的 `branch_name` 中文前綴（最長前綴
     優先，避免如「第一」誤配到「第一金」分點），對照已知券商清單；無法
     判斷者歸「其他/未分類」並單獨列出供人工檢查。
  2. 分點層級 hit-branch 密度：每個 house，其 cohort 分點數，以及其中通過
     hit_count≥3 & p<0.10 門檻（沿用既有 branch_scores 結果）的分點數，
     density = 通過分點數 / cohort 分點數；與全樣本基期密度比較。
  3. house 層級 precursor score：用 grid cache 的 net_amt（原始每日淨額，
     非已算好的分點 lb_pctile），按 (house, trade_date) 加總得到 house-day
     net_amt 序列，重跑與分點版本完全相同的方法——前 N 日累計、對該 house
     自身 2 年歷史取自我常態化百分位、對大跌／大漲事件命中率做單尾二項
     檢定——分別對跌方向與漲方向。
  4. 「集中 vs 分散」旗標：cohort 分點數 ≥ --concentrated-min-branches 但
     通過門檻分點數 ≤ --concentrated-max-sig 的 house，視為「集中」證據
     （偏向真正分點層級行為，非 house 全面性資金流）；反之 density 遠高於
     基期者視為「分散」證據（偏向 house 層級資金流）。

與分點版本共用（direction-agnostic）的建構函式直接從
`run_market_crash_branch_precursor_scan.py` / `run_market_rally_branch_precursor_scan.py`
import，避免重複維護：compute_lookback_percentiles / score_branches /
score_branches_rally。

輸入（`reports/research/branch-footprint-screen/`，皆為既有 artifacts，本
腳本不重跑分點層級 SQL 掃描）：
  _market_crash_precursor_grid_cache.parquet（取 net_amt 原始欄位）
  market_crash_precursor_episodes.csv / market_rally_precursor_episodes.csv
  market_crash_precursor_branch_scores.csv / market_rally_precursor_branch_scores.csv

輸出：
  house_precursor_branch_house_map.csv
  house_precursor_house_scores_crash.csv
  house_precursor_house_scores_rally.csv
  house_precursor_summary.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_house_aggregation.py
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_RESEARCH_DIR = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _RESEARCH_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_crash = _load_module(
    "run_market_crash_branch_precursor_scan", "run_market_crash_branch_precursor_scan.py"
)
_rally = _load_module(
    "run_market_rally_branch_precursor_scan", "run_market_rally_branch_precursor_scan.py"
)

compute_lookback_percentiles = _crash.compute_lookback_percentiles
score_branches_crash = _crash.score_branches
score_branches_rally = _rally.score_branches_rally

OUT = ROOT / "reports/research/branch-footprint-screen"

# 已知券商 house 名稱（人工整理，非窮舉）。最長前綴優先比對，避免例如
# 「第一」誤配到「第一金」分點（cohort 內兩者為不同法人，需分開）。
KNOWN_HOUSES = [
    "元大", "富邦", "凱基", "統一", "兆豐國際", "兆豐", "群益金鼎證券", "群益金鼎",
    "群益", "永豐金", "玉山", "國泰", "中國信託", "第一金", "第一", "土銀", "合庫",
    "臺銀", "台灣企銀", "華南", "康和", "亞東", "宏遠", "新光", "聯邦", "國票",
    "摩根大通", "摩根士丹利", "花旗環球", "美商高盛亞", "港商野村", "瑞銀",
    "日盛", "大昌", "犇亞", "台新", "華泰", "元富", "美好",
    # 資料觀察中出現、非用戶清單但可明確辨識的完整券商／銀行全名。
    "台中銀", "彰銀", "大和國泰", "港麥格理", "陽信",
]
UNCLASSIFIED = "其他/未分類"


def match_house(branch_name: str, houses_sorted: list[str]) -> str:
    for h in houses_sorted:
        if branch_name.startswith(h):
            return h
    return UNCLASSIFIED


def build_house_map(cohort_names: pd.DataFrame) -> pd.DataFrame:
    houses_sorted = sorted(KNOWN_HOUSES, key=len, reverse=True)
    df = cohort_names.copy()
    df["house_name"] = df["branch_name"].apply(lambda n: match_house(n, houses_sorted))
    return df[["branch_id", "branch_name", "house_name"]]


def house_day_panel(grid_cache_path: Path, branch_house: dict[str, str]) -> pd.DataFrame:
    """(house, trade_date) 加總 net_amt；沿用 grid cache 已補 0 的完整日曆。"""
    grid = pd.read_parquet(grid_cache_path, columns=["securities_trader_id", "trade_date", "net_amt"])
    grid["securities_trader_id"] = grid["securities_trader_id"].astype(str)
    grid["house_name"] = grid["securities_trader_id"].map(branch_house)
    panel = (
        grid.groupby(["house_name", "trade_date"], as_index=False)["net_amt"]
        .sum()
        .rename(columns={"house_name": "securities_trader_id"})
    )
    return panel


def house_hit_branch_density(
    branch_scores: pd.DataFrame,
    house_map: pd.DataFrame,
    *,
    sig_hit_count: int,
    sig_p_value: float,
) -> pd.DataFrame:
    sig_ids = set(
        branch_scores.loc[
            (branch_scores["hit_count"] >= sig_hit_count)
            & (branch_scores["p_value_one_sided"] < sig_p_value),
            "branch_id",
        ]
    )
    m = house_map.copy()
    m["is_significant"] = m["branch_id"].isin(sig_ids)
    grp = (
        m.groupby("house_name", as_index=False)
        .agg(n_branches_in_cohort=("branch_id", "count"), n_significant_branches=("is_significant", "sum"))
    )
    grp["n_significant_branches"] = grp["n_significant_branches"].astype(int)
    grp["hit_branch_density_pct"] = round(
        100.0 * grp["n_significant_branches"] / grp["n_branches_in_cohort"], 1
    )
    return grp.sort_values(
        ["hit_branch_density_pct", "n_branches_in_cohort"], ascending=[False, False]
    ).reset_index(drop=True)


def concentrated_vs_distributed(
    density: pd.DataFrame,
    *,
    baseline_density_pct: float,
    concentrated_min_branches: int,
    concentrated_max_sig: int,
    distributed_density_multiple: float,
) -> pd.DataFrame:
    d = density.copy()
    d["concentrated_flag"] = (
        (d["n_branches_in_cohort"] >= concentrated_min_branches)
        & (d["n_significant_branches"] >= 1)
        & (d["n_significant_branches"] <= concentrated_max_sig)
    )
    d["distributed_flag"] = (d["n_branches_in_cohort"] >= concentrated_min_branches) & (
        d["hit_branch_density_pct"] >= baseline_density_pct * distributed_density_multiple
    )
    return d


def combine_house_scores(
    density: pd.DataFrame, flags: pd.DataFrame, house_level: pd.DataFrame
) -> pd.DataFrame:
    out = flags.merge(
        house_level.drop(columns=["branch_name"]).rename(
            columns={
                "branch_id": "house_name",
                "n_episodes_scored": "house_level_n_episodes_scored",
                "n_episodes_total": "house_level_n_episodes_total",
                "hit_count": "house_level_hit_count",
                "hit_rate_pct": "house_level_hit_rate_pct",
                "base_rate_pct": "house_level_base_rate_pct",
                "lift": "house_level_lift",
                "p_value_one_sided": "house_level_p_value",
                "avg_hit_window_net_ntd": "house_level_avg_hit_window_net_ntd",
            }
        ),
        on="house_name",
        how="left",
    )
    return out.sort_values(
        ["house_level_hit_count", "house_level_p_value"], ascending=[False, True]
    ).reset_index(drop=True)


def write_summary_md(
    path: Path,
    *,
    house_map: pd.DataFrame,
    density_crash: pd.DataFrame,
    density_rally: pd.DataFrame,
    combined_crash: pd.DataFrame,
    combined_rally: pd.DataFrame,
    baseline_crash_pct: float,
    baseline_rally_pct: float,
    protocol: dict,
) -> None:
    unclassified = house_map[house_map["house_name"] == UNCLASSIFIED]

    def _density_table(d: pd.DataFrame) -> list[str]:
        lines = [
            "| 券商 | cohort 分點數 | 通過門檻分點數 | 密度% |",
            "|------|-------------:|---------------:|------:|",
        ]
        for r in d.itertuples():
            lines.append(
                f"| {r.house_name} | {r.n_branches_in_cohort} | "
                f"{r.n_significant_branches} | {r.hit_branch_density_pct:.1f} |"
            )
        return lines

    def _top10_table(d: pd.DataFrame) -> list[str]:
        lines = [
            "| rank | 券商 | cohort分點數 | house-hit/總事件 | 命中率% | lift | p值 | 命中窗均額(NT$) |",
            "|-----:|------|------------:|------------------:|--------:|-----:|----:|---------------:|",
        ]
        top = d[d["house_level_hit_count"].notna()].head(10)
        for i, r in enumerate(top.itertuples(), 1):
            amt = (
                "NA"
                if pd.isna(r.house_level_avg_hit_window_net_ntd)
                else f"{r.house_level_avg_hit_window_net_ntd:,.0f}"
            )
            lines.append(
                f"| {i} | {r.house_name} | {r.n_branches_in_cohort} | "
                f"{int(r.house_level_hit_count)}/{int(r.house_level_n_episodes_scored)} | "
                f"{r.house_level_hit_rate_pct:.1f} | {r.house_level_lift:.2f} | "
                f"{r.house_level_p_value:.4f} | {amt} |"
            )
        return lines

    def _flag_table(d: pd.DataFrame, col: str) -> list[str]:
        lines = [
            "| 券商 | cohort分點數 | 通過門檻分點數 | 密度% |",
            "|------|------------:|---------------:|------:|",
        ]
        sub = d[d[col]].sort_values("n_branches_in_cohort", ascending=False)
        for r in sub.itertuples():
            lines.append(
                f"| {r.house_name} | {r.n_branches_in_cohort} | "
                f"{r.n_significant_branches} | {r.hit_branch_density_pct:.1f} |"
            )
        if len(sub) == 0:
            lines.append("| （無） | | | |")
        return lines

    n_sig_house_crash = int((combined_crash["house_level_hit_count"] >= protocol["sig_hit_count"]).sum())
    n_sig_house_rally = int((combined_rally["house_level_hit_count"] >= protocol["sig_hit_count"]).sum())

    lines = [
        "# 大盤大跌／大漲 precursor · 券商 house 層級聚合研究",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "延伸自 `market_crash_precursor_summary.md` / `market_rally_precursor_summary.md`，"
        "檢驗分點層級「精英」名單是否其實是同一券商多分點共現的 house 層級資金流。",
        "",
        "## 方法摘要",
        "",
        f"- house 對照：297 分點 cohort 中文名前綴最長前綴優先比對已知券商清單，"
        f"共 {house_map['house_name'].nunique()} 個 house（含「{UNCLASSIFIED}」）。",
        f"- 分點層級顯著門檻（沿用既有）：hit_count ≥ {protocol['sig_hit_count']} 且 "
        f"p < {protocol['sig_p_value']}。",
        f"- house 層級訊號：house 當日對全市場 net_amt 加總（跨其 cohort 分點）→ "
        f"前 {protocol['lookback_days']} 個交易日累計 → 對該 house 自身歷史分布取百分位 → "
        f"與大跌／大漲事件命中率做單尾二項檢定（基期 {protocol['tail_pct']*100:.0f}%）。",
        f"- 集中 vs 分散旗標：cohort 分點數 ≥ {protocol['concentrated_min_branches']} 者才納入判斷；"
        f"「集中」= 通過門檻分點數在 1～{protocol['concentrated_max_sig']} 之間（偏向真正分點行為）；"
        f"「分散」= 密度 ≥ 基期密度 × {protocol['distributed_density_multiple']}（偏向 house 全面性資金流）。",
        "",
        "## house 對照表：未能分類（人工檢查用）",
        "",
    ]
    if unclassified.empty:
        lines.append("（全部分點皆成功歸類，無未分類項目）")
    else:
        lines += ["| 分點ID | 分點名稱 |", "|--------|----------|"]
        for r in unclassified.itertuples():
            lines.append(f"| `{r.branch_id}` | {r.branch_name} |")
    lines.append("")

    lines += [
        "## 分點層級 hit-branch 密度（大跌方向，依密度排序）",
        "",
        f"全樣本基期密度：{baseline_crash_pct:.1f}%（{protocol['n_sig_branch_crash']}/"
        f"{protocol['n_cohort']} 分點通過門檻）。",
        "",
    ]
    lines += _density_table(density_crash)

    lines += [
        "",
        "## 分點層級 hit-branch 密度（大漲方向，依密度排序）",
        "",
        f"全樣本基期密度：{baseline_rally_pct:.1f}%（{protocol['n_sig_branch_rally']}/"
        f"{protocol['n_cohort']} 分點通過門檻）。",
        "",
    ]
    lines += _density_table(density_rally)

    lines += [
        "",
        f"## house 層級 precursor score · 大跌方向 Top10（通過門檻 house 數：{n_sig_house_crash}）",
        "",
    ]
    lines += _top10_table(combined_crash)

    lines += [
        "",
        f"## house 層級 precursor score · 大漲方向 Top10（通過門檻 house 數：{n_sig_house_rally}）",
        "",
    ]
    lines += _top10_table(combined_rally)

    lines += [
        "",
        "## 集中 vs 分散旗標 — 大跌方向",
        "",
        "### 「集中」（cohort 分點多，僅 1～" + str(protocol["concentrated_max_sig"])
        + " 個通過門檻 → 偏向真正分點層級行為，非 house 全面性資金流）",
        "",
    ]
    lines += _flag_table(combined_crash, "concentrated_flag")
    lines += [
        "",
        "### 「分散」（密度遠高於基期 → 偏向 house 全面性資金流，而非個別分點特有行為）",
        "",
    ]
    lines += _flag_table(combined_crash, "distributed_flag")

    lines += [
        "",
        "## 集中 vs 分散旗標 — 大漲方向",
        "",
        "### 「集中」",
        "",
    ]
    lines += _flag_table(combined_rally, "concentrated_flag")
    lines += [
        "",
        "### 「分散」",
        "",
    ]
    lines += _flag_table(combined_rally, "distributed_flag")

    top_crash_lift = combined_crash["house_level_lift"].max()
    top_rally_lift = combined_rally["house_level_lift"].max()
    branch_top_crash_lift = protocol["branch_top_lift_crash"]
    branch_top_rally_lift = protocol["branch_top_lift_rally"]

    lines += [
        "",
        "## 驗證結論",
        "",
        f"- 大跌方向：house 層級最高 lift={top_crash_lift:.2f}"
        f"（分點層級最高 lift={branch_top_crash_lift:.2f}），"
        f"house 層級通過門檻家數={n_sig_house_crash}（分點層級={protocol['n_sig_branch_crash']}/{protocol['n_cohort']}）。",
        f"- 大漲方向：house 層級最高 lift={top_rally_lift:.2f}"
        f"（分點層級最高 lift={branch_top_rally_lift:.2f}），"
        f"house 層級通過門檻家數={n_sig_house_rally}（分點層級={protocol['n_sig_branch_rally']}/{protocol['n_cohort']}）。",
        "",
        "**verdict**："
        + protocol["verdict_text"],
        "",
        "## 限制與注意事項",
        "",
        "- house 對照表為人工整理的前綴比對，非官方券商代碼對照；「其他/未分類」"
        "項目請人工核對（見上表），避免誤將分點名當券商名。",
        "- house 層級 net_amt 為 cohort 內該 house 分點加總，非其全公司（含非"
        "cohort 分點、自營、法人交易room）完整曝險；`stock_broker_branch_daily` "
        "亦為 FinMind 分點進出，非真實庫存變化。",
        "- 事件數仍少（大跌 11 個、大漲 9 個），house 層級雖降低分點層級的個體"
        "雜訊，但無法解決樣本事件數不足的統計檢定力限制，本研究仍為探索性。",
        "- 「集中」與「分散」旗標門檻（cohort 分點數、密度倍數）為探索性設定，"
        "非正式 graduation gate。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Market precursor house-level aggregation")
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--sig-hit-count", type=int, default=3)
    ap.add_argument("--sig-p-value", type=float, default=0.10)
    ap.add_argument("--concentrated-min-branches", type=int, default=5)
    ap.add_argument("--concentrated-max-sig", type=int, default=2)
    ap.add_argument("--distributed-density-multiple", type=float, default=2.0)
    ap.add_argument("--out-suffix", default="")
    args = ap.parse_args()
    suf = args.out_suffix

    OUT.mkdir(parents=True, exist_ok=True)

    print("loading branch cohort + scores (crash/rally) …", flush=True)
    branch_scores_crash = pd.read_csv(OUT / "market_crash_precursor_branch_scores.csv", dtype={"branch_id": str})
    branch_scores_rally = pd.read_csv(OUT / "market_rally_precursor_branch_scores.csv", dtype={"branch_id": str})
    cohort_names = branch_scores_crash[["branch_id", "branch_name"]].drop_duplicates()
    print(f"cohort n={len(cohort_names)}", flush=True)

    print("building branch -> house map (longest-prefix match) …", flush=True)
    house_map = build_house_map(cohort_names)
    house_map.to_csv(OUT / f"house_precursor_branch_house_map{suf}.csv", index=False)
    n_unclassified = int((house_map["house_name"] == UNCLASSIFIED).sum())
    print(
        f"houses n={house_map['house_name'].nunique()} unclassified={n_unclassified}",
        flush=True,
    )
    branch_house = dict(zip(house_map["branch_id"], house_map["house_name"]))

    print("computing branch-level hit-branch density per house …", flush=True)
    density_crash = house_hit_branch_density(
        branch_scores_crash, house_map, sig_hit_count=args.sig_hit_count, sig_p_value=args.sig_p_value
    )
    density_rally = house_hit_branch_density(
        branch_scores_rally, house_map, sig_hit_count=args.sig_hit_count, sig_p_value=args.sig_p_value
    )
    n_sig_branch_crash = int(
        ((branch_scores_crash["hit_count"] >= args.sig_hit_count) & (branch_scores_crash["p_value_one_sided"] < args.sig_p_value)).sum()
    )
    n_sig_branch_rally = int(
        ((branch_scores_rally["hit_count"] >= args.sig_hit_count) & (branch_scores_rally["p_value_one_sided"] < args.sig_p_value)).sum()
    )
    n_cohort = len(cohort_names)
    baseline_crash_pct = 100.0 * n_sig_branch_crash / n_cohort
    baseline_rally_pct = 100.0 * n_sig_branch_rally / n_cohort
    print(
        f"baseline density crash={baseline_crash_pct:.1f}% ({n_sig_branch_crash}/{n_cohort}) "
        f"rally={baseline_rally_pct:.1f}% ({n_sig_branch_rally}/{n_cohort})",
        flush=True,
    )

    print("building house-day net_amt panel from grid cache …", flush=True)
    t0 = time.time()
    panel = house_day_panel(OUT / "_market_crash_precursor_grid_cache.parquet", branch_house)
    print(f"house-day panel rows={len(panel):,} ({time.time()-t0:.1f}s)", flush=True)

    print("computing house-level lookback percentiles …", flush=True)
    scored = compute_lookback_percentiles(panel, args.lookback_days)

    print("scoring houses vs crash episodes …", flush=True)
    episodes_crash = pd.read_csv(OUT / "market_crash_precursor_episodes.csv", dtype={"trade_date": str})
    houses = house_map["house_name"].unique().tolist()
    identity_map = {h: h for h in houses}
    house_level_crash = score_branches_crash(scored, episodes_crash, identity_map, tail_pct=args.tail_pct)

    print("scoring houses vs rally episodes …", flush=True)
    episodes_rally = pd.read_csv(OUT / "market_rally_precursor_episodes.csv", dtype={"trade_date": str})
    house_level_rally = score_branches_rally(scored, episodes_rally, identity_map, tail_pct=args.tail_pct)

    flags_crash = concentrated_vs_distributed(
        density_crash,
        baseline_density_pct=baseline_crash_pct,
        concentrated_min_branches=args.concentrated_min_branches,
        concentrated_max_sig=args.concentrated_max_sig,
        distributed_density_multiple=args.distributed_density_multiple,
    )
    flags_rally = concentrated_vs_distributed(
        density_rally,
        baseline_density_pct=baseline_rally_pct,
        concentrated_min_branches=args.concentrated_min_branches,
        concentrated_max_sig=args.concentrated_max_sig,
        distributed_density_multiple=args.distributed_density_multiple,
    )

    combined_crash = combine_house_scores(density_crash, flags_crash, house_level_crash)
    combined_rally = combine_house_scores(density_rally, flags_rally, house_level_rally)
    combined_crash.to_csv(OUT / f"house_precursor_house_scores_crash{suf}.csv", index=False)
    combined_rally.to_csv(OUT / f"house_precursor_house_scores_rally{suf}.csv", index=False)
    print(
        f"wrote house_scores_crash rows={len(combined_crash)} "
        f"house_scores_rally rows={len(combined_rally)}",
        flush=True,
    )

    n_branch_multi5 = int((density_crash["n_branches_in_cohort"] >= args.concentrated_min_branches).sum())
    branch_top_lift_crash = float(branch_scores_crash["lift"].max())
    branch_top_lift_rally = float(branch_scores_rally["lift"].max())
    house_top_lift_crash = float(combined_crash["house_level_lift"].max())
    house_top_lift_rally = float(combined_rally["house_level_lift"].max())
    n_sig_house_crash = int((combined_crash["house_level_hit_count"] >= args.sig_hit_count).sum())
    n_sig_house_rally = int((combined_rally["house_level_hit_count"] >= args.sig_hit_count).sum())

    rally_cleaner = (house_top_lift_rally >= branch_top_lift_rally) and (
        n_sig_house_rally / max(1, len(combined_rally)) < n_sig_branch_rally / n_cohort
    )
    verdict_text = (
        (
            "house 層級聚合對大漲方向的雜訊清理效果明確：通過門檻的 house 家數"
            f"（{n_sig_house_rally}/{len(combined_rally)}）遠低於分點層級通過比例"
            f"（{n_sig_branch_rally}/{n_cohort}），且最高 lift 由 {branch_top_lift_rally:.2f}"
            f"（分點）提升至 {house_top_lift_rally:.2f}（house），支持將 house 層級訊號"
            "納入最終系統設計、與分點層級並列或取代分點層級作為大漲方向的主訊號。"
            if rally_cleaner
            else "house 層級聚合未明顯清理大漲方向雜訊——通過門檻 house 比例與"
            "分點層級通過比例相近，house 層級加總可能稀釋了個別分點的異常訊號"
            "（不同分點的買超時間點不同步時，加總會互相抵銷），此發現本身也是"
            "有用資訊：提示大漲方向的雜訊主因不是「house 層級資金流」，設計上"
            "應優先在分點層級做更嚴格的篩選（如提高 hit_count 門檻或延長歷史窗），"
            "而非直接改用 house 層級聚合。"
        )
        + " 大跌方向本身分點層級已經是緊、篩選性名單（"
        f"{n_sig_branch_crash}/{n_cohort}），house 層級聚合後通過家數為"
        f"{n_sig_house_crash}/{len(combined_crash)}，"
        + (
            "同樣維持緊選（支持大跌方向兩層級一致，訊號穩健）。"
            if n_sig_house_crash / max(1, len(combined_crash)) <= n_sig_branch_crash / n_cohort * 2
            else "通過比例明顯放大，需留意是否為 house 層級加總引入的偽訊號（樣本 house 數少於分點數，"
            "多重比較下更容易在少量事件內偶然達標）。"
        )
    )

    protocol = {
        "lookback_days": args.lookback_days,
        "tail_pct": args.tail_pct,
        "sig_hit_count": args.sig_hit_count,
        "sig_p_value": args.sig_p_value,
        "concentrated_min_branches": args.concentrated_min_branches,
        "concentrated_max_sig": args.concentrated_max_sig,
        "distributed_density_multiple": args.distributed_density_multiple,
        "n_cohort": n_cohort,
        "n_sig_branch_crash": n_sig_branch_crash,
        "n_sig_branch_rally": n_sig_branch_rally,
        "n_branch_multi5": n_branch_multi5,
        "branch_top_lift_crash": branch_top_lift_crash,
        "branch_top_lift_rally": branch_top_lift_rally,
        "verdict_text": verdict_text,
    }

    summary_path = OUT / f"house_precursor_summary{suf}.md"
    write_summary_md(
        summary_path,
        house_map=house_map,
        density_crash=density_crash,
        density_rally=density_rally,
        combined_crash=combined_crash,
        combined_rally=combined_rally,
        baseline_crash_pct=baseline_crash_pct,
        baseline_rally_pct=baseline_rally_pct,
        protocol=protocol,
    )
    print(f"wrote {summary_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
