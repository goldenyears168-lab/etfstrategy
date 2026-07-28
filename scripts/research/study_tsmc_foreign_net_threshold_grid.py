#!/usr/bin/env python3
"""台積電(2330) foreign_net「異常重賣超」門檻穩健性檢定 — 完整參數格 grid sweep。

背景：2026-07-17大跌後，發現5家外資分點3日賣超集中在2330。因分點資料只有
2024-07起（~2年），改用官方彙總表 stock_institutional_daily.foreign_net
（2015-01-05至2026-07-27，2820個交易日，11.5年）做長樣本驗證。

方法（walk-forward-safe，無look-ahead）：
  1. 對每一天 t，用「只到 t 為止」的 rolling window（長度 Wp，percentile lookback）
     計算 foreign_net[t] 在該視窗內的百分位（0% = 視窗內最負/賣最兇，100% = 最買超）。
     百分位計算用 window = foreign_net[t-Wp+1 : t+1]（含當天，不看未來）。
  2. 訊號日 = 百分位 <= threshold（例如 5% 門檻 → 當天賣超金額是過去 Wp 天內最兇的
     5%）。
  3. 對每個訊號日，計算「常態池」控制組：同一個 walk-forward window（長度 Wn，
     只看過去，不看未來）內，排除訊號日之後的『非訊號日』的前向超額報酬，取其
     平均，當作該訊號日的局部基準（local baseline，控制時變的波動regime）。
  4. 訊號日的「異常判別」= 訊號日前向超額報酬 - 局部常態池平均前向超額報酬。
     對所有訊號日的這個差值做單樣本t檢定（H0: 差值均值=0）。
  5. 前向超額報酬定義：T+1開盤進場、T+H收盤出場的2330報酬，扣除大盤(IX0001)同期
     報酬（未做beta調整，因子超過但避免多引入一個可調參數）。H 固定 = 5個交易日
     （對齊原始觀察的「大跌後3個交易日」規模，5天涵蓋更完整的中期反應/領先窗）。

Grid: threshold ∈ {5%,10%,15%,20%} × Wp(lookback視窗) ∈ {60,120,250}
      × Wn(常態池窗口) ∈ {60,120,250}  → 36 組合。

額外diagnostic：
  - 檢查 2026-07-17（及07-20/21/22/23/24）當天，foreign_net 在各 Wp 下的
    walk-forward百分位是多少（不為了讓它顯著而調參數，只是報告事實）。
  - 統計36組中有幾組在5%顯著門檻下顯著，跟「36次純雜訊檢定预期~1.8組顯著
    （36*0.05）」做比較，類比242家分點篩選時的「12/242」邏輯。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect
from market_benchmark import load_benchmark_close

STOCK_ID = "2330"
HOLD_DAYS = 5
THRESHOLDS = [0.05, 0.10, 0.15, 0.20]
WP_LIST = [60, 120, 250]  # lookback視窗：算foreign_net百分位用
WN_LIST = [60, 120, 250]  # 常態池窗口：算局部基準用
MIN_POOL_DAYS = 20
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_data(conn) -> pd.DataFrame:
    # 注意：foreign_net 是「股數」而非台幣金額（用 foreign_net/close_price 反推
    # implied_shares 量級核對過：44,183,964 這個量級對台積電單日成交量而言合理，
    # 若當TWD金額則implied share count會小到不合理，故確認為股數）。
    fn = pd.read_sql_query(
        """
        SELECT trade_date, foreign_net, close_price
        FROM stock_institutional_daily
        WHERE stock_id = ? AND source = 'finmind'
        ORDER BY trade_date
        """,
        conn,
        params=(STOCK_ID,),
    )
    bars = pd.read_sql_query(
        """
        SELECT trade_date, open, close
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind'
        ORDER BY trade_date
        """,
        conn,
        params=(STOCK_ID,),
    )
    bench = load_benchmark_close(conn, code="IX0001")
    bench_df = bench.reset_index()
    bench_df.columns = ["trade_date", "bench_close"]

    df = fn.merge(bars, on="trade_date", how="inner").merge(bench_df, on="trade_date", how="inner")
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def compute_forward_excess(df: pd.DataFrame, hold: int) -> np.ndarray:
    """T+1開盤進場，T+hold收盤出場，扣大盤同期報酬。無look-ahead於『訊號判斷』
    階段使用（此為結果變數，訊號判斷本身不可用到它）。"""
    n = len(df)
    open_ = df["open"].to_numpy()
    close_ = df["close"].to_numpy()
    bench = df["bench_close"].to_numpy()
    fwd = np.full(n, np.nan)
    for i in range(n):
        i_entry = i + 1
        i_exit = i + hold
        if i_exit >= n:
            continue
        entry_px = open_[i_entry]
        exit_px = close_[i_exit]
        b0 = bench[i_entry]
        b1 = bench[i_exit]
        if not (entry_px > 0 and exit_px > 0 and b0 > 0 and b1 > 0):
            continue
        stock_ret = exit_px / entry_px - 1.0
        bench_ret = b1 / b0 - 1.0
        fwd[i] = stock_ret - bench_ret
    return fwd


def rolling_percentile_walk_forward(values: np.ndarray, window: int) -> np.ndarray:
    """percentile[i] = 該日值在 values[i-window+1 : i+1] 內的百分位（含當天，只看過去）。
    需要滿窗 (min_periods = window) 才輸出，避免早期樣本視窗太短失真。"""
    n = len(values)
    pct = np.full(n, np.nan)
    for i in range(n):
        if i + 1 < window:
            continue
        win = values[i + 1 - window : i + 1]
        # 百分位：<=當天值的比例（0~1），越低代表當天賣超越極端
        pct[i] = np.mean(win <= win[-1])
    return pct


def build_signal_pool_diffs(
    pct: np.ndarray,
    fwd_excess: np.ndarray,
    threshold: float,
    wn: int,
    hold: int,
    min_pool_days: int = MIN_POOL_DAYS,
) -> np.ndarray:
    """對每個訊號日 i：局部常態池 = [i-wn+1, i-hold] 範圍內，非訊號日 且 fwd_excess
    非NaN 的日子。上界用 i-hold（而非 i-1）是為了避免常態池本身的「前向報酬」在
    day i 當下其實還沒實現完（day j 的 H日前向報酬要到 j+H 才算數，若 j+H > i，
    等於用了day i當下還看不到的未來資訊）——這是本輪研究特別要求要避開的
    look-ahead bug，即使這裡只是統計檢定而非真實交易規則，也照一樣的標準處理。"""
    n = len(pct)
    is_signal = (~np.isnan(pct)) & (pct <= threshold)
    diffs = []
    for i in np.where(is_signal)[0]:
        if np.isnan(fwd_excess[i]):
            continue
        lo = max(0, i - wn + 1)
        hi = i - hold + 1  # exclusive upper bound: 最後一個 j 需滿足 j+hold <= i
        pool_idx = np.arange(lo, max(lo, hi))
        if pool_idx.size == 0:
            continue
        pool_is_signal = is_signal[pool_idx]
        pool_fwd = fwd_excess[pool_idx]
        valid_pool = pool_idx[(~pool_is_signal) & (~np.isnan(pool_fwd))]
        if valid_pool.size < min_pool_days:
            continue
        pool_mean = fwd_excess[valid_pool].mean()
        diffs.append(fwd_excess[i] - pool_mean)
    return np.array(diffs)


def mann_whitney_auc(signal_vals: np.ndarray, control_vals: np.ndarray) -> float | None:
    """AUC = P(control > signal)（單邊：訊號日報酬是否系統性低於控制組）。
    0.5=無判別力，>0.5=訊號日報酬偏低（賣壓預示下跌/延續），<0.5=訊號日報酬偏高（反彈/均值回歸）。"""
    if len(signal_vals) < 2 or len(control_vals) < 2:
        return None
    try:
        u_stat, _ = stats.mannwhitneyu(control_vals, signal_vals, alternative="two-sided")
    except ValueError:
        return None
    auc = u_stat / (len(control_vals) * len(signal_vals))
    return float(auc)


def summarize_cell(diffs: np.ndarray, signal_fwd: np.ndarray, all_fwd_pool: np.ndarray) -> dict:
    n = len(diffs)
    if n < 5:
        return dict(n=n, mean_diff_pct=None, t=None, p=None, sig5=False, auc=None)
    t_stat, p_val = stats.ttest_1samp(diffs, 0)
    auc = mann_whitney_auc(signal_fwd, all_fwd_pool)
    return dict(
        n=n,
        mean_diff_pct=round(float(diffs.mean()) * 100, 3),
        median_diff_pct=round(float(np.median(diffs)) * 100, 3),
        t=round(float(t_stat), 3),
        p=round(float(p_val), 4),
        sig5=bool(p_val < 0.05),
        auc=round(auc, 3) if auc is not None else None,
    )


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    df = load_data(conn)
    conn.close()

    print(f"[INFO] {STOCK_ID} 合併後可用交易日: {len(df)}  範圍 {df['trade_date'].min()} ~ {df['trade_date'].max()}")

    fwd_excess = compute_forward_excess(df, HOLD_DAYS)
    valid_fwd_mask = ~np.isnan(fwd_excess)
    print(f"[INFO] 前向超額報酬(H={HOLD_DAYS})可用樣本: {valid_fwd_mask.sum()} / {len(df)}")

    foreign_net = df["foreign_net"].to_numpy()

    # 先計算三種 Wp 的 walk-forward 百分位（獨立於 threshold/Wn）
    pct_by_wp = {wp: rolling_percentile_walk_forward(foreign_net, wp) for wp in WP_LIST}

    results = []
    for wp in WP_LIST:
        pct = pct_by_wp[wp]
        for threshold in THRESHOLDS:
            is_signal_global = (~np.isnan(pct)) & (pct <= threshold)
            n_signal_global = int(is_signal_global.sum())
            for wn in WN_LIST:
                diffs = build_signal_pool_diffs(pct, fwd_excess, threshold, wn, HOLD_DAYS)
                sig_idx = np.where(is_signal_global & valid_fwd_mask)[0]
                signal_fwd = fwd_excess[sig_idx]
                nonsig_idx = np.where((~is_signal_global) & valid_fwd_mask)[0]
                control_pool_full = fwd_excess[nonsig_idx]
                cell = summarize_cell(diffs, signal_fwd, control_pool_full)
                cell.update(
                    dict(
                        Wp=wp,
                        threshold_pct=int(threshold * 100),
                        Wn=wn,
                        n_signal_global=n_signal_global,
                    )
                )
                results.append(cell)

    out = pd.DataFrame(results)
    cols = ["Wp", "threshold_pct", "Wn", "n_signal_global", "n", "mean_diff_pct",
            "median_diff_pct", "t", "p", "sig5", "auc"]
    out = out[cols]

    print("\n=== 2330 foreign_net 異常重賣超 門檻×視窗 grid (36組合) ===")
    print(out.to_string(index=False))

    n_total = len(out)
    n_sig = int(out["sig5"].sum())
    expected_noise = n_total * 0.05
    print(f"\n[SUMMARY] 36組合中 {n_sig} 組在 p<0.05 顯著（{n_sig/n_total*100:.1f}%）")
    print(f"[SUMMARY] 若全為雜訊，5%門檻下純機率預期顯著組數 ≈ {expected_noise:.1f} 組（{expected_noise/n_total*100:.1f}%）")
    print("[SUMMARY] 類比：242家分點廣度掃描時，純雜訊預期 12/242 (~5%) 顯著")

    # 顯著組的方向是否一致？
    sig_rows = out[out["sig5"]]
    if len(sig_rows) > 0:
        pos = int((sig_rows["mean_diff_pct"] > 0).sum())
        neg = int((sig_rows["mean_diff_pct"] < 0).sum())
        print(f"[SUMMARY] 顯著組中：訊號日超額報酬『高於』常態池 {pos} 組 vs 『低於』常態池 {neg} 組")

    out_path = OUT_DIR / "tsmc_foreign_net_threshold_grid.csv"
    out.to_csv(out_path, index=False)
    print(f"\n[OK] 寫入 {out_path}")

    # --- 額外 diagnostic：7/17事件當天 & 前後幾天的 walk-forward 百分位 ---
    print("\n=== Diagnostic: 2026-07 事件窗口的 walk-forward 百分位（不為了讓它顯著而調參數）===")
    event_dates = ["2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
                    "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-27"]
    date_idx = {d: i for i, d in enumerate(df["trade_date"].tolist())}
    diag_rows = []
    for d in event_dates:
        if d not in date_idx:
            continue
        i = date_idx[d]
        notional_twd = foreign_net[i] * df["close_price"].to_numpy()[i]
        row = {"trade_date": d, "foreign_net_股數萬": round(foreign_net[i] / 1e4, 1),
               "foreign_net_名目金額_億": round(notional_twd / 1e8, 1)}
        for wp in WP_LIST:
            p = pct_by_wp[wp][i]
            row[f"pct_Wp{wp}"] = round(float(p) * 100, 1) if not np.isnan(p) else None
        diag_rows.append(row)
    diag_df = pd.DataFrame(diag_rows)
    print(diag_df.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
