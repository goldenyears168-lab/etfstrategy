#!/usr/bin/env python3
"""H-9A81-FORWARD-TRADEABLE · 把「事後諸葛」的已實現報酬代理指標，改寫成即時可偵測的訊號規則。

背景：scan_quiet_accumulators.py 用整個建倉期結束後才算得出來的「加權平均成本 vs 峰值後最新
收盤價」當作 realized_proxy_pct，這個數字本質上不可交易——在建倉當下不知道「這是不是安靜建倉的
終點」。這支腳本改成「只用截至當日（含）為止的資料」做因果（causal）滾動窗口偵測，模擬一個
真正的跟單者在每個交易日收盤後看得到的東西，然後套用嚴謹的 L1H7（以及更長 horizon）協議回測。

方法（因果、無未來函數）：
對 9A81 過去交易過的每一檔股票（不限於先前用全歷史找到的 7~9 檔候選），逐日計算滾動窗口
（N=20/40/60 個交易日）指標：
  - active_days_N   : 窗口內有出手（buy>0 or sell>0）的天數
  - consistency_N   : 出手日中淨買超（net>0）天數的比例
  - trend_r2_N      : 窗口內累積淨部位（局部，從窗口起點歸零算）對時間的線性迴歸 R²
                       （用 Pearson correlation 的平方，向量化計算；因為 x 是等差數列，
                       corr 對 x 的仿射變換不變，用全域索引取代窗口局部索引結果相同）
  - window_retention_N : 窗口內「目前局部累積部位」/「窗口內局部累積部位的最大值」
                       （避免用未來峰值，只看窗口內至今為止的路徑）
規則 A（窗口內部一致性，主規則）：
  active_days_N >= max(5, 0.4*N) 且 consistency_N >= 0.65 且 trend_r2_N >= 0.6
  且 window_retention_N >= 0.85 且 全域累積部位 cum_global(t) > 0
規則 B（規則A + 全期新高，更嚴格）：
  規則A 且 cum_global(t) >= 0.95 * 截至今天為止的歷史最大累積部位（cummax）

一旦某 (股票, N, 規則) 組合連續滿足條件，只取「由不滿足轉為滿足」的第一天當訊號日（rising edge），
並要求同一 (股票,N,規則) 兩個訊號日之間至少間隔 DEDUP_GAP 個交易日，避免窗口邊界雜訊造成訊號抖動。

回測：對每個訊號日，套用 L1H7（次日開盤進場、7交易日收盤出場）以及 L1H20、L1H60（同邏輯，
只是把 7 換成 20/60），30bps 成本、β=1.15 用 IX0001 調整（沿用
study_whale_branch_l1h7_universe.py 的 build_l1h7_lookup 邏輯，泛化成任意 hold）。

統計：對每個 (N, 規則, horizon) 組合的訊號報酬做 one-sample t-test、Wilcoxon signed-rank、
bootstrap 95% CI（5000 次重抽樣），並額外做「每檔股票只取第一個訊號」的獨立性較高版本。

用法（在 mini 上跑，資料齊全）：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9A81_forward_tradeable.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
TRADER_ID = "9A81"
COST = 0.003
BETA = 1.15
DATA_START = "2024-07-01"
WINDOWS = [20, 40, 60]
HORIZONS = [7, 20, 60]
MIN_CONSISTENCY = 0.65
MIN_TREND_R2 = 0.6
MIN_WINDOW_RETENTION = 0.85
ALLTIME_HIGH_FRAC = 0.95
DEDUP_GAP = 10  # 交易日，同一 (股票,N,規則) 兩訊號日之間至少間隔這麼多天
MIN_TOTAL_ACTIVE_DAYS = 20  # 至少要有這麼多出手日，才值得拉進來算（等於最小窗口長度）
N_BOOTSTRAP = 5000
RNG_SEED = 20260728

MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
KNOWN_CANDIDATES_CSV = ROOT / "reports/research/branch-footprint-screen/quiet_accumulators_candidates.csv"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
PREFIX = "whale_9A81_deepdive_"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def load_known_candidates() -> set[str]:
    if not KNOWN_CANDIDATES_CSV.exists():
        return set()
    df = pd.read_csv(KNOWN_CANDIDATES_CSV, dtype={"stock_id": str})
    df = df[df["securities_trader_id"] == TRADER_ID]
    return set(df["stock_id"].tolist())


def find_stock_universe(conn) -> list[str]:
    """9A81 在 DATA_START 之後，出手 >= MIN_TOTAL_ACTIVE_DAYS 天的所有股票（不限於已知候選）。"""
    q = """
        SELECT stock_id, COUNT(*) as n_active
        FROM stock_broker_branch_daily
        WHERE source = ? AND securities_trader_id = ? AND trade_date >= ?
          AND (buy > 0 OR sell > 0)
        GROUP BY stock_id
        HAVING COUNT(*) >= ?
    """
    df = pd.read_sql_query(q, conn, params=(SOURCE, TRADER_ID, DATA_START, MIN_TOTAL_ACTIVE_DAYS))
    mega = load_mega_blacklist()
    df = df[~df["stock_id"].isin(mega)]
    df = df[~df["stock_id"].str.startswith("00")]
    return sorted(df["stock_id"].tolist())


def load_ix(conn) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code='IX0001' AND date >= ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (DATA_START,),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    dates = sorted(out)
    return pd.DataFrame(
        {"date": dates, "open": [out[d][0] for d in dates], "close": [out[d][1] for d in dates]}
    )


def load_stock_bars(conn, stock_ids: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source='{SOURCE}' AND stock_id IN ({placeholders})
          AND trade_date >= ? AND close > 0
        ORDER BY stock_id, trade_date
    """
    df = pd.read_sql_query(q, conn, params=(*stock_ids, DATA_START))
    df["open"] = df["open"].where(df["open"] > 0, df["close"])
    return df


def load_branch_daily(conn, stock_ids: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE source='{SOURCE}' AND securities_trader_id = ? AND stock_id IN ({placeholders})
          AND trade_date >= ?
    """
    return pd.read_sql_query(q, conn, params=(TRADER_ID, *stock_ids, DATA_START))


def build_hold_lookup(dates: np.ndarray, opens: np.ndarray, closes: np.ndarray, hold: int) -> pd.DataFrame:
    """對一支股票（已依日期排序）的 bars，算每個可能訊號日對應的 L1H{hold} entry/exit。因果：
    訊號日 t 只用 t 及之前資訊；進場＝t+1 開盤；出場＝進場起算第 hold 個交易日收盤。"""
    n = len(dates)
    rows = []
    for sig_i in range(n - 1):
        entry_i = sig_i + 1
        exit_i = entry_i + hold - 1
        if exit_i >= n:
            continue
        rows.append((dates[sig_i], dates[entry_i], opens[entry_i], dates[exit_i], closes[exit_i]))
    return pd.DataFrame(rows, columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close"])


def compute_causal_signals(stock_id: str, bars: pd.DataFrame, branch: pd.DataFrame) -> pd.DataFrame:
    """對單一股票，回傳其完整交易日曆上，每個 (window, rule) 組合逐日是否「符合條件」的布林序列，
    以及診斷用的指標值。只用截至當日（含）為止的資料，完全因果。"""
    g = bars.sort_values("trade_date").reset_index(drop=True)
    b = branch.set_index("trade_date")[["buy", "sell", "net"]]
    b = b.reindex(g["trade_date"]).fillna(0.0).reset_index(drop=True)
    net = b["net"].to_numpy(dtype=float)
    buy = b["buy"].to_numpy(dtype=float)
    sell = b["sell"].to_numpy(dtype=float)
    active_flag = pd.Series(((buy > 0) | (sell > 0)).astype(float))
    pos_flag = pd.Series((net > 0).astype(float))
    cum_global = pd.Series(net).cumsum()
    idx = pd.Series(np.arange(len(g), dtype=float))
    alltime_max = cum_global.cummax()

    out = pd.DataFrame({"trade_date": g["trade_date"], "cum_global": cum_global})

    for N in WINDOWS:
        active_days_N = active_flag.rolling(N, min_periods=N).sum()
        pos_active_N = pos_flag.rolling(N, min_periods=N).sum()
        consistency_N = pos_active_N / active_days_N.replace(0, np.nan)
        trend_r2_N = cum_global.rolling(N, min_periods=N).corr(idx) ** 2
        base = cum_global.shift(N).fillna(0.0)
        # 只有 t>=N (min_periods 保證) 時 base 才有意義；shift 產生的 NaN 在前 N 天已被 min_periods 蓋掉
        rollmax = cum_global.rolling(N, min_periods=N).max()
        local_cum = cum_global - base
        local_max = rollmax - base
        window_retention_N = local_cum / local_max.replace(0, np.nan)
        active_thresh = max(5, int(np.floor(0.4 * N)))

        qualifies_a = (
            (active_days_N >= active_thresh)
            & (consistency_N >= MIN_CONSISTENCY)
            & (trend_r2_N >= MIN_TREND_R2)
            & (window_retention_N >= MIN_WINDOW_RETENTION)
            & (cum_global > 0)
        )
        qualifies_a = qualifies_a.fillna(False)
        qualifies_b = qualifies_a & (cum_global >= ALLTIME_HIGH_FRAC * alltime_max) & (alltime_max > 0)
        qualifies_b = qualifies_b.fillna(False)

        out[f"active_days_{N}"] = active_days_N
        out[f"consistency_{N}"] = consistency_N
        out[f"trend_r2_{N}"] = trend_r2_N
        out[f"window_retention_{N}"] = window_retention_N
        out[f"qualifies_A_{N}"] = qualifies_a
        out[f"qualifies_B_{N}"] = qualifies_b

    out["stock_id"] = stock_id
    return out


def extract_signal_events(diag: pd.DataFrame, window: int, rule: str) -> pd.DataFrame:
    """從逐日診斷表抽出 rising-edge 訊號日，並套用最小間隔去重。"""
    col = f"qualifies_{rule}_{window}"
    events = []
    last_sig_i = None
    prev = False
    for i, (flag) in enumerate(diag[col].to_numpy()):
        is_rising = bool(flag) and not prev
        if is_rising:
            if last_sig_i is None or (i - last_sig_i) >= DEDUP_GAP:
                events.append(i)
                last_sig_i = i
        prev = bool(flag)
    if not events:
        return pd.DataFrame(
            columns=[
                "stock_id", "signal_date", "window", "rule",
                f"consistency_{window}", f"trend_r2_{window}", f"active_days_{window}",
                f"window_retention_{window}", "cum_global",
            ]
        )
    rows = diag.iloc[events].copy()
    rows["window"] = window
    rows["rule"] = rule
    rows = rows.rename(columns={"trade_date": "signal_date"})
    keep = [
        "stock_id", "signal_date", "window", "rule",
        f"consistency_{window}", f"trend_r2_{window}", f"active_days_{window}",
        f"window_retention_{window}", "cum_global",
    ]
    rows = rows[keep].rename(
        columns={
            f"consistency_{window}": "consistency",
            f"trend_r2_{window}": "trend_r2",
            f"active_days_{window}": "active_days",
            f"window_retention_{window}": "window_retention",
        }
    )
    return rows


def attach_forward_returns(
    events: pd.DataFrame, stock_lookups: dict[str, dict[int, pd.DataFrame]], ix_lookups: dict[int, pd.DataFrame]
) -> pd.DataFrame:
    records = []
    for row in events.itertuples(index=False):
        rec = row._asdict() if hasattr(row, "_asdict") else dict(zip(events.columns, row))
        sid = rec["stock_id"]
        sig_date = rec["signal_date"]
        sid_lookups = stock_lookups.get(sid)
        if sid_lookups is None:
            continue
        ok = True
        for h in HORIZONS:
            lk = sid_lookups.get(h)
            ixlk = ix_lookups.get(h)
            if lk is None or ixlk is None or sig_date not in lk.index or sig_date not in ixlk.index:
                ok = False
                break
        if not ok:
            continue
        for h in HORIZONS:
            lk = sid_lookups[h].loc[sig_date]
            ixlk = ix_lookups[h].loc[sig_date]
            r_s = lk["exit_close"] / lk["entry_open"] - 1 - COST
            r_ix = ixlk["exit_close"] / ixlk["entry_open"] - 1
            r_adj = r_s - BETA * r_ix
            rec[f"h{h}_entry_date"] = lk["entry_date"]
            rec[f"h{h}_exit_date"] = lk["exit_date"]
            rec[f"h{h}_r_pct"] = r_s * 100
            rec[f"h{h}_r_ix_pct"] = r_ix * 100
            rec[f"h{h}_r_adj_pct"] = r_adj * 100
        records.append(rec)
    return pd.DataFrame(records)


def bootstrap_ci(values: np.ndarray, n_boot: int, rng: np.random.Generator) -> tuple[float, float]:
    if len(values) == 0:
        return (np.nan, np.nan)
    means = np.empty(n_boot)
    n = len(values)
    for i in range(n_boot):
        sample = rng.choice(values, size=n, replace=True)
        means[i] = sample.mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def summarize(events: pd.DataFrame, horizon: int, rng: np.random.Generator, label: str) -> dict:
    col = f"h{horizon}_r_adj_pct"
    vals = events[col].dropna().to_numpy()
    n = len(vals)
    row = {
        "label": label,
        "horizon": horizon,
        "n": n,
        "n_stocks": events["stock_id"].nunique() if n else 0,
        "mean_r_adj_pct": float(np.mean(vals)) if n else np.nan,
        "median_r_adj_pct": float(np.median(vals)) if n else np.nan,
        "win_rate_pct": float((vals > 0).mean() * 100) if n else np.nan,
    }
    if n >= 2:
        t_stat, p_t = stats.ttest_1samp(vals, 0.0)
        row["t_stat"] = float(t_stat)
        row["p_ttest"] = float(p_t)
        try:
            w_stat, p_w = stats.wilcoxon(vals)
            row["wilcoxon_stat"] = float(w_stat)
            row["p_wilcoxon"] = float(p_w)
        except ValueError:
            row["wilcoxon_stat"] = None
            row["p_wilcoxon"] = None
        lo, hi = bootstrap_ci(vals, N_BOOTSTRAP, rng)
        row["boot_ci_lo"] = lo
        row["boot_ci_hi"] = hi
    else:
        row["t_stat"] = None
        row["p_ttest"] = None
        row["wilcoxon_stat"] = None
        row["p_wilcoxon"] = None
        row["boot_ci_lo"] = None
        row["boot_ci_hi"] = None
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-total-active-days", type=int, default=MIN_TOTAL_ACTIVE_DAYS)
    args = ap.parse_args()

    t0 = time.time()
    conn = connect(DEFAULT_DB_PATH)

    print(f"[INFO] finding {TRADER_ID} stock universe (>= {args.min_total_active_days} active days since {DATA_START})...")
    stock_ids = find_stock_universe(conn)
    print(f"[INFO] universe size: {len(stock_ids)} stocks · elapsed {time.time()-t0:.1f}s")

    known = load_known_candidates()
    print(f"[INFO] known (hindsight) candidate stocks from quiet_accumulators_candidates.csv: {sorted(known)}")

    t1 = time.time()
    print("[INFO] loading full price bars + IX0001...")
    bars_all = load_stock_bars(conn, stock_ids)
    ix = load_ix(conn)
    branch_all = load_branch_daily(conn, stock_ids)
    conn.close()
    print(f"[INFO] bars rows={len(bars_all):,} branch rows={len(branch_all):,} · elapsed {time.time()-t1:.1f}s")

    # forward-return lookups (per stock, per horizon) + benchmark lookups (per horizon)
    ix_lookups = {}
    for h in HORIZONS:
        lk = build_hold_lookup(ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy(), h)
        ix_lookups[h] = lk.set_index("signal_date")

    t2 = time.time()
    print("[INFO] computing causal daily diagnostics + forward-return lookups per stock...")
    stock_lookups: dict[str, dict[int, pd.DataFrame]] = {}
    diag_frames = []
    for sid, g in bars_all.groupby("stock_id"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        br = branch_all[branch_all["stock_id"] == sid]
        diag = compute_causal_signals(sid, g, br)
        diag_frames.append(diag)
        lookups = {}
        for h in HORIZONS:
            lk = build_hold_lookup(g["trade_date"].to_numpy(), g["open"].to_numpy(), g["close"].to_numpy(), h)
            if not lk.empty:
                lookups[h] = lk.set_index("signal_date")
        stock_lookups[sid] = lookups
    diag_all = pd.concat(diag_frames, ignore_index=True)
    print(f"[INFO] diagnostics computed for {diag_all['stock_id'].nunique()} stocks · elapsed {time.time()-t2:.1f}s")

    # extract signal events per (window, rule)
    t3 = time.time()
    all_events = []
    for N in WINDOWS:
        for rule in ["A", "B"]:
            for sid, dg in diag_all.groupby("stock_id"):
                ev = extract_signal_events(dg, N, rule)
                if not ev.empty:
                    all_events.append(ev)
    events_raw = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    print(f"[INFO] raw signal events across all (window,rule) combos: {len(events_raw):,} · elapsed {time.time()-t3:.1f}s")

    events_raw.to_csv(OUT_DIR / f"{PREFIX}signal_events_raw.csv", index=False)

    # attach forward returns
    t4 = time.time()
    events_with_returns = []
    for (N, rule), sub in events_raw.groupby(["window", "rule"]):
        withret = attach_forward_returns(sub, stock_lookups, ix_lookups)
        if not withret.empty:
            events_with_returns.append(withret)
    events_final = pd.concat(events_with_returns, ignore_index=True) if events_with_returns else pd.DataFrame()
    print(f"[INFO] events with computable forward returns (all horizons available): {len(events_final):,} · elapsed {time.time()-t4:.1f}s")
    events_final["is_known_candidate"] = events_final["stock_id"].isin(known)
    events_final.to_csv(OUT_DIR / f"{PREFIX}signal_events_with_returns.csv", index=False)

    # summary stats per (window, rule, horizon) -- all events, and first-signal-per-stock only
    rng = np.random.default_rng(RNG_SEED)
    summary_rows = []
    overlap_rows = []
    for (N, rule), sub in events_final.groupby(["window", "rule"]):
        stocks_hit = set(sub["stock_id"].unique())
        overlap = stocks_hit & known
        overlap_rows.append(
            {
                "window": N, "rule": rule,
                "n_events": len(sub), "n_stocks_hit": len(stocks_hit),
                "n_overlap_with_known": len(overlap), "n_known_total": len(known),
                "overlap_frac_of_hit": len(overlap) / len(stocks_hit) if stocks_hit else np.nan,
                "recall_of_known": len(overlap) / len(known) if known else np.nan,
                "stocks_hit": ",".join(sorted(stocks_hit)),
                "overlap_stocks": ",".join(sorted(overlap)),
                "known_missed": ",".join(sorted(known - stocks_hit)),
            }
        )
        first_only = sub.sort_values("signal_date").drop_duplicates("stock_id", keep="first")
        for h in HORIZONS:
            summary_rows.append(summarize(sub, h, rng, label=f"N{N}_{rule}_all_events"))
            summary_rows.append(summarize(first_only, h, rng, label=f"N{N}_{rule}_first_per_stock"))

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_DIR / f"{PREFIX}summary_stats.csv", index=False)
    overlap_df = pd.DataFrame(overlap_rows)
    overlap_df.to_csv(OUT_DIR / f"{PREFIX}overlap_with_known.csv", index=False)

    # split known-candidate vs newly-discovered stocks: does the hindsight-known set still
    # look good once we force a genuinely causal (non-cherry-picked) entry date on it?
    split_rows = []
    for (N, rule), sub in events_final.groupby(["window", "rule"]):
        for h in HORIZONS:
            known_sub = sub[sub["is_known_candidate"]]
            new_sub = sub[~sub["is_known_candidate"]]
            split_rows.append(summarize(known_sub, h, rng, label=f"N{N}_{rule}_known_subset"))
            split_rows.append(summarize(new_sub, h, rng, label=f"N{N}_{rule}_new_subset"))
    split_df = pd.DataFrame(split_rows)
    split_df.insert(0, "window", split_df["label"].str.extract(r"N(\d+)_").astype(int))
    split_df.insert(1, "rule", split_df["label"].str.extract(r"_([AB])_"))
    split_df.insert(2, "group", split_df["label"].str.extract(r"_(known_subset|new_subset)$"))
    split_df.to_csv(OUT_DIR / f"{PREFIX}summary_stats_known_vs_new.csv", index=False)
    print("\n[INFO] === Known-candidate subset vs newly-discovered subset (causal entry timing) ===")
    print(
        split_df[["window", "rule", "group", "horizon", "n", "mean_r_adj_pct", "median_r_adj_pct", "win_rate_pct", "p_ttest", "p_wilcoxon"]]
        .to_string()
    )

    print("\n[INFO] === Overlap with hindsight-known candidates ===")
    print(overlap_df[["window", "rule", "n_events", "n_stocks_hit", "n_overlap_with_known", "recall_of_known"]].to_string())

    print("\n[INFO] === Summary stats (all events) ===")
    print(
        summary_df[summary_df["label"].str.endswith("all_events")][
            ["label", "horizon", "n", "n_stocks", "mean_r_adj_pct", "median_r_adj_pct", "win_rate_pct", "p_ttest", "p_wilcoxon"]
        ].to_string()
    )

    print("\n[INFO] === Summary stats (first signal per stock only) ===")
    print(
        summary_df[summary_df["label"].str.endswith("first_per_stock")][
            ["label", "horizon", "n", "n_stocks", "mean_r_adj_pct", "median_r_adj_pct", "win_rate_pct", "p_ttest", "p_wilcoxon"]
        ].to_string()
    )

    print(f"\n[OK] → {PREFIX}signal_events_raw.csv")
    print(f"[OK] → {PREFIX}signal_events_with_returns.csv")
    print(f"[OK] → {PREFIX}summary_stats.csv")
    print(f"[OK] → {PREFIX}overlap_with_known.csv")
    print(f"[DONE] total elapsed {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
