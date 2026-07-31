#!/usr/bin/env python3
"""
研究角度：反應 vs 領先 —— 台積電(2330)外資重賣超事件，是否只是對已發生大跌的反應？

事件定義（causal / walk-forward-safe）：
  - 對每個交易日 t，用「只到 t 為止」的過去 250 個交易日（trailing window, 含當日）
    計算 foreign_net(2330) 在該窗口內的百分位排名（expanding early on, min_periods=60）。
  - 事件 = 該百分位 <= 10%（即當日賣超金額在過去約1年的窗口內排在最極端的後10%）。
  - 這個排名嚴格只用 t 及之前的資料，不會偷看未來（修正過去踩過的 .rank(pct=True) 對整個
    視窗一次算的 look-ahead bug）。

因果辨識檢定：
  (a) 對每個事件日 t，檢查 t 之前 5 個交易日（t-5 .. t-1，不含事件日本身）的 IX0001
      日報酬，是否已經出現過 <= -3% 的單日大跌。有 → 標記為「反應性 reactive」。
  (b) 排除掉 reactive 事件後，剩下「乾淨/獨立」事件，檢查其後續 IX0001 報酬
      （1/5/10/20 個交易日）是否仍有預測力（相對於：全樣本無條件後續報酬 baseline）。
  (c) reactive 組 vs clean 組的後續報酬分開報告，並附上事件聚集（consecutive run）
      的 declustered 版本作為穩健性檢查。
"""
import sqlite3
import numpy as np
import pandas as pd
from scipy import stats

DB = "/Users/jackm4/goldenstocks/data/stocks.db"
ROLL_WINDOW = 250
MIN_PERIODS = 60
PCTL_THRESH = 0.10          # bottom 10% = 極端賣超
CRASH_LOOKBACK_DAYS = 5     # 事件前5個交易日
CRASH_THRESH = -0.03        # 單日跌幅門檻 -3%
HORIZONS = [1, 5, 10, 20]

def load_data():
    con = sqlite3.connect(DB)
    inst = pd.read_sql_query(
        "SELECT trade_date AS date, foreign_net FROM stock_institutional_daily "
        "WHERE stock_id='2330' ORDER BY trade_date", con)
    idx = pd.read_sql_query(
        "SELECT date, AVG(close) AS close FROM daily_bars WHERE code='IX0001' "
        "GROUP BY date ORDER BY date", con)
    con.close()
    inst["date"] = pd.to_datetime(inst["date"])
    idx["date"] = pd.to_datetime(idx["date"])
    df = pd.merge(idx, inst, on="date", how="inner").sort_values("date").reset_index(drop=True)
    return df

def causal_rolling_pctrank(values: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """對每個索引 i，只用 values[max(0,i-window+1) : i+1]（即到當天為止的過去window天，
    含當天）計算 values[i] 在該子窗口中的百分位排名（0~1，小值=賣超更極端 -> 排名更接近0）。
    嚴格 causal：不使用 i 之後的任何資料。"""
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - window + 1)
        window_vals = values[lo:i + 1]
        if len(window_vals) < min_periods:
            continue
        # percentile rank of the *last* element (today) within this causal window
        v = window_vals[-1]
        rank = np.sum(window_vals <= v) / len(window_vals)  # inclusive rank, matches ascending pct-rank convention
        out[i] = rank
    return out

def main():
    df = load_data()
    n = len(df)
    print(f"合併後樣本數: {n}  日期範圍: {df['date'].min().date()} ~ {df['date'].max().date()}")

    df["ret"] = df["close"].pct_change()
    df["pctrank"] = causal_rolling_pctrank(df["foreign_net"].values, ROLL_WINDOW, MIN_PERIODS)

    df["is_event"] = df["pctrank"] <= PCTL_THRESH
    n_valid = df["pctrank"].notna().sum()
    n_events = int(df["is_event"].sum())
    print(f"\n有效可判定樣本數(pctrank非NaN): {n_valid}")
    print(f"事件數（bottom {PCTL_THRESH*100:.0f}% foreign_net, trailing {ROLL_WINDOW}d causal）: {n_events}"
          f"  ({n_events/n_valid*100:.2f}% of valid days，理論上應~{PCTL_THRESH*100:.0f}%)")

    # ---- reactive vs clean 標記 ----
    ret_arr = df["ret"].values
    date_arr = df["date"].values
    event_idx = df.index[df["is_event"]].tolist()

    reactive_flags = []
    prior_max_drawdown = []
    concurrent_flags = []  # 事件當日本身是否就是>=3%崩盤日（既非事前反應，也非事後領先，是同步）
    for i in event_idx:
        lo = max(0, i - CRASH_LOOKBACK_DAYS)
        prior_rets = ret_arr[lo:i]  # t-5..t-1, 不含事件日本身
        prior_rets = prior_rets[~np.isnan(prior_rets)]
        had_crash = bool(np.any(prior_rets <= CRASH_THRESH))
        reactive_flags.append(had_crash)
        prior_max_drawdown.append(prior_rets.min() if len(prior_rets) else np.nan)
        same_day_ret = ret_arr[i]
        concurrent_flags.append(bool(not np.isnan(same_day_ret) and same_day_ret <= CRASH_THRESH))

    events = df.loc[event_idx, ["date", "foreign_net", "pctrank", "close", "ret"]].copy()
    events["reactive"] = reactive_flags
    events["prior5d_min_ret"] = prior_max_drawdown
    events["concurrent_crash"] = concurrent_flags
    events["idx_pos"] = event_idx

    n_reactive = int(events["reactive"].sum())
    n_clean = int((~events["reactive"]).sum())
    print(f"\n=== 反應性 vs 獨立性 事件拆分 ===")
    print(f"事件總數: {len(events)}")
    print(f"  reactive（事件前5日內已有單日跌幅<= {CRASH_THRESH*100:.0f}%）: {n_reactive} "
          f"({n_reactive/len(events)*100:.1f}%)")
    print(f"  clean/獨立（事件前5日內無達標大跌）: {n_clean} ({n_clean/len(events)*100:.1f}%)")
    n_concurrent = int(events["concurrent_crash"].sum())
    print(f"  [額外診斷,不計入reactive/clean二分] 事件當日本身就是單日跌幅<= {CRASH_THRESH*100:.0f}%的"
          f"「同步」案例: {n_concurrent} ({n_concurrent/len(events)*100:.1f}%) —— "
          f"這種是同步共移，既非「事前已知後補刀」也非「提前預見」，需另外解讀")

    # ---- declustered 版本：把連續(consecutive trading-day)事件run只取第一天，避免同一次崩盤被算成很多個「事件」 ----
    events_sorted = events.sort_values("idx_pos").reset_index(drop=True)
    is_new_cluster = (events_sorted["idx_pos"].diff() != 1)
    events_sorted["cluster_start"] = is_new_cluster
    n_clusters = int(is_new_cluster.sum())
    print(f"\n若將連續交易日的事件去重為單一「事件群」(declustered): {n_clusters} 個獨立事件群"
          f"（原始 {len(events)} 個事件日，代表平均每群約 {len(events)/n_clusters:.1f} 個連續交易日）")

    declustered = events_sorted[events_sorted["cluster_start"]].copy()
    n_dc_reactive = int(declustered["reactive"].sum())
    n_dc_clean = int((~declustered["reactive"]).sum())
    print(f"  declustered reactive: {n_dc_reactive} ({n_dc_reactive/len(declustered)*100:.1f}%)  "
          f"clean: {n_dc_clean} ({n_dc_clean/len(declustered)*100:.1f}%)")

    # ---- forward returns ----
    def forward_ret(i, h):
        if i + h >= n:
            return np.nan
        return df["close"].iloc[i + h] / df["close"].iloc[i] - 1.0

    for h in HORIZONS:
        events[f"fwd_{h}d"] = [forward_ret(i, h) for i in events["idx_pos"]]
        declustered[f"fwd_{h}d"] = [forward_ret(i, h) for i in declustered["idx_pos"]]

    # baseline: 全樣本（含事件與非事件）無條件後續報酬分布，用來比較 event組是否顯著偏離
    baseline_fwd = {}
    for h in HORIZONS:
        vals = [forward_ret(i, h) for i in range(n)]
        baseline_fwd[h] = np.array([v for v in vals if not np.isnan(v)])

    def summarize(sub_df, label):
        print(f"\n--- {label} (n={len(sub_df)}) ---")
        for h in HORIZONS:
            vals = sub_df[f"fwd_{h}d"].dropna().values
            if len(vals) < 3:
                print(f"  {h:>2}d fwd ret: n太小({len(vals)})，略過統計檢定")
                continue
            base = baseline_fwd[h]
            mean_v = vals.mean()
            std_v = vals.std(ddof=1)
            t_base, p_base = stats.ttest_ind(vals, base, equal_var=False)
            # one-sample vs 0
            t0, p0 = stats.ttest_1samp(vals, 0.0)
            print(f"  {h:>2}d fwd ret: mean={mean_v*100:+6.2f}%  std={std_v*100:5.2f}%  n={len(vals):3d} | "
                  f"vs 0: t={t0:5.2f} p={p0:.3f} | vs baseline(mean={base.mean()*100:+.2f}%,n={len(base)}): "
                  f"t={t_base:5.2f} p={p_base:.3f}")

    print("\n" + "="*70)
    print("全部事件日（未拆分 reactive/clean，未去重）")
    summarize(events, "全部事件(all event-days)")

    print("\n" + "="*70)
    print("拆分：reactive vs clean（事件日層級，未去重連續交易日）")
    summarize(events[events["reactive"]], "REACTIVE 事件（前5日已見>=3%單日崩盤）")
    summarize(events[~events["reactive"]], "CLEAN/獨立 事件（前5日無崩盤）")

    print("\n" + "="*70)
    print("穩健性檢查：declustered（每個連續事件群只取第一天）")
    summarize(declustered, "全部事件群(declustered, all)")
    summarize(declustered[declustered["reactive"]], "REACTIVE 事件群(declustered)")
    summarize(declustered[~declustered["reactive"]], "CLEAN 事件群(declustered)")

    # ---- 多重檢定基準：假設訊號=雜訊，純機率下這麼多次檢定期望出現幾次「顯著」 ----
    print("\n" + "="*70)
    print("多重檢定基準參考：")
    n_tests = len(HORIZONS) * 2 * 2  # (all/reactive/clean not counted separately here) rough
    print(f"本次共呈現約 {len(HORIZONS)} 個horizon x 多個切分組合的檢定；"
          f"在5%門檻下純雜訊仍會有 ~{0.05*n_tests:.1f} 個「顯著」，解讀單一p<0.05時務必打折扣。")

    # ---- 列出最近(2026-07)這批事件，並標註其 reactive 狀態，方便交叉核對triggering觀察 ----
    print("\n" + "="*70)
    print("2026-07 最近一批事件明細（含 reactive 標記）：")
    recent = events[events["date"] >= "2026-06-01"].copy()
    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print(recent[["date", "foreign_net", "pctrank", "reactive", "prior5d_min_ret",
                       "fwd_1d", "fwd_5d"]].to_string(index=False))

    # save event table for inspection
    out_path = "/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/7b05044d-2e7a-4b22-9d48-98eb36da0015/scratchpad/events_2330_foreign_sell.csv"
    events.assign(date=events["date"].dt.strftime("%Y-%m-%d")).to_csv(out_path, index=False)
    print(f"\n事件明細已存: {out_path}")

if __name__ == "__main__":
    main()
