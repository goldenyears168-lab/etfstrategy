#!/usr/bin/env python3
"""做空(dayflip-futures-short)跟做多(dayflip-post-dump-long)共用同一筆保證金.

背景：使用者問「分鐘級搭配多空配置呢，用同一筆錢」。先查資料發現一個先前沒注意到
的事實：single_pick_tradelog.csv（現行LIVE dayflip-futures-short用的
pick_rule=smallest_qualifying_gap 歷史交易，74筆）裡，每一筆的(股票,交易日)
都同時出現在all_trades.csv（做多候選清單，221筆）——74/74完全重疊。也就是說，
短邊當天挑中做空的那檔股票，幾乎都會是長邊候選池裡的一員，時間軸上很可能是：
早盤(08:45起)先放空隔日沖客的追價，等倒貨/反彈訊號確認後，同一檔股票同一筆
保證金換邊做多——不是兩條不相干的策略各自要準備資金，是「同一筆錢在同一檔股票
上換邊用」的天然時序關係。

這裡驗證：
  1) 短邊出場時間 vs 長邊進場時間的先後關係（真的是『先空後多接力』，還是常常
     同時要兩邊都持倉）
  2) 用單一共用保證金池(shared pool)同時跑短+多兩條腿，跟『短邊自己一個池、
     多邊自己另一個池』(分開池、資金要兩份)比，總報酬/資金效率差多少

短邊資料：single_pick_tradelog.csv（entry_price/exit_price已經是個股期貨價格，
entry固定08:45、exit用exit_time欄位，收盤平倉=13:45）。長邊沿用已驗證的
rolling entry + 5%移動停利多日出場規則（EOD-only，已知結論）。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_long_shared_capital_handoff.py
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

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
POOL_SCENARIOS_NTD = (300_000, 600_000, 1_000_000)


def load_short_trades() -> list[dict]:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minutes(con: sqlite3.Connection, stock_id: str, trade_date: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, trade_date)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def find_long_leg(con: sqlite3.Connection, fut_cache: dict, stock_id: str, trade_date: str) -> dict | None:
    entry = find_entry_price(con, stock_id, trade_date)
    if entry is None:
        return None
    entry_stock_px, entry_kind = entry
    day_close = _t01_stock_close(con, stock_id, trade_date)
    if day_close is None or day_close <= 0:
        return None
    entry_frac = entry_stock_px / day_close

    entry_minute = "13:30"
    if entry_kind == "intraday_signal":
        minutes_map = load_stock_minutes(con, stock_id, trade_date)
        for mm, px in sorted(minutes_map.items()):
            if px == entry_stock_px:
                entry_minute = mm
                break

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
                "entry_day": trade_date, "entry_minute": entry_minute,
                "exit_day": d, "exit_minute": "13:30",
                "margin": margin, "pnl": margin / MARGIN_RATE * (net_ret / 100), "net_ret_pct": net_ret,
            }
    return None


def build_combined_legs(con: sqlite3.Connection, fut_cache: dict, short_trades: list[dict]) -> list[dict]:
    combined = []
    for s in short_trades:
        sid, t01 = s["stock"], s["trade_date"]
        short_entry_px = float(s["entry_price"])
        short_exit_px = float(s["exit_price"])
        short_margin = estimate_margin_ntd(short_entry_px)
        short_pnl_pct = float(s["pnl_pct"])
        short_pnl = short_margin / MARGIN_RATE * (short_pnl_pct / 100)
        short_leg = {
            "entry_day": t01, "entry_minute": "08:45",
            "exit_day": t01, "exit_minute": s["exit_time"],
            "margin": short_margin, "pnl": short_pnl,
        }
        long_leg = find_long_leg(con, fut_cache, sid, t01)
        combined.append({"stock": sid, "trade_date": t01, "short": short_leg, "long": long_leg})
    return combined


def classify_timing(pair: dict) -> str:
    if pair["long"] is None:
        return "no_long_signal"
    s, l = pair["short"], pair["long"]
    if l["entry_day"] > s["exit_day"]:
        return "next_day_or_later"
    if l["entry_day"] == s["exit_day"] and l["entry_minute"] >= s["exit_minute"]:
        return "same_day_after_short_exit"
    return "overlap_before_short_exit"


def run_shared_pool(pairs: list[dict], calendar: list[str], total_capital: float) -> dict:
    events = []
    for p in pairs:
        s = p["short"]
        events.append((s["entry_day"], s["entry_minute"], 1, "entry", "short", p, s))
        events.append((s["exit_day"], s["exit_minute"], 0, "exit", "short", p, s))
        if p["long"] is not None:
            entry_min = p["long"]["entry_minute"]
            events.append((p["long"]["entry_day"], entry_min, 1, "entry", "long", p, p["long"]))
            events.append((p["long"]["exit_day"], p["long"]["exit_minute"], 0, "exit", "long", p, p["long"]))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    available = total_capital
    taken = {"short": 0, "long": 0}
    skipped = {"short": 0, "long": 0}
    realized = 0.0
    open_margin = {}
    for day, minute, _, kind, leg_type, p, leg in events:
        key = (p["stock"], p["trade_date"], leg_type)
        if kind == "exit":
            if key in open_margin:
                available += open_margin.pop(key)
                realized += leg["pnl"]
        else:
            if leg["margin"] <= available:
                available -= leg["margin"]
                open_margin[key] = leg["margin"]
                taken[leg_type] += 1
            else:
                skipped[leg_type] += 1
    return {"taken": taken, "skipped": skipped, "total_ret_pct": realized / total_capital * 100}


def run_separate_pools(pairs: list[dict], total_capital_each: float) -> dict:
    # 短邊：single_trade_per_day已保證同時最多1筆，只要margin<=池子就一定成交
    short_realized = sum(p["short"]["pnl"] for p in pairs if p["short"]["margin"] <= total_capital_each)
    short_taken = sum(1 for p in pairs if p["short"]["margin"] <= total_capital_each)
    short_skipped = len(pairs) - short_taken

    long_pairs = [p for p in pairs if p["long"] is not None]
    events = []
    for p in long_pairs:
        events.append((p["long"]["entry_day"], p["long"]["entry_minute"], 1, p))
        events.append((p["long"]["exit_day"], p["long"]["exit_minute"], 0, p))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    available = total_capital_each
    long_taken, long_skipped, long_realized = 0, 0, 0.0
    open_margin = {}
    for day, minute, kind, p in events:
        key = (p["stock"], p["trade_date"])
        if kind == 0:
            if key in open_margin:
                available += open_margin.pop(key)
                long_realized += p["long"]["pnl"]
        else:
            if p["long"]["margin"] <= available:
                available -= p["long"]["margin"]
                open_margin[key] = p["long"]["margin"]
                long_taken += 1
            else:
                long_skipped += 1
    combined_ret_pct = (short_realized + long_realized) / (total_capital_each * 2) * 100
    return {
        "short_taken": short_taken, "short_skipped": short_skipped,
        "long_taken": long_taken, "long_skipped": long_skipped,
        "combined_ret_pct_on_2x_capital": combined_ret_pct,
    }


def main() -> None:
    short_trades = load_short_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))
    calendar = build_calendar(con, min(s["trade_date"] for s in short_trades), "2026-08-07")

    print("=== 做空/做多共用保證金——時序關係 + 資金效率測試 ===")
    pairs = build_combined_legs(con, fut_cache, short_trades)
    print(f"短邊交易數: {len(pairs)}（單一挑股規則的現行LIVE歷史交易）\n")

    timing = {}
    for p in pairs:
        cat = classify_timing(p)
        timing[cat] = timing.get(cat, 0) + 1
    print("時序關係分布：")
    for cat, n in sorted(timing.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}筆（{n/len(pairs)*100:.0f}%）")

    print(f"\n{'共用池(NTD)':>12} {'短邊成交':>8} {'短邊跳過':>8} {'多邊成交':>8} {'多邊跳過':>8} {'總報酬%':>10}")
    shared_rows = []
    for cap in POOL_SCENARIOS_NTD:
        r = run_shared_pool(pairs, calendar, cap)
        shared_rows.append((cap, r))
        print(f"{cap:>12,} {r['taken']['short']:>8} {r['skipped']['short']:>8} "
              f"{r['taken']['long']:>8} {r['skipped']['long']:>8} {r['total_ret_pct']:>10.1f}")

    print(f"\n{'各自獨立池(每邊NTD)':>18} {'短邊成交':>8} {'短邊跳過':>8} {'多邊成交':>8} {'多邊跳過':>8} {'合計報酬%(以2倍本金計)':>20}")
    separate_rows = []
    for cap in POOL_SCENARIOS_NTD:
        r = run_separate_pools(pairs, cap)
        separate_rows.append((cap, r))
        print(f"{cap:>18,} {r['short_taken']:>8} {r['short_skipped']:>8} "
              f"{r['long_taken']:>8} {r['long_skipped']:>8} {r['combined_ret_pct_on_2x_capital']:>20.1f}")

    print(
        "\n對照解讀：『共用池X』只用X的本金同時跑兩腿；『各自獨立池X』要準備2X本金\n"
        "（短邊X+多邊X各自獨立）。如果共用池X的總報酬%（以X為分母）接近甚至超過\n"
        "獨立池的合計報酬%（以2X為分母），代表共用同一筆錢確實能省下一半資金需求\n"
        "還拿到差不多的報酬；如果共用池的報酬%明顯更低，代表共用會讓兩邊互相排擠、\n"
        "省資金是有代價的。"
    )

    print(
        "\n⚠️ 限制：\n"
        "  1) 短邊entry_price/exit_price直接當個股期貨價格使用（tradelog本身是為了\n"
        "     驗證期貨下單邏輯產生的，未逐筆核對是否已含滑價/手續費）。\n"
        "  2) 短邊margin用跟長邊同一套estimate_margin_ntd()概估，非官方逐檔試算表\n"
        "     （config/order.yaml的margin_cap_twd=300000是硬上限，不是這裡用的估算式）。\n"
        "  3) 74筆短邊交易對應的長邊訊號，只用該檔股票本身，沒有考慮同一天長邊\n"
        "     候選池裡『非短邊挑中』的其他股票是否也該搶同一筆共用資金——這裡刻意\n"
        "     只測『同一檔股票換邊』這個最直接的假設，全候選池的共用資金排程是\n"
        "     更大的題目，留待後續。\n"
        "  4) 長邊出場精度沿用EOD-only（已知：分鐘級連續追蹤反而更差，見前一輪\n"
        "     post-dump-long-intraday-exit-precision結論）。"
    )

    best_cap = POOL_SCENARIOS_NTD[0]
    shared_at_best = dict(shared_rows)[best_cap]
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-long-shared-capital-handoff",
        ts="2026-08-09",
        params={"pool_scenarios_ntd": list(POOL_SCENARIOS_NTD)},
        n_observations=len(pairs),
        metric_name=f"shared_pool_total_ret_pct_at_{best_cap}",
        metric_value=shared_at_best["total_ret_pct"],
        status="kept",
        source=__file__,
        notes=(
            f"驗證dayflip-futures-short（現行LIVE）跟dayflip-post-dump-long（研究草稿）\n"
            f"能不能共用同一筆保證金——發現74/74短邊交易的(股票,交易日)都同時是長邊\n"
            f"候選，時序分布見腳本輸出。共用池 vs 各自獨立池的資金效率比較見輸出表格，\n"
            f"詳細數字見metric_value與腳本stdout。"
        ),
        tags=["dayflip-short", "dayflip-futures-short", "post-dump", "long-side", "shared-capital", "capital-efficiency"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
