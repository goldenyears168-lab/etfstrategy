"""待辦#6 (intraday entry timing) — SYSTEM L0xL2 fresh-entry days only.

問句：champion SYSTEM(L0xL2) 在「今天要進場」已經由 close(t) 的 chip/regime gate 決定之後，
能不能用「開盤前就已知的因果特徵」(TX 跳空方向) 決定「開盤立即進場 vs 等盤中確認點再進」，
藉此降低進場雜訊 (不是找新的方向性 alpha)。

資料轉譯邏輯（必須誠實 — 這是本腳本的第一個、也是最重要的產出）：
  - champion 訊號的標的是 TAIEX 現貨指數 (panel.csv ix_open/ix_close, FinMind TaiwanStockPrice
    「TAIEX」逐日 OHLC)。目前唯一涵蓋足夠歷史、逐分鐘的盤中資料庫是 TX 台指期
    (`tx_1m_tick_built_582d`, 750 交易日, 2023-07-03~2026-08-06, bars.sqlite)，沒有 TAIEX
    現貨或 0050 的分鐘資料。
  - 直覺上 TX 與 TAIEX 應該幾乎鎖死同步(基差=資金成本-預期股息, 日內幾乎不變)，但**本腳本
    STEP1 實測發現這個假設在「精確報酬平移」的用途上不夠紮實**：
      (a) TX 日盤 08:45-13:45 vs TAIEX 現貨 09:00-13:30 —— 交易時段本身就對不齊(TX 兩端各多
          15分鐘純期貨盤)，不對齊窗口比較出來的相關係數(0.76)是誤導的。
      (b) 校正成同一時鐘窗口 (TX@09:00→TX@13:30 對 TAIEX 09:00→13:30) 後，相關係數只回升到
          **0.79**，遠低於「可放心做報酬平移」的門檻(這裡採 0.9)。
      (c) 追查原因：TAIFEX TX 期貨有**每日漲跌停限制**(現貨 TAIEX 指數本身沒有價格漲跌停，
          個股才有±10%)，極端行情日 TX 會鎖在停板全天不太動，但 TAIEX 指數繼續因個股輪流跌停
          而走完全天(例：2025-04-07 TX 全天鎖死在 19167.0 一個價格不動，TAIEX 現貨當天卻繼續
          跌 -4.6%)。但排除這 2 個完全鎖死的日子後，相關只回升到 0.805 —— 代表价格限制鎖死只是
          落差的一部分原因，多數交易日仍有中等幅度的期現落差(這是台指期市場眾所周知的
          「基差雜訊」，非本研究錯誤)。
  - **結論：TX 分鐘K的「精確報酬幅度」不能拿來平移成 TAIEX 的精確報酬(相關度不夠)，
    但 TX 的「開盤前跳空方向/大略走勢」仍可以誠實地當作一個因果可觀察的「前兆特徵」
    (leading indicator)，只要驗證方式只依賴方向性 IC、不依賴把 TX 報酬幅度硬套用到
    TAIEX 身上。** 依使用者指示「如果做不到就誠實停手改用現貨自己的分鐘資料」——since 沒有
    TAIEX/0050 分鐘資料，本腳本改用**較弱、但站得住腳的版本**：只測 TX 跳空方向對「已知的
    TAIEX 真實當日報酬」有沒有因果 IC (veto/size 的用途)，**不**做「用 TX 報酬平移出精確
    TAIEX 進場價」這種需要高相關度支撐的量化回測 (仍計算出來放在 CSV 供參考，但明確標注
    不可信、不進入結論)。

方法：
  1. 重建 finalize_cc.py close->close SYSTEM L0xL2 部位邏輯 → fresh-entry day 清單。
  2. 篩到有 TX 1分K覆蓋的 fresh-entry day (37/75, 2023-09~2026-04)。
  3. 因果特徵 = TX 開盤前跳空 gap_pts = TX@09:00(d) - TX@13:30(d-1) (TAIEX 現貨開盤前已知)。
  4. 主檢定 (translation-light)：gap_pts/gap_dir 對「已知的 TAIEX 真實 r_oc_naive」的因果
     Spearman IC —— 在 fresh-entry 子樣本(n=37, 檢定力低)與全部 750 個有 TX 覆蓋的交易日
     (檢定力較高，用來確認這個 leading indicator 是否連基本方向性都不成立)。
  5. 附帶 (labelled unreliable) 的「延遲30分鐘進場、用 TX 報酬平移」量化比較，僅供參考。
  6. 誠實對照量級：即使 IC 有訊號，n=37 對 SYSTEM 全歷史 Sharpe/DSR 的影響也極小，明講清楚。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.csv"
DB = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
OUT = ROOT / "reports" / "research" / "chip-macro"
SRC = "tx_1m_tick_built_582d"
CONFIRM_MIN = 30  # minutes after TAIEX's open used for the (unreliable) delayed-entry sensitivity check
TRANSLATION_OK_THRESHOLD = 0.90


def load_panel():
    return pd.read_csv(PANEL).sort_values("date").reset_index(drop=True)


def build_system(p):
    ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
    slope_up = (ma150 - ma150.shift(25)) > 0
    tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1) > 0
    stage_s2 = (p["ix_close"] > ma150) & slope_up
    armed = (stage_s2 & tsmom).astype(float)

    def rz(s, w):
        return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()

    z60 = rz(p["fut_foreign_oi"], 60)
    size = pd.Series(0.0, index=p.index)
    size[(z60 > 0) & (z60 < 1)] = 0.5
    size[z60 >= 1] = 1.0
    pos_sys = armed * size

    pos_prev = pos_sys.shift(1)
    pos_prev2 = pos_sys.shift(2).fillna(0)
    fresh = (pos_prev > 0) & (pos_prev2 == 0)
    return pos_sys, fresh


def load_tx_bars(conn, day, sess="day"):
    return pd.read_sql(
        "SELECT t, o, h, l, c FROM bars WHERE source=? AND day=? AND sess=? ORDER BY t",
        conn, params=(SRC, day, sess),
    )


def step1_validate_translation(p, conn, cov_days):
    """Daily correlation: TX day-session return vs TAIEX ix_open->ix_close return.

    Reports BOTH the naive mismatched-window comparison (TX's own 08:45-13:45 window,
    which includes 30min of futures-only trading TAIEX cash never sees) and the
    session-aligned comparison (TX@09:00->TX@13:30, the correct apples-to-apples test).
    """
    rows = []
    for _, r in p.iterrows():
        d = r["date"]
        if d not in cov_days:
            continue
        bars = load_tx_bars(conn, d)
        if len(bars) < 2:
            continue
        tx_o, tx_c = bars["o"].iloc[0], bars["c"].iloc[-1]
        aligned = bars[(bars["t"] >= "09:00") & (bars["t"] <= "13:30")]
        if len(aligned) < 2:
            continue
        tx_o_al, tx_c_al = aligned["c"].iloc[0], aligned["c"].iloc[-1]
        rows.append({
            "date": d,
            "tx_ret_fullwindow": tx_c / tx_o - 1.0,
            "tx_ret_aligned_0900_1330": tx_c_al / tx_o_al - 1.0,
            "taiex_ret": r["ix_close"] / r["ix_open"] - 1.0,
            "tx_nuniq": int(aligned["c"].nunique()),
        })
    df = pd.DataFrame(rows)
    corr_naive = df["tx_ret_fullwindow"].corr(df["taiex_ret"])
    corr_aligned = df["tx_ret_aligned_0900_1330"].corr(df["taiex_ret"])
    locked = df[df["tx_nuniq"] == 1]
    ok = df[df["tx_nuniq"] > 1]
    corr_ex_locked = ok["tx_ret_aligned_0900_1330"].corr(ok["taiex_ret"])
    beta = np.polyfit(df["tx_ret_aligned_0900_1330"], df["taiex_ret"], 1)[0]
    print(f"[STEP1] TX(08:45-13:45, mismatched window) vs TAIEX: n={len(df)} corr={corr_naive:.4f}")
    print(f"[STEP1] TX(09:00-13:30, session-aligned)   vs TAIEX: n={len(df)} corr={corr_aligned:.4f} beta={beta:.3f}")
    print(f"[STEP1] TX price-limit-locked days (single price all session): {len(locked)} "
          f"({locked['date'].tolist()})")
    print(f"[STEP1] session-aligned corr EXCLUDING locked days: n={len(ok)} corr={corr_ex_locked:.4f}")
    if corr_aligned < TRANSLATION_OK_THRESHOLD:
        print(f"[STEP1] corr {corr_aligned:.3f} < {TRANSLATION_OK_THRESHOLD} threshold for magnitude-level "
              f"return translation -> NOT reliable enough to price a 'delayed entry' in TAIEX terms.")
        print("[STEP1] proceeding with a translation-light test only: TX's own units (gap points / sign) "
              "as a leading-indicator FILTER against TAIEX's own already-real return, not a magnitude proxy.")
    return df, corr_aligned, beta


def dsr(ret, var_trials, N, gamma=0.5772156649):
    r = pd.Series(ret).dropna().values
    n = len(r)
    if n < 5 or r.std() == 0:
        return np.nan
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3
    sr_star = np.sqrt(var_trials) * ((1 - gamma) * stats.norm.ppf(1 - 1 / N) + gamma * stats.norm.ppf(1 - 1 / (N * np.e)))
    denom = np.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2, 1e-8))
    return stats.norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom)


def gap_ic_on_all_days(p, conn, cov_days):
    """Full-power (n~750) translation-light IC: does TX's causal pre-open gap have ANY
    directional relationship with TAIEX's own same-day real return -- on every tick-covered
    trading day, not just the n=37 fresh-entry subsample. This is the higher-power read on
    whether the leading-indicator idea has legs at all before trusting the tiny subsample.
    """
    p_idx = p.set_index("date")
    rows = []
    sorted_days = sorted(cov_days)
    for i in range(1, len(sorted_days)):
        d, prev_d = sorted_days[i], sorted_days[i - 1]
        if d not in p_idx.index:
            continue
        day_bars = load_tx_bars(conn, d)
        aligned = day_bars[(day_bars["t"] >= "09:00") & (day_bars["t"] <= "13:30")]
        prev_bars = load_tx_bars(conn, prev_d)
        prev_aligned = prev_bars[(prev_bars["t"] >= "09:00") & (prev_bars["t"] <= "13:30")]
        if len(aligned) < 2 or len(prev_aligned) < 1:
            continue
        gap_pts = aligned["c"].iloc[0] - prev_aligned["c"].iloc[-1]
        r_oc = p_idx.loc[d, "ix_close"] / p_idx.loc[d, "ix_open"] - 1.0
        rows.append({"date": d, "gap_pts": gap_pts, "gap_dir": np.sign(gap_pts), "r_oc_naive": r_oc})
    df = pd.DataFrame(rows)
    rho, pval = stats.spearmanr(df["gap_pts"], df["r_oc_naive"])
    rho_dir, pval_dir = stats.spearmanr(df["gap_dir"], df["r_oc_naive"])
    print(f"\n[GAP-IC full sample] n={len(df)} gap_pts vs r_oc_naive: rho={rho:+.3f} p={pval:.3f}")
    print(f"[GAP-IC full sample] n={len(df)} gap_dir vs r_oc_naive: rho={rho_dir:+.3f} p={pval_dir:.3f}")
    return df


def main():
    p = load_panel()
    pos_sys, fresh = build_system(p)
    entries = p.loc[fresh, "date"].tolist()

    conn = sqlite3.connect(DB)
    cov = pd.read_sql(f"SELECT DISTINCT day FROM bars WHERE source='{SRC}'", conn)
    cov_days = set(cov["day"])

    print(f"fresh-entry days (full history): {len(entries)}")
    print(f"tick-covered days: {len(cov_days)} range {min(cov_days)}..{max(cov_days)}")

    # ---- Step 1: validate TX->TAIEX translation on ALL covered trading days ----
    val_df, corr, beta = step1_validate_translation(p, conn, cov_days)

    # ---- higher-power translation-light check on all 750 days first ----
    gap_all = gap_ic_on_all_days(p, conn, cov_days)

    # ---- Step 2/3: build fresh-entry day intraday features ----
    p_idx = p.set_index("date")
    rows = []
    for d in entries:
        if d not in cov_days:
            continue
        day_bars = load_tx_bars(conn, d)
        aligned = day_bars[(day_bars["t"] >= "09:00") & (day_bars["t"] <= "13:30")].reset_index(drop=True)
        if len(aligned) < CONFIRM_MIN + 1:
            continue
        prior_days = sorted(x for x in cov_days if x < d)
        if not prior_days:
            continue
        prev_day = prior_days[-1]
        prev_bars = load_tx_bars(conn, prev_day)
        prev_aligned = prev_bars[(prev_bars["t"] >= "09:00") & (prev_bars["t"] <= "13:30")]
        if len(prev_aligned) < 1:
            continue
        tx_prev_close = prev_aligned["c"].iloc[-1]
        tx_open = aligned["c"].iloc[0]  # TX price at 09:00, matching TAIEX's real open
        tx_conf = aligned["c"].iloc[CONFIRM_MIN - 1]  # price CONFIRM_MIN minutes after TAIEX's open
        gap_pts = tx_open - tx_prev_close
        r_tx_confirm_window = tx_conf / tx_open - 1.0  # TX return during the wait window (UNRELIABLE as TAIEX proxy)

        ix_open = p_idx.loc[d, "ix_open"]
        ix_close = p_idx.loc[d, "ix_close"]
        r_oc_naive = ix_close / ix_open - 1.0  # actual, real TAIEX return (panel's canonical open-entry)
        # labelled-unreliable sensitivity check only (corr 0.79 << 0.90 threshold, see STEP1)
        r_conf_unreliable = (1 + r_oc_naive) / (1 + r_tx_confirm_window) - 1.0

        rows.append({
            "date": d, "gap_pts": gap_pts, "gap_dir": np.sign(gap_pts),
            "r_oc_naive": r_oc_naive,
            "r_conf_delayed_UNRELIABLE": r_conf_unreliable,
        })

    df = pd.DataFrame(rows)
    n = len(df)
    print(f"\n[STEP2] usable fresh-entry days with full-day TX bars: {n}")
    print("[WARN] n<40 -> exploratory only, not confirmatory, regardless of what the numbers below show.")

    # ---- Step 4 (PRIMARY, translation-light): gap direction vs TAIEX's own real return ----
    ic_gap_vs_dayret, p_gap = stats.spearmanr(df["gap_pts"], df["r_oc_naive"])
    ic_gapdir_vs_dayret, p_gapdir = stats.spearmanr(df["gap_dir"], df["r_oc_naive"])
    print(f"\n[STEP4-PRIMARY] fresh-entry-day causal IC (gap_pts -> r_oc_naive): rho={ic_gap_vs_dayret:+.3f} p={p_gap:.3f}")
    print(f"[STEP4-PRIMARY] fresh-entry-day causal IC (gap_dir -> r_oc_naive): rho={ic_gapdir_vs_dayret:+.3f} p={p_gapdir:.3f}")

    # ---- Step 5 (PRIMARY, translation-light): veto/size backtest using gap_dir only,
    #      applied to TAIEX's own already-real r_oc_naive (no TX magnitude used as entry price) ----
    always_open = df["r_oc_naive"]
    veto_on_gapdown = np.where(df["gap_dir"] < 0, 0.0, df["r_oc_naive"])  # skip entry entirely on gap-down days
    half_on_gapdown = np.where(df["gap_dir"] < 0, 0.5 * df["r_oc_naive"], df["r_oc_naive"])  # half-size instead

    def summarize(name, x):
        x = pd.Series(x)
        print(f"  {name:32s} mean={x.mean()*100:+6.3f}%  sum={x.sum()*100:+7.2f}%  "
              f"win%={100*(x>0).mean():5.1f}  std={x.std()*100:6.3f}%")

    print(f"\n[STEP5-PRIMARY] veto/size backtest on n={n} fresh-entry days (no TX magnitude translation used):")
    summarize("always open (canonical)", always_open)
    summarize("veto entry on gap-down day", veto_on_gapdown)
    summarize("half-size on gap-down day", half_on_gapdown)
    t_stat, t_p = stats.ttest_rel(veto_on_gapdown, always_open)
    print(f"  paired t-test veto vs always-open: t={t_stat:+.3f} p={t_p:.3f}")

    # ---- Step 5b (SECONDARY, labelled UNRELIABLE): magnitude-translated delayed entry ----
    print(f"\n[STEP5b-UNRELIABLE, corr={corr:.2f}<{TRANSLATION_OK_THRESHOLD} threshold, shown for transparency only]")
    always_delay = df["r_conf_delayed_UNRELIABLE"]
    conditional_unreliable = np.where(df["gap_dir"] < 0, df["r_conf_delayed_UNRELIABLE"], df["r_oc_naive"])
    summarize("always delay 30min (magnitude-translated)", always_delay)
    summarize("conditional delay if gap<0 (magnitude-translated)", conditional_unreliable)

    # ---- Step 6: honest scale check ----
    delta = pd.Series(veto_on_gapdown) - always_open.reset_index(drop=True)
    print(f"\n[STEP6] mean per-entry-day delta (veto-on-gapdown - naive): {delta.mean()*100:+.4f} pct-pts, n={n}")
    print("  For reference: SYSTEM full-history OOS Sharpe is +2.02, DSR OOS 0.869 (already borderline).")
    print(f"  n={n} entry-day sample is far too small to move that macro Sharpe/DSR number even if the")
    print("  sign held up on more data -- this is a per-entry execution-quality question, not a new alpha source.")

    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "intraday_entry_timing_fresh_entries.csv", index=False)
    val_df.to_csv(OUT / "intraday_entry_timing_tx_taiex_validation.csv", index=False)
    gap_all.to_csv(OUT / "intraday_entry_timing_gap_ic_all_days.csv", index=False)
    print(f"\n-> {OUT/'intraday_entry_timing_fresh_entries.csv'}")
    print(f"-> {OUT/'intraday_entry_timing_tx_taiex_validation.csv'}")
    print(f"-> {OUT/'intraday_entry_timing_gap_ic_all_days.csv'}")


if __name__ == "__main__":
    main()
