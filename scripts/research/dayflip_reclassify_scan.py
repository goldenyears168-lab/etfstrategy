#!/usr/bin/env python3
"""dayflip-futures-short 分點宇宙全掃描：找出「大買後短期內出光、不建倉」的其他分點.

只讀 DB。輸出到 reports/research/branch_dayflip_reclassify/。

方法：對 stock_broker_branch_daily 全部 ~1000 個 securities_trader_id 逐一掃描
（用 idx_branch_daily_trader_date 索引逐分點抓資料，避免對 222M 列做整表 JOIN）。

大買事件定義：與 production（src/order/dayflip_short_signal.py）完全一致 ——
T0 買進股數 × T0 收盤價 >= MIN_BUY_NTD(3000萬)，用 stock_daily_bars(source='finmind')
收盤價（該表非全市場，見 docs/research-integrity-checklist.md A14；本掃描繼承同一個
已知限制，跟 production 用同一張表、同一個限制，口徑一致，不是新引入的偏誤）。

flip 定義：跟 FROZEN_SPEC_V1 的 `T+1 賣出股數 / T0 買進股數` 同一種「股數比」，
只是把窗口從 1 天延伸到 N=5/10 天（前視），公式與門檻完全沿用 spec（分子分母皆為
股數，價格不參與，無需價格 join）：flip_Nd = SUM(sell shares, d0+1..d0+N) / buy0_shares。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import stock_db

OUT = Path("/Users/jackm4/goldenstocks/reports/research/branch_dayflip_reclassify")
OUT.mkdir(parents=True, exist_ok=True)

MIN_BUY_NTD = 30_000_000.0
FWD_WINDOWS = (5, 10)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    t_start = time.time()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)

    log("loading price map (stock_daily_bars, source=finmind) ...")
    px: dict[tuple[str, str], float] = {}
    for sid, d, c in con.execute(
        "SELECT stock_id, trade_date, close FROM stock_daily_bars "
        "WHERE source='finmind' AND close > 0"
    ):
        px[(str(sid), str(d))] = float(c)
    log(f"price rows: {len(px)}")

    cal = sorted({d for (_, d) in px})
    ci = {d: i for i, d in enumerate(cal)}
    log(f"calendar days: {len(cal)} ({cal[0]} .. {cal[-1]})")

    log("loading distinct trader_id list ...")
    trader_ids = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT securities_trader_id FROM stock_broker_branch_daily"
    ).fetchall())
    log(f"distinct traders: {len(trader_ids)}")

    all_events: list[dict] = []
    branch_stats: list[dict] = []
    t0 = time.time()
    for idx, tid in enumerate(trader_ids):
        rows = con.execute(
            "SELECT trade_date, stock_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=?",
            (tid,),
        ).fetchall()
        # per_stock[sid][date] = (buy_shares, sell_shares)
        per_stock: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
        for d, sid, b, s in rows:
            per_stock[str(sid)][str(d)] = (float(b or 0), float(s or 0))

        tid_events = []
        for sid, byd in per_stock.items():
            for d0, (b0, _s0) in byd.items():
                if b0 <= 0:
                    continue
                p = px.get((sid, d0))
                if p is None:
                    continue
                amt = b0 * p
                if amt < MIN_BUY_NTD:
                    continue
                i0 = ci.get(d0)
                if i0 is None:
                    continue
                row = {"tid": tid, "sid": sid, "d0": d0, "buy0_shares": b0, "amt_ntd": amt}
                for n in FWD_WINDOWS:
                    if i0 + n >= len(cal):
                        row[f"flip_{n}d"] = None
                        continue
                    sell_sum = 0.0
                    for d in cal[i0 + 1 : i0 + 1 + n]:
                        _b, sl = byd.get(d, (0.0, 0.0))
                        sell_sum += sl
                    row[f"flip_{n}d"] = sell_sum / b0
                tid_events.append(row)

        if tid_events:
            all_events.extend(tid_events)
            n10 = [r["flip_10d"] for r in tid_events if r["flip_10d"] is not None]
            n5 = [r["flip_5d"] for r in tid_events if r["flip_5d"] is not None]
            if len(n10) >= 5:
                n10s = sorted(n10)
                branch_stats.append({
                    "tid": tid,
                    "n_events_total": len(tid_events),
                    "n_events_scoreable_10d": len(n10),
                    "n_events_scoreable_5d": len(n5),
                    "median_flip_10d": n10s[len(n10s) // 2],
                    "mean_flip_10d": sum(n10) / len(n10),
                    "flip_rate_10d_ge0.40": sum(1 for x in n10 if x >= 0.40) / len(n10),
                    "flip_rate_10d_ge0.60": sum(1 for x in n10 if x >= 0.60) / len(n10),
                    "flip_rate_5d_ge0.40": (sum(1 for x in n5 if x >= 0.40) / len(n5)) if n5 else None,
                    "total_amt_ntd": sum(r["amt_ntd"] for r in tid_events),
                })

        if idx % 50 == 0 or idx == len(trader_ids) - 1:
            log(f"  [{idx+1}/{len(trader_ids)}] tid={tid} rows={len(rows)} "
                f"events_so_far={len(all_events)} elapsed={time.time()-t0:.0f}s")

    log(f"scan done: {len(all_events)} big-buy events, {len(branch_stats)} scoreable branches, "
        f"{time.time()-t0:.0f}s")
    con.close()

    with (OUT / "per_event_flip.json").open("w") as f:
        json.dump(all_events, f)
    log("wrote per_event_flip.json")

    branch_stats.sort(key=lambda r: -r["flip_rate_10d_ge0.40"])
    (OUT / "branch_flip_ranking.json").write_text(
        json.dumps(branch_stats, ensure_ascii=False, indent=1)
    )
    log(f"wrote branch_flip_ranking.json ({len(branch_stats)} branches)")
    log(f"total wall time: {time.time()-t_start:.0f}s")
    log("DONE")


if __name__ == "__main__":
    main()
