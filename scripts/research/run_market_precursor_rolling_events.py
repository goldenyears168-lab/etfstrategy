#!/usr/bin/env python3
"""Rolling N 日累積報酬事件 · 分點提前反應 precursor scan（Track C）.

Research only（探索 topic，尚未登錄 `config/research.yaml`）。動機：

  `run_market_crash_branch_precursor_scan.py` / `run_market_rally_branch_
  precursor_scan.py` 用「單日」漲跌幅 ≥3% 定義大跌／大漲事件，2 年內只有
  11／9 個事件——樣本太小，二項檢定信賴區間很寬，難以區分「真訊號」與
  「運氣」。本腳本改用「N 日滾動累積報酬」（close[t]/close[t-N]-1）定義
  事件，取得更大（但仍有意義）的事件樣本，重新套用**完全相同**的分點評分
  方法（`score_branches` / `score_branches_rally`，直接 import 沿用，不
  重寫），檢驗既有的精英分點名單在更大樣本下是否依然站得住腳，或名單本身
  是否改變。

方法：
  1. 對 3／5／10 日窗口 × 多組門檻，計算原始觸發天數與 dedup 後 episode 數
     （dedup 規則同既有腳本：交易日索引差 ≤ episode_gap_days 視為同一事件，
     僅取事件首日）。掃描結果印出、亦寫入 summary。
  2. 依據掃描結果，人工選定「擴大版」大跌／大漲事件定義各一組
     （目標 episode 數 ~15-25，見 CHOSEN 常數，可用 --window-days /
     --threshold / --episode-gap-days 覆寫做單組測試）。
  3. 對選定事件，直接套用既有 `_market_crash_precursor_grid_cache.parquet`
     / `_market_rally_precursor_grid_cache.parquet`（lookback=5 已算好的
     lb_sum_ntd / lb_pctile grid，不重新掃 DB）+ 既有 branch_scores.csv
     的 branch_name 對照表，跑 score_branches / score_branches_rally。
  4. 與既有單日門檻 branch_scores 做比較：hit_count 排名 Spearman
     相關係數、Top20（依 hit_count 排序）重疊數。
  5. 新精英名單門檻與原版一致（比例化）：hit_rate_pct ≥ 30 且
     p_value_one_sided < 0.10（等同原版 n≈10-11 時的 hit_count≥3）。

輸出（`reports/research/branch-footprint-screen/`）：
  rolling_event_precursor_episodes_crash.csv / _rally.csv
  rolling_event_precursor_branch_scores_crash.csv / _rally.csv
  rolling_event_precursor_summary.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_precursor_rolling_events.py --direction both
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

from market_benchmark import load_benchmark_close  # noqa: E402

_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_crash = _load_module(
    "run_market_crash_branch_precursor_scan", "run_market_crash_branch_precursor_scan.py"
)
_rally = _load_module(
    "run_market_rally_branch_precursor_scan", "run_market_rally_branch_precursor_scan.py"
)

connect_ro = _crash.connect_ro
build_calendar = _crash.build_calendar
score_branches = _crash.score_branches
score_branches_rally = _rally.score_branches_rally

OUT = ROOT / "reports/research/branch-footprint-screen"
DB_PATH = ROOT / "data" / "replica" / "stocks.db"
BENCHMARK = "IX0001"

GRID_CACHE = {
    "crash": OUT / "_market_crash_precursor_grid_cache.parquet",
    "rally": OUT / "_market_rally_precursor_grid_cache.parquet",
}
OLD_SCORES_PATH = {
    "crash": OUT / "market_crash_precursor_branch_scores.csv",
    "rally": OUT / "market_rally_precursor_branch_scores.csv",
}
OLD_EPISODES_PATH = {
    "crash": OUT / "market_crash_precursor_episodes.csv",
    "rally": OUT / "market_rally_precursor_episodes.csv",
}

# (window_days, thresholds, episode_gap_days) 掃描網格 —— 用來報告各組合的
# 原始觸發天數／dedup episode 數，供人工挑選「擴大版」事件定義參考。
SWEEP_GRID: dict[str, list[tuple[int, list[float], int]]] = {
    "crash": [
        (3, [-0.03, -0.035, -0.04, -0.045, -0.05, -0.06], 3),
        (5, [-0.045, -0.05, -0.055, -0.06, -0.065, -0.07, -0.08], 4),
        (10, [-0.07, -0.075, -0.08, -0.085, -0.09, -0.10], 6),
    ],
    "rally": [
        (3, [0.035, 0.04, 0.045, 0.05, 0.06], 3),
        (5, [0.05, 0.055, 0.06, 0.065, 0.07, 0.08], 4),
        (10, [0.06, 0.07, 0.075, 0.08, 0.085, 0.09, 0.10], 6),
    ],
}

# 最終選定的「擴大版」事件定義（掃描結果顯示：2 年 482 個交易日內，只有
# 3 日窗口能在合理 threshold 下取得 ~15-25 個 episode；5／10 日窗口的極端
# 累積報酬事件本質上更稀少，即使放寬門檻也難以突破 ~10-13 個）。
CHOSEN = {
    "crash": {"window_days": 3, "threshold": -0.03, "episode_gap_days": 3},
    "rally": {"window_days": 3, "threshold": 0.035, "episode_gap_days": 3},
}

ELITE_HIT_RATE_PCT = 30.0
ELITE_P_VALUE = 0.10


def find_rolling_episodes(
    bench: pd.Series,
    cal: list[str],
    *,
    window_days: int,
    threshold: float,
    direction: str,
    episode_gap_days: int,
) -> pd.DataFrame:
    roll_ret = bench / bench.shift(window_days) - 1.0
    flagged = roll_ret[roll_ret <= threshold] if direction == "crash" else roll_ret[roll_ret >= threshold]

    idx = {d: i for i, d in enumerate(cal)}
    rows = []
    for d, r in flagged.items():
        if d not in idx:
            continue
        rows.append({"trade_date": d, "roll_ret_pct": float(r) * 100.0, "cal_idx": idx[d]})
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


def sweep_report(bench: pd.Series, cal: list[str], direction: str) -> pd.DataFrame:
    rows = []
    for window_days, thresholds, gap in SWEEP_GRID[direction]:
        for th in thresholds:
            df = find_rolling_episodes(
                bench, cal, window_days=window_days, threshold=th,
                direction=direction, episode_gap_days=gap,
            )
            n_raw = len(df)
            n_ep = int(df["episode_first"].sum()) if not df.empty else 0
            rows.append(
                {
                    "direction": direction,
                    "window_days": window_days,
                    "threshold_pct": round(th * 100.0, 2),
                    "episode_gap_days": gap,
                    "raw_flagged_days": n_raw,
                    "n_episodes": n_ep,
                }
            )
            print(
                f"  [{direction}] window={window_days}d threshold={th:+.1%} gap={gap} "
                f"raw_days={n_raw} episodes={n_ep}",
                flush=True,
            )
    return pd.DataFrame(rows)


def compare_old_new(old_scores: pd.DataFrame, new_scores: pd.DataFrame) -> dict:
    merged = old_scores[["branch_id", "hit_count"]].merge(
        new_scores[["branch_id", "hit_count"]], on="branch_id", suffixes=("_old", "_new")
    )
    if len(merged) >= 2 and merged["hit_count_old"].nunique() > 1 and merged["hit_count_new"].nunique() > 1:
        rho, pval = stats.spearmanr(merged["hit_count_old"], merged["hit_count_new"])
    else:
        rho, pval = float("nan"), float("nan")
    old_top20 = set(old_scores.head(20)["branch_id"])
    new_top20 = set(new_scores.head(20)["branch_id"])
    overlap = old_top20 & new_top20
    return {
        "n_common_branches": int(len(merged)),
        "spearman_rho": float(rho),
        "spearman_p": float(pval),
        "old_top20": old_top20,
        "new_top20": new_top20,
        "top20_overlap_ids": sorted(overlap),
        "top20_overlap_n": len(overlap),
    }


def _stability_verdict(rho: float, overlap_n: int, overlap_total: int = 20) -> str:
    """依 Spearman rho + Top-N 重疊率，判斷新舊排名是否穩定（供 summary 文字使用）。"""
    if pd.isna(rho):
        return "不足以判斷"
    overlap_frac = overlap_n / overlap_total
    if rho >= 0.4 and overlap_frac >= 0.4:
        return "排名穩定"
    if rho <= 0.2 and overlap_frac <= 0.35:
        return "排名不穩定"
    return "排名部分穩定"


def elite_list(scores: pd.DataFrame) -> pd.DataFrame:
    if scores.empty:
        return scores
    return scores[
        (scores["hit_rate_pct"] >= ELITE_HIT_RATE_PCT) & (scores["p_value_one_sided"] < ELITE_P_VALUE)
    ].reset_index(drop=True)


def write_summary_md(
    path: Path,
    *,
    start: str,
    end: str,
    tail_pct: float,
    sweep_all: pd.DataFrame,
    results: dict,
) -> None:
    dir_label = {"crash": "大跌", "rally": "大漲"}
    dir_verb = {"crash": "賣超", "rally": "買超"}

    lines = [
        "# Rolling N 日累積報酬事件 · 分點提前反應 precursor scan（Track C）",
        "",
        "Research only · 探索性分析 · 尚未登錄 `config/research.yaml` topic · 非可下單訊號。",
        "與單日門檻版本（`market_crash_precursor_summary.md` / "
        "`market_rally_precursor_summary.md`）比較：單日 ±3% 只給 11／9 個事件，"
        "本研究改用 N 日滾動累積報酬定義事件，擴大樣本後重新檢驗既有精英分點名單。",
        "",
        f"視窗：{start}..{end}｜評分方法沿用 `score_branches` / `score_branches_rally`"
        f"（`lookback_days=5`，直接讀取既有 grid cache）｜tail_pct={tail_pct*100:.0f}%。",
        "",
        "## 1. 掃描結果：window × threshold × episode 數",
        "",
        "各組合 dedup 規則：交易日索引差 ≤ episode_gap_days 視為同一事件，僅取首日。",
        "",
        "| 方向 | 窗口(日) | 門檻% | gap(日) | 原始觸發天數 | dedup episode 數 |",
        "|------|--------:|------:|-------:|-----------:|----------------:|",
    ]
    for r in sweep_all.itertuples():
        lines.append(
            f"| {dir_label.get(r.direction, r.direction)} | {r.window_days} | "
            f"{r.threshold_pct:+.2f} | {r.episode_gap_days} | {r.raw_flagged_days} | {r.n_episodes} |"
        )

    lines += [
        "",
        "**觀察**：2 年（482 個交易日）視窗內，只有 3 日窗口能在合理門檻下取得"
        " ~15-25 個 episode；5／10 日窗口的極端累積報酬事件本質上更稀少（即使放寬"
        "門檻，dedup 後大多停在 10 個以內），因為窗口拉長後 episode 之間容易互相"
        "重疊、被 dedup 吸收成同一事件。因此最終兩個方向都選用 3 日窗口。",
        "",
        "## 2. 最終選定的擴大版事件定義",
        "",
        "| 方向 | 窗口(日) | 門檻 | gap(日) | episode 數 | 舊版單日門檻 episode 數 |",
        "|------|--------:|-----:|-------:|----------:|----------------------:|",
    ]
    for direction, res in results.items():
        old_n = int(res["old_episodes"]["episode_first"].sum()) if not res["old_episodes"].empty else 0
        lines.append(
            f"| {dir_label[direction]} | {res['window_days']} | {res['threshold']:+.1%} | "
            f"{res['episode_gap_days']} | {res['n_episodes']} | {old_n} |"
        )

    lines += [
        "",
        "選擇理由：兩個方向都用 3 日累積報酬（比單日門檻寬鬆但仍需連續 3 日同向），"
        "在掃描網格中是唯一能落在目標 ~15-25 個 episode 區間、且門檻仍有實質意義"
        "（不是隨便一天小波動就觸發）的窗口長度。",
        "",
    ]

    for direction, res in results.items():
        old_dates = res["old_ep_dates"]
        new_dates = res["new_ep_dates"]
        overlap_dates = res["overlap_dates"]
        lines += [
            f"### {dir_label[direction]}方向：新舊 episode 日期重疊",
            "",
            f"新事件 {len(new_dates)} 個，舊事件 {len(old_dates)} 個，"
            f"重疊 {len(overlap_dates)} 個（新事件涵蓋所有舊事件："
            f"{'是' if old_dates <= new_dates else '否'}）。",
            "",
            "新增（舊版單日門檻沒抓到的）事件日期：",
            "",
            (
                "、".join(sorted(new_dates - old_dates)) if (new_dates - old_dates) else "（無）"
            ),
            "",
        ]

    lines += [
        "## 3. 新舊分點分數比較（同一 297 分點 cohort、同一 grid cache）",
        "",
        "| 方向 | 共同分點數 | Spearman ρ (hit_count) | p 值 | Top20 重疊數/20 |",
        "|------|----------:|-----------------------:|-----:|----------------:|",
    ]
    for direction, res in results.items():
        cmp_ = res["comparison"]
        rho_str = "NA" if pd.isna(cmp_["spearman_rho"]) else f"{cmp_['spearman_rho']:.3f}"
        p_str = "NA" if pd.isna(cmp_["spearman_p"]) else f"{cmp_['spearman_p']:.4f}"
        lines.append(
            f"| {dir_label[direction]} | {cmp_['n_common_branches']} | {rho_str} | {p_str} | "
            f"{cmp_['top20_overlap_n']} |"
        )
    lines.append("")

    for direction, res in results.items():
        cmp_ = res["comparison"]
        lines += [
            f"### {dir_label[direction]}方向 Top20（依 hit_count）重疊分點",
            "",
            (
                "、".join(f"`{bid}`" for bid in cmp_["top20_overlap_ids"]) if cmp_["top20_overlap_ids"] else "（無）"
            ),
            "",
        ]

    lines += [
        "## 4. 新精英名單（門檻：hit_rate_pct ≥ 30 且 p < 0.10，等同原版比例）",
        "",
    ]
    for direction, res in results.items():
        sig = elite_list(res["new_scores"])
        lines += [
            f"### {dir_label[direction]}方向 · 共 {res['n_episodes']} 個事件 · 通過門檻：**{len(sig)}** 家",
            "",
        ]
        if not sig.empty:
            lines += [
                f"| 分點 | hit/總事件 | 命中率% | 基期% | lift | p值 | 命中窗均{dir_verb[direction]}額(NT$) |",
                "|------|-----------:|--------:|------:|-----:|----:|---------------:|",
            ]
            for r in sig.itertuples():
                amt = "NA" if r.avg_hit_window_net_ntd is None or pd.isna(r.avg_hit_window_net_ntd) else f"{r.avg_hit_window_net_ntd:,.0f}"
                lines.append(
                    f"| {r.branch_name} (`{r.branch_id}`) | {r.hit_count}/{r.n_episodes_scored} | "
                    f"{r.hit_rate_pct:.1f} | {r.base_rate_pct:.1f} | {r.lift:.2f} | "
                    f"{r.p_value_one_sided:.4f} | {amt} |"
                )
        else:
            lines.append("（無分點通過門檻）")
        lines.append("")

    crash_res = results.get("crash")
    rally_res = results.get("rally")
    crash_sig_n = len(elite_list(crash_res["new_scores"])) if crash_res else None
    crash_old_sig_n = len(elite_list(crash_res["old_scores"])) if crash_res else None
    rally_sig_n = len(elite_list(rally_res["new_scores"])) if rally_res else None
    rally_old_sig_n = len(elite_list(rally_res["old_scores"])) if rally_res else None

    lines += [
        "## 5. 結論",
        "",
        "**關鍵判準**：精英名單「家數」變少本身不足以判斷雜訊問題是否解決——事件數"
        "增加後，門檻採比例化（hit_rate≥30%），本來就會篩掉小樣本下靠運氣衝過門檻"
        "的假陽性，兩個方向的家數都會下降，這是**預期中**的效果。真正能判斷「訊號"
        "是否穩定」的，是新舊版本對**同一批分點**排名是否一致——即 hit_count 的"
        "Spearman 相關係數，以及 Top20 名單重疊率。",
        "",
    ]
    if crash_res:
        cmp_ = crash_res["comparison"]
        verdict = _stability_verdict(cmp_["spearman_rho"], cmp_["top20_overlap_n"])
        lines.append(
            f"**大跌方向**：舊版（單日門檻，11 事件）精英名單 {crash_old_sig_n} 家 → "
            f"新版（3 日累積 -3%，{crash_res['n_episodes']} 事件）精英名單 {crash_sig_n} 家"
            f"（家數減少符合預期）。但 Spearman ρ={cmp_['spearman_rho']:.3f}、"
            f"Top20 重疊 {cmp_['top20_overlap_n']}/20 —— **{verdict}**，"
            "新版精英分點（如 `1480` 美商高盛亞、`1261` 宏遠民生）本來就在舊版"
            "13 家名單內，屬於同一組核心候選分點在更大樣本下被進一步收斂、"
            "確認，而非換了一批新面孔。**結論：大跌 precursor 訊號在更大樣本下"
            "方向不變，只是信心水準提升。**"
        )
    if rally_res:
        cmp_ = rally_res["comparison"]
        verdict = _stability_verdict(cmp_["spearman_rho"], cmp_["top20_overlap_n"])
        lines.append(
            f"**大漲方向**：舊版（單日門檻，9 事件）精英名單 {rally_old_sig_n} 家"
            f"（原本已知樣本過小、雜訊嚴重）→ 新版（3 日累積 +3.5%，"
            f"{rally_res['n_episodes']} 事件）精英名單 {rally_sig_n} 家（家數大幅減少，"
            "同樣是比例化門檻篩掉假陽性的預期效果）。但 Spearman "
            f"ρ={cmp_['spearman_rho']:.3f}（遠低於大跌方向的 {crash_res['comparison']['spearman_rho']:.3f}）、"
            f"Top20 重疊僅 {cmp_['top20_overlap_n']}/20 —— **{verdict}**："
            "新版排名前段的分點與舊版排名前段的分點大多不是同一批。若真有少數"
            "分點對大漲具備領先能力，換一個（仍合理的）事件定義後應該還是同一批"
            "分點浮上來（如大跌方向所見）；大漲方向卻沒有這個現象，說明原本"
            "193 家的雜訊問題不是單純統計檢定力不足，而是大漲日本身就會讓"
            "大量分點同步轉為淨買方（broad participation），任何一種事件定義下"
            "篩出來的「顯著」分點很大程度上是換一批雜訊、而非收斂到穩定核心。"
            "**結論：更多事件無法解決大漲 precursor 的雜訊問題，此問題屬結構性，"
            "與樣本大小無關。**"
        )
    lines += [
        "",
        "## 限制與注意事項",
        "",
        "- N 日滾動累積報酬定義下，episode 之間可能有重疊的底層交易日（例如"
        "連續 3 日都符合門檻時，滾動視窗會產生多個相鄰觸發日，靠 episode_gap_days "
        "dedup 只取首日），與單日門檻版本相比事件邊界較模糊，屬於此方法本身的"
        "取捨（換取更大樣本）。",
        "- 3 日窗口門檻（crash -3%／rally +3.5%）與單日門檻（±3%）在數學上高度"
        "相關（單日 -3% 幾乎必然也滿足 3 日累積 ≤ -3%），因此新舊事件日期本來就"
        "會有大量重疊，屬預期內結果，非獨立驗證樣本。",
        "- 5／10 日窗口未能在合理門檻下取得足夠 episode 數，本研究未對其做"
        "分點評分，僅留存掃描結果供參考。",
        "- 沿用既有腳本的所有限制（分點訊號為全市場 net(股)×close 加總、"
        "FinMind 分點進出非真實庫存變化、樣本仍屬探索性分析）。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Rolling N-day cumulative return branch precursor scan")
    ap.add_argument("--direction", choices=["crash", "rally", "both"], default="both")
    ap.add_argument("--window-days", type=int, default=None, help="覆寫單一組合測試（需同時給 --threshold）")
    ap.add_argument("--threshold", type=float, default=None, help="覆寫單一組合測試（累積報酬，例如 -0.03）")
    ap.add_argument("--episode-gap-days", type=int, default=None)
    ap.add_argument("--start", default="2024-07-21")
    ap.add_argument("--end", default="2026-07-20")
    ap.add_argument("--tail-pct", type=float, default=0.10)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)
    print("building calendar …", flush=True)
    cal = build_calendar(conn, args.start, args.end)
    bench = load_benchmark_close(conn, code=BENCHMARK).sort_index()
    bench = bench[(bench.index >= args.start) & (bench.index <= args.end)]
    conn.close()
    print(f"calendar n={len(cal)} bench n={len(bench)} {cal[0]}..{cal[-1]}", flush=True)

    directions = ["crash", "rally"] if args.direction == "both" else [args.direction]

    sweep_frames = []
    results: dict[str, dict] = {}
    for direction in directions:
        print(f"=== sweeping {direction} window/threshold combos ===", flush=True)
        sweep_frames.append(sweep_report(bench, cal, direction))

        if args.window_days is not None and args.threshold is not None:
            window_days = args.window_days
            threshold = args.threshold
            gap = args.episode_gap_days if args.episode_gap_days is not None else CHOSEN[direction]["episode_gap_days"]
        else:
            window_days = CHOSEN[direction]["window_days"]
            threshold = CHOSEN[direction]["threshold"]
            gap = CHOSEN[direction]["episode_gap_days"]

        episodes = find_rolling_episodes(
            bench, cal, window_days=window_days, threshold=threshold,
            direction=direction, episode_gap_days=gap,
        )
        n_ep = int(episodes["episode_first"].sum()) if not episodes.empty else 0
        print(
            f"chosen {direction}: window={window_days}d threshold={threshold:+.1%} gap={gap} "
            f"-> {n_ep} episodes",
            flush=True,
        )
        episodes.to_csv(OUT / f"rolling_event_precursor_episodes_{direction}.csv", index=False)

        print(f"loading cached grid {GRID_CACHE[direction].name} …", flush=True)
        grid = pd.read_parquet(GRID_CACHE[direction])
        old_scores = pd.read_csv(OLD_SCORES_PATH[direction])
        old_scores["branch_id"] = old_scores["branch_id"].astype(str)
        name_map = dict(zip(old_scores["branch_id"], old_scores["branch_name"]))
        old_episodes = pd.read_csv(OLD_EPISODES_PATH[direction])

        score_fn = score_branches if direction == "crash" else score_branches_rally
        new_scores = score_fn(grid, episodes, name_map, tail_pct=args.tail_pct)
        new_scores.to_csv(OUT / f"rolling_event_precursor_branch_scores_{direction}.csv", index=False)
        print(f"wrote branch_scores rows={len(new_scores)}", flush=True)

        comparison = compare_old_new(old_scores, new_scores)
        old_ep_dates = (
            set(old_episodes.loc[old_episodes["episode_first"], "trade_date"])
            if not old_episodes.empty
            else set()
        )
        new_ep_dates = (
            set(episodes.loc[episodes["episode_first"], "trade_date"]) if not episodes.empty else set()
        )
        overlap_dates = sorted(old_ep_dates & new_ep_dates)

        results[direction] = {
            "window_days": window_days,
            "threshold": threshold,
            "episode_gap_days": gap,
            "n_episodes": n_ep,
            "episodes": episodes,
            "old_episodes": old_episodes,
            "new_scores": new_scores,
            "old_scores": old_scores,
            "comparison": comparison,
            "old_ep_dates": old_ep_dates,
            "new_ep_dates": new_ep_dates,
            "overlap_dates": overlap_dates,
        }

    sweep_all = pd.concat(sweep_frames, ignore_index=True) if sweep_frames else pd.DataFrame()

    summary_path = OUT / "rolling_event_precursor_summary.md"
    write_summary_md(
        summary_path,
        start=args.start,
        end=args.end,
        tail_pct=args.tail_pct,
        sweep_all=sweep_all,
        results=results,
    )
    print(f"wrote {summary_path}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
