#!/usr/bin/env python3
"""H5：8家大跌溫度計分點是否各自有不同的最佳領先天數（而非統一5天）.

Research diagnostic（一次性）· 非正式風控規則 · 非可下單訊號。唯讀，不修改
`run_market_crash_thermometer_dashboard.py`／config。

方法：
1. 對8家分點重算 2024-07~asof 每日 net_amt(NT$)（複用production
   `build_branch_panel`/`build_full_grid`/`compute_lb_pctile`/`bounded_pctrank`
   邏輯，直接 import，不重寫）。
2. Cross-correlation：分點當日 net_amt vs IX0001 未來1~10個交易日累計報酬率，
   逐lag算 Pearson／Spearman，找每家「相關性最強」的lag。
3. 用各家「個人最佳lag」重算 lb_pctile（lookback_days=該lag），再用production
   同一套 bounded_pctrank（history_days=120、事件排除窗=lookback_days）在
   `market_crash_precursor_episodes.csv` 16次大跌事件上評估「個別分點判別力」
   （分點自己的 (1-lb_pctile) 當alarm分數，對bounded常態池取百分位排名，
   16事件平均），跟統一5天版本比較。

輸出：reports/research/branch-footprint-screen/adv_h5_optimal_lag_per_branch.md
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
EPISODES_CSV = OUT_DIR / "market_crash_precursor_episodes.csv"
DB_PATH = ROOT / "data" / "stocks.db"

# --- import production dashboard module by path (read-only reuse, no edits) ---
_spec = importlib.util.spec_from_file_location(
    "crash_dashboard", ROOT / "scripts/research/run_market_crash_thermometer_dashboard.py"
)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)  # type: ignore[union-attr]

from market_benchmark import load_benchmark_close  # noqa: E402

PANEL = cd.PANEL
IDS = list(PANEL.keys())
UNIFORM_LOOKBACK = 5
HISTORY_DAYS = 120
LAGS = list(range(1, 11))


def main() -> None:
    conn = cd.connect_ro(DB_PATH)
    asof = cd.latest_trade_date(conn)
    print(f"asof={asof}", flush=True)

    # 這8家分點在 stock_broker_branch_daily 的資料起點（早於此的calendar日子會被
    # build_full_grid fillna(0)成假的net_amt=0，稀釋cross-correlation與lb_pctile，
    # 故明確查真實資料起點再裁切calendar，不能只靠 n_days 往回抓固定天數）。
    placeholders = ",".join("?" for _ in IDS)
    branch_min_date = conn.execute(
        f"SELECT MIN(trade_date) FROM stock_broker_branch_daily "
        f"WHERE source=? AND securities_trader_id IN ({placeholders})",
        (cd.SOURCE, *IDS),
    ).fetchone()[0]
    print(f"branch data starts at {branch_min_date}", flush=True)

    cal_full = cd.build_calendar(conn, end=asof, n_days=560, extra_ids=IDS)
    cal = [d for d in cal_full if d >= branch_min_date]
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    panel_raw = cd.build_branch_panel(conn, IDS, start=cal[0], end=asof)
    print(f"panel rows={len(panel_raw)}", flush=True)
    grid = cd.build_full_grid(panel_raw, IDS, cal)

    bench = load_benchmark_close(conn, code="IX0001").sort_index()
    bench = bench[bench.index.isin(cal)]
    bench_dates = bench.index.tolist()
    print(f"bench n={len(bench)} {bench_dates[0]}..{bench_dates[-1]}", flush=True)

    crash_dates, all_event_dates = cd.load_event_dates(conn, cal)
    event_idx = {cal.index(d) for d in all_event_dates if d in cal}
    conn.close()

    episodes = pd.read_csv(EPISODES_CSV, dtype={"trade_date": str})
    event_test_dates = [d for d in episodes["trade_date"].tolist() if d in cal]
    print(f"event test dates n={len(event_test_dates)} (episodes csv rows={len(episodes)})", flush=True)

    # ---- Step 2: cross-correlation net_amt(t) vs future L-day cumulative return ----
    idx_of_bench = {d: i for i, d in enumerate(bench_dates)}
    bench_vals = bench.values

    def future_ret(lag: int) -> pd.Series:
        """close[t+lag]/close[t]-1，索引為 trade_date（僅bench календар內）。"""
        n = len(bench_vals)
        out = np.full(n, np.nan)
        if n > lag:
            out[: n - lag] = bench_vals[lag:] / bench_vals[: n - lag] - 1.0
        return pd.Series(out, index=bench_dates)

    future_rets = {lag: future_ret(lag) for lag in LAGS}

    xcorr_rows = []
    best_lag_by_branch: dict[str, int] = {}
    corr_table_by_branch: dict[str, pd.DataFrame] = {}
    for bid in IDS:
        name = PANEL[bid][0]
        g = grid[grid["securities_trader_id"] == bid].set_index("trade_date")["net_amt"]
        rows = []
        for lag in LAGS:
            fr = future_rets[lag]
            joined = pd.concat([g, fr.rename("fwd_ret")], axis=1, join="inner").dropna()
            if len(joined) < 30:
                rows.append({"lag": lag, "n": len(joined), "pearson_r": np.nan, "spearman_r": np.nan, "pearson_p": np.nan})
                continue
            pear_r, pear_p = stats.pearsonr(joined["net_amt"], joined["fwd_ret"])
            spear_r, spear_p = stats.spearmanr(joined["net_amt"], joined["fwd_ret"])
            rows.append(
                {
                    "lag": lag,
                    "n": len(joined),
                    "pearson_r": pear_r,
                    "pearson_p": pear_p,
                    "spearman_r": spear_r,
                    "spearman_p": spear_p,
                }
            )
        tbl = pd.DataFrame(rows)
        corr_table_by_branch[bid] = tbl
        # 「相關性最強」= pearson_r 最大正值（重賣超net_amt負值 對應 未來下跌負報酬 → 正相關）
        valid = tbl.dropna(subset=["pearson_r"])
        best_row = valid.loc[valid["pearson_r"].idxmax()] if not valid.empty else None
        best_lag = int(best_row["lag"]) if best_row is not None else UNIFORM_LOOKBACK
        best_lag_by_branch[bid] = best_lag
        for r in rows:
            xcorr_rows.append({"branch_id": bid, "branch_name": name, **r})
        print(f"{name}({bid}) best_lag={best_lag} pearson_r={best_row['pearson_r'] if best_row is not None else 'NA'}", flush=True)

    xcorr_df = pd.DataFrame(xcorr_rows)

    # ---- Step 3: per-branch discriminative power, uniform-5 vs custom best-lag ----
    def branch_discriminative_power(bid: str, lookback_days: int) -> tuple[float | None, list[dict]]:
        sub_grid = grid[grid["securities_trader_id"] == bid][["securities_trader_id", "trade_date", "net_amt"]]
        scored = cd.compute_lb_pctile(sub_grid, lookback_days)
        scored = scored.dropna(subset=["lb_pctile"])
        scores_by_date = dict(zip(scored["trade_date"], 1.0 - scored["lb_pctile"]))
        details = []
        prs = []
        for d in event_test_dates:
            pr, n_normal = cd.bounded_pctrank(
                scores_by_date, cal, event_idx,
                test_date=d, history_days=HISTORY_DAYS, lookback_days=lookback_days,
            )
            details.append({"trade_date": d, "pctrank": pr, "n_normal": n_normal})
            if pr is not None:
                prs.append(pr)
        avg = float(np.mean(prs)) if prs else None
        return avg, details

    comparison_rows = []
    detail_rows = []
    for bid in IDS:
        name = PANEL[bid][0]
        uniform_power, uniform_detail = branch_discriminative_power(bid, UNIFORM_LOOKBACK)
        custom_lag = best_lag_by_branch[bid]
        custom_power, custom_detail = branch_discriminative_power(bid, custom_lag)
        delta = None if (uniform_power is None or custom_power is None) else custom_power - uniform_power
        comparison_rows.append(
            {
                "branch_id": bid,
                "branch_name": name,
                "best_lag": custom_lag,
                "uniform5_power_pct": uniform_power,
                "custom_lag_power_pct": custom_power,
                "delta_pct": delta,
            }
        )
        for d in uniform_detail:
            detail_rows.append({"branch_id": bid, "lookback": "uniform5", **d})
        for d in custom_detail:
            detail_rows.append({"branch_id": bid, "lookback": f"custom{custom_lag}", **d})
        print(f"{name}({bid}): uniform5={uniform_power} custom(lag={custom_lag})={custom_power} delta={delta}", flush=True)

    comparison_df = pd.DataFrame(comparison_rows)
    detail_df = pd.DataFrame(detail_rows)

    xcorr_df.to_csv(OUT_DIR / "_h5_xcorr_by_branch.csv", index=False)
    comparison_df.to_csv(OUT_DIR / "_h5_lag_comparison.csv", index=False)
    detail_df.to_csv(OUT_DIR / "_h5_event_detail.csv", index=False)

    write_report(xcorr_df, comparison_df, detail_df, corr_table_by_branch, asof, cal, event_test_dates)


def write_report(
    xcorr_df: pd.DataFrame,
    comparison_df: pd.DataFrame,
    detail_df: pd.DataFrame,
    corr_table_by_branch: dict[str, pd.DataFrame],
    asof: str,
    cal: list[str],
    event_test_dates: list[str],
) -> None:
    lines = []
    lines.append("# H5：8家分點是否各自有不同的最佳領先天數（vs 統一5天）")
    lines.append("")
    lines.append("Research diagnostic（一次性假說驗證）· 非正式風控規則 · 非可下單訊號 · "
                 "唯讀分析，未修改 `run_market_crash_thermometer_dashboard.py` 或任何 config。")
    lines.append("")
    lines.append(f"asof={asof}；calendar={cal[0]}..{cal[-1]}（{len(cal)}個交易日）；"
                 f"16次大跌事件測試日={len(event_test_dates)}（來源："
                 "`market_crash_precursor_episodes.csv`）。")
    lines.append("")
    lines.append("## 方法摘要")
    lines.append("")
    lines.append("1. 8家分點 net_amt(NT$)=net(股)×close，複用 production "
                 "`build_branch_panel`/`build_full_grid` 邏輯（同一份程式碼 import，未重寫）。")
    lines.append("2. Cross-correlation：分點『當日net_amt』（原始值，負=賣超）vs "
                 "IX0001『未來lag個交易日累計報酬率』（lag=1~10），逐lag算Pearson/Spearman "
                 "（全樣本≈2年每日資料，非僅事件日）。因為『重賣超(net_amt很負)→未來大跌"
                 "(報酬很負)』兩者同向變動，預期為**正**相關；取Pearson r最大（最正）的"
                 "lag為該分點『最佳領先天數』。")
    lines.append("3. 用各家『最佳lag』重算 `lb_pctile`（rolling-sum window=該lag），"
                 "以同一套 `bounded_pctrank`（history_days=120、事件排除窗=lookback_days，"
                 "與production同方法）在16次大跌事件上評分：分點自己的 `1-lb_pctile` 當"
                 "『賣超警戒分數』，對bounded常態池（排除任一事件±lookback天）取百分位排名，"
                 "16事件平均＝『個別分點判別力』。跟統一5天版本比較。")
    lines.append("4. **注意**：cross-correlation的『lag』原意是『未來報酬窗口』，"
                 "拿來當`lb_pctile`的『過去net_amt加總窗口』(lookback_days)是一個"
                 "工程上的轉譯假設（『這家分點的訊號要花N天才完全反映，所以往前"
                 "累加N天』），不是嚴格數學上同一件事，請視為探索性假說而非證明。")
    lines.append("")
    lines.append("## Step 2：Cross-correlation結果（8家×lag 1~10）")
    lines.append("")
    for bid, tbl in corr_table_by_branch.items():
        name = PANEL_NAME(comparison_df, bid)
        lines.append(f"### {name}（{bid}）")
        lines.append("")
        lines.append("| lag(交易日) | n | Pearson r | Pearson p | Spearman r | Spearman p |")
        lines.append("|---:|---:|---:|---:|---:|---:|")
        for r in tbl.itertuples():
            pr = "NA" if pd.isna(r.pearson_r) else f"{r.pearson_r:+.4f}"
            pp = "NA" if pd.isna(r.pearson_p) else f"{r.pearson_p:.3f}"
            sr = "NA" if pd.isna(r.spearman_r) else f"{r.spearman_r:+.4f}"
            sp = "NA" if pd.isna(r.spearman_p) else f"{r.spearman_p:.3f}"
            lines.append(f"| {r.lag} | {r.n} | {pr} | {pp} | {sr} | {sp} |")
        lines.append("")

    lines.append("## Step 2 摘要：各家最佳lag（Pearson r 最大值所在lag）")
    lines.append("")
    lines.append("| 分點 | ID | 統一版lookback | Cross-corr最佳lag | 該lag Pearson r | 該lag p值 | 是否顯著(p<0.05) |")
    lines.append("|------|----|---:|---:|---:|---:|:---:|")
    for r in comparison_df.itertuples():
        tbl = corr_table_by_branch[r.branch_id]
        row = tbl[tbl["lag"] == r.best_lag].iloc[0]
        sig = "✓" if (not pd.isna(row["pearson_p"]) and row["pearson_p"] < 0.05) else ""
        lines.append(
            f"| {r.branch_name} | {r.branch_id} | {UNIFORM_LOOKBACK_LINE()} | {r.best_lag} | "
            f"{row['pearson_r']:+.4f} | {row['pearson_p']:.3f} | {sig} |"
        )
    lines.append("")

    lines.append("## Step 3：個別分點判別力比較 —— 統一5天 vs 客製化最佳lag")
    lines.append("")
    lines.append("16次大跌事件、bounded-rolling(120日)+事件排除walk-forward，"
                 "分點自身`1-lb_pctile`當警戒分數的平均百分位排名（50%=亂猜，越高越好）。")
    lines.append("")
    lines.append("| 分點 | ID | 統一5天判別力% | 客製化lag | 客製化判別力% | 差異(pp) | 判定 |")
    lines.append("|------|----|---:|---:|---:|---:|:---:|")
    for r in comparison_df.sort_values("delta_pct", ascending=False, na_position="last").itertuples():
        u = "NA" if r.uniform5_power_pct is None or pd.isna(r.uniform5_power_pct) else f"{r.uniform5_power_pct:.1f}"
        c = "NA" if r.custom_lag_power_pct is None or pd.isna(r.custom_lag_power_pct) else f"{r.custom_lag_power_pct:.1f}"
        d = "NA" if r.delta_pct is None or pd.isna(r.delta_pct) else f"{r.delta_pct:+.1f}"
        verdict = ""
        if r.delta_pct is not None and not pd.isna(r.delta_pct):
            if r.delta_pct >= 5:
                verdict = "✓ 明顯提升"
            elif r.delta_pct <= -5:
                verdict = "✗ 明顯變差"
            else:
                verdict = "≈ 無明顯差異"
        lines.append(f"| {r.branch_name} | {r.branch_id} | {u} | {r.best_lag} | {c} | {d} | {verdict} |")
    lines.append("")

    lines.append("## 結論與建議")
    lines.append("")
    improved = comparison_df[
        comparison_df["delta_pct"].notna() & (comparison_df["delta_pct"] >= 5) & (comparison_df["best_lag"] != UNIFORM_LOOKBACK)
    ]
    if improved.empty:
        lines.append("- **沒有任何一家分點的客製化最佳lag，在16事件bounded walk-forward下"
                     "帶來≥5個百分點的判別力提升。** 現行『全部統一5天』的設定，在本次"
                     "16事件樣本檢驗下沒有被證明是次優的。")
    else:
        for r in improved.itertuples():
            lines.append(f"- **{r.branch_name}（{r.branch_id}）**：最佳lag={r.best_lag}天"
                         f"（vs統一5天），判別力 {r.uniform5_power_pct:.1f}% → "
                         f"{r.custom_lag_power_pct:.1f}%（{r.delta_pct:+.1f}pp），建議可考慮"
                         "調整此分點的lookback天數，但需注意下方過擬合風險。")
    lines.append("")
    lines.append("## 誠實限制（務必讀）")
    lines.append("")
    lines.append("- **樣本量極小**：16次大跌事件（且多集中在少數幾個『事件群』，例如"
                 "2025-04-07~09三天連續、2026-06-08~10兩天相鄰），bounded walk-forward的"
                 "常態池排除機制會讓相鄰事件互相排擠彼此的『常態日』，實際獨立樣本數"
                 "遠低於16。判別力數字的信賴區間極寬，±10-20個百分點的差異很可能只是"
                 "雜訊，不是真實訊號。")
    lines.append("- **Cross-correlation本身也是小樣本問題的延伸**：雖然全樣本每日資料"
                 "有~480個交易日，理論上n夠大，但net_amt序列本身高度自相關（同一批"
                 "外資／法人可能連續數日同方向下單），且『大跌』事件本質上是稀疏、"
                 "群聚的尾端現象——8家分點×10個lag＝80次相關係數檢定，即使不做"
                 "多重比較校正，仍有相當機率其中幾個『看起來最強』的lag只是雜訊"
                 "湊巧對到；本報告**沒有**做Bonferroni等多重比較校正，請謹慎解讀"
                 "『最佳lag』的統計顯著性。")
    lines.append("- **Lag語義轉譯的假設風險**：如Step 4方法說明，把『未來報酬預測窗口』"
                 "直接套用成『過去net_amt加總窗口』，兩者概念不完全等價，只是一個"
                 "合理但未經證明的工程假設。")
    lines.append("- **統一5天判別力的絕對值本身也可能與production script的權重"
                 "來源數字（PANEL注解裡的86.0%/72.7%/...）不完全一致**"
                 "——本報告的評分事件集合（16次，取自"
                 "`market_crash_precursor_episodes.csv`全部列）與production內部"
                 "推導這些權重時用的事件集合、視窗參數可能有差異，本報告只在"
                 "『同一套方法、同一組事件』下比較統一5天vs客製化lag，橫向比較"
                 "有效，但不建議拿本報告的判別力絕對值去覆蓋production現有權重。")
    lines.append("- 本分析為單一假說（H5）驗證，不建議僅憑此報告修改"
                 "`run_market_crash_thermometer_dashboard.py` 的 `PANEL`/`LOOKBACK_DAYS_DEFAULT`；"
                 "若要採用，建議先在更長事件樣本（例如納入更早期歷史或用其他"
                 "大跌定義擴大事件數）上複驗。")
    lines.append("")

    report_path = OUT_DIR / "adv_h5_optimal_lag_per_branch.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {report_path}", flush=True)


def PANEL_NAME(comparison_df: pd.DataFrame, bid: str) -> str:
    row = comparison_df[comparison_df["branch_id"] == bid]
    return row["branch_name"].iloc[0] if not row.empty else bid


def UNIFORM_LOOKBACK_LINE() -> str:
    return f"{UNIFORM_LOOKBACK}天"


if __name__ == "__main__":
    main()
