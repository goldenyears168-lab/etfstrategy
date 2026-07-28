#!/usr/bin/env python3
"""H-9661-HOLDING-MOMENTUM · 9661（富邦-新店）既有持倉「消長動能」的因果訊號 + 前瞻40交易日回測。

背景（見 config/research.yaml whale-individual-9217-9661-identity-validation topic）：對 9661 已經
做過「單日訊號觸發」「月度無條件淨買超 panel regression」「conviction tail 尾部事件」三種角度，
全部 rejected/contested，無穩健正向 edge。這次是第四種、也是使用者這輪指定的新角度：不問「有沒有
新訊號」，而是問「既有持倉最近是加速加碼還是開始減碼，這個動能本身有沒有預測力」。

方法（完全因果，無未來函數）：
對 9661 交易過、且達到最小活躍天數門檻的每一檔股票，逐日計算：
  cum(t)        = 截至 t（含）為止的累積淨買超股數（net 的 cumsum，只用該股票歷史全部資料）
  delta_short(t)= cum(t) - cum(t-short)   (窗口內淨部位變化量)
  z_short(t)    = (delta_short(t) - hist_mean) / hist_std
                  hist_mean/hist_std 用「該 (分點,股票) 配對過去 delta_short 值的擴張窗口」
                  （只用 t-1 及之前的 delta_short 觀測值，需要至少 MIN_HIST 筆歷史才開始判定，
                  嚴格因果——t 當天的 delta_short 不會混進自己的標準化基準）
逐日分類（只在 cum(t) > 0，即目前仍是淨多頭部位時才分類）：
  ACCEL : z_short(t) >= Z_ACCEL         （短窗加碼速度顯著高於這檔股票自己的歷史常態）
  DECEL : z_short(t) <= Z_DECEL         （短窗轉為明顯減碼/賣出，含逐步出場與一次性出清）
  FLAT  : 介於中間
訊號日 = 從非該分類轉為該分類的第一天（rising edge），同一 (股票,short/long窗口組合,分類) 訊號
之間至少間隔 DEDUP_GAP 個交易日去重。用 (short,long) 三組窗口對做敏感度檢查：(10,40)/(20,60)/(30,90)。

前瞻回測（嚴謹協議，沿用 study_whale_9A81_forward_tradeable.py 的 build_hold_lookup 邏輯泛化任意
hold 天數）：訊號日 t 次日開盤進場，第 40 個交易日（主要）／第 20 個交易日（次要對照）收盤出場，
30bps 成本，β=1.15 用 IX0001 調整。

統計：對每個 (short,long,category,horizon) 組合的訊號報酬做 one-sample t-test、Wilcoxon
signed-rank、bootstrap 95% CI（5000次）。同時做「每檔股票只取第一個訊號」版本降低虛擬複製。

用法（在 mini 上跑，資料齊全）：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9661_holding_momentum_forward40.py
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
TRADER_ID = "9661"
COST = 0.003
BETA = 1.15
DATA_START = "2024-07-01"
WINDOW_PAIRS = [(10, 40), (20, 60), (30, 90)]
PRIMARY_PAIR = (20, 60)
HORIZONS = [20, 40]
Z_ACCEL = 1.0
Z_DECEL = -1.0
MIN_HIST = 40  # 至少要有這麼多筆過去 delta_short 觀測值，才開始做 z-score 分類（避免早期基準不穩）
DEDUP_GAP = 10  # 交易日，同一 (股票,窗口組合,分類) 兩訊號日之間至少間隔這麼多天
MIN_TOTAL_ACTIVE_DAYS = 20  # 至少要有這麼多出手日，才值得拉進來算
N_BOOTSTRAP = 5000
RNG_SEED = 20260728

MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
PREFIX = "whale_9661_momentum_"


def load_mega_blacklist() -> set[str]:
    if not MEGA_BLACKLIST_PATH.exists():
        return set()
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def find_stock_universe(conn) -> list[str]:
    q = """
        SELECT stock_id, COUNT(*) as n_active
        FROM stock_broker_branch_daily
        WHERE source = ? AND securities_trader_id = ? AND trade_date >= ?
          AND (buy > 0 OR sell > 0)
        GROUP BY stock_id
        HAVING COUNT(*) >= ?
    """
    df = pd.read_sql_query(q, conn, params=(SOURCE, TRADER_ID, DATA_START, MIN_TOTAL_ACTIVE_DAYS))
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


def compute_causal_momentum(stock_id: str, bars: pd.DataFrame, branch: pd.DataFrame) -> pd.DataFrame:
    """完全因果的逐日持倉動能診斷。t 只用 t 及之前的資料。"""
    g = bars.sort_values("trade_date").reset_index(drop=True)
    b = branch.set_index("trade_date")[["buy", "sell", "net"]]
    b = b.reindex(g["trade_date"]).fillna(0.0).reset_index(drop=True)
    net = b["net"].to_numpy(dtype=float)
    cum = pd.Series(net).cumsum()

    out = pd.DataFrame({"trade_date": g["trade_date"], "cum": cum})

    for short, long in WINDOW_PAIRS:
        delta_short = cum - cum.shift(short)
        delta_long = cum - cum.shift(long)
        # 因果 z-score：用 t-1 及之前的 delta_short 觀測值做擴張窗口 mean/std（shift(1) 排除當天自己）
        hist_mean = delta_short.shift(1).expanding(min_periods=MIN_HIST).mean()
        hist_std = delta_short.shift(1).expanding(min_periods=MIN_HIST).std(ddof=0)
        z_short = (delta_short - hist_mean) / hist_std.replace(0, np.nan)

        state = pd.Series(np.where(cum > 0, "FLAT", "NA"), index=cum.index)
        state = state.where(~((cum > 0) & (z_short >= Z_ACCEL)), "ACCEL")
        state = state.where(~((cum > 0) & (z_short <= Z_DECEL)), "DECEL")
        state = state.where(z_short.notna() | (cum <= 0), "NA")  # z 缺值(歷史不足)時標 NA，不分類

        tag = f"{short}_{long}"
        out[f"delta_short_{tag}"] = delta_short
        out[f"delta_long_{tag}"] = delta_long
        out[f"z_short_{tag}"] = z_short
        out[f"state_{tag}"] = state

    out["stock_id"] = stock_id
    return out


def extract_transition_events(diag: pd.DataFrame, short: int, long: int, category: str) -> pd.DataFrame:
    """抽出「轉為 category」的 rising-edge 訊號日（從非 category 轉為 category），最小間隔去重。"""
    tag = f"{short}_{long}"
    col = f"state_{tag}"
    states = diag[col].to_numpy()
    events = []
    last_sig_i = None
    prev = None
    for i, s in enumerate(states):
        is_rising = (s == category) and (prev != category)
        if is_rising:
            if last_sig_i is None or (i - last_sig_i) >= DEDUP_GAP:
                events.append(i)
                last_sig_i = i
        prev = s
    if not events:
        return pd.DataFrame(columns=["stock_id", "signal_date", "short", "long", "category", "z_short", "cum"])
    rows = diag.iloc[events][["stock_id", "trade_date", "cum", f"z_short_{tag}"]].copy()
    rows = rows.rename(columns={"trade_date": "signal_date", f"z_short_{tag}": "z_short"})
    rows["short"] = short
    rows["long"] = long
    rows["category"] = category
    return rows[["stock_id", "signal_date", "short", "long", "category", "z_short", "cum"]]


def attach_forward_returns(
    events: pd.DataFrame, stock_lookups: dict[str, dict[int, pd.DataFrame]], ix_lookups: dict[int, pd.DataFrame]
) -> pd.DataFrame:
    records = []
    for row in events.itertuples(index=False):
        rec = dict(zip(events.columns, row))
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
    vals = events[col].dropna().to_numpy() if col in events.columns else np.array([])
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

    # ---- 0. 基本身分與規模輪廓 ----
    prof = pd.read_sql_query(
        """
        SELECT securities_trader, COUNT(DISTINCT stock_id) AS n_stocks,
               COUNT(DISTINCT trade_date) AS n_dates,
               MIN(trade_date) AS min_date, MAX(trade_date) AS max_date,
               SUM(CASE WHEN buy>0 OR sell>0 THEN 1 ELSE 0 END) AS n_active_rows
        FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id=?
        GROUP BY securities_trader
        """,
        conn, params=(SOURCE, TRADER_ID),
    )
    print("[PROFILE] 9661 basic profile:")
    print(prof.to_string())
    prof.to_csv(OUT_DIR / f"{PREFIX}profile.csv", index=False)

    print(f"[INFO] finding {TRADER_ID} stock universe (>= {args.min_total_active_days} active days since {DATA_START})...")
    stock_ids = find_stock_universe(conn)
    mega = load_mega_blacklist()
    n_mega_hit = len(set(stock_ids) & mega)
    print(f"[INFO] universe size: {len(stock_ids)} stocks (of which {n_mega_hit} are on mega-cap blacklist) · elapsed {time.time()-t0:.1f}s")

    t1 = time.time()
    print("[INFO] loading full price bars + IX0001 + branch daily...")
    bars_all = load_stock_bars(conn, stock_ids)
    ix = load_ix(conn)
    branch_all = load_branch_daily(conn, stock_ids)
    conn.close()
    print(f"[INFO] bars rows={len(bars_all):,} branch rows={len(branch_all):,} · elapsed {time.time()-t1:.1f}s")

    ix_lookups = {}
    for h in HORIZONS:
        lk = build_hold_lookup(ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy(), h)
        ix_lookups[h] = lk.set_index("signal_date")

    t2 = time.time()
    print("[INFO] computing causal daily momentum diagnostics + forward-return lookups per stock...")
    stock_lookups: dict[str, dict[int, pd.DataFrame]] = {}
    diag_frames = []
    latest_rows = []
    for sid, g in bars_all.groupby("stock_id"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) < MIN_HIST + max(long for _, long in WINDOW_PAIRS) // 2:
            continue
        br = branch_all[branch_all["stock_id"] == sid]
        diag = compute_causal_momentum(sid, g, br)
        diag_frames.append(diag)
        latest_rows.append(diag.iloc[-1].copy().to_dict() | {"close": g["close"].iloc[-1]})
        lookups = {}
        for h in HORIZONS:
            lk = build_hold_lookup(g["trade_date"].to_numpy(), g["open"].to_numpy(), g["close"].to_numpy(), h)
            if not lk.empty:
                lookups[h] = lk.set_index("signal_date")
        stock_lookups[sid] = lookups
    diag_all = pd.concat(diag_frames, ignore_index=True)
    print(f"[INFO] diagnostics computed for {diag_all['stock_id'].nunique()} stocks · elapsed {time.time()-t2:.1f}s")

    # ---- 現況快照：目前 cum>0 的所有持倉，各窗口組合的最新分類 ----
    latest_df = pd.DataFrame(latest_rows)
    current_holdings = latest_df[latest_df["cum"] > 0].copy()
    current_holdings["position_value_ntd"] = current_holdings["cum"] * current_holdings["close"]
    current_holdings["is_mega_blacklist"] = current_holdings["stock_id"].isin(mega)
    keep_cols = ["stock_id", "cum", "close", "position_value_ntd", "is_mega_blacklist"] + [
        f"state_{s}_{l}" for s, l in WINDOW_PAIRS
    ] + [f"z_short_{s}_{l}" for s, l in WINDOW_PAIRS]
    current_holdings = current_holdings[keep_cols].sort_values("position_value_ntd", ascending=False)
    current_holdings.to_csv(OUT_DIR / f"{PREFIX}current_holdings_snapshot.csv", index=False)
    print(f"\n[INFO] current holdings (cum>0) as of latest date: n={len(current_holdings)}")
    print(current_holdings.head(40).to_string())

    # ---- extract transition events per (short,long,category) ----
    t3 = time.time()
    all_events = []
    for short, long in WINDOW_PAIRS:
        for category in ["ACCEL", "DECEL"]:
            for sid, dg in diag_all.groupby("stock_id"):
                ev = extract_transition_events(dg, short, long, category)
                if not ev.empty:
                    all_events.append(ev)
    events_raw = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    print(f"[INFO] raw transition events across all (short,long,category) combos: {len(events_raw):,} · elapsed {time.time()-t3:.1f}s")
    events_raw.to_csv(OUT_DIR / f"{PREFIX}signal_events_raw.csv", index=False)

    t4 = time.time()
    events_with_returns = []
    for (short, long, category), sub in events_raw.groupby(["short", "long", "category"]):
        withret = attach_forward_returns(sub, stock_lookups, ix_lookups)
        if not withret.empty:
            events_with_returns.append(withret)
    events_final = pd.concat(events_with_returns, ignore_index=True) if events_with_returns else pd.DataFrame()
    print(f"[INFO] events with computable forward returns (all horizons available): {len(events_final):,} · elapsed {time.time()-t4:.1f}s")
    events_final.to_csv(OUT_DIR / f"{PREFIX}signal_events_with_returns.csv", index=False)

    rng = np.random.default_rng(RNG_SEED)
    summary_rows = []
    for (short, long, category), sub in events_final.groupby(["short", "long", "category"]):
        first_only = sub.sort_values("signal_date").drop_duplicates("stock_id", keep="first")
        for h in HORIZONS:
            summary_rows.append(
                {"short": short, "long": long, "category": category}
                | summarize(sub, h, rng, label=f"S{short}L{long}_{category}_all_events")
            )
            summary_rows.append(
                {"short": short, "long": long, "category": category}
                | summarize(first_only, h, rng, label=f"S{short}L{long}_{category}_first_per_stock")
            )
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_DIR / f"{PREFIX}summary_stats.csv", index=False)

    print("\n[INFO] === Summary stats (all events) ===")
    print(
        summary_df[summary_df["label"].str.endswith("all_events")][
            ["label", "horizon", "n", "n_stocks", "mean_r_adj_pct", "median_r_adj_pct", "win_rate_pct", "p_ttest", "p_wilcoxon", "boot_ci_lo", "boot_ci_hi"]
        ].to_string()
    )
    print("\n[INFO] === Summary stats (first signal per stock only) ===")
    print(
        summary_df[summary_df["label"].str.endswith("first_per_stock")][
            ["label", "horizon", "n", "n_stocks", "mean_r_adj_pct", "median_r_adj_pct", "win_rate_pct", "p_ttest", "p_wilcoxon", "boot_ci_lo", "boot_ci_hi"]
        ].to_string()
    )

    print(f"\n[OK] → {PREFIX}profile.csv")
    print(f"[OK] → {PREFIX}current_holdings_snapshot.csv")
    print(f"[OK] → {PREFIX}signal_events_raw.csv")
    print(f"[OK] → {PREFIX}signal_events_with_returns.csv")
    print(f"[OK] → {PREFIX}summary_stats.csv")
    print(f"[DONE] total elapsed {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
