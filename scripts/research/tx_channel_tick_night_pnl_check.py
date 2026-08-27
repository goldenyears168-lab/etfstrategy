#!/usr/bin/env python3
"""夜盤版 tick級驗證——複製 tx_channel_tick_validation.py / tx_channel_tick_pnl_check.py
（日盤）的兩層方法，套用到夜盤，並用 tx_channel_tick_night_concat_validation.py 驗證過的
跨日拼接邏輯取得夜盤tick序列。

方法一致性聲明（跟日盤版本比較）：
  - 第一層(bar vs tick資料落差)、第二層(訊號/損益重跑比較) 的比對邏輯完全複製日盤版本
    （分鐘OHLC resample方式、compare_bars差異統計、run_sleeve訊號狀態機、ATR門檻用
    pooled 5th percentile）。
  - 兩個蓄意差異，皆因夜盤資料結構限制、如實揭露：
    1. bars.sqlite 的夜盤bar快取本身只到23:59（從未收錄00:00~05:00尾段，見
       tx_channel_tick_night_concat_validation.py的診斷輸出）。要跟快取做「同一段時間」
       的apples-to-apples比較，本腳本的『bar-cache版』比較只能用15:00~23:59這段，
       tick-derived版也同步截到23:59——不是tick資料的限制，是快取的限制。
    2. 額外補一個快取沒有對照組的「完整夜盤」分析（15:00 D ~ 約05:00 D+1，用
       跨日拼接後的tick序列建bar，沒有bar-cache版本可比較）——回答「如果去掉快取的
       截斷限制，凌晨0-5點那段尾巴本身貢獻多少交易/損益」，這是bar快取涵蓋率的問題，
       不是tick-vs-bar精度問題，兩者不要混為一談。
  - 記帳：PnL重跑改用 force-close（session最後一筆未平倉部位用最後一根bar收盤價強制
    平倉），複製 tx_channel_session_end_accounting.py 的修正邏輯——因為H-TXD-SESSION-
    END-ACCOUNTING已證實 simulate_pnl_realistic 原版會靜默丟棄尾部位。bar-cache版跟
    tick-derived版用同一套force-close邏輯，這樣兩邊的比較才是純粹的「資料來源」效應，
    不會混進「記帳方法不同」的干擾變數。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_channel_tick_night_concat_validation import build_night_session  # noqa: E402
from tx_channel_tick_validation import compare_bars  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COOLDOWN = 8
SLEEVES = [34, 55, 89]
W233 = 233
NIGHT_TRUNC_END = "23:59:59"  # 對齊bar快取的截斷邊界


def resample_ticks(ticks: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    """ticks 需含 dt/price/volume 欄；start/end 為 None 代表不截。"""
    t = ticks.set_index("dt").sort_index()
    if start is not None and end is not None:
        t = t.between_time(start, end)
    if t.empty:
        return pd.DataFrame()
    ohlc = t["price"].resample("1min").ohlc()
    vol = t["volume"].resample("1min").sum()
    out = ohlc.join(vol.rename("volume")).dropna(subset=["open"])
    out = out.reset_index().rename(columns={"dt": "Datetime", "open": "Open", "high": "High",
                                              "low": "Low", "close": "Close", "volume": "Volume"})
    return out


def simulate_with_forceclose(dataset: pd.DataFrame) -> list[dict]:
    """複製 tx_channel_session_end_accounting.simulate_with_forceclose：
    逐行 fill_lag_bars=1 成交，session結尾強制平倉，回傳完整trades(含尾部位)。"""
    trades: list[dict] = []
    n = len(dataset)
    if n < 2:
        return trades
    pos_dir = dataset["Signal"].iat[0]
    fill_idx0 = min(FILL_LAG_BARS, n - 1)
    entry_price = dataset["Open"].iat[fill_idx0]
    entry_time = dataset["Datetime"].iat[fill_idx0]
    pending = None

    for i in range(1, n):
        sig = dataset["Signal"].iat[i]
        if pending is not None and i == pending["fill_at"]:
            fill_price = dataset["Open"].iat[i]
            pnl_pts = (fill_price - entry_price) if pos_dir == 1 else (entry_price - fill_price)
            trades.append(dict(direction="long" if pos_dir == 1 else "short",
                                 entry_time=entry_time, exit_time=dataset["Datetime"].iat[i],
                                 entry_price=entry_price, exit_price=fill_price,
                                 pnl=pnl_pts - COST_PTS_PER_TRADE, forced=False))
            pos_dir = pending["new_dir"]
            entry_price = fill_price
            entry_time = dataset["Datetime"].iat[i]
            pending = None
        if sig != pos_dir and pending is None:
            pending = dict(fill_at=min(i + FILL_LAG_BARS, n - 1), new_dir=sig)

    last_close = dataset["Close"].iat[n - 1]
    pnl_pts = (last_close - entry_price) if pos_dir == 1 else (entry_price - last_close)
    trades.append(dict(direction="long" if pos_dir == 1 else "short",
                         entry_time=entry_time, exit_time=dataset["Datetime"].iat[n - 1],
                         entry_price=entry_price, exit_price=last_close,
                         pnl=pnl_pts - COST_PTS_PER_TRADE, forced=True))
    return trades


def run_sleeve(df: pd.DataFrame, window: int, atr_threshold: float) -> list[dict]:
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return []
    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return []
    dataset["Signal"] = 1
    short, last_entry = False, -COOLDOWN
    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
    trades = simulate_with_forceclose(dataset)
    for t in trades:
        t["window"] = window
    return trades


def run_config(bars_by_day: dict, days: list[str], windows: list[int], atr_threshold: float) -> dict:
    daily_pnls, all_trades = [], []
    for day in days:
        df = bars_by_day.get(day)
        if df is None or df.empty:
            daily_pnls.append(0.0)
            continue
        day_trades = []
        for w in windows:
            day_trades.extend(run_sleeve(df, w, atr_threshold))
        daily_pnls.append(sum(t["pnl"] for t in day_trades))
        all_trades.extend(day_trades)
    arr = np.array(daily_pnls)
    return dict(total_pnl=float(arr.sum()), total_trades=len(all_trades), n_days=len(arr),
                positive_days=int((arr > 0).sum()), daily_pnls=arr.tolist())


def assert_accounting_closed(bars_by_day: dict, days: list[str], windows: list[int], atr_threshold: float) -> None:
    """記帳紀律assert：確認每個(day,window)區塊裡最後一筆trade一定是forced=True
    （沒有靜默丟棄尾部位）——只要該區塊產生了至少一筆訊號bar。"""
    for day in days:
        df = bars_by_day.get(day)
        if df is None or df.empty:
            continue
        for w in windows:
            trades = run_sleeve(df, w, atr_threshold)
            if trades:
                assert trades[-1]["forced"] is True, f"{day} window={w} 缺少force-close尾部位"


def main() -> None:
    all_days = load_days()
    sample_days = all_days[::5]
    print(f"樣本天數: {len(sample_days)}（跟日盤版tx_channel_tick_pnl_check.py同樣抽樣節奏）\n")

    # ---- bar-cache 版本（sess='night'，快取本身只到23:59） ----
    cache_bars = {}
    for d in sample_days:
        df = load_day_bars_with_sess(d)
        seg = df[df["sess"] == "night"].reset_index(drop=True)
        if not seg.empty:
            cache_bars[d] = seg[["Datetime", "Open", "High", "Low", "Close", "Volume"]]

    # ---- tick-derived 版本，兩種範圍 ----
    tick_bars_trunc = {}     # 對齊快取：只到23:59（apples-to-apples）
    tick_bars_full = {}      # 完整夜盤：15:00 D ~ 約05:00 D+1（快取沒有的部分）
    concat_diag = []
    for d in sample_days:
        combined, diag = build_night_session(d)
        concat_diag.append(diag)
        if combined is None or combined.empty:
            continue
        tb_trunc = resample_ticks(combined, "15:00:00", NIGHT_TRUNC_END)
        if not tb_trunc.empty:
            tick_bars_trunc[d] = tb_trunc
        tb_full = resample_ticks(combined, None, None)
        if not tb_full.empty:
            tick_bars_full[d] = tb_full

    common_days = sorted(set(cache_bars) & set(tick_bars_trunc))
    print(f"cache/tick(截斷至23:59)兩邊都有效的天數: {len(common_days)}/{len(sample_days)}\n")

    # ===== 第一層：資料完整性（bar-cache vs tick-resample，15:00~23:59） =====
    print("=== 第一層：分鐘bar落差（15:00~23:59，對齊快取範圍） ===")
    bar_diffs = []
    for d in common_days:
        cb = cache_bars[d].copy()
        # compare_bars（沿用日盤版 tx_channel_tick_validation.py）內部直接把
        # cached["Datetime"] 當成合併鍵 cached["minute"]，假設它已經是 "HH:MM" 字串
        # （tx_channel_tick_validation.load_cached_day_bars 是直接吃SQL raw string，
        # 沒有轉pd.to_datetime）。本腳本的 cache_bars 來自 load_day_bars_with_sess，
        # Datetime 欄位已被轉成 Timestamp——這裡把整欄轉回同樣的 "HH:MM" 字串格式，
        # 保持跟日盤版一致的合併鍵定義，否則 Timestamp vs 字串會merge不上（悄悄變成
        # 0配對，比對結果失真但不報錯，是很隱蔽的一種bug）。
        cb["Datetime"] = cb["Datetime"].dt.strftime("%H:%M")
        tb = tick_bars_trunc[d]
        cmp = compare_bars(cb, tb, d)
        bar_diffs.append(cmp)
        if cmp.get("n_matched_minutes", 0) > 0:
            cd = cmp["diffs"]["Close"]
            print(f"  {d}: 配對{cmp['n_matched_minutes']}/{cmp['n_cache_minutes']}分鐘  "
                  f"Close最大差={cd['max_abs_diff']:.1f}pt  平均差={cd['mean_abs_diff']:.3f}pt  "
                  f"完全一致={cd['n_exact_match']}/{cd['n_total']}")
    valid_diffs = [r for r in bar_diffs if r.get("n_matched_minutes", 0) > 0]
    mean_close_diffs = [r["diffs"]["Close"]["mean_abs_diff"] for r in valid_diffs]
    max_close_diffs = [r["diffs"]["Close"]["max_abs_diff"] for r in valid_diffs]
    print(f"\n彙總({len(valid_diffs)}天有效比對): Close平均差 mean={np.mean(mean_close_diffs):.3f}pt "
          f"median={np.median(mean_close_diffs):.3f}pt  單根最大差={np.max(max_close_diffs):.1f}pt\n")

    # ===== 第二層：訊號/損益重跑比較（同一段15:00~23:59） =====
    atr_th_cache = compute_global_atr_threshold(common_days, cache_bars)
    atr_th_tick = compute_global_atr_threshold(common_days, tick_bars_trunc)
    print(f"ATR門檻(pooled 5th pct, {len(common_days)}天樣本): cache版={atr_th_cache:.2f}  tick版={atr_th_tick:.2f}\n")

    # 記帳紀律assert：確認force-close真的每個區塊都補了尾部位
    assert_accounting_closed(cache_bars, common_days, [W233], atr_th_cache)
    assert_accounting_closed(tick_bars_trunc, common_days, [W233], atr_th_tick)
    assert_accounting_closed(cache_bars, common_days, SLEEVES, atr_th_cache)
    assert_accounting_closed(tick_bars_trunc, common_days, SLEEVES, atr_th_tick)
    print("記帳紀律assert通過：所有(day,window)區塊皆以force-close結尾，無靜默丟棄的尾部位。\n")

    print("=== w233（夜盤，15:00~23:59） ===")
    r_cache = run_config(cache_bars, common_days, [W233], atr_th_cache)
    r_tick = run_config(tick_bars_trunc, common_days, [W233], atr_th_tick)
    print(f"cache版: trades={r_cache['total_trades']}  net={r_cache['total_pnl']:,.1f}pt  正報酬天={r_cache['positive_days']}/{r_cache['n_days']}")
    print(f"tick版:  trades={r_tick['total_trades']}  net={r_tick['total_pnl']:,.1f}pt  正報酬天={r_tick['positive_days']}/{r_tick['n_days']}")
    pnl_diff_pct_w233 = (r_tick["total_pnl"] - r_cache["total_pnl"]) / abs(r_cache["total_pnl"]) * 100 if r_cache["total_pnl"] else float("nan")
    print(f"損益差距: {pnl_diff_pct_w233:+.1f}%\n")

    print("=== w34+w55+w89（夜盤，15:00~23:59） ===")
    r_cache_p = run_config(cache_bars, common_days, SLEEVES, atr_th_cache)
    r_tick_p = run_config(tick_bars_trunc, common_days, SLEEVES, atr_th_tick)
    print(f"cache版: trades={r_cache_p['total_trades']}  net={r_cache_p['total_pnl']:,.1f}pt  正報酬天={r_cache_p['positive_days']}/{r_cache_p['n_days']}")
    print(f"tick版:  trades={r_tick_p['total_trades']}  net={r_tick_p['total_pnl']:,.1f}pt  正報酬天={r_tick_p['positive_days']}/{r_tick_p['n_days']}")
    pnl_diff_pct_portfolio = (r_tick_p["total_pnl"] - r_cache_p["total_pnl"]) / abs(r_cache_p["total_pnl"]) * 100 if r_cache_p["total_pnl"] else float("nan")
    print(f"損益差距: {pnl_diff_pct_portfolio:+.1f}%\n")

    print("=== 逐日損益比較（w233） ===")
    cache_daily = dict(zip(common_days, r_cache["daily_pnls"]))
    tick_daily = dict(zip(common_days, r_tick["daily_pnls"]))
    n_same_sign = 0
    for d in common_days:
        c, t = cache_daily[d], tick_daily[d]
        same = (c > 0) == (t > 0) or (c == 0 and t == 0)
        n_same_sign += int(same)
        print(f"  {d}: cache={c:>9.1f}pt  tick={t:>9.1f}pt  同號={'Y' if same else 'N'}")
    print(f"\n方向一致天數: {n_same_sign}/{len(common_days)}")

    # ===== 額外揭露：快取沒對照組的完整夜盤(含00:00~05:00尾段) =====
    print("\n=== 額外揭露（無bar-cache對照組）：完整夜盤 15:00 D ~ 約05:00 D+1（tick-only） ===")
    full_common_days = sorted(set(tick_bars_full) & set(common_days))
    atr_th_full = compute_global_atr_threshold(full_common_days, tick_bars_full)
    r_full_w233 = run_config(tick_bars_full, full_common_days, [W233], atr_th_full)
    r_full_portfolio = run_config(tick_bars_full, full_common_days, SLEEVES, atr_th_full)
    print(f"w233 完整夜盤(tick-only): trades={r_full_w233['total_trades']}  net={r_full_w233['total_pnl']:,.1f}pt  "
          f"（對照同一批天數截斷版trades={r_tick['total_trades']}  net={r_tick['total_pnl']:,.1f}pt）")
    print(f"portfolio 完整夜盤(tick-only): trades={r_full_portfolio['total_trades']}  net={r_full_portfolio['total_pnl']:,.1f}pt  "
          f"（對照截斷版trades={r_tick_p['total_trades']}  net={r_tick_p['total_pnl']:,.1f}pt）")
    print("解讀：以上『完整版 vs 截斷版』的差距，反映的是bar-cache的涵蓋率缺口(00:00~05:00從未進入"
          "快取)，不是tick-vs-bar精度問題——這是本輪意外發現的第二個問題，跟主要研究問題"
          "(資料落差會不會顯著影響策略結論)分開陳述，不要混為一談。")

    out = dict(
        sample_days=sample_days, common_days=common_days,
        night_trunc_end=NIGHT_TRUNC_END,
        bar_diffs=bar_diffs,
        w233=dict(cache=r_cache, tick=r_tick, pnl_diff_pct=pnl_diff_pct_w233),
        portfolio=dict(cache=r_cache_p, tick=r_tick_p, pnl_diff_pct=pnl_diff_pct_portfolio),
        atr_threshold=dict(cache=atr_th_cache, tick=atr_th_tick),
        daily_comparison=dict(cache=cache_daily, tick=tick_daily, n_same_sign=n_same_sign, n_days=len(common_days)),
        full_night_extra=dict(
            n_days=len(full_common_days),
            w233=r_full_w233, portfolio=r_full_portfolio,
            atr_threshold_full=atr_th_full,
        ),
        concat_diag=concat_diag,
        caveats=[
            "bar-cache(sess='night')只涵蓋15:00~23:59，00:00~05:00的夜盤尾段從未進入快取——"
            "本腳本的主比較(cache vs tick)因此也截在23:59，是快取本身的限制，不是刻意選擇。",
            f"樣本{len(sample_days)}天，來自bars.sqlite可用的83天(2026-04-01~2026-07-31)每5天抽1天，"
            f"跟日盤版同一批交易日、同一抽樣節奏；覆蓋比例約{len(sample_days)}/83"
            f"={len(sample_days)/83*100:.0f}%。",
            "PnL重跑改用force-close記帳（跟日盤版tx_channel_tick_pnl_check.py用的"
            "simulate_pnl_realistic不同，後者已知會靜默丟棄session尾部位——H-TXD-SESSION-"
            "END-ACCOUNTING）。cache版跟tick版統一用force-close，比較本身仍是公平的，但"
            "這代表本腳本算出的絕對pnl數字，跟日盤版tick_pnl_check_results.json的絕對數字"
            "不能直接並排比較(記帳基礎不同)，只能各自看『cache vs tick』的相對差距。",
            "只測了w233與w34+55+89(cooldown=8, rsi_exit=False)這兩組設定，沒有窮舉所有"
            "研究過的參數組合。",
            "沒有做正式統計顯著性檢定，跟這條研究線一貫的方法論缺口(H-SC-STAT-"
            "SIGNIFICANCE-GAP)一致，這裡也不例外。",
        ],
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "tick_night_pnl_check_results.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
