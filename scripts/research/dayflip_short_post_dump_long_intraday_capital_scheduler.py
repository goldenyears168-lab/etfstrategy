#!/usr/bin/env python3
"""資金排程從『日頻率』升級成『分鐘頻率』——測試同一筆錢一天內能被用幾次.

背景：使用者問「同一筆錢一天可以同時多種操作嗎」，現行
dayflip_short_post_dump_long_capital_simulation.py 是逐日重新分配保證金——
一天結束才處理出場、才騰出保證金給隔天的新訊號，就算A股票10:30就出場、
B股票10:45才觸發訊號，現行模型也只會在「隔天」才讓B股票有機會搶到A的保證金。

這裡把出場的時間戳從「日」精細化到「分鐘」（用個股自己的1分K × 當日期貨/現貨
basis比例，重建個股期貨的分鐘級近似價格——basis相關係數已驗證0.987-0.994），
比較「日頻率排程」vs「分鐘頻率排程」在同一組訊號/出場規則/總資金下，
成交筆數、因資金跳過筆數、總報酬有沒有差異。不改訊號或出場規則本身（那兩者
已經驗證過），純粹測資金排程時間解析度的影響。

⚠️ 重要的先驗判斷：現行2,000,000 NTD情境平均保證金使用率只有16.7%（capital_
simulation.py的輸出），代表資金在日頻率下本來就幾乎沒被用滿——分鐘級排程的
邊際效益預期在大資金情境下很小，只有在資金較緊繃的小資金情境（如300k）才可能
看到實質差異。這裡會掃過300k/600k/1M/2M四種情境驗證這個判斷。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_post_dump_long_intraday_capital_scheduler.py
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
    MARGIN_RATE,
    MAX_HOLD_DAYS,
    ROUND_TRIP_COST_PCT,
    TRAIL_PCT,
    _t01_stock_close,
    build_calendar,
    estimate_margin_ntd,
    find_entry_price,
    load_trades,
)

ROOT = Path(__file__).resolve().parents[2]
CAPITAL_SCENARIOS_NTD = (300_000, 600_000, 1_000_000, 2_000_000)


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, trade_date: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, trade_date)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def prepare_events(con: sqlite3.Connection, signals: list[dict], fut_cache: dict, calendar: list[str]) -> list[dict]:
    """對每筆訊號算出 entry (day,minute) 跟 exit (day,minute)，供分鐘級排程用."""
    cal_idx = {d: i for i, d in enumerate(calendar)}
    events = []
    for s in signals:
        sid, t01 = s["stock"], s["trade_date"]
        if t01 not in cal_idx:
            continue
        i0 = cal_idx[t01]
        m = fut_cache.get(sid) or {}

        # entry：沿用capital_simulation.py既有的日頻率進場邏輯，但額外記錄進場分鐘
        entry_minute_map = load_stock_minute_closes(con, sid, t01)
        entry = find_entry_price(con, sid, t01)
        if entry is None or t01 not in m:
            continue
        entry_stock_price, entry_kind = entry
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        entry_frac = entry_stock_price / day_close
        fut_entry_price = float(m[t01][1]) * entry_frac
        if fut_entry_price <= 0:
            continue
        entry_minute = "13:30"
        if entry_kind == "intraday_signal":
            for mm, px in sorted(entry_minute_map.items()):
                if px == entry_stock_price:
                    entry_minute = mm
                    break

        # exit：先用日頻率找出場日（跟run_simulation()同邏輯），再精細化到分鐘
        dates = sorted(m)
        if t01 not in dates:
            continue
        j0 = dates.index(t01)
        peak = fut_entry_price
        exit_day, exit_close, exit_reason = None, None, None
        for h in range(1, MAX_HOLD_DAYS + 1):
            if j0 + h >= len(dates):
                break
            d = dates[j0 + h]
            px = m.get(d)
            if px is None:
                continue
            close = float(px[1])
            if close <= 0:
                continue
            peak = max(peak, close)
            pullback = (peak - close) / peak * 100
            if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
                exit_day, exit_close = d, close
                exit_reason = "trail" if pullback >= TRAIL_PCT else "max_hold"
                break
        if exit_day is None:
            continue

        # 精細化出場分鐘：用出場日的個股分鐘K × 當日basis比例重建期貨分鐘價
        exit_minute = "13:30"
        if exit_reason == "trail":
            exit_day_stock_close = _t01_stock_close(con, sid, exit_day)
            exit_day_fut_close = float((m.get(exit_day) or [0, 0])[1])
            if exit_day_stock_close and exit_day_stock_close > 0 and exit_day_fut_close > 0:
                basis_ratio = exit_day_fut_close / exit_day_stock_close
                exit_day_minutes = load_stock_minute_closes(con, sid, exit_day)
                intraday_peak = peak  # 進場以來(不含出場日)的最高收盤已經算進peak
                for mm, stock_px in sorted(exit_day_minutes.items()):
                    approx_fut_px = stock_px * basis_ratio
                    intraday_peak = max(intraday_peak, approx_fut_px)
                    pb = (intraday_peak - approx_fut_px) / intraday_peak * 100
                    if pb >= TRAIL_PCT:
                        exit_minute = mm
                        break

        margin = estimate_margin_ntd(fut_entry_price)
        raw_ret = (exit_close / fut_entry_price - 1) * 100
        net_ret = raw_ret - ROUND_TRIP_COST_PCT
        notional = margin / MARGIN_RATE
        pnl = notional * (net_ret / 100)
        events.append({
            "stock": sid, "entry_day": t01, "entry_minute": entry_minute,
            "exit_day": exit_day, "exit_minute": exit_minute,
            "margin": margin, "pnl": pnl, "n_seats": s["n_seats"],
        })
    return events


def run_daily_scheduler(events: list[dict], calendar: list[str], total_capital: float) -> dict:
    cal_idx = {d: i for i, d in enumerate(calendar)}
    by_entry_day: dict[str, list[dict]] = {}
    for e in events:
        by_entry_day.setdefault(e["entry_day"], []).append(e)
    for d in by_entry_day:
        by_entry_day[d].sort(key=lambda e: -e["n_seats"])
    by_exit_day: dict[str, list[dict]] = {}
    for e in events:
        by_exit_day.setdefault(e["exit_day"], []).append(e)

    available = total_capital
    taken, skipped = 0, 0
    realized = 0.0
    open_margin = {}
    for day in calendar:
        for e in by_exit_day.get(day, []):
            key = (e["stock"], e["entry_day"], e["exit_day"])
            if key in open_margin:
                available += open_margin.pop(key)
                realized += e["pnl"]
        for e in by_entry_day.get(day, []):
            if e["margin"] <= available:
                available -= e["margin"]
                open_margin[(e["stock"], e["entry_day"], e["exit_day"])] = e["margin"]
                taken += 1
            else:
                skipped += 1
    return {"taken": taken, "skipped_for_capital": skipped, "total_ret_pct": realized / total_capital * 100}


def run_minute_scheduler(events: list[dict], total_capital: float) -> dict:
    timeline = []
    for e in events:
        timeline.append((e["exit_day"], e["exit_minute"], 0, "exit", e))  # 0=exit先處理
        timeline.append((e["entry_day"], e["entry_minute"], 1, "entry", e))  # 1=entry後處理
    timeline.sort(key=lambda x: (x[0], x[1], x[2]))

    available = total_capital
    taken, skipped = 0, 0
    realized = 0.0
    open_margin = {}
    for day, minute, _, kind, e in timeline:
        key = (e["stock"], e["entry_day"], e["exit_day"])
        if kind == "exit":
            if key in open_margin:
                available += open_margin.pop(key)
                realized += e["pnl"]
        else:
            if key in open_margin:
                continue
            if e["margin"] <= available:
                available -= e["margin"]
                open_margin[key] = e["margin"]
                taken += 1
            else:
                skipped += 1
    return {"taken": taken, "skipped_for_capital": skipped, "total_ret_pct": realized / total_capital * 100}


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    signals = [{"stock": t["stock"], "trade_date": t["trade_date"], "n_seats": int(t["n_seats"])} for t in trades]
    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-07")

    print("=== 分鐘級 vs 日級 資金排程比較 ===")
    events = prepare_events(con, signals, fut_cache, calendar)
    print(f"可分析事件數: {len(events)}/{len(trades)}（entry/exit都能定位到分鐘級的訊號）\n")

    same_day_pairs = sum(1 for e in events if e["exit_day"] == e["entry_day"])
    print(f"（其中同一天內進出的僅{same_day_pairs}筆——多數持有跨越好幾個交易日，"
          f"分鐘級排程的空間主要在『不同部位的進場/出場交錯發生在同一天』）\n")

    print(f"{'總資金(NTD)':>14} {'成交(日)':>9} {'跳過(日)':>9} {'總報酬%(日)':>12} "
          f"{'成交(分鐘)':>10} {'跳過(分鐘)':>10} {'總報酬%(分鐘)':>13} {'成交差':>7}")
    rows = []
    for cap in CAPITAL_SCENARIOS_NTD:
        daily = run_daily_scheduler(events, calendar, cap)
        minute = run_minute_scheduler(events, cap)
        rows.append((cap, daily, minute))
        print(
            f"{cap:>14,} {daily['taken']:>9} {daily['skipped_for_capital']:>9} {daily['total_ret_pct']:>12.1f} "
            f"{minute['taken']:>10} {minute['skipped_for_capital']:>10} {minute['total_ret_pct']:>13.1f} "
            f"{minute['taken']-daily['taken']:>7}"
        )

    print(
        "\n⚠️ 限制：\n"
        "  1) 出場分鐘用『個股自己的1分K收盤 × 當日期貨/現貨日收盤比例』近似重建，\n"
        "     只反映日均basis，沒有真正的盤中basis波動（跟entry_frac用法一致的近似）。\n"
        "  2) max_hold觸發的出場沒有精細化到分鐘（用當日收盤時間13:30代表，因為\n"
        "     『滿10天強制出場』本來就不是被某個盤中價格觸發，沒有更精確的時間點\n"
        "     可定義）。\n"
        "  3) 只比較成交數/總報酬，沒有做分鐘級mark-to-market，所以沒有分鐘級的\n"
        "     Sharpe/max drawdown可比——這裡只回答『資金排程解析度對成交筆數/報酬\n"
        "     有沒有影響』，不是完整重跑NAV模擬。"
    )

    best_cap_row = rows[-1]
    diff_2m = best_cap_row[2]["taken"] - best_cap_row[1]["taken"]
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-intraday-capital-scheduler",
        ts="2026-08-09",
        params={"capital_scenarios_ntd": list(CAPITAL_SCENARIOS_NTD)},
        n_observations=len(events),
        metric_name="taken_diff_minute_vs_daily_at_2m",
        metric_value=diff_2m,
        status="kept" if any(r[2]["taken"] > r[1]["taken"] for r in rows) else "rejected",
        source=__file__,
        notes=(
            "測試把資金排程從日頻率升級成分鐘頻率，能不能讓同一筆資金一天內被\n"
            f"重複利用更多次。掃過{CAPITAL_SCENARIOS_NTD}NTD，逐一比較成交數/因資金\n"
            "跳過數/總報酬，詳見腳本輸出表格。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "capital-scheduler", "intraday"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
