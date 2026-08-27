#!/usr/bin/env python3
"""補資料缺口＋重新驗證一個曾被 rejected 的結論：H-TXD-SESSION-END-ACCOUNTING 的
w233/combo 負號結論，是不是被夜盤 bar 快取「硬截斷在23:59（從未收錄00:00~05:00）」
這個資料缺口污染了。

背景：
  - tx_channel_session_end_accounting.py 用 83 天 bar 快取（sess='night' 只到23:59）跑誠實
    force-close 結算，得到 w233 -5,417.8pt／combo(w34+55+89) -12,310.3pt，兩者都是負的。
  - tx_channel_session_end_hac_significance.py 對這兩個負數字做過 HAC 顯著性檢定。
  - 2026-08-07 用17天真實tick重建完整夜盤（tx_channel_tick_night_pnl_check.py 的
    full_night_extra 區塊）後，w233 單一window 在夜盤區段從 -2,018.7pt 翻成 +663.2pt（42筆
    交易，33筆變42筆是因為00:00~05:00那段本來就有訊號、之前被快取截斷丟掉）——但17天樣本
    完全沒做顯著性檢定，不能當結論。

這支腳本做的事（見 CLAUDE.md 誠實回測紀律 1-8）：
  1. 用 finmind_tx_tick_by_day/ 的 tick 資料＋跨日拼接（複製
     tx_channel_tick_night_concat_validation.build_night_session 100%不改），把「完整夜盤」
     (15:00 D ~ 約05:00 D+1，不截斷) 重建到最多可用的天數——4段既有『day session bar cache
     維持不動』的 tick-derived 交易日：
       julsep2025(65d) + octdec2025(62d) + janmar2026(55d) + apr-jul2026(83d) = 265 天
     全部4段的 day session 都是既有 bar cache（未動一根bar），只重建 night。
  2. 用 tx_channel_session_end_accounting.run_block（原封不動 import，未改任何結算規則：
     COOLDOWN=8、FILL_LAG_BARS=1、COST_PTS_PER_TRADE=5.9、force-close session 尾部位）
     在（day=舊cache, night=新重建完整夜盤）上重新跑 w233 單獨、w34+55+89 組合。
  3. 逐日 net_pts（day+night 兩個 session 相加）做 t 檢定 + Newey-West HAC(maxlags=1/5/10)，
     判準跟 tx_channel_session_end_hac_significance.py 同一套（HAC p<0.05 全部maxlags成立
     才算顯著，正負不放寬）。one_sample_test/lag1_autocorr/newey_west_test/verdict_for 直接
     import 原腳本函式，不重寫。
  4. 額外保留「原始83天子樣本」(2026-04-01~2026-07-31，即舊 -5,417.8/-12,310.3 那組人)
     的獨立HAC結果，做265天 vs 83天、full-night vs truncated-night 的直接對照。

ATR門檻沿用原方法：對每個交易日，把（day cache bars + 新重建完整夜盤bars）串接成一天的
序列（此串接會跨過日盤收盤13:45→夜盤開盤15:00的空檔，這是原腳本
tx_channel_daynight_split.py/tx_channel_session_end_accounting.py本來就有的已知簡化，
不是本腳本新引入的問題），對「這批全部交易日」池化算5th percentile，固定下來套用到
day/night兩個session的run_block——這是descriptive full-sample統計，不是walk-forward，
沿用原腳本方法論，不做IS/OOS切分（跟原腳本一致）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import warnings
from functools import lru_cache
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tx_channel_tick_night_concat_validation as concat_mod  # noqa: E402
from tx_channel_geometry_multiday import DB_PATH  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_channel_session_end_accounting import run_block  # noqa: E402 — 原封不動複用結算邏輯
from tx_channel_session_end_hac_significance import (  # noqa: E402 — 原封不動複用檢定函式
    lag1_autocorr,
    newey_west_test,
    one_sample_test,
    verdict_for,
)
from tx_channel_tick_night_pnl_check import resample_ticks  # noqa: E402

# 幫 _load_raw 加 cache：同一個 calendar-date json 在「D的同日段」跟「D-1的次日段」會被
# 讀兩次，補這格能把批次重建時間砍掉接近一半。純加速，不改任何拼接/驗證邏輯。
concat_mod._load_raw = lru_cache(maxsize=16)(concat_mod._load_raw)
build_night_session = concat_mod.build_night_session

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
OUT_JSON = OUT_DIR / "fullnight_session_end_hac_265d.json"
OUT_CSV = OUT_DIR / "fullnight_session_end_daily_netpts_265d.csv"

W233 = 233
COMBO_WINDOWS = (34, 55, 89)
SESSIONS = ("day", "night")

# 4段既有 day-session bar cache（皆未改動），依時間先後串起來 = 265個交易日
DAY_RANGES = [
    ("julsep2025", "tx_1m_julsep_holdout_cache.json"),
    ("octdec2025", "tx_1m_octdec_holdout_cache.json"),
    ("janmar2026", "tx_1m_janmar_holdout_cache.json"),
    ("apr_jul2026", "tx_1m_daynight_cache.json"),
]
# 原本被判定「負號、已做過HAC」的那組樣本，用來對照
BASELINE_83D_SOURCE = "tx_1m_daynight_cache.json"


def load_days_for_source(source: str) -> list[str]:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (source,)
        ).fetchall()
    return [r[0] for r in rows]


def load_day_session_bars(source: str, day: str) -> pd.DataFrame:
    """day session bars——原封不動吃既有 bar cache，完全不重建。"""
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v FROM bars WHERE source=? AND day=? AND sess='day' ORDER BY t",
            conn, params=(source, day),
        )
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    return df[["Datetime", "Open", "High", "Low", "Close", "Volume"]]


def build_full_night_bars(day: str) -> tuple[pd.DataFrame | None, dict]:
    """完整夜盤（15:00 D ~ 約05:00 D+1，不截斷），tick跨日拼接後 resample 成1分K。
    複用 build_night_session（跨日拼接，已在 tick_night_concat_validation.py 驗證過）+
    resample_ticks(start=None,end=None)（複用 tick_night_pnl_check.py 的resample，不截斷）。"""
    combined, diag = build_night_session(day)
    if combined is None or combined.empty:
        return None, diag
    bars = resample_ticks(combined, None, None)
    diag["n_night_bars_full"] = int(len(bars))
    diag["night_bar_first_t"] = str(bars["Datetime"].iloc[0]) if not bars.empty else None
    diag["night_bar_last_t"] = str(bars["Datetime"].iloc[-1]) if not bars.empty else None
    if bars.empty:
        diag["error"] = diag.get("error") or "resample後夜盤bar為空"
        return None, diag
    return bars, diag


def net_for_day(day_seg: pd.DataFrame, night_seg: pd.DataFrame, windows: tuple[int, ...],
                 atr_threshold: float) -> tuple[float, float]:
    """回傳 (day_session net_pts, night_session net_pts)，windows裡每個window各自跑一次
    run_block(=counted+tail都force-close過)，同一session內加總。"""
    day_net = 0.0
    night_net = 0.0
    for w in windows:
        tr, tl = run_block(day_seg, w, atr_threshold)
        day_net += sum(t["pnl"] for t in tr) + sum(t["pnl"] for t in tl)
        tr, tl = run_block(night_seg, w, atr_threshold)
        night_net += sum(t["pnl"] for t in tr) + sum(t["pnl"] for t in tl)
    return day_net, night_net


def main() -> None:
    t_start = time.time()

    # ---- 蒐集4段既有day-session cache的完整交易日清單 ----
    range_days: dict[str, list[str]] = {}
    all_days: list[str] = []
    day_source_map: dict[str, str] = {}
    for label, source in DAY_RANGES:
        days = load_days_for_source(source)
        range_days[label] = days
        all_days.extend(days)
        for d in days:
            day_source_map[d] = source
    all_days = sorted(all_days)
    print(f"day-session bar cache 涵蓋範圍：{len(all_days)}天，{all_days[0]}~{all_days[-1]}"
          f"（{', '.join(f'{label}={len(range_days[label])}天' for label, _ in DAY_RANGES)}）")
    print("day session 全部沿用既有cache，本腳本不重建任何一根day bar，只重建night。\n")

    # ---- 逐日重建完整夜盤（tick跨日拼接，不截斷）----
    print(f"=== 重建完整夜盤（{len(all_days)}天，tick跨日拼接）===")
    night_bars: dict[str, pd.DataFrame] = {}
    night_diag: list[dict] = []
    n_ok, n_fail = 0, 0
    for i, day in enumerate(all_days):
        bars, diag = build_full_night_bars(day)
        diag["day"] = day
        night_diag.append(diag)
        if bars is not None:
            night_bars[day] = bars
            n_ok += 1
        else:
            n_fail += 1
        if (i + 1) % 40 == 0 or (i + 1) == len(all_days):
            elapsed = time.time() - t_start
            print(f"  ...{i + 1}/{len(all_days)}天  成功={n_ok}  失敗={n_fail}  "
                  f"耗時={elapsed:.0f}s")

    print(f"\n完整夜盤重建結果：成功{n_ok}/{len(all_days)}天  失敗{n_fail}天")
    fail_examples = [d for d in night_diag if d.get("day") not in night_bars][:20]
    if fail_examples:
        print("失敗天數樣本（前20筆，含原因）：")
        for f in fail_examples:
            print(f"  {f['day']}: {f.get('error', '未知')}")

    usable_days = [d for d in all_days if d in night_bars]
    print(f"\n可用交易日（day cache + 完整夜盤都齊）：{len(usable_days)}天\n")

    # ---- 載入day session bars（既有cache，未動）----
    day_bars: dict[str, pd.DataFrame] = {
        d: load_day_session_bars(day_source_map[d], d) for d in usable_days
    }
    # 保底檢查：day bars不能是空的（若空，代表該天day session本身缺資料，一併剔除）
    usable_days = [d for d in usable_days if not day_bars[d].empty]
    print(f"day session bars也非空的可用交易日：{len(usable_days)}天\n")

    # ---- ATR門檻：day+完整night串接，池化全部可用天數的5th percentile（沿用原方法論）----
    stripped = {
        d: pd.concat([day_bars[d], night_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]]],
                     ignore_index=True)
        for d in usable_days
    }
    atr_threshold_full = compute_global_atr_threshold(usable_days, stripped)
    print(f"ATR門檻（{len(usable_days)}天池化5th percentile，day+完整夜盤串接）: {atr_threshold_full:.2f}")

    # 對照：只用83天子樣本（apr_jul2026）重算一次ATR門檻，因為原始
    # tx_channel_session_end_accounting.py 的門檻是只用83天池化算的，不是265天。
    baseline_days = [d for d in usable_days if day_source_map[d] == BASELINE_83D_SOURCE]
    stripped_83 = {d: stripped[d] for d in baseline_days}
    atr_threshold_83 = compute_global_atr_threshold(baseline_days, stripped_83)
    print(f"ATR門檻（僅83天子樣本池化，跟原腳本樣本範圍一致）: {atr_threshold_83:.2f}\n")

    # ---- 逐日跑 force-close 結算：w233 / combo，265天版(用265天門檻) + 83天子樣本版(用83天門檻) ----
    def run_full_pass(days: list[str], atr_threshold: float, tag: str) -> dict:
        daily_w233, daily_combo = {}, {}
        daily_w233_sess, daily_combo_sess = {}, {}
        for day in days:
            d_net_w233, n_net_w233 = net_for_day(day_bars[day], night_bars[day], (W233,), atr_threshold)
            d_net_combo, n_net_combo = net_for_day(day_bars[day], night_bars[day], COMBO_WINDOWS, atr_threshold)
            daily_w233[day] = round(float(d_net_w233 + n_net_w233), 2)
            daily_combo[day] = round(float(d_net_combo + n_net_combo), 2)
            daily_w233_sess[day] = dict(day=round(float(d_net_w233), 2), night=round(float(n_net_w233), 2))
            daily_combo_sess[day] = dict(day=round(float(d_net_combo), 2), night=round(float(n_net_combo), 2))
        print(f"[{tag}] {len(days)}天  w233總損益={sum(daily_w233.values()):,.1f}pt  "
              f"combo總損益={sum(daily_combo.values()):,.1f}pt")
        return dict(daily_w233=daily_w233, daily_combo=daily_combo,
                    daily_w233_sess=daily_w233_sess, daily_combo_sess=daily_combo_sess)

    print("=== 逐日 force-close 結算（完整夜盤）===")
    pass_265 = run_full_pass(usable_days, atr_threshold_full, "265天全樣本(完整夜盤)")
    pass_83 = run_full_pass(baseline_days, atr_threshold_83, "83天子樣本(完整夜盤,對照舊-5417.8/-12310.3)")
    print()

    # ---- 統計顯著性：t檢定 + Newey-West HAC(1/5/10)，兩個樣本各自跑一次 ----
    def significance_block(daily: dict[str, float], name: str) -> dict:
        series = list(daily.values())
        test = one_sample_test(series)
        lag1 = lag1_autocorr(series)
        nw = {str(L): newey_west_test(series, L) for L in (1, 5, 10)}
        verdict = verdict_for(name, test, nw)
        return dict(naive_t_test=test, lag1_autocorr=round(lag1, 4), newey_west_hac=nw, verdict=verdict)

    sig_265_w233 = significance_block(pass_265["daily_w233"], "w233(265天,完整夜盤)")
    sig_265_combo = significance_block(pass_265["daily_combo"], "combo(265天,完整夜盤)")
    sig_83_w233 = significance_block(pass_83["daily_w233"], "w233(83天子樣本,完整夜盤)")
    sig_83_combo = significance_block(pass_83["daily_combo"], "combo(83天子樣本,完整夜盤)")

    print("=== 統計顯著性檢定 ===")
    for label, sig in [("w233(265天)", sig_265_w233), ("combo(265天)", sig_265_combo),
                        ("w233(83天子樣本)", sig_83_w233), ("combo(83天子樣本)", sig_83_combo)]:
        print(f"-- {label} --")
        print(f"  {sig['verdict']}")
    print()

    # ---- 對照：舊的截斷版數字 vs 這次完整夜盤版數字 ----
    old_83_w233 = -5417.8
    old_83_combo = -12310.3
    new_83_w233 = round(float(sum(pass_83["daily_w233"].values())), 1)
    new_83_combo = round(float(sum(pass_83["daily_combo"].values())), 1)
    new_265_w233 = round(float(sum(pass_265["daily_w233"].values())), 1)
    new_265_combo = round(float(sum(pass_265["daily_combo"].values())), 1)

    print("=== 對照：截斷版(舊，23:59截斷) vs 完整夜盤版(新) ===")
    print(f"w233   83天子樣本: 舊(截斷)={old_83_w233:,.1f}pt  新(完整夜盤)={new_83_w233:,.1f}pt  "
          f"符號{'不變(維持負)' if (old_83_w233<0)==(new_83_w233<0) else '翻轉'}")
    print(f"combo  83天子樣本: 舊(截斷)={old_83_combo:,.1f}pt  新(完整夜盤)={new_83_combo:,.1f}pt  "
          f"符號{'不變(維持負)' if (old_83_combo<0)==(new_83_combo<0) else '翻轉'}")
    print(f"w233   265天全樣本(完整夜盤，無截斷版對照組，只列供參考): {new_265_w233:,.1f}pt")
    print(f"combo  265天全樣本(完整夜盤，無截斷版對照組，只列供參考): {new_265_combo:,.1f}pt\n")

    # ---- 存檔 ----
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    out = dict(
        hypothesis="H-TXD-NIGHT-SESSION-TRUNCATION 資料缺口修補＋H-TXD-SESSION-END-ACCOUNTING"
                   "負號結論的擴大樣本重新驗證",
        script=str(Path(__file__).resolve()),
        engines_reused=dict(
            night_concat="tx_channel_tick_night_concat_validation.build_night_session（原封不動）",
            night_resample="tx_channel_tick_night_pnl_check.resample_ticks（原封不動，start=end=None不截斷）",
            settlement="tx_channel_session_end_accounting.run_block（原封不動，force-close結尾未平倉部位）",
            significance_tests="tx_channel_session_end_hac_significance.{one_sample_test,lag1_autocorr,"
                                "newey_west_test,verdict_for}（原封不動）",
        ),
        day_session_source="4段既有bar cache完全未動：julsep2025/octdec2025/janmar2026(holdout caches)+"
                            "apr_jul2026(tx_1m_daynight_cache.json，即H-TXD-SESSION-END-ACCOUNTING"
                            "原本用的83天)，串起來共265個交易日",
        night_reconstruction=dict(
            n_days_attempted=len(all_days),
            n_days_success=n_ok,
            n_days_failed=n_fail,
            failed_days_sample=[dict(day=f["day"], error=f.get("error", "未知")) for f in fail_examples],
            n_usable_days_with_both_day_and_night=len(usable_days),
            elapsed_seconds=round(time.time() - t_start, 1),
        ),
        atr_threshold=dict(pooled_265d=atr_threshold_full, pooled_83d_subsample=atr_threshold_83),
        grand_totals=dict(
            baseline_83d_truncated_published=dict(w233=old_83_w233, combo=old_83_combo),
            fullnight_83d_subsample=dict(w233=new_83_w233, combo=new_83_combo,
                                          w233_sign_flipped=bool((old_83_w233 < 0) != (new_83_w233 < 0)),
                                          combo_sign_flipped=bool((old_83_combo < 0) != (new_83_combo < 0))),
            fullnight_265d_full_sample=dict(w233=new_265_w233, combo=new_265_combo),
            seventeen_day_sample_reference=dict(
                note="今天稍早tick_night_pnl_check.py的17天樣本(僅夜盤本身,非day+night合併net_pts)：",
                w233_night_only_truncated=-2018.7, w233_night_only_fullnight=663.2,
                portfolio_night_only_truncated=-2439.8, portfolio_night_only_fullnight=-3165.3,
            ),
        ),
        significance_test=dict(
            w233_265d=sig_265_w233, combo_265d=sig_265_combo,
            w233_83d_subsample=sig_83_w233, combo_83d_subsample=sig_83_combo,
        ),
        per_day_net_pts=dict(
            n265=dict(w233=pass_265["daily_w233"], combo=pass_265["daily_combo"]),
            n83=dict(w233=pass_83["daily_w233"], combo=pass_83["daily_combo"]),
        ),
        night_build_diagnostics_sample=night_diag[::10],  # 每10天存一筆診斷，避免JSON過大
        caveats=[
            "ATR門檻用「day cache bars + 新重建完整夜盤」串接後池化5th percentile，此串接會跨過"
            "日盤收盤(13:45)→夜盤開盤(15:00)的空檔(沿用tx_channel_daynight_split.py/"
            "tx_channel_session_end_accounting.py原本就有的簡化，不是本腳本新引入)。",
            "265天樣本的4段day-session bar cache來源不同(3段holdout cache+1段主cache)，"
            "其day session bar本身的建置方法本腳本未重新稽核，直接信任既有cache。",
            "沒有做IS/OOS切分——ATR門檻是對「這批全部可用天數」池化算一次固定值，descriptive "
            "full-sample統計，不是walk-forward預測性宣稱，沿用兩支被複用腳本原本的方法論。",
            "window=233/w34+55+89的參數本身是先前研究輪次選定的，本腳本不重新搜參，只驗證"
            "「修掉夜盤截斷資料缺口後，這兩組已選定參數的結論方向會不會變」。",
            "265天樣本橫跨2025-07-01~2026-07-31，中間跨過3次合約換月，front-month判定用"
            "build_night_session原本的『當晚15:00後成交量最大合約』邏輯，未額外驗證換月當天"
            "是否有雙掛牌污染。",
        ],
    )
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))

    df = pd.DataFrame({
        "day": usable_days,
        "source_range": [day_source_map[d] for d in usable_days],
        "w233_net_pts_265d_threshold": [pass_265["daily_w233"][d] for d in usable_days],
        "w233_day_sess": [pass_265["daily_w233_sess"][d]["day"] for d in usable_days],
        "w233_night_sess_fullnight": [pass_265["daily_w233_sess"][d]["night"] for d in usable_days],
        "combo_net_pts_265d_threshold": [pass_265["daily_combo"][d] for d in usable_days],
        "combo_day_sess": [pass_265["daily_combo_sess"][d]["day"] for d in usable_days],
        "combo_night_sess_fullnight": [pass_265["daily_combo_sess"][d]["night"] for d in usable_days],
    })
    df.to_csv(OUT_CSV, index=False)

    print(f"saved -> {OUT_JSON}")
    print(f"saved -> {OUT_CSV}")
    print(f"\n總耗時: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
