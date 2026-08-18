#!/usr/bin/env python3
"""M4-Step5：母體覆蓋缺口——scan_5d_net95 目前只看得到 9217 一半的 tape.

發現：9217 在 2024-07-01~2026-08-17 的分點 tape 共 311,226 列 (stock,day)，其中
155,749 列（50.0%）在 stock_daily_bars 找不到對應收盤價；2,236 檔交易標的裡有
1,183 檔在 DB 完全沒有價格。scan_5d_net95 的 SQL 是 INNER JOIN 價格表，所以這些
活動被**靜默丟棄**——研究母體與 live watch 腳本看到的都是「本專案剛好回補過價格
的那半邊宇宙」。

本腳本從 FinMind 唯讀取數（不寫 DB）補上缺價最多的 N 檔標的，重跑 scan_5d_net95，
量化「補齊後母體會多幾筆事件、L1H7 統計會往哪走」。

DB 唯讀。用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m4_coverage_gap_extrapolation.py --top 40
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro
from project_dotenv import load_project_dotenv

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"
CACHE = OUT_DIR / "songshan_m4_gap_price_cache.parquet"
SOURCE = "finmind"
START, END = "2024-07-01", "2026-08-17"
FETCH_START = "2024-04-01"
BUY_FLOOR, NET_MIN = 50_000_000.0, 0.95
COST, HOLD, BETA = 0.003, 7, 1.15


def _load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


MGEN = _load("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    conn = connect_ro(DEFAULT_DB_PATH)
    mega = MGEN.load_mega(MGEN.MEGA_PATH)
    calendar = MGEN.load_calendar(conn, START, END)

    print("=" * 96)
    print("(1) 缺口盤點")
    print("=" * 96)
    tot, nomatch, stocks, st_missing = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN p.close IS NULL THEN 1 ELSE 0 END),
               COUNT(DISTINCT b.stock_id),
               COUNT(DISTINCT CASE WHEN p.close IS NULL THEN b.stock_id END)
        FROM stock_broker_branch_daily b
        LEFT JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date
             AND p.source=? AND p.close>0
        WHERE b.source=? AND b.securities_trader_id='9217' AND b.trade_date BETWEEN ? AND ?
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        """, (SOURCE, SOURCE, START, END)).fetchone()
    gap = {"tape_rows": tot, "rows_without_price": nomatch,
           "pct_without_price": round(100 * nomatch / tot, 1),
           "stocks_traded": stocks, "stocks_with_any_missing_day": st_missing}
    print(json.dumps(gap, ensure_ascii=False, indent=1))

    cand = pd.read_sql_query(
        """
        SELECT b.stock_id, COUNT(*) AS miss_days, SUM(b.buy) AS buy_shares
        FROM stock_broker_branch_daily b
        LEFT JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date
             AND p.source=? AND p.close>0
        WHERE b.source=? AND b.securities_trader_id='9217' AND p.close IS NULL AND b.buy>0
          AND b.trade_date BETWEEN ? AND ?
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY b.stock_id ORDER BY buy_shares DESC
        """, conn, params=(SOURCE, SOURCE, START, END))
    targets = [s for s in cand["stock_id"].tolist() if s not in mega][: args.top]
    print(f"\n[INFO] 取買進股數最多的 {len(targets)} 檔缺價標的補價")

    # ---- FinMind 取價（唯讀，不寫 DB） ----
    if CACHE.exists():
        px = pd.read_parquet(CACHE)
        have = set(px["stock_id"])
    else:
        px, have = pd.DataFrame(), set()
    todo = [s for s in targets if s not in have]
    if todo:
        load_project_dotenv()
        from finmind_client import fetch_finmind
        frames = [px] if len(px) else []
        for i, sid in enumerate(todo, 1):
            try:
                rows = fetch_finmind("TaiwanStockPrice", sid,
                                     date.fromisoformat(FETCH_START), date.fromisoformat(END))
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}/{len(todo)}] {sid} FAILED {exc}")
                continue
            sub = pd.DataFrame(rows)
            if sub.empty:
                print(f"  [{i}/{len(todo)}] {sid} empty")
                continue
            sub = sub.rename(columns={"date": "trade_date"})
            sub["stock_id"] = sid
            frames.append(sub[["stock_id", "trade_date", "open", "close"]])
            print(f"  [{i}/{len(todo)}] {sid} n={len(sub)}")
            time.sleep(0.6)
        px = pd.concat(frames, ignore_index=True)
        px = px[px["close"] > 0].drop_duplicates(["stock_id", "trade_date"])
        px.to_parquet(CACHE, index=False)
    px = px[px["stock_id"].isin(targets)]
    print(f"[INFO] 補價 panel：{px['stock_id'].nunique()} 檔 / {len(px)} 列")

    print("\n" + "=" * 96)
    print("(2) 用補上的價格重跑 scan_5d_net95（僅這批缺價標的）")
    print("=" * 96)
    tape = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, buy, sell FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id='9217' AND trade_date BETWEEN ? AND ?
        """, conn, params=(SOURCE, START, END))
    tape = tape[tape["stock_id"].isin(targets)]
    m = tape.merge(px, on=["stock_id", "trade_date"], how="inner")
    m["buy_amt"] = m["buy"] * m["close"]
    m["sell_amt"] = m["sell"] * m["close"]

    grid = pd.MultiIndex.from_product(
        [sorted(m["stock_id"].unique()), calendar], names=["stock_id", "trade_date"]
    ).to_frame(index=False)
    g = grid.merge(m[["stock_id", "trade_date", "buy_amt", "sell_amt"]],
                   on=["stock_id", "trade_date"], how="left").fillna({"buy_amt": 0, "sell_amt": 0})
    g = g.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    gb = g.groupby("stock_id", sort=False)
    g["buy_5d"] = gb["buy_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    g["sell_5d"] = gb["sell_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    g["net_ratio"] = np.where(g["buy_5d"] > 0, (g["buy_5d"] - g["sell_5d"]) / g["buy_5d"], np.nan)
    trig = (g["buy_5d"] >= BUY_FLOOR) & (g["net_ratio"] >= NET_MIN)
    g["triggered"] = trig
    prev = g.groupby("stock_id", sort=False)["triggered"].shift(1).fillna(False).astype(bool)
    ev = g[g["triggered"] & ~prev].rename(columns={"trade_date": "signal_date"})
    ev = ev[["stock_id", "signal_date", "buy_5d", "sell_5d", "net_ratio"]].sort_values("signal_date")
    print(f"[RESULT] 這批缺價標的新增 rising-edge 事件 n = {len(ev)}"
          f"（涵蓋 {ev['stock_id'].nunique()} 檔）")
    print(ev.to_string(index=False))

    # ---- L1H7 ----
    ix = MGEN.load_ix(conn)
    ixm = {d: (o, c) for d, o, c in ix}
    ixd = sorted(ixm)
    trades = []
    for r in ev.itertuples(index=False):
        b = px[px["stock_id"] == r.stock_id].sort_values("trade_date")
        bl = list(zip(b["trade_date"], b["open"], b["close"]))
        nxt = [x for x in bl if x[0] > r.signal_date and x[1] > 0]
        if not nxt:
            continue
        ed, eo = nxt[0][0], nxt[0][1]
        after = [x for x in bl if x[0] >= ed]
        if len(after) < HOLD:
            continue
        xc = after[HOLD - 1][2]
        bn = [d for d in ixd if d > r.signal_date]
        if not bn:
            continue
        be = bn[0]
        ba = [d for d in ixd if d >= be]
        if len(ba) < HOLD:
            continue
        bo, bc = ixm[be][0], ixm[ba[HOLD - 1]][1]
        r_adj = (xc / eo - 1 - COST) - BETA * (bc / bo - 1)
        trades.append({"stock_id": r.stock_id, "signal_date": r.signal_date,
                       "r_adj_pct": round(r_adj * 100, 3)})
    td = pd.DataFrame(trades)
    print(f"\n[RESULT] 可評估 L1H7 的新增事件 n = {len(td)}")
    if len(td):
        v = td["r_adj_pct"].to_numpy()
        add = {"n": int(len(v)), "mean_pct": round(float(np.mean(v)), 3),
               "median_pct": round(float(np.median(v)), 3),
               "win_rate_pct": round(float((v > 0).mean() * 100), 1)}
        print(json.dumps(add, ensure_ascii=False))
        print(td.sort_values("r_adj_pct").to_string(index=False))

        cur = pd.read_csv(OUT_DIR / "songshan_m4_trades_extended_20260817.csv",
                          dtype={"stock_id": str})
        allv = np.concatenate([cur["r_adj_pct"].to_numpy(), v])
        merged = {"n": int(len(allv)), "mean_pct": round(float(np.mean(allv)), 3),
                  "median_pct": round(float(np.median(allv)), 3),
                  "win_rate_pct": round(float((allv > 0).mean() * 100), 1)}
        print("\n[合併後母體（現有 + 這批補價新增）]", json.dumps(merged, ensure_ascii=False))
    else:
        add = {"n": 0}
        merged = None

    (OUT_DIR / "songshan_m4_coverage_gap.json").write_text(json.dumps(
        {"gap": gap, "targets": targets, "new_events": json.loads(ev.to_json(orient="records")),
         "new_trades_stats": add, "merged_population": merged},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] → {OUT_DIR / 'songshan_m4_coverage_gap.json'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
