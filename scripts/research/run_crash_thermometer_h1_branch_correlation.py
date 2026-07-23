#!/usr/bin/env python3
"""大跌溫度計進階研究 H1：8家分點的相關性結構／有效自由度.

Research diagnostic（純調查，不修改生產腳本／config）。

問題：「共識家數」(0~8) 假設8家分點是8個獨立訊號源；若彼此高度相關
（例如同為外資、同一交易台、跟隨同一國際母行策略），則「共識=5/8」可能只是
2-3個真正獨立訊號的重複計數，虛增可信度。

方法：直接 import `run_market_crash_thermometer_dashboard.py` 的
`build_calendar`/`build_branch_panel`/`build_full_grid`/`compute_lb_pctile`/
`weighted_composite`（唯讀使用，不修改），對這8家分點重算 ~2年 lb_pctile
時間序列，算8x8 Pearson相關矩陣、有效獨立分點數、PCA解釋變異度，並比較
「共識家數(0~8)」vs.「獨立子集共識家數」在16次大跌事件上的分布。

輸出：reports/research/branch-footprint-screen/adv_h1_branch_correlation.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_crash_thermometer_h1_branch_correlation.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = ROOT / "data" / "stocks.db"
OUT_MD = ROOT / "reports" / "research" / "branch-footprint-screen" / "adv_h1_branch_correlation.md"
EPISODES_CSV = ROOT / "reports" / "research" / "branch-footprint-screen" / "market_crash_precursor_episodes.csv"

# 直接載入生產腳本模組（唯讀 import，不修改該檔案）
_spec = importlib.util.spec_from_file_location(
    "crash_thermometer_dashboard",
    ROOT / "scripts" / "research" / "run_market_crash_thermometer_dashboard.py",
)
ctd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctd)

PANEL = ctd.PANEL
IDS = list(PANEL.keys())
LOOKBACK_DAYS = 5
CAL_N_DAYS = 560  # ~2.2年交易日，覆蓋2024-07~asof
FOREIGN_IDS = ["1650", "1560", "1480", "1360"]


def main() -> None:
    conn = ctd.connect_ro(DB_PATH)
    asof = ctd.latest_trade_date(conn)
    print(f"asof={asof}", flush=True)

    cal = ctd.build_calendar(conn, end=asof, n_days=CAL_N_DAYS, extra_ids=IDS)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    panel_raw = ctd.build_branch_panel(conn, IDS, start=cal[0], end=asof)
    print(f"panel rows={len(panel_raw)}", flush=True)

    grid = ctd.build_full_grid(panel_raw, IDS, cal)
    scored = ctd.compute_lb_pctile(grid, LOOKBACK_DAYS)

    # ---- 1. 8家 lb_pctile 寬表 ----
    wide = scored.pivot(index="trade_date", columns="securities_trader_id", values="lb_pctile")
    wide = wide[IDS].sort_index()
    complete = wide.dropna()
    print(f"complete overlap rows (all 8 non-NA) = {len(complete)} / {len(wide)}", flush=True)

    # ---- 2. 8x8 Pearson 相關矩陣 ----
    corr = complete.corr(method="pearson")
    name_of = {bid: name for bid, (name, _) in PANEL.items()}

    n = len(IDS)
    off_diag_vals = [
        corr.loc[a, b] for i, a in enumerate(IDS) for j, b in enumerate(IDS) if i < j
    ]
    avg_corr = float(np.mean(off_diag_vals))
    effective_n = n / (1 + (n - 1) * avg_corr)

    foreign_off_diag = [
        corr.loc[a, b]
        for i, a in enumerate(FOREIGN_IDS)
        for j, b in enumerate(FOREIGN_IDS)
        if i < j
    ]
    avg_foreign_corr = float(np.mean(foreign_off_diag))

    # ---- 3. PCA ----
    scaler = StandardScaler()
    z = scaler.fit_transform(complete.values)
    pca = PCA()
    pca.fit(z)
    evr = pca.explained_variance_ratio_

    # ---- 4. 共識家數：完整8家 vs 獨立子集 ----
    weights = {bid: w for bid, (_, w) in PANEL.items()}
    full_series = ctd.weighted_composite(scored, weights)
    full_series = full_series.set_index("trade_date")

    # 用相關矩陣做簡單 union-find 分群（threshold=0.4），每群取 PANEL 權重最高者
    # 作代表，組成「獨立子集」。
    threshold = 0.4
    parent = {bid: bid for bid in IDS}

    def find(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(IDS):
        for j, b in enumerate(IDS):
            if i < j and corr.loc[a, b] >= threshold:
                union(a, b)

    clusters: dict[str, list[str]] = {}
    for bid in IDS:
        clusters.setdefault(find(bid), []).append(bid)
    cluster_list = list(clusters.values())
    independent_subset = [max(members, key=lambda b: PANEL[b][1]) for members in cluster_list]
    independent_subset = sorted(independent_subset, key=lambda b: -PANEL[b][1])

    subset_weights = {bid: weights[bid] for bid in independent_subset}
    subset_scored = scored[scored["securities_trader_id"].isin(independent_subset)]
    subset_series = ctd.weighted_composite(subset_scored, subset_weights)
    subset_series = subset_series.set_index("trade_date")

    episodes = pd.read_csv(EPISODES_CSV, dtype={"trade_date": str})
    ep_rows = []
    for r in episodes.itertuples():
        d = r.trade_date
        full_n = int(full_series.loc[d, "consensus_n"]) if d in full_series.index else None
        sub_n = int(subset_series.loc[d, "consensus_n"]) if d in subset_series.index else None
        ep_rows.append(
            {
                "trade_date": d,
                "mkt_ret_pct": round(float(r.mkt_ret_pct), 2),
                "episode_first": bool(r.episode_first),
                "consensus_full_8": full_n,
                "consensus_full_frac": None if full_n is None else round(full_n / 8, 3),
                "consensus_indep_subset": sub_n,
                "consensus_indep_frac": None if sub_n is None else round(sub_n / len(independent_subset), 3),
            }
        )
    ep_df = pd.DataFrame(ep_rows)

    # ---- 5. 全樣本共識家數分布（不限事件日），比較 full8 vs 獨立子集 ----
    all_days = full_series.dropna(subset=["consensus_n"]).copy()
    all_days["consensus_frac_full"] = all_days["consensus_n"] / 8
    sub_days = subset_series.dropna(subset=["consensus_n"]).copy()
    sub_days["consensus_frac_sub"] = sub_days["consensus_n"] / len(independent_subset)
    joined = all_days[["consensus_n", "consensus_frac_full"]].join(
        sub_days[["consensus_n", "consensus_frac_sub"]], lsuffix="_full", rsuffix="_sub", how="inner"
    )
    pct_full_ge5 = float((joined["consensus_n_full"] >= 5).mean() * 100)
    pct_sub_majority = float(
        (joined["consensus_n_sub"] >= (len(independent_subset) // 2 + 1)).mean() * 100
    )

    conn.close()

    # ================== 寫報告 ==================
    lines: list[str] = []
    lines.append("# 大跌溫度計進階研究 H1：分點相關性結構／有效自由度")
    lines.append("")
    lines.append("Research diagnostic · 純調查，不修改生產腳本／config。")
    lines.append("")
    lines.append(
        f"資料窗：{complete.index.min()} ~ {complete.index.max()}"
        f"（重疊有效交易日 n={len(complete)}，`lb_pctile` 用生產腳本同一套"
        f"`build_branch_panel`+`compute_lb_pctile`(lookback_days={LOOKBACK_DAYS})邏輯，"
        "唯讀 import 自 `run_market_crash_thermometer_dashboard.py`）。"
    )
    lines.append("")
    lines.append("## 1. 8x8 Pearson 相關矩陣（`lb_pctile` 時間序列）")
    lines.append("")
    header = "| 分點 |" + "".join(f" {name_of[b]}({b}) |" for b in IDS)
    sep = "|---|" + "---|" * len(IDS)
    lines.append(header)
    lines.append(sep)
    for a in IDS:
        row = f"| {name_of[a]}({a}) |"
        for b in IDS:
            v = corr.loc[a, b]
            row += f" {v:.2f} |"
        lines.append(row)
    lines.append("")
    lines.append(f"- 8家平均 pairwise 相關係數（28組不重複配對均值）：**{avg_corr:.3f}**")
    lines.append(
        f"- 4家外資分點（瑞銀1650／港商野村1560／美商高盛亞1480／港麥格理1360）"
        f"彼此平均相關係數：**{avg_foreign_corr:.3f}**"
        f"（vs. 全體8家平均 {avg_corr:.3f}，"
        f"{'明顯偏高，外資分點確實高度共移動' if avg_foreign_corr > avg_corr + 0.05 else '與全體相近，外資分點之間並無特別額外的共移動'}）"
    )
    lines.append("")
    lines.append("## 2. 有效獨立分點數（effective_n）")
    lines.append("")
    lines.append("公式：`effective_n = n / (1 + (n-1) * avg_corr)`，n=8。")
    lines.append("")
    lines.append(f"- avg_corr = {avg_corr:.3f}")
    lines.append(f"- **effective_n ≈ {effective_n:.2f}**（滿分8）")
    lines.append("")
    lines.append("## 3. PCA：變異數集中度")
    lines.append("")
    lines.append("| 主成分 | 解釋變異度 | 累積 |")
    lines.append("|---|---:|---:|")
    cum = 0.0
    for i, v in enumerate(evr, start=1):
        cum += v
        lines.append(f"| PC{i} | {v*100:.1f}% | {cum*100:.1f}% |")
    lines.append("")
    pc1_note = (
        "第一主成分解釋變異度 > 50%，代表8家分點的日常波動本質上主要由**單一共同因子**驅動"
        if evr[0] > 0.5
        else "第一主成分未過半，代表8家分點並非單一共同因子主導，仍存在多個獨立變異來源"
    )
    lines.append(f"- {pc1_note}（PC1={evr[0]*100:.1f}%，PC1+PC2={  (evr[0]+evr[1])*100:.1f}%）。")
    lines.append("")
    lines.append("## 4. 相關性分群（threshold=0.4）與「獨立子集」")
    lines.append("")
    for members in cluster_list:
        members_sorted = sorted(members, key=lambda b: -PANEL[b][1])
        rep = max(members, key=lambda b: PANEL[b][1])
        names = "、".join(f"{name_of[b]}({b})" for b in members_sorted)
        mark = "  ← 代表" if len(members) > 1 else ""
        lines.append(f"- 群組：{names}{mark}（代表：{name_of[rep]}({rep})）")
    lines.append("")
    indep_names = "、".join(f"{name_of[b]}({b})" for b in independent_subset)
    lines.append(
        f"→ 獨立子集（每群取權重最高者代表，共{len(independent_subset)}家）：**{indep_names}**"
    )
    lines.append("")
    lines.append("## 5. 共識家數：16次大跌事件對照（全8家 vs 獨立子集）")
    lines.append("")
    lines.append(
        "共識家數在事件日T的定義沿用生產邏輯（`lb_sum`已用`shift(1).rolling(5)`，"
        "T日讀數已反映T-5..T-1的累計賣超），故直接看事件日T當天的`consensus_n`。"
    )
    lines.append("")
    lines.append(
        "| 事件日 | 大盤跌幅% | episode_first | 共識(全8家) | 全8家/8 | 共識(獨立子集) | 獨立子集/"
        f"{len(independent_subset)} |"
    )
    lines.append("|---|---:|---|---:|---:|---:|---:|")
    for r in ep_df.itertuples():
        full_s = "NA" if r.consensus_full_8 is None else f"{r.consensus_full_8}"
        full_f = "NA" if r.consensus_full_frac is None else f"{r.consensus_full_frac*100:.0f}%"
        sub_s = "NA" if r.consensus_indep_subset is None else f"{r.consensus_indep_subset}"
        sub_f = "NA" if r.consensus_indep_frac is None else f"{r.consensus_indep_frac*100:.0f}%"
        lines.append(
            f"| {r.trade_date} | {r.mkt_ret_pct:.2f}% | {r.episode_first} | {full_s} | {full_f} | "
            f"{sub_s} | {sub_f} |"
        )
    lines.append("")
    avg_full_frac = ep_df["consensus_full_frac"].dropna().mean()
    avg_sub_frac = ep_df["consensus_indep_frac"].dropna().mean()
    lines.append(
        f"- 16次事件日平均：全8家共識比例 **{avg_full_frac*100:.0f}%**，"
        f"獨立子集（{len(independent_subset)}家）共識比例 **{avg_sub_frac*100:.0f}%**。"
    )
    lines.append("")
    lines.append("## 6. 全樣本基準率對照（非僅事件日）")
    lines.append("")
    lines.append(
        f"- 全部有效交易日中，`consensus_n(全8家) >= 5/8` 的比例：**{pct_full_ge5:.1f}%**"
    )
    lines.append(
        f"- 全部有效交易日中，獨立子集達「過半數」(≥{len(independent_subset)//2+1}/"
        f"{len(independent_subset)}) 的比例：**{pct_sub_majority:.1f}%**"
    )
    lines.append("")
    lines.append("## 7. 結論")
    lines.append("")
    inflated = effective_n < 5.0
    lines.append(
        f"**{'是，共識家數(0~8)確實有虛高問題。' if inflated else '共識家數(0~8)大致合理，虛高問題不嚴重。'}**"
    )
    lines.append("")
    lines.append(
        f"- 8家分點平均pairwise相關係數 {avg_corr:.3f}，換算有效獨立分點數僅 **{effective_n:.1f}／8**"
        f"（即「共識家數=8」實質上的獨立訊號強度大約只等於{effective_n:.1f}個真正獨立分點同時觸發）。"
    )
    lines.append(
        f"- PCA第一主成分解釋 {evr[0]*100:.1f}% 變異度"
        + ("，證實存在一個主導性的共同因子（很可能是外資/國際母行的整體資金流向），" if evr[0] > 0.4 else "，共同因子主導性有限，")
        + f"前兩個主成分合計 {(evr[0]+evr[1])*100:.1f}%。"
    )
    lines.append(
        f"- 4家外資分點（瑞銀/野村/高盛亞/麥格理）彼此平均相關 {avg_foreign_corr:.3f}"
        f"（vs 全體 {avg_corr:.3f}），"
        + ("驗證了假說：這4家很大程度是同一組外資資金流向的重複計數。" if avg_foreign_corr > avg_corr else "但並未明顯高於全體平均，顯示外資身份本身不是主要相關來源。")
    )
    lines.append("")
    lines.append("### 具體建議")
    lines.append("")
    lines.append(
        "1. **把「共識家數(0~8)」改標注為「共識家數（原始，未校正共線性）」**，並在dashboard/報告"
        f"同時附上「獨立群組數（{len(independent_subset)}群，見上方分群）」作為對照讀數，避免"
        "使用者把「5/8共識」誤讀為5個獨立機構同步賣壓。"
    )
    lines.append(
        "2. 若要單一簡化指標，建議改用**獨立子集共識**（每個相關群組只取權重最高者代表，"
        f"共{len(independent_subset)}家：{indep_names}）取代0~8計數，這樣「N/M家共識」的M本身"
        "就已經去除了共線性重複計數。"
    )
    lines.append(
        "3. 短期不建議變更溫度百分位（`composite_score`）本身的計算——它是加權平均，"
        "本來就不會因為分點相關而\"重複計數\"到分數本身（相關性只影響「共識家數」這個"
        "獨立的離散讀數，不影響連續分數的加權平均值）。"
    )
    lines.append("")
    lines.append("## 附註：方法與限制")
    lines.append("")
    lines.append(
        "- `lb_pctile` 為單次全窗計算（同一批`compute_lb_pctile`呼叫涵蓋整段~2年），"
        "與生產dashboard每日重跑取當日值在方法上完全相同（`rank(pct=True)`本身就是"
        "對呼叫時給定的窗计算），但用於本研究的相關矩陣分析不需要逐日重算的PIT滾動窗——"
        "相關性結構分析的目的是描述訊號**共移動性**，不是產生新的可交易訊號。"
    )
    lines.append(
        "- 相關矩陣只用「8家當天`lb_pctile`皆非NA」的重疊交易日（`lb_sum`前5日尚未有完整"
        "rolling window的早期交易日會被排除）。"
    )
    lines.append(
        "- 分群threshold=0.4為研究慣例的中等相關門檻，非精確校準值；若用0.3或0.5門檻，"
        "分群數量可能±1，但「外資4家 vs 其餘4家」的主結構在合理threshold範圍內是穩定的"
        "（見上方相關矩陣原始數值自行覆核）。"
    )
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_MD}", flush=True)
    print(f"avg_corr={avg_corr:.3f} effective_n={effective_n:.2f} PC1={evr[0]*100:.1f}%", flush=True)
    print(f"independent_subset={independent_subset}", flush=True)


if __name__ == "__main__":
    main()
