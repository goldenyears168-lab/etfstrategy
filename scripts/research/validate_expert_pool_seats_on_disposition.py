#!/usr/bin/env python3
"""在「處置股子樣本」上驗證現行專家池 9 席 seats（公平主場檢驗）.

專家池 config/second_disp_expert_pool_watch.json 的主場是「2026 第二次處置窗內、
專家席當日買進達門檻 → L1H7 跟單」。通用 universe 非其主場，故本腳本只在處置窗子
樣本上檢驗每一席，並跟「處置窗內全分點買進 baseline」對照，判定 seat 是否真有增量。

協議（與 run_second_disp_top30_l1h7.py 一致）：
  * 事件：seat 在某處置股 [p_start,p_end] 窗內、當日 buy>0 且 buy_amt≥seat.abs_floor_yi。
    buy_amt = buy_shares × close。
  * L1H7：進場 T+1 open → 出場 T+7 close，成本 30bps，excess = 股 − 1.15×IX0001。
  * dedupe：同股 5 曆日內只留首筆。
資料源：分點 = replica（完整）；價格 = local∪replica finmind（最大覆蓋）；IX = local daily_bars。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/validate_expert_pool_seats_on_disposition.py
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
REPLICA_DB = ROOT / "data/replica/stocks.db"
LOCAL_DB = ROOT / "data/stocks.db"
EP_JSON = ROOT / "reports/research/branch-footprint-screen/second_disp_top30_l1h7/twse_second_disp_2026.json"
CFG = ROOT / "config/second_disp_expert_pool_watch.json"
OUT = ROOT / "reports/research/branch-footprint-screen/expanded"

COST, HOLD, BETA, DEDUP_DAYS = 0.003, 7, 1.15, 5


def months(lo: str, hi: str):
    y, m = int(lo[:4]), int(lo[5:7])
    out = []
    while f"{y:04d}-{m:02d}-01" <= hi:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"))
        y, m = ny, nm
    return out


def build_l1h7(dates, opens, closes) -> pd.DataFrame:
    n = len(dates)
    rows = []
    for i in range(n - 1):
        e, x = i + 1, i + HOLD
        if x >= n:
            continue
        rows.append((dates[i], dates[e], opens[e], dates[x], closes[x]))
    return pd.DataFrame(rows, columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close"])


def load_prices(sids):
    frames = []
    for db, pref in [(LOCAL_DB, 0), (REPLICA_DB, 1)]:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ph = ",".join("?" * len(sids))
        d = pd.read_sql_query(
            f"""SELECT stock_id, trade_date, open, close FROM stock_daily_bars
                WHERE source='finmind' AND close>0 AND trade_date>='2025-11-01' AND stock_id IN ({ph})""",
            c, params=list(sids))
        c.close(); d["pref"] = pref; frames.append(d)
    px = pd.concat(frames, ignore_index=True)
    px["stock_id"] = px["stock_id"].astype(str)
    px["open"] = px["open"].where(px["open"] > 0, px["close"])
    return px.sort_values("pref").drop_duplicates(["stock_id", "trade_date"], keep="first")


def load_ix():
    c = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    rows = c.execute("""SELECT date,open,close FROM daily_bars WHERE code='IX0001'
        AND date>='2025-11-01' AND open>0 AND close>0 ORDER BY date,
        CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END""").fetchall()
    c.close()
    o = {}
    for d, op, cl in rows:
        o.setdefault(d, (float(op), float(cl)))
    ds = sorted(o)
    return build_l1h7(np.array(ds), np.array([o[d][0] for d in ds]), np.array([o[d][1] for d in ds])).set_index("signal_date")


def l1h7_excess(df, bars_lookup, ix_lookup):
    """df: signal_date, stock_id, amt → r_adj_pct; dedupe 5 cal days per stock/(group already single-seat)."""
    recs = []
    for r in df.itertuples(index=False):
        lk = bars_lookup.get(r.stock_id)
        if lk is None or r.signal_date not in lk.index or r.signal_date not in ix_lookup.index:
            continue
        s = lk.loc[r.signal_date]; b = ix_lookup.loc[r.signal_date]
        r_s = s["exit_close"] / s["entry_open"] - 1 - COST
        r_ix = b["exit_close"] / b["entry_open"] - 1
        recs.append({"signal_date": r.signal_date, "stock_id": r.stock_id, "r_adj_pct": (r_s - BETA * r_ix) * 100})
    ev = pd.DataFrame(recs)
    if ev.empty:
        return ev
    ev = ev.sort_values(["stock_id", "signal_date"])
    keep = []
    for _, g in ev.groupby("stock_id"):
        last = None
        for _, x in g.iterrows():
            if last is None or (pd.Timestamp(x.signal_date) - pd.Timestamp(last)).days > DEDUP_DAYS:
                keep.append(x); last = x.signal_date
    return pd.DataFrame(keep)


def stats_row(name, tid, ev):
    ra = ev["r_adj_pct"].to_numpy() if len(ev) else np.array([])
    n = len(ra)
    row = {"seat": tid, "name": name, "n": n, "n_stocks": ev["stock_id"].nunique() if n else 0,
           "median_r_adj_pct": float(np.median(ra)) if n else None,
           "mean_r_adj_pct": float(np.mean(ra)) if n else None,
           "win_rate_pct": float((ra > 0).mean() * 100) if n else None,
           "p_value": float(stats.ttest_1samp(ra, 0.0)[1]) if n >= 5 else None}
    return row


def main():
    ep = json.loads(EP_JSON.read_text("utf-8"))
    wins = defaultdict(list)
    for e in ep:
        wins[e["sid"]].append((e["p_start"], e["p_end"]))
    sids = sorted(wins)
    cfg = json.loads(CFG.read_text("utf-8"))
    seats = {str(s["id"]): (s["name"], float(s["abs_floor_yi"]) * 1e8) for s in cfg["seats"]}
    seat_ids_upper = {k.upper() for k in seats}
    print(f"[INFO] disposition episodes={len(ep)} stocks={len(sids)} seats={len(seats)}")

    # pull branch tape for disposition stocks over window span (month chunks)
    span_lo = min(e["p_start"] for e in ep)
    span_hi = min(max(e["p_end"] for e in ep), "2026-07-22")
    conn = sqlite3.connect(f"file:{REPLICA_DB}?mode=ro", uri=True)
    ph = ",".join("?" * len(sids))
    frames = []
    for lo, hi in months(span_lo, span_hi):
        d = pd.read_sql_query(
            f"""SELECT trade_date, stock_id, securities_trader_id, buy FROM stock_broker_branch_daily
                WHERE source='finmind' AND buy>0 AND trade_date>=? AND trade_date<? AND trade_date<=?
                  AND stock_id IN ({ph})""",
            conn, params=[lo, hi, span_hi, *sids])
        if len(d):
            frames.append(d)
        print(f"[SCAN] {lo[:7]} rows={len(d):,}")
    conn.close()
    tape = pd.concat(frames, ignore_index=True)
    tape["stock_id"] = tape["stock_id"].astype(str)

    # keep only rows inside that stock's disposition window
    def in_win(row):
        for a, b in wins.get(row.stock_id, []):
            if a <= row.trade_date <= b:
                return True
        return False
    tape = tape[tape.apply(in_win, axis=1)].copy()
    print(f"[INFO] branch-buy rows inside disposition windows: {len(tape):,}")

    px = load_prices(sids)
    print(f"[INFO] disposition stocks with price: {px.stock_id.nunique()}/{len(sids)}")
    cmap = {(s, d): c for s, d, c in zip(px.stock_id, px.trade_date, px.close)}
    tape["close"] = [cmap.get((s, d)) for s, d in zip(tape.stock_id, tape.trade_date)]
    tape = tape[tape["close"].notna()].copy()
    tape["buy_amt"] = tape["buy"] * tape["close"]
    tape = tape.rename(columns={"trade_date": "signal_date"})

    ix_lookup = load_ix()
    bars_lookup = {}
    for sid, g in px.sort_values("trade_date").groupby("stock_id"):
        lk = build_l1h7(g.trade_date.to_numpy(), g.open.to_numpy(), g.close.to_numpy())
        if not lk.empty:
            bars_lookup[sid] = lk.set_index("signal_date")

    rows = []
    # baseline A: ALL branch buys in windows (amt>=5M), any branch
    base = tape[tape.buy_amt >= 5e6][["signal_date", "stock_id", "buy_amt"]].rename(columns={"buy_amt": "amt"})
    rows.append(stats_row("[baseline] 處置窗全分點買進≥5M", "ALL", l1h7_excess(base, bars_lookup, ix_lookup)))
    # baseline B: day-Top1 buyer in windows (per stock-day the biggest buyer), amt>=5M
    t1 = tape.sort_values("buy_amt", ascending=False).drop_duplicates(["signal_date", "stock_id"])
    t1 = t1[t1.buy_amt >= 5e6][["signal_date", "stock_id", "buy_amt"]].rename(columns={"buy_amt": "amt"})
    rows.append(stats_row("[baseline] 處置窗當日Top1買方≥5M", "TOP1", l1h7_excess(t1, bars_lookup, ix_lookup)))

    # each seat with its configured abs floor
    for tid, (name, floor) in seats.items():
        sub = tape[(tape.securities_trader_id.str.upper() == tid.upper()) & (tape.buy_amt >= floor)]
        sub = sub[["signal_date", "stock_id", "buy_amt"]].rename(columns={"buy_amt": "amt"})
        r = stats_row(name, tid, l1h7_excess(sub, bars_lookup, ix_lookup))
        r["abs_floor_yi"] = floor / 1e8
        rows.append(r)

    res = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT / "expert_pool_seat_validation_disposition.csv", index=False)
    pd.set_option("display.width", 220, "display.max_columns", 20)
    print("\n=== 處置股子樣本 · 9 席 seats 驗證（vs baseline）===")
    print(res.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
