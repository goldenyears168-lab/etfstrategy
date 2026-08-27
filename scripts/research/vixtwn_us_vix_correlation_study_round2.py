#!/usr/bin/env python3
"""VIXTWN/美股VIX關聯性研究第二輪——把round1的rank correlation深化成可解讀的
量級、測試不對稱性(恐慌傳導快/平靜傳導慢)、領先窗口衰減，並回頭測試
『美股VIX隔夜異常飆升』這個regime旗標跟dayflip短/長邊歷史績效的關係
（跟8/9那輪VIX水位相關性不同，這裡用『變動/飆升』而非『水位』，round1
發現變動關係比水位關係更適合當predictive訊號）。

延續round1（vixtwn_us_vix_correlation_study.py）的因果正確設計：一律用
『美股VIX前一交易日變動』（台股開盤前已知）去解釋『當天/隔天VIXTWN或
dayflip交易績效』，不用同期或未來資訊。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/vixtwn_us_vix_correlation_study_round2.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats as sstats

import stock_db
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"


def load_vix_series(con: sqlite3.Connection, symbol: str, source: str) -> dict[str, float]:
    rows = con.execute(
        "SELECT date, close FROM market_vix_daily WHERE symbol=? AND source=? AND close IS NOT NULL ORDER BY date",
        (symbol, source),
    ).fetchall()
    return {str(d): float(c) for d, c in rows}


def build_lagged_pairs(vixtwn: dict, vix: dict, lag_days: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """us_lead_val[i] = 美股VIX第(N-lag_days)筆相對第(N-lag_days-1)筆的變動，
    tw_ret[i] = VIXTWN(t)相對VIXTWN(t-1)的變動——lag_days=1即round1的『前一日』
    設計，lag_days=2/3測試領先窗口再往前推是否還有預測力（衰減結構）。"""
    us_dates = sorted(vix)
    tw_dates_all = sorted(vixtwn)
    us_idx = 0
    tw_ret, us_lead_val, dates_out = [], [], []
    for i in range(1, len(tw_dates_all)):
        d0_tw, d1 = tw_dates_all[i - 1], tw_dates_all[i]
        while us_idx + 1 < len(us_dates) and us_dates[us_idx + 1] < d1:
            us_idx += 1
        j = us_idx - (lag_days - 1)
        if j < 1:
            continue
        d0_us, d_prev_us = us_dates[j], us_dates[j - 1]
        us_lead_val.append(vix[d0_us] - vix[d_prev_us])
        tw_ret.append(vixtwn[d1] - vixtwn[d0_tw])
        dates_out.append(d1)
    return np.array(us_lead_val), np.array(tw_ret), dates_out


def load_short_trades() -> list[dict]:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def us_vix_change_before(con: sqlite3.Connection, vix: dict, as_of: str) -> float | None:
    """as_of(T0)當天，美股VIX最近一筆相對再前一筆的變動——T0決策時已知的資訊。"""
    prior = [d for d in vix if d <= as_of]
    if len(prior) < 2:
        return None
    prior.sort()
    return vix[prior[-1]] - vix[prior[-2]]


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    vixtwn = load_vix_series(con, "VIXTWN", "computed")
    vix = load_vix_series(con, "VIX", "yahoo")

    print("=== VIXTWN/美股VIX 關聯性研究 round 2 ===\n")

    # --- 1) 量級：OLS回歸把round1的IC=+0.296變成可解讀的斜率/R^2 ---
    us_lag1, tw_ret1, _dates1 = build_lagged_pairs(vixtwn, vix, lag_days=1)
    slope, intercept, r, p, se = sstats.linregress(us_lag1, tw_ret1)
    print("--- 1) 量級（美股VIX前一日變動 → 隔天VIXTWN變動，OLS）---")
    print(f"斜率 = {slope:+.3f}（美股VIX每漲1點，VIXTWN隔天平均漲{slope:.3f}點）")
    print(f"R = {r:+.3f}，R^2 = {r**2:.3f}（解釋力，其餘{(1-r**2)*100:.0f}%由其他因素決定）")
    print(f"n = {len(us_lag1)}\n")

    # --- 2) 不對稱性：美股VIX上升日 vs 下降日，對VIXTWN的影響力是否不同 ---
    up_mask = us_lag1 > 0
    down_mask = us_lag1 < 0
    ic_up, p_up = sstats.spearmanr(us_lag1[up_mask], tw_ret1[up_mask])
    ic_down, p_down = sstats.spearmanr(us_lag1[down_mask], tw_ret1[down_mask])
    print("--- 2) 不對稱性：恐慌上升日 vs 平靜下降日 ---")
    print(f"美股VIX上升日(n={up_mask.sum()}): IC={ic_up:+.3f} (p={p_up:.2e}) "
          f"· VIXTWN平均跟漲={tw_ret1[up_mask].mean():+.3f}")
    print(f"美股VIX下降日(n={down_mask.sum()}): IC={ic_down:+.3f} (p={p_down:.2e}) "
          f"· VIXTWN平均跟跌={tw_ret1[down_mask].mean():+.3f}")
    print("（若IC(上升日) > IC(下降日)，代表恐慌傳導比平靜情緒傳導更一致/更快）\n")

    # --- 3) 領先窗口衰減：lag=1/2/3天 ---
    print("--- 3) 領先窗口衰減（美股VIX第N天前的變動，對『今天』VIXTWN變動還有沒有解釋力）---")
    for lag in (1, 2, 3, 5):
        u, t, _ = build_lagged_pairs(vixtwn, vix, lag_days=lag)
        ic, p = sstats.spearmanr(u, t)
        print(f"  lag={lag}天: IC={ic:+.3f} (p={p:.2e}) n={len(u)}")
    print()

    # --- 4) 回頭測試：美股VIX『飆升』regime旗標 vs dayflip短邊績效 ---
    # round1(8/9)那輪測的是VIX『水位』，短邊完全沒關係；這裡改測『變動/飆升』，
    # 因為round1已經證實變動關係比水位關係更適合當predictive訊號。
    short_trades = load_short_trades()
    spike_pairs = []
    for t in short_trades:
        chg = us_vix_change_before(con, vix, t["trade_date"])
        if chg is None:
            continue
        spike_pairs.append((chg, float(t["pnl_pct"])))
    ic_spike, p_spike, best_th_note = float("nan"), float("nan"), "n<20，未測試"
    if len(spike_pairs) >= 20:
        chg_arr = np.array([p[0] for p in spike_pairs])
        pnl_arr = np.array([p[1] for p in spike_pairs])
        ic_spike, p_spike = sstats.spearmanr(chg_arr, pnl_arr)
        median_chg = float(np.median(chg_arr))
        high_spike = pnl_arr[chg_arr >= median_chg]
        low_spike = pnl_arr[chg_arr < median_chg]
        print("--- 4) 美股VIX『變動』(非水位) vs 短邊歷史績效 ---")
        print(f"IC(美股VIX變動, 短邊pnl_pct) = {ic_spike:+.3f} (p={p_spike:.3f}) n={len(spike_pairs)}")
        print(f"VIX變動>=中位數({median_chg:+.2f}) n={len(high_spike)} 平均pnl={high_spike.mean():+.3f}% "
              f"vs VIX變動<中位數 n={len(low_spike)} 平均pnl={low_spike.mean():+.3f}%")
        best_th_note = f"IC={ic_spike:+.3f}(p={p_spike:.3f})，中位數切分兩組平均pnl {high_spike.mean():+.3f}% vs {low_spike.mean():+.3f}%"
        if len(high_spike) >= 5 and len(low_spike) >= 5:
            tstat, tp = sstats.ttest_ind(high_spike, low_spike, equal_var=False)
            print(f"  兩組t-test: t={tstat:+.2f} p={tp:.3f}")
            best_th_note += f"，t-test t={tstat:+.2f} p={tp:.3f}"
    else:
        print(f"--- 4) 樣本太小(n={len(spike_pairs)})，跳過 ---")

    print(
        "\n⚠️ 限制：\n"
        "  1) 斜率/R^2是23年全樣本OLS，沒有做walk-forward切分——round1已經\n"
        "     驗證過這個關係逐年都顯著、方向穩定，這裡的重點是『量級多大』\n"
        "     不是『存不存在』，全樣本估計量級更穩定。\n"
        "  2) 第4項用的是短邊74筆真實交易，樣本天生小，即使發現顯著關係也\n"
        "     不足以直接當交易濾網——沿用今天稍早排除excess_gap濾網時的同一\n"
        "     個判斷標準：發現顯著只是『值得繼續』的訊號，不是『可以上線』。\n"
        "  3) VIXTWN(computed)不是官方量表，跟round1同樣的方法論限制。"
    )

    survives = not np.isnan(ic_spike) and abs(ic_spike) > 0.2 and p_spike < 0.05
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-us-vix-change-regime-check",
        ts="2026-08-10",
        params={"vix_symbol": "VIX", "measure": "prior_day_change_not_level"},
        n_observations=len(spike_pairs),
        metric_name="ic_us_vix_change_vs_short_pnl",
        metric_value=float(ic_spike) if not np.isnan(ic_spike) else 0.0,
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"round2延伸：測試美股VIX『變動』(非round1(8/9)那輪測過的『水位』，"
            f"水位對短邊完全沒關係)跟短邊74筆真實交易績效的關係。{best_th_note}。"
            f"項目1-3(量級/不對稱性/領先窗口衰減)是VIXTWN本身的市場結構研究，"
            f"不涉及dayflip交易，不需要另外記錄——完整數字見腳本輸出。"
        ),
        tags=["dayflip-short", "vix-change", "regime", "us-vix"],
    )
    print("\n(項目4已記入trial registry；項目1-3是VIXTWN本身的市場結構研究，"
          "不涉及dayflip交易績效，不需要另外記錄)")


if __name__ == "__main__":
    main()
