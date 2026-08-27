#!/usr/bin/env python3
"""回放最近一個月（用真實DB重新產生候選，不是套用停在2026-07-01的舊CSV）.

背景：使用者要「回放過去一個月的交易紀錄用報酬」。既有的研究快照
（all_trades.csv／single_pick_tradelog.csv）都停在2026-07-01，資料庫本身
分點資料到2026-08-07、個股日線到2026-08-07、個股1分K到2026-07-30——用
dayflip_short_signal.build_candidates()（正式production函式，不是研究複本）
直接對DB重新產生候選，涵蓋範圍取T0+1落在[2026-07-08, 2026-08-07]的交易日，
1分K缺口（07-31~08-07沒有分鐘資料）會讓長邊entry偵測在那幾天找不到訊號，
誠實列為資料缺口，不是強行填補。

短邊模擬（比照dayflip_short_order.pick_signal的規則，但離線版）：
  fgap = futures_daily_cache當日開盤 / 前一日收盤 - 1，>=FGAP_MIN才合格，
  挑fgap最小的（pick_rule=smallest_qualifying_gap）當短邊當天唯一一筆。
  出場：day_low觸及entry×(1-2%)視為觸價回補，否則收盤平倉（比照
  cover_target_pct=0.02／force_close_at邏輯的離線近似）。

長邊模擬：直接呼叫src/order/dayflip_post_dump_long_order.py的正式函式
（available_pool_ntd/per_name_cap_ntd/short_side_open_stock_id_today/
Position），比照上一輪已經驗證過、修好_today()日期mock bug的回放harness。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_long_last_month_replay.py
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path

import stock_db
from order import dayflip_post_dump_long_order as M
from order.dayflip_post_dump_long_signal import FGAP_MIN, build_candidates, estimate_margin_ntd, last_close
from order.dayflip_short_signal import estimate_margin_ntd as short_estimate_margin_ntd

from dayflip_short_post_dump_long_capital_simulation import MARGIN_RATE, find_entry_price

ROOT = Path(__file__).resolve().parents[2]
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
COVER_TARGET_PCT = 0.02
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
ROUND_TRIP_COST_PCT = 0.05

WINDOW_TRADE_DATE_START = "2026-07-08"
WINDOW_TRADE_DATE_END = "2026-08-07"
KBAR_COVERAGE_END = "2026-07-30"  # 長邊entry偵測（需要1分K）的資料實際上限


def build_recent_calendar(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE stock_id='2330' AND source='finmind' AND trade_date BETWEEN '2026-06-20' AND ? ORDER BY trade_date",
        (WINDOW_TRADE_DATE_END,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    calendar = build_recent_calendar(con)
    ci = {d: i for i, d in enumerate(calendar)}
    t0_list = [d for d in calendar if ci[d] + 1 < len(calendar) and calendar[ci[d] + 1] >= WINDOW_TRADE_DATE_START
               and calendar[ci[d] + 1] <= WINDOW_TRADE_DATE_END]

    print("=== 最近一個月回放（DB即時重建候選，不用舊CSV）===")
    print(f"T0+1窗口: {WINDOW_TRADE_DATE_START} ~ {WINDOW_TRADE_DATE_END}")
    print(f"⚠️ 個股1分K只到{KBAR_COVERAGE_END}，超過這天的長邊entry訊號偵測不到資料，會列為no_kbar_data\n")

    tmp_dir = Path(tempfile.mkdtemp(prefix="dfpdlong_lastmonth_"))
    M._LEDGER_PATH = tmp_dir / "long_ledger.json"
    M._SHORT_LEDGER_PATH = tmp_dir / "short_ledger.json"
    state = M.load_ledger()

    short_trades_log = []
    long_taken, long_skipped_cap, long_skipped_pool, long_skipped_same_stock, long_no_kbar = 0, 0, 0, 0, 0
    short_total_pnl, long_total_pnl = 0.0, 0.0
    pool_overflow_days = []

    for t0 in t0_list:
        t01 = calendar[ci[t0] + 1]
        M._today = lambda d=t01: d

        candidates = build_candidates(t0)
        if not candidates:
            M._SHORT_LEDGER_PATH.write_text(json.dumps({"date": t01, "status": "idle"}))
            continue

        # --- 短邊：離線版pick_signal規則 ---
        scored = []
        for c in candidates:
            t0_close = last_close(c.stock_id, t0)
            m = fut_cache.get(c.stock_id) or {}
            row = m.get(t01)
            if t0_close is None or t0_close <= 0 or row is None:
                continue
            open_px, close_px, _high, low_px = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            if open_px <= 0:
                continue
            fgap = open_px / t0_close - 1
            if fgap < FGAP_MIN:
                continue
            scored.append((fgap, c.stock_id, open_px, close_px, low_px))
        short_today_stock = None
        if scored:
            scored.sort(key=lambda x: x[0])
            fgap, sid, open_px, close_px, low_px = scored[0]
            target = open_px * (1 - COVER_TARGET_PCT)
            if low_px <= target:
                exit_px, how = target, "觸價回補"
            else:
                exit_px, how = close_px, "收盤平倉"
            net_ret = (open_px - exit_px) / open_px * 100 - ROUND_TRIP_COST_PCT
            margin = short_estimate_margin_ntd(open_px)
            pnl = margin / MARGIN_RATE * (net_ret / 100)
            short_total_pnl += pnl
            short_today_stock = sid
            short_trades_log.append({"date": t01, "stock": sid, "fgap": round(fgap * 100, 2),
                                      "net_ret_pct": round(net_ret, 2), "pnl": round(pnl), "how": how})
            M._SHORT_LEDGER_PATH.write_text(json.dumps({
                "date": t01, "status": "entered", "stock_id": sid, "margin_ntd": margin,
            }))
        else:
            M._SHORT_LEDGER_PATH.write_text(json.dumps({"date": t01, "status": "idle"}))

        # --- 長邊：直接呼叫正式模組函式 ---
        open_stock_ids = {p["stock_id"] for p in M.open_positions(state)}
        short_stock_today = M.short_side_open_stock_id_today()
        for c in candidates:
            sid = c.stock_id
            if sid in open_stock_ids:
                continue
            if short_stock_today is not None and sid == short_stock_today:
                long_skipped_same_stock += 1
                continue
            t0_close = last_close(sid, t0)
            m = fut_cache.get(sid) or {}
            row = m.get(t01)
            if t0_close is None or t0_close <= 0 or row is None:
                continue
            open_px = float(row[0])
            if open_px <= 0:
                continue
            fgap = open_px / t0_close - 1
            if fgap < FGAP_MIN:
                continue
            if t01 > KBAR_COVERAGE_END:
                long_no_kbar += 1
                continue
            entry = find_entry_price(con, sid, t01)
            if entry is None:
                continue
            entry_stock_px, _kind = entry
            day_close_stock = con.execute(
                "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
                (sid, t01),
            ).fetchone()
            if not day_close_stock:
                continue
            entry_frac = entry_stock_px / float(day_close_stock[0])
            fut_close_t01 = float(row[1])
            fut_entry = fut_close_t01 * entry_frac
            margin = estimate_margin_ntd(fut_entry)

            cap = M.per_name_cap_ntd()
            if margin > cap:
                long_skipped_cap += 1
                continue
            available = M.available_pool_ntd(state)
            if margin > available:
                long_skipped_pool += 1
                continue

            # 多日trailing stop出場（沿用futures_daily_cache）
            dates_sorted = sorted(fut_cache.get(sid, {}))
            if t01 not in dates_sorted:
                continue
            i0 = dates_sorted.index(t01)
            peak = fut_entry
            exit_ret, exit_day = None, None
            for h in range(1, MAX_HOLD_DAYS + 1):
                if i0 + h >= len(dates_sorted):
                    break
                d = dates_sorted[i0 + h]
                px = float(fut_cache[sid][d][1])
                if px <= 0:
                    continue
                peak = max(peak, px)
                pullback = (peak - px) / peak * 100
                if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
                    exit_ret = (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
                    exit_day = d
                    break
            if exit_ret is None:
                continue  # 還在多日持倉期、資料窗口內還沒出場，簡化不計入本輪損益

            pos = M.Position(
                position_id=f"lastmonth_{t01}_{sid}", stock_id=sid, futures_symbol=sid,
                entry_day=t01, entry_minute="replay", entry_price=fut_entry, peak_price=peak,
                margin_ntd=round(margin), margin_source="estimate_fallback", lots=M.LOTS,
                status="closed", exit_price=exit_ret, exit_day=exit_day, close_reason="trailing_stop_or_maxhold",
            )
            state.positions.append(asdict(pos))
            pnl = margin / MARGIN_RATE * (exit_ret / 100)
            long_total_pnl += pnl
            long_taken += 1
            open_stock_ids.add(sid)

        margin_used = M.margin_used_ntd(state)
        short_used = M.short_side_margin_used_today()
        combined = margin_used + short_used
        if combined > M.TOTAL_CAPITAL_NTD:
            pool_overflow_days.append({"day": t01, "combined": combined})

    print(f"短邊模擬交易: {len(short_trades_log)} 筆 · 總損益(粗估): {short_total_pnl:,.0f} NTD")
    for r in short_trades_log:
        print(f"  {r['date']} {r['stock']} fgap={r['fgap']}% {r['how']} net_ret={r['net_ret_pct']:+.2f}% pnl={r['pnl']:+,}")

    print(f"\n長邊成交: {long_taken} 筆")
    print(f"  因15%單股上限跳過: {long_skipped_cap}")
    print(f"  因共用池不足跳過: {long_skipped_pool}")
    print(f"  因同股互斥guard跳過: {long_skipped_same_stock}")
    print(f"  因1分K資料缺口({KBAR_COVERAGE_END}之後)跳過: {long_no_kbar}")
    print(f"長邊模擬總損益(粗估): {long_total_pnl:,.0f} NTD")

    print(f"\n資金池超額天數: {len(pool_overflow_days)}")
    for e in pool_overflow_days:
        print(f"  {e['day']}: combined={e['combined']:,.0f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 這不是套用既有研究快照（all_trades.csv/single_pick_tradelog.csv，\n"
        "     停在2026-07-01），是直接對DB重新跑build_candidates()產生的全新\n"
        "     候選清單——樣本外於先前所有研究/回放，第一次真的看『最近一個月』。\n"
        "  2) 個股1分K資料缺口(07-31~08-07)讓長邊entry偵測在那幾天完全找不到\n"
        f"     訊號(共{long_no_kbar}次)——這段時間的長邊表現無法評估，不是『沒訊號』\n"
        "     是『沒資料』。\n"
        "  3) 保證金全部用粗估公式(價格×2000×13.5%)，已知對高價股嚴重失真\n"
        "     （見前一輪8299案例），這裡的損益/資金池數字方向參考、金額不精確。\n"
        "  4) 短邊出場用day_low觸價/收盤平倉的簡化模擬，不是逐筆委託單成交\n"
        "     模擬，跟真實下單行為可能有落差。\n"
        "  5) 樣本數很小（約22個交易日、幾筆訊號），任何『最近一個月表現好/壞』\n"
        "     的結論統計上都不可靠，只能當『有沒有明顯異常』的健檢，不能當\n"
        "     績效評估。"
    )


if __name__ == "__main__":
    main()
