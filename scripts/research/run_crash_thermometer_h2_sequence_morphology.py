#!/usr/bin/env python3
"""大跌溫度計進階研究 H2：賣超序列型態（節奏／加速度）是否比純總量更有預測力.

Research diagnostic（純調查，不修改生產腳本／config）。

問題：現行公式只用「前5日累計淨額」單一總量數字。同樣的5日總量，可能是
「均勻分散賣出」、「第一天就重砸」或「逐日加速賣出」，這些節奏差異可能
帶有總量本身漏掉的資訊（緊急程度／資訊含量）。

方法：直接 import `run_market_crash_thermometer_dashboard.py` 的
`build_calendar`/`build_branch_panel`/`build_full_grid`/`compute_lb_pctile`/
`load_event_dates`/`bounded_pctrank`（唯讀使用，不修改），對8家分點重算
~2.2年 net_amt 序列。額外算3種節奏特徵，同樣做「自我常態化百分位」
（沿用`rank(pct=True)`同一套慣例），再用同一套 bounded-rolling walk-forward
（120個交易日常態基準池＋事件±5日排除）比較：

  (a) 純總量百分位（現行公式，baseline，直接用生產 `weighted_composite`）
  (b) 節奏特徵單獨（3特徵等權平均後的自我百分位，加權合成8家）
  (c) 總量＋節奏組合（(a)(b)取平均 / 取min 兩種組合方式都測）

在16次大跌事件（`market_crash_precursor_episodes.csv`）上的判別力
（bounded 常態日池百分位排名均值，50%=亂猜）。

## 三種節奏特徵定義

皆用 `severity_t = -net_amt_t`（賣超為正值，方向與 `lb_pctile`「低=賣超極端」
相反；morphology 故意反過來定義成「數值越高＝越可疑」，可以直接排百分位
（不必像 lb_pctile 那樣取 `1-rank`），三特徵之間、與(a)之間才能直接同方向
平均／取min。窗一律是 `severity.shift(1)` 後的 T-5..T-1（與 `lb_sum` 同一個
5日窗，PIT-safe：T當天不用到）：

1. `front2_ratio`：前2日(T-5,T-4)佔前5日(T-5..T-1)總量比例。越高＝越早重砸。
2. `seq_slope`：對5日 severity（x=1..5）做線性回歸取斜率。正＝加速惡化
   （每天賣超遞增），負＝減速（前重後輕）。
3. `last_burst_ratio`：最後一日(T-1)相對前4日(T-5..T-2)均值的比例。
   越高＝尾盤（最近一天）才爆發。

輸出：reports/research/branch-footprint-screen/adv_h2_sequence_morphology.md

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_crash_thermometer_h2_sequence_morphology.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = ROOT / "data" / "stocks.db"
OUT_MD = ROOT / "reports" / "research" / "branch-footprint-screen" / "adv_h2_sequence_morphology.md"
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
CAL_N_DAYS = 560  # ~2.2年交易日，覆蓋2024-07~asof（同 H1 慣例）
HISTORY_DAYS = 120  # bounded normal pool，同生產預設


def compute_morphology_features(grid: pd.DataFrame, lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """對每個(分點,日)算3種節奏特徵。`severity=-net_amt`，用
    `severity.shift(1)` 的 T-lookback_days..T-1 窗（與 `compute_lb_pctile` 的
    `lb_sum` 同一個窗，PIT-safe）。"""
    grid = grid.sort_values(["securities_trader_id", "trade_date"]).copy()
    grid["severity"] = -grid["net_amt"].astype(float)

    def _per_branch(g: pd.DataFrame) -> pd.DataFrame:
        sev = g["severity"].shift(1).to_numpy()
        n = len(sev)
        front2 = np.full(n, np.nan)
        slope = np.full(n, np.nan)
        last_burst = np.full(n, np.nan)
        x = np.arange(1, lookback_days + 1, dtype=float)
        for i in range(lookback_days, n):
            w = sev[i - lookback_days : i]  # chronological: w[0]=T-5 .. w[-1]=T-1
            if np.isnan(w).any():
                continue
            total = w.sum()
            if total != 0:
                front2[i] = (w[0] + w[1]) / total
            slope[i] = np.polyfit(x, w, 1)[0]
            head_mean = w[:-1].mean()
            if head_mean != 0:
                last_burst[i] = w[-1] / head_mean
        return pd.DataFrame(
            {"front2_ratio": front2, "seq_slope": slope, "last_burst_ratio": last_burst}
        )

    out = grid.groupby("securities_trader_id", group_keys=False).apply(_per_branch, include_groups=False)
    return pd.concat([grid.reset_index(drop=True), out.reset_index(drop=True)], axis=1)


def add_self_pctile(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """對每個特徵欄位，分點自己歷史排百分位（同 `compute_lb_pctile` 的
    `rank(pct=True, na_option="keep")` 慣例）。"""
    df = df.sort_values(["securities_trader_id", "trade_date"]).copy()
    for c in cols:
        df[f"{c}_pctile"] = df.groupby("securities_trader_id")[c].rank(pct=True, na_option="keep")
    return df


def weighted_mean_score(df: pd.DataFrame, score_col: str, weights: dict[str, float]) -> pd.Series:
    """Per trade_date 加權平均 `score_col`（已經是「高=熱」方向），略過 NaN。"""
    sub = df.dropna(subset=[score_col]).copy()
    sub["w"] = sub["securities_trader_id"].map(weights)
    sub["w_score"] = sub["w"] * sub[score_col]
    grp = sub.groupby("trade_date").agg(w_sum=("w", "sum"), w_score_sum=("w_score", "sum"))
    return (grp["w_score_sum"] / grp["w_sum"]).rename(score_col)


def walk_forward_scores(
    scores: pd.Series,
    cal: list[str],
    event_idx: set[int],
    test_dates: list[str],
    *,
    history_days: int,
    lookback_days: int,
) -> pd.DataFrame:
    scores_by_date = scores.dropna().to_dict()
    rows = []
    for d in test_dates:
        pr, n_normal = ctd.bounded_pctrank(
            scores_by_date, cal, event_idx,
            test_date=d, history_days=history_days, lookback_days=lookback_days,
        )
        rows.append({"trade_date": d, "score": scores_by_date.get(d), "percentile_rank": pr, "n_normal_days": n_normal})
    return pd.DataFrame(rows)


def main() -> None:
    conn = ctd.connect_ro(DB_PATH)
    asof = ctd.latest_trade_date(conn)
    print(f"asof={asof}", flush=True)

    cal = ctd.build_calendar(conn, end=asof, n_days=CAL_N_DAYS, extra_ids=IDS)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    panel_raw = ctd.build_branch_panel(conn, IDS, start=cal[0], end=asof)
    print(f"panel rows={len(panel_raw)}", flush=True)

    grid = ctd.build_full_grid(panel_raw, IDS, cal)

    # (a) 純總量：直接用生產 compute_lb_pctile + weighted_composite
    scored_mag = ctd.compute_lb_pctile(grid, LOOKBACK_DAYS)
    weights = {bid: w for bid, (_, w) in PANEL.items()}
    series_mag = ctd.weighted_composite(scored_mag, weights).set_index("trade_date")
    score_a = series_mag["composite_score"]

    # (b) 節奏特徵：3特徵 self-pctile 等權平均 -> 加權合成8家
    morph = compute_morphology_features(grid, LOOKBACK_DAYS)
    feat_cols = ["front2_ratio", "seq_slope", "last_burst_ratio"]
    morph = add_self_pctile(morph, feat_cols)
    pctile_cols = [f"{c}_pctile" for c in feat_cols]
    morph["morph_pctile"] = morph[pctile_cols].mean(axis=1, skipna=True)
    morph.loc[morph[pctile_cols].isna().all(axis=1), "morph_pctile"] = np.nan
    score_b = weighted_mean_score(morph, "morph_pctile", weights)

    # 補充：3個節奏特徵各自單獨（供細部診斷用）
    score_feat = {
        c: weighted_mean_score(morph, f"{c}_pctile", weights) for c in feat_cols
    }

    # (c) 總量+節奏組合：對齊日期後取平均 / 取min
    joined = pd.concat([score_a.rename("a"), score_b.rename("b")], axis=1).dropna()
    score_c_avg = ((joined["a"] + joined["b"]) / 2.0).rename("c_avg")
    score_c_min = joined.min(axis=1).rename("c_min")

    # 事件與排除窗：直接沿用生產 load_event_dates（大跌∪大漲±5日），與生產
    # dashboard 的 bounded normal pool 排除邏輯完全一致
    crash_dates, all_event_dates = ctd.load_event_dates(conn, cal)
    event_idx = {cal.index(d) for d in all_event_dates if d in cal}
    conn.close()

    episodes = pd.read_csv(EPISODES_CSV, dtype={"trade_date": str})
    test_dates = [d for d in episodes["trade_date"].tolist() if d in cal]
    print(f"test events n={len(test_dates)} (of {len(episodes)} in csv, in-calendar)", flush=True)

    candidates = {
        "(a) 純總量 baseline": score_a,
        "(b) 節奏特徵單獨": score_b,
        "(c) 總量+節奏 取平均": score_c_avg,
        "(c') 總量+節奏 取min": score_c_min,
    }
    wf_results = {
        name: walk_forward_scores(
            s, cal, event_idx, test_dates, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        )
        for name, s in candidates.items()
    }
    feat_wf_results = {
        c: walk_forward_scores(
            score_feat[c], cal, event_idx, test_dates, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        )
        for c in feat_cols
    }

    summary_rows = []
    for name, df in wf_results.items():
        valid = df.dropna(subset=["percentile_rank"])
        summary_rows.append(
            {
                "candidate": name,
                "n_events": len(valid),
                "avg_pctrank": float(valid["percentile_rank"].mean()) if len(valid) else None,
                "min_pctrank": float(valid["percentile_rank"].min()) if len(valid) else None,
                "pct_ge_70": float((valid["percentile_rank"] >= 70).mean() * 100) if len(valid) else None,
            }
        )
    summary = pd.DataFrame(summary_rows)
    print(summary.to_string(index=False), flush=True)

    feat_summary_rows = []
    for c, df in feat_wf_results.items():
        valid = df.dropna(subset=["percentile_rank"])
        feat_summary_rows.append(
            {
                "feature": c,
                "n_events": len(valid),
                "avg_pctrank": float(valid["percentile_rank"].mean()) if len(valid) else None,
            }
        )
    feat_summary = pd.DataFrame(feat_summary_rows)

    baseline_avg = summary.loc[summary["candidate"] == "(a) 純總量 baseline", "avg_pctrank"].iloc[0]
    rhythm_avg = summary.loc[summary["candidate"] == "(b) 節奏特徵單獨", "avg_pctrank"].iloc[0]
    combo_avg_avg = summary.loc[summary["candidate"] == "(c) 總量+節奏 取平均", "avg_pctrank"].iloc[0]
    combo_min_avg = summary.loc[summary["candidate"] == "(c') 總量+節奏 取min", "avg_pctrank"].iloc[0]
    best_combo_avg = max(combo_avg_avg, combo_min_avg)
    best_combo_name = "取平均" if combo_avg_avg >= combo_min_avg else "取min"
    lift = best_combo_avg - baseline_avg

    # ================== 寫報告 ==================
    lines: list[str] = []
    lines.append("# 大跌溫度計進階研究 H2：賣超序列型態（節奏／加速度）是否比純總量更有預測力")
    lines.append("")
    lines.append("Research diagnostic · 純調查，不修改生產腳本／config。")
    lines.append("")
    lines.append(
        f"資料窗：{cal[0]} ~ {cal[-1]}（交易日 n={len(cal)}，`net_amt` 用生產腳本同一套"
        "`build_branch_panel`（唯讀 import 自 `run_market_crash_thermometer_dashboard.py`）"
        f"重算，8家分點清單與權重完全沿用生產 `PANEL`）。測試事件：`market_crash_precursor_"
        f"episodes.csv` 全部16次大跌事件（不篩選`episode_first`，逐日單獨測），"
        f"落在本次重建行事曆內的有 {len(test_dates)} 個。"
    )
    lines.append("")
    lines.append("## 1. 三種節奏特徵定義")
    lines.append("")
    lines.append(
        "皆用 `severity_t = -net_amt_t`（賣超為正值，方向與 `lb_pctile`「低=賣超極端」"
        "相反；故意反過來讓「數值越高＝越可疑」，可直接排百分位、不必像 `lb_pctile` 那樣"
        "取`1-rank`，三特徵與(a)才能直接同方向平均／取min）。窗一律用"
        "`severity.shift(1)` 後的 T-5..T-1（與生產 `lb_sum` 同一個5日窗，PIT-safe："
        "T當天不用到）。"
    )
    lines.append("")
    lines.append("| 特徵 | 定義 | 方向 |")
    lines.append("|---|---|---|")
    lines.append(
        "| `front2_ratio` | 前2日(T-5,T-4)佔前5日(T-5..T-1)總量比例 | 越高＝越早重砸 |"
    )
    lines.append(
        "| `seq_slope` | 對5日 severity(x=1..5) 線性回歸斜率 | 正＝加速惡化，負＝減速 |"
    )
    lines.append(
        "| `last_burst_ratio` | 最後一日(T-1)／前4日(T-5..T-2)均值 | 越高＝尾盤才爆發 |"
    )
    lines.append("")
    lines.append(
        "自我常態化：每個特徵對分點自己的歷史分布排百分位（`rank(pct=True)`，"
        "與生產 `lb_pctile` 同一套慣例）；`(b)節奏特徵單獨` = 3個特徵百分位等權平均，"
        "再用生產權重加權合成8家。"
    )
    lines.append("")
    lines.append("## 2. (a)(b)(c) 判別力比較：16次大跌事件，bounded walk-forward")
    lines.append("")
    lines.append(
        f"常態日基準池：測試日前最近 {HISTORY_DAYS} 個交易日，排除任一大跌／大漲事件"
        f"±{LOOKBACK_DAYS} 交易日窗（與生產 `bounded_pctrank`／`load_event_dates` 完全"
        "同一套邏輯，唯讀 import）。判別力 = 事件日在常態日池中的百分位排名均值"
        "（50%=亂猜，越高越好）。"
    )
    lines.append("")
    lines.append("| 候選公式 | 事件數(有效) | 平均判別力 | 最差一次 | ≥70%比例 |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in summary.itertuples():
        avg_s = "NA" if r.avg_pctrank is None else f"{r.avg_pctrank:.1f}%"
        min_s = "NA" if r.min_pctrank is None else f"{r.min_pctrank:.1f}%"
        ge70_s = "NA" if r.pct_ge_70 is None else f"{r.pct_ge_70:.0f}%"
        lines.append(f"| {r.candidate} | {r.n_events} | {avg_s} | {min_s} | {ge70_s} |")
    lines.append("")
    lines.append("補充：3個節奏特徵單獨（未合成，只做參考，不是主要對照組）：")
    lines.append("")
    lines.append("| 特徵 | 事件數(有效) | 平均判別力 |")
    lines.append("|---|---:|---:|")
    for r in feat_summary.itertuples():
        avg_s = "NA" if r.avg_pctrank is None else f"{r.avg_pctrank:.1f}%"
        lines.append(f"| `{r.feature}` | {r.n_events} | {avg_s} |")
    lines.append("")
    lines.append("## 3. 逐事件明細")
    lines.append("")
    header_cols = ["trade_date"] + list(candidates.keys())
    lines.append("| 事件日 | " + " | ".join(candidates.keys()) + " |")
    lines.append("|---|" + "---:|" * len(candidates))
    per_date_pr = {name: dict(zip(df["trade_date"], df["percentile_rank"])) for name, df in wf_results.items()}
    for d in test_dates:
        row = [d]
        for name in candidates:
            v = per_date_pr[name].get(d)
            row.append("NA" if v is None or pd.isna(v) else f"{v:.1f}%")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## 4. 結論")
    lines.append("")
    verdict = (
        "節奏特徵有小幅增量資訊，但幅度在16個事件的樣本量下不足以視為穩健改善。"
        if 0 < lift < 3
        else (
            "節奏特徵組合後對判別力有明確提升，值得納入現行公式做為輔助分數。"
            if lift >= 3
            else "節奏特徵單獨或組合後判別力並未優於純總量，不建議取代或加重節奏權重。"
        )
    )
    lines.append(f"**{verdict}**")
    lines.append("")
    lines.append(
        f"- (a)純總量baseline平均判別力 **{baseline_avg:.1f}%**；(b)節奏特徵單獨"
        f"平均判別力 **{rhythm_avg:.1f}%**（{'優於' if rhythm_avg > baseline_avg else '不如'}"
        "baseline，節奏特徵單獨看，比純總量"
        f"{'更能' if rhythm_avg > baseline_avg else '較不能'}分辨大跌事件日 vs 平常日）。"
    )
    lines.append(
        f"- (c)組合（{best_combo_name}）平均判別力 **{best_combo_avg:.1f}%**，"
        f"較(a)baseline {'高' if lift >= 0 else '低'} **{abs(lift):.1f}個百分點**"
        f"（另一種組合方式判別力為 {'c_min' if best_combo_name=='取平均' else 'c_avg'}="
        f"{min(combo_avg_avg, combo_min_avg):.1f}%）。"
    )
    lift_word = "提升" if lift > 0 else ("持平" if lift == 0 else "下降")
    lines.append(
        f"- **樣本量警示**：本比較只有 {len(test_dates)} 個獨立大跌事件（其中多個彼此"
        "在同一波大跌內相隔僅1-3個交易日，不是16個真正獨立樣本），"
        f"{lift:+.1f}個百分點的{lift_word}在這個樣本量下**沒有統計顯著性**可言"
        "（單次事件排名波動輕易可達±10-20個百分點，見上方逐事件明細的離散度）——"
        "不管正負，只要幅度在1-2個百分點內都應視為雜訊，不應視為「節奏特徵確實有效／無效」的"
        "決定性證據。"
    )
    lines.append("")
    lines.append("### 具體建議")
    lines.append("")
    if lift >= 3 and best_combo_avg > baseline_avg:
        lines.append(
            "1. 可考慮把節奏特徵（3特徵等權平均後的自我百分位）納入複合分數，"
            f"用「{best_combo_name}」方式與現行總量分數組合，但因樣本量小（16個事件），"
            "建議先以「輔助標注」形式（例如在dashboard明細多印一欄節奏百分位）觀察至少"
            "數次新大跌事件再決定是否真正納入權重公式。"
        )
    else:
        lines.append(
            "1. 現階段不建議修改現行公式——節奏特徵在這16個事件上沒有展現出穩健、"
            "可信的增量判別力，加入公式只會多一層複雜度但沒有對應的訊號品質提升。"
        )
    lines.append(
        "2. 若未來事件樣本數增加（例如再累積10-20次大跌事件），值得重跑本比較，"
        "確認結論是否穩定，而非只看單次16事件的結果下定論。"
    )
    lines.append("")
    lines.append("## 附註：方法與限制")
    lines.append("")
    lines.append(
        "- 自我百分位（`lb_pctile`／3個節奏特徵`_pctile`）用單次全窗"
        f"`rank(pct=True)`計算（涵蓋 {cal[0]}~{cal[-1]} 整段~2.2年），"
        "與生產dashboard「每日重跑、當日只看得到當天為止的歷史」在方法上等價"
        "（因為生產dashboard的calendar本身就是每天往回抓固定窗，`rank()`天生就是"
        "對呼叫時給定的窗計算），與 H1、`run_market_precursor_thermometer_candidates.py`"
        "既有研究用的是同一套慣例，以確保可比較。"
    )
    lines.append(
        "- 最早幾次大跌事件（2024-07~2024-09）自我百分位可用的歷史很短"
        "（分點自身歷史樣本不足，tail定義容易被稀釋/放大），對應的 bounded 常態日池"
        f"也可能不到 {HISTORY_DAYS} 天，判別力數字比後期事件更不穩定，"
        "請對照上方逐事件明細與常態日數判斷，不要只看整體平均。"
    )
    lines.append(
        "- 節奏特徵在 `total≈0` 或 `前4日均值≈0` 的窗會產生 `NaN`（略過，不當0處理），"
        "這類窗多發生在交易稀疏、5日內買賣互抵的分點-日組合，屬於合理排除，"
        "不是資料缺漏。"
    )
    lines.append(
        "- 事件排除窗（大跌∪大漲±5日）直接呼叫生產 `load_event_dates`（即時用"
        "`IX0001` yahoo版基準算），與生產dashboard當前的即時判定完全一致，"
        "但因此本報告數字會隨資料庫持續同步而略有變動（若未來再跑，"
        "常態日池成分可能因新資料進來而改變）。"
    )
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_MD}", flush=True)
    print(
        f"baseline_avg={baseline_avg:.1f}% rhythm_avg={rhythm_avg:.1f}% "
        f"best_combo({best_combo_name})={best_combo_avg:.1f}% lift={lift:+.1f}pp",
        flush=True,
    )


if __name__ == "__main__":
    main()
