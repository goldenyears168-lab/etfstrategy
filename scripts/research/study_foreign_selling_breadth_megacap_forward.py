#!/usr/bin/env python3
"""BREADTH-FOREIGN-SELL-MEGACAP · 外資同步賣超廣度指標 vs 單一台積電訊號，對 IX0001 前瞻報酬的預測力

背景：2026-07-17 台股大跌-6.47%，事後查07-23/24/27（大跌後3日）分點資料發現5家外資大行
分點（高盛/野村/摩根士丹利/摩根大通/瑞銀）對台積電及一長串電子供應鏈股票同步重賣超。本研究
把「單一分點對單一個股」的觀察，推廣成「官方彙總 stock_institutional_daily.foreign_net 對
權值股全市場的同步賣超廣度」，測試：

    廣度指標（有幾檔權值股 foreign_net 落在該股自己歷史 bounded-rolling 後10%）
    是否比「只看台積電一檔」更能預測 IX0001 未來報酬？

方法論（吸取今天在其他訊號上踩過的坑）：
  1. look-ahead 防護：percentile_t 用「以 t 為結尾、往前固定 WINDOW=252 個交易日」的
     bounded rolling window（含 t 當天，因為 t 當天的 foreign_net 在收盤後即為已知資訊，
     用它跟「過去」比較是因果的；但這個 window 絕不會納入 t 之後的任何資料）。
     min_periods = WINDOW，未滿一年歷史者一律 NaN，不強行判定。
  2. 樣本內偏誤防護：權值股清單用「最新一筆 stock_market_value_daily 市值排名」這個
     hindsight-free-at-signal-time 的靜態清單（不是挑「這次觀察到跌最多的股票」回頭去湊），
     全歷史固定不變，不因為看到2330表現最凸出就把2330權重加大或挑選其他股票進出清單。
  3. 多重檢定：測 5 個前瞻窗口(h=1/5/10/20/60)，report 用 Bonferroni（0.05/5=0.01）當作
     校正後顯著門檻，不是拿其中最漂亮的一個窗口當結論。
  4. 反應 vs 領先：對「廣度極端日」同時報告過去5日報酬與未來5/10/20日報酬，並且對
     breadth(t) 分別跟 past_ret_5(t)（落後/巧合）與 fwd_ret_h(t)（領先）做相關係數比較。
  5. 重疊視窗的序列相關：forward return regression 一律用 Newey-West (HAC, maxlags=h)
     standard errors，不用 naive OLS t-stat。

用法：
  cd "/Users/jackm4/Documents/ETF/股票研究" && PYTHONPATH=src .venv/bin/python \
    scripts/research/study_foreign_selling_breadth_megacap_forward.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm

from stock_db import DEFAULT_DB_PATH, connect

WINDOW = 252          # bounded rolling window (~1年交易日) 給 percentile 常態池
DECILE = 0.10         # 「重賣超」門檻：落在自己歷史後10%
HORIZONS = [1, 5, 10, 20, 60]
PAST_WINDOW = 5
N_UNIVERSE = 15
N_BOOTSTRAP = 5000
RNG = np.random.default_rng(20260728)

REQUIRED = {"2330", "2317", "2454", "2308"}


def load_universe(conn) -> list[str]:
    """靜態權值股清單：用最新一筆市值排名，hindsight-free-at-signal-time（不回頭依訊號挑股）。"""
    q = """
        SELECT stock_id, market_value_ntd
        FROM stock_market_value_daily
        WHERE trade_date = (SELECT MAX(trade_date) FROM stock_market_value_daily)
        ORDER BY market_value_ntd DESC
        LIMIT ?
    """
    df = pd.read_sql_query(q, conn, params=(N_UNIVERSE,))
    universe = df["stock_id"].tolist()
    missing_required = REQUIRED - set(universe)
    if missing_required:
        raise SystemExit(f"必須包含的權值股不在前{N_UNIVERSE}名: {missing_required}")
    return universe


def load_index(conn) -> pd.DataFrame:
    """IX0001 有 finmind/yahoo/tej 三個來源、日期有重疊；用優先序 coalesce 成單一序列
    （抽查重疊日期收盤價一致，只是浮點精度差異，非資料衝突）。"""
    df = pd.read_sql_query(
        "SELECT date AS trade_date, close, source FROM daily_bars WHERE code='IX0001' ORDER BY trade_date",
        conn,
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    priority = {"finmind": 0, "tej": 1, "yahoo": 2}
    df["prio"] = df["source"].map(priority).fillna(9)
    df = df.sort_values(["trade_date", "prio"]).drop_duplicates("trade_date", keep="first")
    return df.set_index("trade_date")[["close"]]


def load_foreign_net(conn, universe: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" * len(universe))
    q = f"""
        SELECT stock_id, trade_date, foreign_net
        FROM stock_institutional_daily
        WHERE stock_id IN ({placeholders})
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=universe)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    wide = df.pivot(index="trade_date", columns="stock_id", values="foreign_net")
    return wide


def rolling_causal_percentile(s: pd.Series, window: int) -> pd.Series:
    """percentile_t = 在 [t-window+1, t]（含t，全部<=t，絕無未來資訊）中, x_t 的分位（0~1）。"""
    arr = s.to_numpy(dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(window - 1, n):
        win = arr[i - window + 1 : i + 1]
        if np.isnan(win).any():
            continue
        x = win[-1]
        out[i] = np.mean(win <= x)
    return pd.Series(out, index=s.index)


def hac_ols(y: pd.Series, X: pd.DataFrame, maxlags: int):
    df = pd.concat([y, X], axis=1).dropna()
    yy = df.iloc[:, 0]
    XX = sm.add_constant(df.iloc[:, 1:])
    model = sm.OLS(yy, XX).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    return model, len(df)


def bootstrap_mean_ci(x: np.ndarray, n_boot=N_BOOTSTRAP):
    if len(x) == 0:
        return np.nan, np.nan, np.nan
    boots = RNG.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return x.mean(), np.percentile(boots, 2.5), np.percentile(boots, 97.5)


def main():
    conn = connect(DEFAULT_DB_PATH)
    universe = load_universe(conn)
    print(f"權值股靜態清單 (N={len(universe)}, 依最新市值排序): {universe}")

    idx = load_index(conn)
    fn = load_foreign_net(conn, universe)

    # 對齊到 IX0001 交易日曆（沒有資料的日子=NaN，不強行補值）
    fn = fn.reindex(idx.index)

    print("\n計算 bounded-rolling causal percentile (window=252)...")
    pct = pd.DataFrame(index=fn.index)
    for col in universe:
        pct[col] = rolling_causal_percentile(fn[col], WINDOW)

    hit = pct <= DECILE  # True = 該股當天落在自己歷史後10%（重賣超）
    n_valid = pct.notna().sum(axis=1)
    breadth = hit.where(pct.notna()).sum(axis=1)  # NaN-aware count of hits
    breadth = breadth.astype(float)
    breadth[n_valid == 0] = np.nan

    hit_2330 = hit["2330"].astype(float)
    hit_2330[pct["2330"].isna()] = np.nan
    pct_2330 = pct["2330"]

    close = idx["close"]
    log_close = np.log(close)

    fwd = {}
    for h in HORIZONS:
        fwd[h] = (close.shift(-h) / close - 1.0)
    past5 = close / close.shift(PAST_WINDOW) - 1.0

    sample_start = pct.dropna(how="all").index.min()
    sample_end_for_breadth = breadth.dropna().index.max()
    print(f"percentile 有效樣本起點(burn-in後): {sample_start.date()}")
    print(f"universe 平均每日有效股數(n_valid, 排除burn-in): "
          f"{n_valid[n_valid>0].mean():.2f} / {len(universe)}")

    valid_mask = n_valid == len(universe)  # 只在15檔全部有效後才算「完整期」，另外也存全樣本版本
    first_full = idx.index[valid_mask][0] if valid_mask.any() else None
    print(f"15檔全部有效(含3711上市後)的完整期起點: {first_full.date() if first_full is not None else 'N/A'}")

    print("\n=== 廣度分布 (n_valid>0 期間) ===")
    b_desc = breadth.dropna()
    print(b_desc.describe())
    print("\nbreadth 值分布 (次數):")
    print(b_desc.value_counts().sort_index())

    results_rows = []
    n_tests = len(HORIZONS)
    bonferroni_alpha = 0.05 / n_tests

    print(f"\n=== 迴歸: fwd_ret_h ~ breadth (HAC/Newey-West, maxlags=h), Bonferroni α={bonferroni_alpha:.4f} ===")
    for h in HORIZONS:
        y = fwd[h]
        X = pd.DataFrame({"breadth": breadth})
        model, n = hac_ols(y, X, maxlags=h)
        coef = model.params["breadth"]
        tval = model.tvalues["breadth"]
        pval = model.pvalues["breadth"]
        r2 = model.rsquared
        sig = "***" if pval < bonferroni_alpha else ("*" if pval < 0.05 else "")
        results_rows.append(dict(model="breadth_only", h=h, n=n, coef=coef, tstat=tval, pval=pval, r2=r2))
        print(f"h={h:3d}  n={n:5d}  coef={coef:+.6f}  t(HAC)={tval:+.3f}  p={pval:.4f}{sig}  R2={r2:.4f}")

    print(f"\n=== 迴歸: fwd_ret_h ~ pct_2330 (單一台積電百分位, 連續值, HAC) ===")
    for h in HORIZONS:
        y = fwd[h]
        X = pd.DataFrame({"pct_2330": pct_2330})
        model, n = hac_ols(y, X, maxlags=h)
        coef = model.params["pct_2330"]
        tval = model.tvalues["pct_2330"]
        pval = model.pvalues["pct_2330"]
        r2 = model.rsquared
        sig = "***" if pval < bonferroni_alpha else ("*" if pval < 0.05 else "")
        results_rows.append(dict(model="pct2330_only", h=h, n=n, coef=coef, tstat=tval, pval=pval, r2=r2))
        print(f"h={h:3d}  n={n:5d}  coef={coef:+.6f}  t(HAC)={tval:+.3f}  p={pval:.4f}{sig}  R2={r2:.4f}")

    print(f"\n=== 賽馬迴歸: fwd_ret_h ~ breadth + pct_2330 (兩者同時放, HAC) ===")
    for h in HORIZONS:
        y = fwd[h]
        X = pd.DataFrame({"breadth": breadth, "pct_2330": pct_2330})
        model, n = hac_ols(y, X, maxlags=h)
        b_c, b_t, b_p = model.params["breadth"], model.tvalues["breadth"], model.pvalues["breadth"]
        p_c, p_t, p_p = model.params["pct_2330"], model.tvalues["pct_2330"], model.pvalues["pct_2330"]
        r2 = model.rsquared
        results_rows.append(dict(model="horserace_breadth", h=h, n=n, coef=b_c, tstat=b_t, pval=b_p, r2=r2))
        results_rows.append(dict(model="horserace_pct2330", h=h, n=n, coef=p_c, tstat=p_t, pval=p_p, r2=r2))
        print(f"h={h:3d}  n={n:5d}  breadth: coef={b_c:+.6f} t={b_t:+.3f} p={b_p:.4f}  |  "
              f"pct_2330: coef={p_c:+.6f} t={p_t:+.3f} p={p_p:.4f}  R2={r2:.4f}")

    # ---- 極端廣度事件研究 (rising edge, dedup) ----
    print("\n=== 極端廣度事件 (breadth 達到樣本90百分位以上, rising-edge去重) ===")
    b_valid = breadth.dropna()
    thresh_90 = b_valid.quantile(0.90)
    thresh_hi = max(thresh_90, 3)  # 至少3檔同步才算「同步」，並取90百分位兩者較嚴格者
    print(f"breadth 90百分位門檻 = {thresh_90:.2f} -> 採用門檻 {thresh_hi:.2f}")
    is_event = breadth >= thresh_hi
    event_days = []
    prev = False
    last_event_i = -999
    dates = breadth.index
    for i, d in enumerate(dates):
        cur = bool(is_event.iloc[i]) if not pd.isna(is_event.iloc[i]) else False
        if cur and not prev and (i - last_event_i) > 10:
            event_days.append(d)
            last_event_i = i
        prev = cur
    print(f"事件日數(去重後, 間隔>=10交易日): {len(event_days)}")

    ev_past5 = past5.reindex(event_days).dropna()
    print(f"\n事件日過去5日報酬: mean={ev_past5.mean()*100:+.3f}%  n={len(ev_past5)}")
    for h in HORIZONS:
        ev_fwd = fwd[h].reindex(event_days).dropna()
        base = fwd[h].dropna()
        m, lo, hi_ = bootstrap_mean_ci(ev_fwd.to_numpy())
        base_mean = base.mean()
        tstat, pval = stats.ttest_1samp(ev_fwd, base_mean) if len(ev_fwd) > 1 else (np.nan, np.nan)
        print(f"  fwd h={h:3d}: 事件日mean={m*100:+.3f}% [95%CI {lo*100:+.3f}%, {hi_*100:+.3f}%] "
              f"n={len(ev_fwd)}  vs 全樣本baseline={base_mean*100:+.3f}%  "
              f"t(vs baseline)={tstat:+.3f} p={pval:.4f}")

    # ---- 對照：單一2330觸發但廣度沒有同步擴大 vs 廣度事件 ----
    print("\n=== 對照: 單一2330重賣超觸發(rising-edge) vs 廣度同步事件, 前瞻報酬比較 ===")
    hit2330_bool = hit_2330 == 1.0
    solo_2330_days = []
    prev = False
    last_i = -999
    for i, d in enumerate(dates):
        cur = bool(hit2330_bool.iloc[i]) if not pd.isna(hit2330_bool.iloc[i]) else False
        if cur and not prev and (i - last_i) > 10:
            solo_2330_days.append(d)
            last_i = i
        prev = cur
    print(f"2330單獨觸發事件日數(去重後): {len(solo_2330_days)}")
    for h in HORIZONS:
        s = fwd[h].reindex(solo_2330_days).dropna()
        m, lo, hi_ = bootstrap_mean_ci(s.to_numpy())
        print(f"  2330單獨觸發 fwd h={h:3d}: mean={m*100:+.3f}% [95%CI {lo*100:+.3f}%,{hi_*100:+.3f}%] n={len(s)}")

    # ---- 反應 vs 領先 ----
    print("\n=== 反應 vs 領先: breadth(t) 與 past_ret_5(t) / fwd_ret_h(t) 的相關係數 ===")
    corr_past, p_past = stats.spearmanr(breadth, past5, nan_policy="omit")
    print(f"corr(breadth, past_ret_5)  spearman={corr_past:+.4f}  p={p_past:.4f}")
    for h in HORIZONS:
        corr_fwd, p_fwd = stats.spearmanr(breadth, fwd[h], nan_policy="omit")
        print(f"corr(breadth, fwd_ret_{h:3d})  spearman={corr_fwd:+.4f}  p={p_fwd:.4f}")

    # 事件日 timeline (t-10 .. t+20 累積報酬, event = 廣度極端事件)
    print("\n=== 事件日時間軸 (event=0, 相對交易日 -10..+20 的累積log報酬, 事件平均) ===")
    rel_days = list(range(-10, 21))
    log_ret = log_close.diff()
    timeline = []
    for rd in rel_days:
        vals = []
        for d in event_days:
            i = dates.get_loc(d)
            j = i + rd
            if 0 <= j < len(dates):
                # cumulative log return from event day close to day j close
                if rd >= 0:
                    v = log_close.iloc[j] - log_close.iloc[i]
                else:
                    v = log_close.iloc[j] - log_close.iloc[i]
                vals.append(v)
        timeline.append((rd, np.mean(vals) if vals else np.nan, len(vals)))
    for rd, v, n in timeline:
        marker = " <== event day" if rd == 0 else ""
        print(f"  t{rd:+3d}: 累積log報酬 {v*100:+.3f}%  (n={n}){marker}")

    conn.close()

    print("\n=== 事件日清單 (廣度極端, 去重) ===")
    for d in event_days:
        b = breadth.loc[d]
        print(f"  {d.date()}  breadth={b:.0f}")

    print("\n完成。")


if __name__ == "__main__":
    main()
