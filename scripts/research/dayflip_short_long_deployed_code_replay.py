#!/usr/bin/env python3
"""真的『回放』——直接驅動 src/order/dayflip_post_dump_long_order.py 的正式函式，
不是另一支邏輯相近但獨立重寫的backtest腳本.

前幾輪的backtest（dayflip_short_long_shared_capital_handoff.py、
dayflip_short_long_final_deployed_rules_check.py）都是「邏輯上比照」部署規則
重寫一遍，跟真正跑在worker裡的程式碼是兩份獨立實作——如果部署程式碼本身有
bug，這些backtest不會抓到，因為它們验证的是自己的重寫版本，不是正式代碼。

這裡改成直接import並呼叫正式模組的函式：available_pool_ntd()、
per_name_cap_ntd()、short_side_open_stock_id_today()、short_side_margin_used_today()、
Position dataclass、TRAIL_PCT/MAX_HOLD_DAYS常數——把_LEDGER_PATH跟
_SHORT_LEDGER_PATH指向暫存檔，餵歷史資料進去跑，用『正式ledger會長什麼樣子』
驗證整個系統，不是重新驗證一份邏輯相似但獨立的複本。

唯一沒辦法回放的是broker即時API本身（resolve_live_futures_symbol/
fetch_1m_bars/query_real_margin_ntd/place_futopt_order）——這些換成歷史資料
版的等效實作，並且保證金一律用estimate_margin_ntd()粗估（跟正式margin_source=
"estimate_fallback"路徑完全一致的計算方式），明確標註跟真實broker_api數字會有落差。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_long_deployed_code_replay.py
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np

import stock_db
from order import dayflip_post_dump_long_order as M
from order.dayflip_post_dump_long_signal import estimate_margin_ntd
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

from dayflip_short_post_dump_long_capital_simulation import (
    FUT_CACHE_PATH,
    MARGIN_RATE,
    _t01_stock_close,
    build_calendar,
    find_entry_price,
    load_trades,
)
from dayflip_short_long_shared_capital_handoff import load_short_trades

ROOT = Path(__file__).resolve().parents[2]


def load_stock_minutes(con: sqlite3.Connection, stock_id: str, trade_date: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, trade_date)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    trades = load_trades()  # 全候選池（221筆），做多這邊每天可能不只一個候選
    short_trades = {(t["stock"], t["trade_date"]): t for t in load_short_trades()}  # 74筆，短邊每天最多1筆

    all_dates = sorted({t["trade_date"] for t in trades} | {d for (_s, d) in short_trades})
    calendar = build_calendar(con, min(all_dates), "2026-08-07")
    cal_idx = {d: i for i, d in enumerate(calendar)}

    trades_by_date: dict[str, list[dict]] = {}
    for t in trades:
        trades_by_date.setdefault(t["trade_date"], []).append(t)

    # 把正式模組的ledger路徑導向暫存檔，讓真正的load_ledger()/save_ledger()跑在回放資料上
    tmp_dir = Path(tempfile.mkdtemp(prefix="dfpdlong_replay_"))
    long_ledger_path = tmp_dir / "long_ledger.json"
    short_ledger_path = tmp_dir / "short_ledger.json"
    M._LEDGER_PATH = long_ledger_path
    M._SHORT_LEDGER_PATH = short_ledger_path

    print("=== 直接驅動 src/order/dayflip_post_dump_long_order.py 正式函式的回放 ===")
    print(f"日曆範圍: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）")
    print(f"長邊候選池: {len(trades)}筆 · 短邊歷史交易: {len(short_trades)}筆\n")

    realized_pnl = 0.0
    taken, skipped_capital, skipped_same_stock, skipped_cap = 0, 0, 0, 0
    pool_overflow_days = []
    closes_log = []

    state = M.load_ledger()

    for day_idx, day in enumerate(calendar):
        # ⚠️ 第一次跑發現的bug：short_side_open_stock_id_today()/
        # short_side_margin_used_today()內部呼叫的_today()回傳『真實』今天日期
        # （2026-08-09），不是回放模擬的歷史日——導致raw.get("date")!=_today()
        # 永遠成立、guard跟pool扣款永遠被當成『短邊今天沒進場』略過，整個共用
        # 機制被靜默停用，第一版跑出的『0超額/0guard觸發』完全不可信。這裡把
        # M._today也一併monkey-patch成回傳回放中的day，才能讓正式函式的日期
        # 比對邏輯正確運作。
        M._today = lambda d=day: d
        short_today = None
        for (sid, d), t in short_trades.items():
            if d == day:
                short_today = (sid, t)
                break

        # 寫短邊當天的ledger快照（entered直到exit_time，這裡簡化成entered整天，
        # 因為single_pick_tradelog.csv沒有記錄「盤中哪一刻已經平倉」，只有
        # exit_time——保守起見全天都視為entered，等同假設guard/pool最嚴格的情況）
        if short_today is not None:
            sid, t = short_today
            short_margin = estimate_margin_ntd(float(t["entry_price"]))
            short_ledger_path.write_text(json.dumps({
                "date": day, "status": "entered", "stock_id": sid, "margin_ntd": short_margin,
            }))
        else:
            short_ledger_path.write_text(json.dumps({"date": day, "status": "idle"}))

        # --- 出場：用正式TRAIL_PCT/MAX_HOLD_DAYS常數 + trading_days_held() ---
        still_open = []
        for p in M.open_positions(state):
            m = fut_cache.get(p["stock_id"]) or {}
            px = m.get(day)
            if px is None:
                still_open.append(p)
                continue
            close = float(px[1])
            if close <= 0:
                still_open.append(p)
                continue
            peak = max(float(p["peak_price"]), close)
            p["peak_price"] = peak
            pullback = (peak - close) / peak * 100 if peak > 0 else 0.0
            hold_days = M.trading_days_held(p["entry_day"], day)
            if pullback >= M.TRAIL_PCT or hold_days >= M.MAX_HOLD_DAYS:
                notional = p["margin_ntd"] / MARGIN_RATE
                net_ret = (close / p["entry_price"] - 1) * 100 - 5 * 0.01  # ROUND_TRIP_COST_PCT=0.05(%)
                pnl = notional * (net_ret / 100)
                realized_pnl += pnl
                p["status"] = "closed"
                closes_log.append({"day": day, "stock": p["stock_id"], "pnl": pnl, "hold_days": hold_days})
        # positions dict已經被原地mutate（status改成closed的會被open_positions()自然濾掉）

        # --- 進場：用正式per_name_cap_ntd() / available_pool_ntd() / short_side_open_stock_id_today() ---
        short_stock_today = M.short_side_open_stock_id_today()
        open_stock_ids = {p["stock_id"] for p in M.open_positions(state)}
        for t in trades_by_date.get(day, []):
            sid = t["stock"]
            if sid in open_stock_ids:
                continue
            if short_stock_today is not None and sid == short_stock_today:
                skipped_same_stock += 1
                continue
            entry = find_entry_price(con, sid, day)
            if entry is None:
                continue
            entry_stock_px, _kind = entry
            day_close = _t01_stock_close(con, sid, day)
            if day_close is None or day_close <= 0:
                continue
            entry_frac = entry_stock_px / day_close
            m = fut_cache.get(sid) or {}
            if day not in m:
                continue
            fut_close = float(m[day][1])
            if fut_close <= 0:
                continue
            fut_entry = fut_close * entry_frac
            margin = estimate_margin_ntd(fut_entry)  # 回放限制：無法呼叫真實broker margin API

            cap = M.per_name_cap_ntd()
            if margin > cap:
                skipped_cap += 1
                continue
            available = M.available_pool_ntd(state)
            if margin > available:
                skipped_capital += 1
                continue

            pos = M.Position(
                position_id=f"replay_{day}_{sid}", stock_id=sid, futures_symbol=sid,
                entry_day=day, entry_minute="replay", entry_price=fut_entry,
                peak_price=fut_entry, margin_ntd=round(margin), margin_source="estimate_fallback",
                lots=M.LOTS, last_action="entered",
            )
            state.positions.append(asdict(pos))
            open_stock_ids.add(sid)
            taken += 1

        # --- pool overflow檢查：用正式available_pool_ntd()當天結束時的值 ---
        margin_used = M.margin_used_ntd(state)
        short_used = M.short_side_margin_used_today()
        combined = margin_used + short_used
        if combined > M.TOTAL_CAPITAL_NTD:
            pool_overflow_days.append({"day": day, "combined": combined, "over_by": combined - M.TOTAL_CAPITAL_NTD})

    M.save_ledger(state)

    print(f"長邊成交: {taken} 筆")
    print(f"因300,000共用池不足跳過: {skipped_capital} 筆")
    print(f"因15%單股上限跳過: {skipped_cap} 筆")
    print(f"因同股互斥guard跳過（短邊當天正在放空同一檔）: {skipped_same_stock} 筆")
    print(f"長邊回放總損益: {realized_pnl:,.0f} NTD（用estimate_margin_ntd粗估，非真實broker_api）")
    print(f"\n資金池超額天數（用正式available_pool_ntd()/TOTAL_CAPITAL_NTD計算）: {len(pool_overflow_days)}")
    if pool_overflow_days:
        for e in pool_overflow_days[:10]:
            print(f"  {e['day']}: combined={e['combined']:,.0f} 超出={e['over_by']:,.0f}")
        if len(pool_overflow_days) > 10:
            print(f"  ...其餘{len(pool_overflow_days)-10}天省略")

    print(
        "\n⚠️ 限制：\n"
        "  1) 保證金一律用estimate_margin_ntd()粗估——回放無法呼叫真實broker\n"
        "     query_real_margin_ntd() API（那個API需要即時報價，歷史日期查不到）。\n"
        "     上一輪已發現粗估對高價股(如8299)嚴重高估，這裡的pool overflow\n"
        "     天數/金額因此還是偏保守（真實運作時預期會更少）。\n"
        "  2) 短邊ledger快照簡化成『當天全天entered』（single_pick_tradelog.csv\n"
        "     沒有記錄盤中哪一刻平倉，只有exit_time這個標籤），比真實情況更\n"
        "     嚴格——真實系統裡短邊常常提早幾十分鐘平倉，讓長邊更早能用那筆\n"
        "     保證金，這裡的回放对guard/pool的限制是保守上界。\n"
        "  3) 進場價/出場邏輯本身跟前幾輪backtest相同（find_entry_price+日頻率\n"
        "     trailing stop），這裡的新意在於『資金/guard判斷』改用正式模組的\n"
        "     真實函式呼叫，不是重寫版本。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-long-deployed-code-replay",
        ts="2026-08-09",
        params={"pool_ntd": M.TOTAL_CAPITAL_NTD, "single_name_cap_pct": M.SINGLE_NAME_CAP_PCT},
        n_observations=taken,
        metric_name="pool_overflow_days_count",
        metric_value=len(pool_overflow_days),
        status="kept",
        source=__file__,
        notes=(
            f"直接驅動src/order/dayflip_post_dump_long_order.py正式函式的回放\n"
            f"（非重寫版backtest）：成交{taken}筆、因資金跳過{skipped_capital}筆、\n"
            f"因15%上限跳過{skipped_cap}筆、因同股guard跳過{skipped_same_stock}筆、\n"
            f"資金池超額{len(pool_overflow_days)}天（用粗估保證金，真實API會更準確）。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "replay", "deployed-code"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")
    print(f"(暫存ledger: {tmp_dir} · 不影響正式 data/order/dayflip_post_dump_long_ledger.json)")


if __name__ == "__main__":
    main()
