#!/usr/bin/env python3
"""把已驗證的多日持有『拆成很多個數十分鐘短線波段』，配對比較是否比原本更好.

⚠️ 先講清楚跟這條研究線衝突的既有結論：
  - 「不到一天就賣」已測過，樣本外近零/負（post-dump-long-sameday-exit，rejected）
  - 「多腳分段來回進場」（day級重複進場）已測過，沒有顯著效益
    （post-dump-long-multileg-reentry，rejected）
這裡測的是這兩者的更極端版本（拆到數十分鐘），但用的是分鐘級資料（前兩輪都只有
日頻率資料可用），所以是一個新的、獨立的測試，不能假設結論會一樣，要用資料驗證。

方法（配對比較，不重新篩樣本）：拿已知結果的多日交易（進場日、出場日都用
post_dump_long_capital_simulation.py同一套規則算出），在同一段[進場日,出場日]
窗口內，改用「個股1分K × 當日期貨/現貨basis比例」重建的分鐘級近似期貨價格，
搭配真台指期1分K(tx_1m_tick_built_582d)當大盤基準，重複偵測局部量級縮小版的
rolling_relative_dip訊號（窗口5分鐘、門檻0.2%、確認5分鐘——原版是15分鐘/0.3%/
10分鐘，這裡刻意縮小配合「數十分鐘」的時間尺度），每次訊號進場後用小型固定
停利1%/停損0.5%/60分鐘時間停損（先到者出場，且不跨日——收盤前強制平倉，
符合『數十分鐘波段』的定位，不留倉）出場，把同一筆交易拆解出的所有腳的淨損益
加總，跟原本『一路抱到出場』的淨損益直接配對比較。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_post_dump_long_intraday_multileg_decompose.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

from dayflip_short_post_dump_long_capital_simulation import (
    FUT_CACHE_PATH,
    ROUND_TRIP_COST_PCT as ORIGINAL_ROUND_TRIP_COST_PCT,
    TRAIL_PCT,
    MAX_HOLD_DAYS,
    _t01_stock_close,
    build_calendar,
    find_entry_price,
    load_trades,
)

ROOT = Path(__file__).resolve().parents[2]
TX_BARS_DB = Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"
TX_SESS = "day"

LOCAL_WINDOW_MIN = 5
LOCAL_LAG_THRESHOLD_PCT = 0.2
LOCAL_CONFIRM_MINUTES = 5
LEG_TARGET_PCT = 1.0
LEG_STOP_PCT = 0.5
LEG_TIME_STOP_MIN = 60
LEG_ROUND_TRIP_COST_PCT = 0.05


def load_stock_minutes(con: sqlite3.Connection, stock_id: str, trade_date: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, trade_date)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def load_tx_minutes(tx_con: sqlite3.Connection, trade_date: str) -> dict[str, float]:
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND sess=? AND day=? AND c > 0",
        (TX_SOURCE, TX_SESS, trade_date),
    ).fetchall()
    return {t[:5]: c for t, c in rows}


def find_signals_in_day(stock_closes: dict[str, float], tx_closes: dict[str, float]) -> list[str]:
    """一天內重複偵測局部相對弱勢反彈訊號，回傳確認分鐘的清單（按時間排序）."""
    minutes = sorted(set(stock_closes) & set(tx_closes))
    if len(minutes) < LOCAL_WINDOW_MIN + LOCAL_CONFIRM_MINUTES + 5:
        return []

    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < LOCAL_WINDOW_MIN:
            continue
        m0 = minutes[i - LOCAL_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        tx_ret = (tx_closes[m] / tx_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - tx_ret
    lag_minutes = [m for m in minutes if m in rolling_lag]

    signals = []
    start_idx = 0
    while start_idx < len(lag_minutes):
        worst_idx, worst_val = None, 0.0
        for i in range(start_idx, len(lag_minutes)):
            m = lag_minutes[i]
            if rolling_lag[m] < -LOCAL_LAG_THRESHOLD_PCT and rolling_lag[m] < worst_val:
                worst_val = rolling_lag[m]
                worst_idx = i
        if worst_idx is None:
            break
        worst_minute = lag_minutes[worst_idx]
        confirmed_idx = None
        for i in range(worst_idx + 1, len(lag_minutes)):
            elapsed = i - worst_idx
            m = lag_minutes[i]
            if elapsed >= LOCAL_CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
                confirmed_idx = i
                break
        if confirmed_idx is None:
            break
        signals.append(lag_minutes[confirmed_idx])
        start_idx = confirmed_idx + 1
    return signals


def decompose_trade(
    con: sqlite3.Connection, tx_con: sqlite3.Connection, fut_cache: dict,
    stock_id: str, window_days: list[str],
) -> list[float]:
    """在[進場日..出場日]窗口內找所有數十分鐘短波段腳，回傳每腳淨報酬%清單."""
    legs = []
    for day in window_days:
        stock_closes = load_stock_minutes(con, stock_id, day)
        if len(stock_closes) < 50:
            continue
        tx_closes = load_tx_minutes(tx_con, day)
        if len(tx_closes) < 50:
            continue
        day_stock_close = _t01_stock_close(con, stock_id, day)
        m = fut_cache.get(stock_id) or {}
        day_fut_close = float((m.get(day) or [0, 0])[1])
        if not day_stock_close or day_stock_close <= 0 or day_fut_close <= 0:
            continue
        basis_ratio = day_fut_close / day_stock_close

        signal_minutes = find_signals_in_day(stock_closes, tx_closes)
        minutes_sorted = sorted(stock_closes)
        idx_by_minute = {mm: i for i, mm in enumerate(minutes_sorted)}

        for sig_m in signal_minutes:
            entry_idx = idx_by_minute.get(sig_m)
            if entry_idx is None:
                continue
            entry_px = stock_closes[sig_m] * basis_ratio
            exit_ret = None
            for j in range(entry_idx + 1, len(minutes_sorted)):
                mm2 = minutes_sorted[j]
                px2 = stock_closes[mm2] * basis_ratio
                ret = (px2 / entry_px - 1) * 100
                elapsed = j - entry_idx
                if ret >= LEG_TARGET_PCT or ret <= -LEG_STOP_PCT or elapsed >= LEG_TIME_STOP_MIN:
                    exit_ret = ret
                    break
            if exit_ret is None and len(minutes_sorted) > entry_idx + 1:
                last_px = stock_closes[minutes_sorted[-1]] * basis_ratio
                exit_ret = (last_px / entry_px - 1) * 100
            if exit_ret is not None:
                legs.append(exit_ret - LEG_ROUND_TRIP_COST_PCT)
    return legs


def original_multiday_net_ret(fut_cache: dict, stock_id: str, entry_day: str, entry_frac: float) -> tuple[float, str] | None:
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if entry_day not in dates:
        return None
    i0 = dates.index(entry_day)
    fut_close_t01 = float(m[entry_day][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac
    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        if i0 + h >= len(dates):
            return None
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            return (px / fut_entry - 1) * 100 - ORIGINAL_ROUND_TRIP_COST_PCT, d
    return None


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    calendar = build_calendar(con, min(t["trade_date"] for t in trades), "2026-08-07")
    cal_idx = {d: i for i, d in enumerate(calendar)}

    paired = []
    n_no_original, n_no_legs = 0, 0
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        entry = find_entry_price(con, sid, t01)
        if entry is None:
            continue
        entry_px, _ = entry
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        entry_frac = entry_px / day_close

        orig = original_multiday_net_ret(fut_cache, sid, t01, entry_frac)
        if orig is None:
            n_no_original += 1
            continue
        orig_ret, exit_day = orig

        if t01 not in cal_idx or exit_day not in cal_idx:
            continue
        window_days = calendar[cal_idx[t01]:cal_idx[exit_day] + 1]
        legs = decompose_trade(con, tx_con, fut_cache, sid, window_days)
        if not legs:
            n_no_legs += 1
            continue
        decomposed_ret = float(np.sum(legs))
        paired.append({
            "stock": sid, "trade_date": t01, "exit_day": exit_day,
            "orig_ret": orig_ret, "decomposed_ret": decomposed_ret, "n_legs": len(legs),
        })

    print("=== 拆解成數十分鐘短波段 vs 原本多日抱單——配對比較 ===")
    print(f"可配對交易數: {len(paired)}/{len(trades)}（原始多日路徑算不出={n_no_original}，"
          f"窗口內找不到任何短波段訊號={n_no_legs}）\n")

    if not paired:
        print("無可比較樣本，結束。")
        return

    orig_arr = np.array([p["orig_ret"] for p in paired])
    decomp_arr = np.array([p["decomposed_ret"] for p in paired])
    diff = decomp_arr - orig_arr
    win_rate = float(np.mean(diff > 0))
    n_legs_arr = np.array([p["n_legs"] for p in paired])

    print(f"配對交易數: {len(paired)}")
    print(f"平均每筆拆出的短波段腳數: {n_legs_arr.mean():.1f}（中位數{np.median(n_legs_arr):.0f}）")
    print(f"原始多日抱單：平均淨報酬={orig_arr.mean():+.3f}% std={orig_arr.std():.3f}%")
    print(f"拆解短波段版：平均淨報酬={decomp_arr.mean():+.3f}% std={decomp_arr.std():.3f}%")
    print(f"配對差異（拆解-原始）：平均={diff.mean():+.3f}% 拆解較優比例={win_rate*100:.0f}%")

    # 日聚集穩健性檢查
    by_date = {}
    for p, d in zip(paired, diff):
        by_date.setdefault(p["trade_date"], []).append(d)
    date_level_diff = np.array([np.mean(v) for v in by_date.values()])
    print(f"\n日聚集後（{len(date_level_diff)}個不同訊號日）：平均差異={date_level_diff.mean():+.3f}% "
          f"拆解較優的日數比例={float(np.mean(date_level_diff>0))*100:.0f}%")

    print(
        "\n⚠️ 限制：\n"
        "  1) 短波段訊號的窗口(5分)/門檻(0.2%)/確認(5分)/停利(1%)/停損(0.5%)/時間停損\n"
        "     (60分)都是單次選定，沒有sweep調參——只測『這個方向的一種具體實作』，\n"
        "     不代表已經找出最優參數組合。\n"
        "  2) 每腳都用個股1分K × 當日basis比例近似重建期貨分鐘價，跟前一支腳本\n"
        "     同樣的近似限制（不反映盤中basis波動）。\n"
        "  3) 沒有做資金排程模擬——這裡只比較『同一筆交易換一種切法，報酬總和\n"
        "     好不好』，不是完整NAV/Sharpe/回檔比較。\n"
        "  4) 窗口固定用原始多日抱單已知的[進場日,出場日]，沒有假設拆解版可能會\n"
        "     提早發現訊號惡化而縮短窗口，這對拆解版是保守設定（沒有給它額外優勢），\n"
        "     但也代表沒有測試『拆解本身能不能更早獲利了結』這個潛在好處。"
    )

    survives = diff.mean() > 0
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-intraday-multileg-decompose",
        ts="2026-08-09",
        params={
            "local_window_min": LOCAL_WINDOW_MIN, "local_lag_threshold_pct": LOCAL_LAG_THRESHOLD_PCT,
            "local_confirm_minutes": LOCAL_CONFIRM_MINUTES, "leg_target_pct": LEG_TARGET_PCT,
            "leg_stop_pct": LEG_STOP_PCT, "leg_time_stop_min": LEG_TIME_STOP_MIN,
        },
        n_observations=len(paired),
        metric_name="mean_paired_diff_pct_decomposed_minus_original",
        metric_value=float(diff.mean()),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"把已驗證的多日trailing-stop持有拆成數十分鐘短波段（rolling_relative_dip\n"
            f"局部縮小版+固定停利1%/停損0.5%/60分時間停損），跟原始多日抱單配對比較\n"
            f"{len(paired)}筆交易。平均差異(拆解-原始)={diff.mean():+.3f}%、拆解較優比例\n"
            f"{win_rate*100:.0f}%、日聚集後較優日數比例{float(np.mean(date_level_diff>0))*100:.0f}%。"
            f"跟已拒絕的same-day-exit/day級multi-leg-reentry方向{'一致（也是拒絕）' if not survives else '不一致（這個更細顆粒度版本反而有效）'}。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "intraday-scalping", "multileg-decompose"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
