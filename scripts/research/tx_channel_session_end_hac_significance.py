#!/usr/bin/env python3
"""H-TXD-SESSION-END-ACCOUNTING 統計顯著性補做（呼應 H-SC-STAT-SIGNIFICANCE-GAP）。

背景：tx_channel_session_end_accounting.py 證實 session 結尾未平倉的尾部位被靜默
丟棄，force-close 誠實結算後 w233 單一window +19,362.6→-5,417.8pt、
w34+w55+w89組合 +79,863.9→-12,310.3pt——但這兩個數字至今只是「83天總和」，
從未做過逐日層級的正式統計顯著性檢定（naive t + Newey-West HAC）。

這支腳本不重新發明結算邏輯——force-close 規則（COOLDOWN=8、FILL_LAG_BARS、
COST_PTS_PER_TRADE、ATR 門檻用 IS 天池化固定值、day/night 各自從 session 第一根
bar 重新起算不跨界）100% 複用 tx_channel_session_end_accounting.py 的
run_block/simulate_with_forceclose，只是額外：
  1. 加一個 window=233 的獨立跑批（原始腳本 SLEEVES 只有 34/55/89，從未跑過233）
  2. 把彙總粒度從「83天一個總和」拆成「逐日 net_pts」（day+night 兩個 session
     的 pnl 相加成一個交易日），供 t 檢定/HAC 使用

顯著性判定標準（跟 H-SC-STAT-SIGNIFICANCE-GAP 系列同一套，不因方向放寬或收緊）：
HAC 校正後 p<0.05 在 maxlags=1,5,10 全部成立才算「穩健顯著」；只要有一個
maxlags 不過門檻，就是「不顯著」，不論點估計正負號。
"""
from __future__ import annotations

import json
import math
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats as sps

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_multiday import load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_channel_session_end_accounting import run_block  # noqa: E402 — 原封不動複用結算邏輯

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
OUT_JSON = OUT_DIR / "session_end_hac_significance.json"
OUT_CSV = OUT_DIR / "session_end_daily_netpts.csv"

W233 = 233
COMBO_WINDOWS = (34, 55, 89)
SESSIONS = ("day", "night")


# ============================== 統計檢定（跟 slow_cell_adaptive_recognition_speed.py
# 同一套 naive t / lag1_autocorr / Newey-West HAC 實作，保持方法論一致） ==============================

def one_sample_test(x):
    x = np.asarray(x, dtype=float)
    n = len(x)
    m = float(np.mean(x))
    sd = float(np.std(x, ddof=1))
    se = sd / math.sqrt(n)
    t_stat = m / se if se > 0 else float("nan")
    df = n - 1
    p_t = float(2.0 * sps.t.sf(abs(t_stat), df)) if se > 0 else float("nan")
    t_crit = float(sps.t.ppf(0.975, df))
    ci_lo, ci_hi = m - t_crit * se, m + t_crit * se
    return dict(n=n, mean=round(m, 3), sd=round(sd, 3), se=round(se, 3),
                t_stat=round(t_stat, 4), df=df, p_value_t=p_t,
                ci_95_t=[round(ci_lo, 3), round(ci_hi, 3)])


def lag1_autocorr(x):
    x = np.asarray(x, dtype=float)
    xm = x - x.mean()
    num = float(np.sum(xm[:-1] * xm[1:]))
    den = float(np.sum(xm * xm))
    return num / den if den > 0 else float("nan")


def newey_west_test(x, maxlags):
    """單樣本均值檢定的 HAC(Newey-West) 版本：對常數項迴歸 y = c + e，
    cov_type='HAC' 取代 naive iid 標準誤。"""
    x = np.asarray(x, dtype=float)
    y = x
    Xc = np.ones((len(x), 1))
    model = sm.OLS(y, Xc).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    mean_ = float(model.params[0])
    se_ = float(model.bse[0])
    t_ = float(model.tvalues[0])
    p_ = float(model.pvalues[0])
    ci = model.conf_int(alpha=0.05)
    return dict(maxlags=maxlags, mean=round(mean_, 3), se_hac=round(se_, 3),
                t_stat_hac=round(t_, 4), p_value_hac=p_,
                ci_95_hac=[round(float(ci[0, 0]), 3), round(float(ci[0, 1]), 3)])


def verdict_for(series_name, test_naive, nw_tests):
    ps = [nw_tests[str(L)]["p_value_hac"] for L in (1, 5, 10)]
    all_sig = all(p < 0.05 for p in ps)
    m = test_naive["mean"]
    if all_sig and m > 0:
        return (f"{series_name}: SIGNIFICANT POSITIVE — HAC p<0.05 全部maxlags(1/5/10)成立"
                f"，mean={m:.2f}pt/day，統計上可信的正面 edge。")
    if all_sig and m < 0:
        return (f"{series_name}: SIGNIFICANT NEGATIVE — HAC p<0.05 全部maxlags(1/5/10)成立"
                f"，mean={m:.2f}pt/day，統計上可信地虧錢（跟正面結果用同一套HAC<0.05門檻，"
                f"不因方向放寬）。")
    return (f"{series_name}: NOT SIGNIFICANT — HAC p-value(maxlags 1/5/10)="
            f"{[round(p, 4) for p in ps]}，至少一個maxlags未過0.05門檻，樣本量下無法"
            f"可信地判定方向（不論naive p值或點估計正負號為何）。")


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    atr_threshold = compute_global_atr_threshold(days, stripped)

    daily_w233: dict[str, float] = {}
    daily_combo: dict[str, float] = {}
    daily_w233_by_sess: dict[str, dict[str, float]] = {}
    daily_combo_by_sess: dict[str, dict[str, float]] = {}

    total_counted_w233, total_tail_w233 = 0.0, 0.0
    total_counted_combo, total_tail_combo = 0.0, 0.0
    n_tail_w233, n_tail_combo = 0, 0

    for day in days:
        w233_sum = 0.0
        combo_sum = 0.0
        daily_w233_by_sess[day] = {}
        daily_combo_by_sess[day] = {}
        for sess in SESSIONS:
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)

            tr, tl = run_block(seg, W233, atr_threshold)
            c_pnl = sum(t["pnl"] for t in tr)
            t_pnl = sum(t["pnl"] for t in tl)
            total_counted_w233 += c_pnl
            total_tail_w233 += t_pnl
            n_tail_w233 += len(tl)
            sess_net = c_pnl + t_pnl
            w233_sum += sess_net
            daily_w233_by_sess[day][sess] = round(float(sess_net), 2)

            combo_sess_sum = 0.0
            for w in COMBO_WINDOWS:
                tr, tl = run_block(seg, w, atr_threshold)
                c_pnl = sum(t["pnl"] for t in tr)
                t_pnl = sum(t["pnl"] for t in tl)
                total_counted_combo += c_pnl
                total_tail_combo += t_pnl
                n_tail_combo += len(tl)
                combo_sess_sum += c_pnl + t_pnl
            combo_sum += combo_sess_sum
            daily_combo_by_sess[day][sess] = round(float(combo_sess_sum), 2)

        daily_w233[day] = round(float(w233_sum), 2)
        daily_combo[day] = round(float(combo_sum), 2)

    # ---- 交叉核對：逐日彙總的總和必須等於 tx_channel_session_end_accounting.py
    # 已發表的83天總損益數字（w233 -5,417.8pt / combo -12,310.3pt），確認拆解
    # 沒有引入任何邏輯差異 ----
    w233_grand_total = round(float(total_counted_w233 + total_tail_w233), 1)
    combo_grand_total = round(float(total_counted_combo + total_tail_combo), 1)
    w233_series_total = round(float(sum(daily_w233.values())), 1)
    combo_series_total = round(float(sum(daily_combo.values())), 1)

    w233_series = list(daily_w233.values())
    combo_series = list(daily_combo.values())

    test_w233 = one_sample_test(w233_series)
    test_combo = one_sample_test(combo_series)
    lag1_w233 = lag1_autocorr(w233_series)
    lag1_combo = lag1_autocorr(combo_series)
    nw_w233 = {str(L): newey_west_test(w233_series, L) for L in (1, 5, 10)}
    nw_combo = {str(L): newey_west_test(combo_series, L) for L in (1, 5, 10)}

    verdict_w233 = verdict_for("w233", test_w233, nw_w233)
    verdict_combo = verdict_for("w34+w55+w89 combo", test_combo, nw_combo)

    # 檢定力下限概估：把95% CI收斂到±X pt/day要多少天樣本（假設sd不變）
    def days_needed(sd, target_half_width, z=1.96):
        if target_half_width <= 0:
            return None
        return math.ceil((z * sd / target_half_width) ** 2)

    power_note = dict(
        w233=dict(sd=test_w233["sd"], n=test_w233["n"],
                   days_needed_for_ci_half_width_100pt=days_needed(test_w233["sd"], 100),
                   days_needed_for_ci_half_width_50pt=days_needed(test_w233["sd"], 50)),
        combo=dict(sd=test_combo["sd"], n=test_combo["n"],
                    days_needed_for_ci_half_width_100pt=days_needed(test_combo["sd"], 100),
                    days_needed_for_ci_half_width_50pt=days_needed(test_combo["sd"], 50)),
        reference="tmf-slow-pattern-cell線後續用了484天(H-SC-STAT-SIGNIFICANCE-GAP"
                  "第八輪)才把lag1=+0.335的序列從naive p=0.029校正到HAC p=0.058~0.091；"
                  "本檢定只有83天，天數級距遠小於該線最終使用的樣本量。",
    )

    out = dict(
        hypothesis="H-TXD-SESSION-END-ACCOUNTING 逐日統計顯著性補做"
                   "（呼應 H-SC-STAT-SIGNIFICANCE-GAP 方法論）",
        script=str(Path(__file__).resolve()),
        engine_reused="tx_channel_session_end_accounting.py 的 run_block/"
                      "simulate_with_forceclose（原封不動 import，未改動任何結算規則）",
        data_source="tmf_channel bars.sqlite（tx_1m_daynight_cache.json 來源）83天，"
                    "day/night session 各自從第一根bar重新起算，跟 tx_channel_daynight_split.py "
                    "同一套資料管線",
        methodology_note=(
            "原始 tx_channel_session_end_accounting.py 只印出83天總和(w34+w55+w89"
            "組合的 -12,310.3pt)，SLEEVES 只有[34,55,89]、從未單獨跑過window=233，也"
            "沒有逐日輸出。本腳本：(1) 額外對window=233單獨跑一輪force-close結算"
            "(SLEEVES沒有233，是新增的獨立呼叫)；(2) 把兩者的彙總粒度從83天總和"
            "拆成逐日net_pts(day+night兩個session相加成一個交易日)。force-close"
            "規則、COOLDOWN、FILL_LAG_BARS、COST_PTS_PER_TRADE、ATR門檻全部直接"
            "import原腳本函式，未重寫或調整任何一個假設。"
        ),
        cross_check=dict(
            w233_grand_total_from_trades=w233_grand_total,
            w233_grand_total_from_daily_series_sum=w233_series_total,
            w233_matches_published_minus5417_8=bool(abs(w233_grand_total - (-5417.8)) < 1.0),
            combo_grand_total_from_trades=combo_grand_total,
            combo_grand_total_from_daily_series_sum=combo_series_total,
            combo_matches_published_minus12310_3=bool(abs(combo_grand_total - (-12310.3)) < 1.0),
            n_tail_positions_w233=n_tail_w233,
            n_tail_positions_combo=n_tail_combo,
        ),
        significance_test=dict(
            w233=dict(naive_t_test=test_w233, lag1_autocorr=round(lag1_w233, 4),
                       newey_west_hac=nw_w233,
                       p_value_comparison=dict(
                           naive=test_w233["p_value_t"],
                           hac_maxlags1=nw_w233["1"]["p_value_hac"],
                           hac_maxlags5=nw_w233["5"]["p_value_hac"],
                           hac_maxlags10=nw_w233["10"]["p_value_hac"],
                       ),
                       verdict=verdict_w233),
            combo_w34_w55_w89=dict(naive_t_test=test_combo, lag1_autocorr=round(lag1_combo, 4),
                                     newey_west_hac=nw_combo,
                                     p_value_comparison=dict(
                                         naive=test_combo["p_value_t"],
                                         hac_maxlags1=nw_combo["1"]["p_value_hac"],
                                         hac_maxlags5=nw_combo["5"]["p_value_hac"],
                                         hac_maxlags10=nw_combo["10"]["p_value_hac"],
                                     ),
                                     verdict=verdict_combo),
        ),
        autocorrelation_comparison=dict(
            w233_lag1=round(lag1_w233, 4),
            combo_lag1=round(lag1_combo, 4),
            tmf_slow_pattern_cell_reference_lag1=0.335,
            note=(
                "tmf-slow-pattern-cell線的旗艦配置在484天樣本上量到lag-1自相關"
                "≈+0.335(2024/8崩跌連續多天極端虧損造成)，Newey-West校正把p值從"
                "0.029拉升到0.058~0.091(不再顯著)。本檢定的兩個序列見上方"
                "w233_lag1/combo_lag1實測值，若同樣為正自相關，代表naive t檢定"
                "同樣可能虛高估計顯著性，須以HAC數字為準。"
            ),
        ),
        sample_size_power_limitation=power_note,
        honest_disclosure=(
            "83天樣本量遠小於tmf-slow-pattern-cell線後續驗證用的484天。若本檢定"
            "顯示『不顯著』，正確的結論是『83天檢定力不足以區分真的沒有edge與"
            "有一個小到目前偵測不到的edge/負edge』，不是『證明了沒有edge』——"
            "跟H-SC-STAT-SIGNIFICANCE-GAP第一輪(59天)得到的方法論教訓完全一致。"
            "同時，誠實結算後的點估計(w233 -5,417.8pt、combo -12,310.3pt)在83天"
            "全樣本下維持負號，此檢定的目的是量化這個負號有多大把握站得住，"
            "不是要推翻force-close結算本身的正確性。"
        ),
        per_day_net_pts=dict(
            w233=[dict(day=d, net_pts=v, day_sess=daily_w233_by_sess[d]["day"],
                        night_sess=daily_w233_by_sess[d]["night"]) for d, v in daily_w233.items()],
            combo=[dict(day=d, net_pts=v, day_sess=daily_combo_by_sess[d]["day"],
                         night_sess=daily_combo_by_sess[d]["night"]) for d, v in daily_combo.items()],
        ),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    df = pd.DataFrame({
        "day": days,
        "w233_net_pts": [daily_w233[d] for d in days],
        "w233_day_sess": [daily_w233_by_sess[d]["day"] for d in days],
        "w233_night_sess": [daily_w233_by_sess[d]["night"] for d in days],
        "combo_net_pts": [daily_combo[d] for d in days],
        "combo_day_sess": [daily_combo_by_sess[d]["day"] for d in days],
        "combo_night_sess": [daily_combo_by_sess[d]["night"] for d in days],
    })
    df.to_csv(OUT_CSV, index=False)

    print(f"w233   grand_total(from trades)={w233_grand_total:,.1f}  "
          f"(from daily series sum)={w233_series_total:,.1f}  "
          f"matches published -5,417.8? {out['cross_check']['w233_matches_published_minus5417_8']}")
    print(f"combo  grand_total(from trades)={combo_grand_total:,.1f}  "
          f"(from daily series sum)={combo_series_total:,.1f}  "
          f"matches published -12,310.3? {out['cross_check']['combo_matches_published_minus12310_3']}")
    print()
    print("-- w233 naive t-test --")
    for k, v in test_w233.items():
        print(f"  {k}: {v}")
    print(f"  lag1_autocorr: {lag1_w233:.4f}")
    for L in (1, 5, 10):
        print(f"  HAC maxlags={L}: p={nw_w233[str(L)]['p_value_hac']:.4f}  "
              f"t={nw_w233[str(L)]['t_stat_hac']:.4f}  se={nw_w233[str(L)]['se_hac']:.3f}")
    print(f"  verdict: {verdict_w233}")
    print()
    print("-- combo (w34+w55+w89) naive t-test --")
    for k, v in test_combo.items():
        print(f"  {k}: {v}")
    print(f"  lag1_autocorr: {lag1_combo:.4f}")
    for L in (1, 5, 10):
        print(f"  HAC maxlags={L}: p={nw_combo[str(L)]['p_value_hac']:.4f}  "
              f"t={nw_combo[str(L)]['t_stat_hac']:.4f}  se={nw_combo[str(L)]['se_hac']:.3f}")
    print(f"  verdict: {verdict_combo}")
    print()
    print(f"saved -> {OUT_JSON}")
    print(f"saved -> {OUT_CSV}")


if __name__ == "__main__":
    main()
