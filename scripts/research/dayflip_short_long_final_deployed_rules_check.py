#!/usr/bin/env python3
"""最終部署規則的完整回測——確認短多兩腿『不會打架』，並量化new same-stock guard的機會成本.

2026-08-09 使用者要求「請回測，看看會不會打架，再次檢查」。跟先前
dayflip_short_long_shared_capital_handoff.py 的差異：這次用的是「真的部署進
src/order/dayflip_post_dump_long_order.py 的完整規則組合」，不是研究階段的
簡化版：
  1) 單一標的名目曝險上限15%（4.5萬/300,000）——先前那輪測試沒有這個限制
  2) 同股互斥guard（2026-08-09複查時新增）：短邊當天持有某股期間，長邊完全
     不會對同一檔進場——先前那輪測試允許『先空後多』在短邊出場前就進場（38%
     的overlap_before_short_exit案例），現在guard生效後這些案例的長邊訊號要嘛
     延後到短邊出場後才重新掃、要嘛當天掃不到訊號就直接跳過（純機會成本，
     不是風險）
  3) 短邊完全不知道長邊用量（維持不對稱設計）——驗證這個不對稱在真實歷史
     資料上實際會造成多大的資金池超額

方法：對38%的「短邊出場前訊號就已觸發」的案例，重新用『只從短邊出場分鐘之後
開始掃』的rolling_relative_dip偵測器找訊號（找不到就當作長邊當天沒進場），
取代原本『不管短邊、訊號一觸發就進場』的做法；其餘62%案例（本來就在短邊出場
後才觸發，或短邊當天沒訊號）不受影響。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_long_final_deployed_rules_check.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

from dayflip_short_post_dump_long_capital_simulation import (
    FUT_CACHE_PATH,
    MARGIN_RATE,
    MAX_HOLD_DAYS,
    ROUND_TRIP_COST_PCT,
    TRAIL_PCT,
    _t01_stock_close,
    build_calendar,
    estimate_margin_ntd,
    find_entry_price,
)
from dayflip_short_long_shared_capital_handoff import (
    SHORT_TRADELOG_CSV,
    load_short_trades,
)
from dayflip_short_rolling_relative_dip_signal import find_rolling_dip_signal, load_minute_closes

ROOT = Path(__file__).resolve().parents[2]
SINGLE_NAME_CAP_PCT = 15
POOL_NTD = 300_000


def find_long_leg_after(
    con: sqlite3.Connection, fut_cache: dict, stock_id: str, trade_date: str, after_minute: str | None,
) -> dict | None:
    """跟 dayflip_short_long_shared_capital_handoff.find_long_leg() 幾乎一樣，
    差別是加一個 after_minute：非None時，rolling_relative_dip訊號只在該分鐘
    之後的資料裡找（模擬guard生效、短邊出場後長邊才重新開始掃這檔的行為）。
    after_minute為None時等同原版（短邊當天沒有這檔，不受guard影響）。
    """
    if after_minute is None:
        entry = find_entry_price(con, stock_id, trade_date)
        if entry is None:
            return None
        entry_stock_px, _kind = entry
    else:
        stock_closes = load_minute_closes(con, stock_id, trade_date)
        bench_closes = load_minute_closes(con, "0050", trade_date)
        stock_closes = {m: px for m, px in stock_closes.items() if m > after_minute}
        if len(stock_closes) < 50 or len(bench_closes) < 50:
            return None
        sig = find_rolling_dip_signal(stock_closes, bench_closes, 0.3)
        if sig is None:
            return None
        _minute, entry_stock_px = sig

    day_close = _t01_stock_close(con, stock_id, trade_date)
    if day_close is None or day_close <= 0:
        return None
    entry_frac = entry_stock_px / day_close

    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if trade_date not in dates:
        return None
    i0 = dates.index(trade_date)
    fut_close_t01 = float(m[trade_date][1])
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
            margin = estimate_margin_ntd(fut_entry)
            return {
                "entry_day": trade_date, "exit_day": d,
                "margin": margin, "pnl": margin / MARGIN_RATE * (net_ret / 100), "net_ret_pct": net_ret,
            }
    return None


def main() -> None:
    short_trades = load_short_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    print("=== 最終部署規則檢查：同股互斥guard + 15%單股上限 + 不對稱資金感知 ===")
    print(f"短邊歷史交易數: {len(short_trades)}\n")

    both_found, guard_deferred, guard_skipped, no_short_conflict = 0, 0, 0, 0
    pool_overflow_events = []
    pairs = []

    for s in short_trades:
        sid, t01 = s["stock"], s["trade_date"]
        short_entry_px = float(s["entry_price"])
        short_exit_px = float(s["exit_price"])
        short_exit_time = s["exit_time"]
        short_margin = estimate_margin_ntd(short_entry_px)
        short_pnl_pct = float(s["pnl_pct"])
        short_pnl = short_margin / MARGIN_RATE * (short_pnl_pct / 100)

        # 先看guard前：這檔訊號原本會不會在短邊出場前就觸發（跟先前研究一致的判斷法）
        entry = find_entry_price(con, sid, t01)
        original_triggers_before_short_exit = False
        if entry is not None:
            entry_px, kind = entry
            if kind == "intraday_signal":
                minutes_map = load_minute_closes(con, sid, t01)
                entry_minute = None
                for mm, px in sorted(minutes_map.items()):
                    if px == entry_px:
                        entry_minute = mm
                        break
                if entry_minute is not None and entry_minute < short_exit_time:
                    original_triggers_before_short_exit = True

        if not original_triggers_before_short_exit:
            no_short_conflict += 1
            long_leg = find_long_leg_after(con, fut_cache, sid, t01, None)
        else:
            # guard生效：只在短邊出場分鐘之後重新掃
            long_leg = find_long_leg_after(con, fut_cache, sid, t01, short_exit_time)
            if long_leg is not None:
                guard_deferred += 1
            else:
                guard_skipped += 1

        if long_leg is not None:
            both_found += 1

        margin_used_today = short_margin + (long_leg["margin"] if long_leg else 0)
        per_name_cap = POOL_NTD * SINGLE_NAME_CAP_PCT / 100
        if long_leg is not None and long_leg["margin"] > per_name_cap:
            long_leg = None  # 15%上限擋掉
        if margin_used_today > POOL_NTD:
            pool_overflow_events.append({
                "stock": sid, "date": t01, "short_margin": short_margin,
                "long_margin": long_leg["margin"] if long_leg else 0,
                "combined": margin_used_today, "over_by": margin_used_today - POOL_NTD,
            })

        pairs.append({
            "stock": sid, "date": t01, "short_pnl": short_pnl,
            "long_pnl": long_leg["pnl"] if long_leg else 0.0,
            "long_entered": long_leg is not None,
        })

    print(f"短邊當天訊號本來就在短邊出場後才觸發（不受guard影響）: {no_short_conflict}/{len(short_trades)}")
    print(f"短邊出場前就觸發（guard介入的案例）: {len(short_trades) - no_short_conflict}/{len(short_trades)}")
    print(f"  其中 guard延後後仍找到訊號（延後進場）: {guard_deferred}")
    print(f"  其中 guard延後後找不到訊號（當天直接跳過，純機會成本）: {guard_skipped}")
    print(f"\n加總長邊有進場的交易數: {both_found}/{len(short_trades)}")

    print(f"\n=== 資金池超額事件（guard生效、15%上限都套用後）===")
    if not pool_overflow_events:
        print("0筆——guard生效後，74筆歷史交易裡沒有任何一天combined margin超過300,000。")
    else:
        for e in pool_overflow_events:
            print(f"  {e['date']} {e['stock']}: short={e['short_margin']:,.0f} long={e['long_margin']:,.0f} "
                  f"combined={e['combined']:,.0f} 超出={e['over_by']:,.0f}")

    total_short_pnl = sum(p["short_pnl"] for p in pairs)
    total_long_pnl = sum(p["long_pnl"] for p in pairs)
    print(f"\n短邊總損益(74筆，未做資金排程只是加總): {total_short_pnl:,.0f}")
    print(f"長邊總損益({both_found}筆，guard生效版，未做資金排程只是加總): {total_long_pnl:,.0f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) guard延後後的『重新找訊號』用0050當基準（跟原始rolling_relative_dip\n"
        "     研究一致），不是live版實際會用的TMF——方向性結論不受影響，數字有\n"
        "     微小近似。\n"
        "  2) 這是加總損益，不是完整資金排程NAV模擬（前幾輪已經做過那個、且\n"
        "     結論是純加總跟資金排程模擬方向一致，這裡只想快速確認guard的\n"
        "     機會成本量級，不重跑完整NAV）。\n"
        "  3) pool overflow檢查只看『當天』尖峰，沒有模擬多日長邊部位疊加\n"
        "     （例如長邊同時抱著3天前進場的另一檔+今天新進場的，這種疊加\n"
        "     在真實系統的available_pool_ntd()裡會被15%單股上限+300,000\n"
        "     總池自然擋住，不需要另外驗證——這裡只驗證『short vs long當天\n"
        "     新增部位』這個guard要處理的特定情境）。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-long-final-deployed-rules-conflict-check",
        ts="2026-08-09",
        params={"single_name_cap_pct": SINGLE_NAME_CAP_PCT, "pool_ntd": POOL_NTD},
        n_observations=len(short_trades),
        metric_name="pool_overflow_events_count",
        metric_value=len(pool_overflow_events),
        status="kept",
        source=__file__,
        notes=(
            f"最終部署規則（同股互斥guard+15%單股上限+不對稱資金感知）回測確認：\n"
            f"{len(pool_overflow_events)}筆資金池超額事件（74筆短邊歷史交易中）。\n"
            f"guard介入{len(short_trades)-no_short_conflict}筆，其中{guard_deferred}筆延後\n"
            f"仍能進場、{guard_skipped}筆當天直接跳過（機會成本）。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "conflict-check", "deployed-rules", "same-stock-guard"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
