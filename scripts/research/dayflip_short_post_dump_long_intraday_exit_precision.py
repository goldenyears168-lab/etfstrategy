#!/usr/bin/env python3
"""移動停利用『每日收盤才檢查一次』vs『分鐘級連續追蹤』，出場價差多少.

背景：使用者指出分鐘級資料的價值不在於拆成更多筆交易（那條路已經測過、被拒絕），
而是『追蹤程度較高』——現行 exit.method: trailing_stop 只在 futures_daily_cache.json
的『每日收盤價』上檢查一次5%回檔有沒有觸發，等於一天當中只看一眼。如果盤中已經
從高點回檔超過5%、但收盤前又拉回，EOD-only版本那天不會觸發；如果分鐘級連續追蹤，
會在盤中觸發的那一刻就出場——不是「多做幾筆交易」，是「同一筆交易，用同樣的5%
停利規則，抓到規則被觸發的那一刻，而不是等到收盤才確認」。

這裡測：用個股1分K × 當日期貨/現貨basis比例，重建整個持有期間（進場日到出場日）
的連續分鐘級近似期貨價格，peak-tracking跟5%回檔判斷改成逐分鐘檢查，而不是
逐日檢查，看跟現行EOD-only版本比，出場價/報酬差多少、有多少筆會提早在不同天
出場、提早出場對報酬是利是弊。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_post_dump_long_intraday_exit_precision.py
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
    MAX_HOLD_DAYS,
    ROUND_TRIP_COST_PCT,
    TRAIL_PCT,
    _t01_stock_close,
    build_calendar,
    find_entry_price,
    load_trades,
)

ROOT = Path(__file__).resolve().parents[2]


def load_stock_minutes(con: sqlite3.Connection, stock_id: str, trade_date: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, trade_date)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def eod_only_exit(fut_cache: dict, stock_id: str, entry_day: str, entry_frac: float) -> tuple[float, str, int] | None:
    """現行規格：只在每日收盤價檢查5%回檔（等於 dayflip_short_post_dump_long_capital_simulation.py 的邏輯）."""
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
            net_ret = (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
            return net_ret, d, h
    return None


def intraday_precision_exit(
    con: sqlite3.Connection, fut_cache: dict, calendar: list[str], cal_idx: dict[str, int],
    stock_id: str, entry_day: str, entry_minute: str, entry_frac: float,
) -> tuple[float, str, int] | None:
    """分鐘級連續追蹤：整個持有期用個股1分K × 當日basis比例重建，逐分鐘檢查5%回檔."""
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if entry_day not in dates or entry_day not in cal_idx:
        return None
    i0 = dates.index(entry_day)
    j0 = cal_idx[entry_day]
    fut_close_t01 = float(m[entry_day][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac

    horizon_end = min(i0 + MAX_HOLD_DAYS, len(dates) - 1)
    window_dates = dates[i0:horizon_end + 1]

    peak = fut_entry
    entry_minutes_map = load_stock_minutes(con, stock_id, entry_day)
    triggered = None
    for day_offset, d in enumerate(window_dates):
        day_stock_close = _t01_stock_close(con, stock_id, d)
        day_fut_px = float((m.get(d) or [0, 0])[1])
        if not day_stock_close or day_stock_close <= 0 or day_fut_px <= 0:
            continue
        basis_ratio = day_fut_px / day_stock_close
        if day_offset == 0:
            day_minutes = entry_minutes_map
            start_minute = entry_minute
        else:
            day_minutes = load_stock_minutes(con, stock_id, d)
            start_minute = None
        for mm in sorted(day_minutes):
            if start_minute is not None and mm < start_minute:
                continue
            px_approx = day_minutes[mm] * basis_ratio
            peak = max(peak, px_approx)
            pullback = (peak - px_approx) / peak * 100
            if pullback >= TRAIL_PCT:
                triggered = (px_approx, d, day_offset, mm)
                break
        if triggered:
            break
        if day_offset == len(window_dates) - 1:
            # max_hold触发：用当天最后一分钟价格
            last_mm = sorted(day_minutes)[-1] if day_minutes else None
            if last_mm:
                triggered = (day_minutes[last_mm] * basis_ratio, d, day_offset, last_mm)

    if triggered is None:
        return None
    px, exit_day, hold_days, exit_minute = triggered
    net_ret = (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
    return net_ret, exit_day, hold_days


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))
    calendar = build_calendar(con, min(t["trade_date"] for t in trades), "2026-08-07")
    cal_idx = {d: i for i, d in enumerate(calendar)}

    paired = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        entry = find_entry_price(con, sid, t01)
        if entry is None:
            continue
        entry_px, entry_kind = entry
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        entry_frac = entry_px / day_close

        entry_minutes_map = load_stock_minutes(con, sid, t01)
        entry_minute = "13:30"
        if entry_kind == "intraday_signal":
            for mm, px in sorted(entry_minutes_map.items()):
                if px == entry_px:
                    entry_minute = mm
                    break

        eod = eod_only_exit(fut_cache, sid, t01, entry_frac)
        if eod is None:
            continue
        eod_ret, eod_exit_day, eod_hold_days = eod

        precise = intraday_precision_exit(con, fut_cache, calendar, cal_idx, sid, t01, entry_minute, entry_frac)
        if precise is None:
            continue
        precise_ret, precise_exit_day, precise_hold_days = precise

        paired.append({
            "stock": sid, "trade_date": t01,
            "eod_ret": eod_ret, "eod_exit_day": eod_exit_day, "eod_hold_days": eod_hold_days,
            "precise_ret": precise_ret, "precise_exit_day": precise_exit_day, "precise_hold_days": precise_hold_days,
        })

    print("=== 移動停利：EOD-only 檢查 vs 分鐘級連續追蹤——配對比較 ===")
    print(f"可配對交易數: {len(paired)}/{len(trades)}\n")

    if not paired:
        print("無可比較樣本，結束。")
        return

    eod_arr = np.array([p["eod_ret"] for p in paired])
    precise_arr = np.array([p["precise_ret"] for p in paired])
    diff = precise_arr - eod_arr
    earlier = sum(1 for p in paired if p["precise_hold_days"] < p["eod_hold_days"])
    same_day = sum(1 for p in paired if p["precise_hold_days"] == p["eod_hold_days"])
    later = sum(1 for p in paired if p["precise_hold_days"] > p["eod_hold_days"])

    print(f"EOD-only：平均淨報酬={eod_arr.mean():+.3f}% std={eod_arr.std():.3f}% "
          f"平均持有天數={np.mean([p['eod_hold_days'] for p in paired]):.1f}")
    print(f"分鐘級連續追蹤：平均淨報酬={precise_arr.mean():+.3f}% std={precise_arr.std():.3f}% "
          f"平均持有天數={np.mean([p['precise_hold_days'] for p in paired]):.1f}")
    print(f"配對差異（分鐘級-EOD）：平均={diff.mean():+.3f}% 分鐘級較優比例={float(np.mean(diff>0))*100:.0f}%")
    print(f"\n出場時機分布：分鐘級比EOD更早出場={earlier}筆（{earlier/len(paired)*100:.0f}%）、"
          f"同一天={same_day}筆、分鐘級較晚={later}筆")

    earlier_diffs = [d for p, d in zip(paired, diff) if p["precise_hold_days"] < p["eod_hold_days"]]
    if earlier_diffs:
        print(f"「提早出場」的那{len(earlier_diffs)}筆，提早出場對報酬的平均影響={np.mean(earlier_diffs):+.3f}%"
              f"（{'正面:提早停損/停利躲過後續更差的價格' if np.mean(earlier_diffs)>0 else '負面:提早出場錯過收盤前的價格回升'}）")

    by_date = {}
    for p, d in zip(paired, diff):
        by_date.setdefault(p["trade_date"], []).append(d)
    date_level_diff = np.array([np.mean(v) for v in by_date.values()])
    print(f"\n日聚集後（{len(date_level_diff)}個不同訊號日）：平均差異={date_level_diff.mean():+.3f}% "
          f"分鐘級較優的日數比例={float(np.mean(date_level_diff>0))*100:.0f}%")

    print(
        "\n⚠️ 限制：\n"
        "  1) 全程用個股1分K × 當日期貨/現貨日收盤比例近似重建期貨分鐘價，只反映\n"
        "     日均basis，沒有真正的盤中basis波動——這對『抓到盤中確切觸發點』這個\n"
        "     目的來說是必要但不完美的近似。\n"
        "  2) max_hold_days觸發的出場一律用當天最後一分鐘價，跟EOD-only版本的\n"
        "     『當日收盤價』本應接近但不保證完全相等（個股最後一筆1分K vs 官方\n"
        "     收盤價可能有極小落差）。\n"
        "  3) 沒有考慮：真正要做到『分鐘級連續追蹤』，下單層要有能對應頻率的\n"
        "     輪詢機制（目前poll_script尚未實作），這裡只驗證『如果做得到』報酬\n"
        "     差多少，不是可行性評估。"
    )

    survives = diff.mean() > 0
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-intraday-exit-precision",
        ts="2026-08-09",
        params={"trail_pct": TRAIL_PCT, "max_hold_days": MAX_HOLD_DAYS},
        n_observations=len(paired),
        metric_name="mean_paired_diff_pct_precise_minus_eod",
        metric_value=float(diff.mean()),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"測試5%移動停利用分鐘級連續追蹤(而非現行EOD-only每日收盤檢查一次)出場，\n"
            f"跟現行版本配對比較{len(paired)}筆交易。EOD平均{eod_arr.mean():+.3f}%、\n"
            f"分鐘級平均{precise_arr.mean():+.3f}%，差異{diff.mean():+.3f}%，分鐘級較優比例\n"
            f"{float(np.mean(diff>0))*100:.0f}%。{earlier}筆({earlier/len(paired)*100:.0f}%)分鐘級\n"
            f"比EOD更早出場。跟高頻拆單/資金分鐘排程不同，這裡不改變交易筆數/持倉\n"
            f"結構，純粹測『同一規則、检查频率不同』的執行精度差異。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "exit-precision", "intraday-monitoring"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
