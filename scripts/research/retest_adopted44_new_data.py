#!/usr/bin/env python3
"""用新數據(mini 831 家 tape + adj_close)重測個股專家池 44 檔 adopted 是否撐得住 +2%.

方法：凍結每檔的 core 專家分點 + champion floor（不重新 grid-search＝不二次過擬合），
在延伸+除權息校正的數據上原樣重放：
  signal 日 = 回看1日(今/昨)內、core 分點中個別 net 買額(net×close)≥floor 的家數 ≥ k
             （k：單核=1；多核=2，比照 run_expert_pool_watch「在場≥2家」共識）
  L1H7：進場 T+1 open → 出場 T+7 close · 成本 30bps · r_adj = 股(adj) − 1.15×IX0001
  dedup：同股 5 曆日內只留首筆。
比較 new_med vs +2% 門檻、vs 原 orig_med；分類 HOLD / WEAKEN / FAIL / THIN。

用法（mini）：PYTHONPATH=src .venv/bin/python scripts/research/retest_adopted44_new_data.py --branch-db data/stocks.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EP = ROOT / "reports/research/branch-footprint-screen/expert_pool"
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5
START = "2024-07-01"


def load_specs() -> list[dict]:
    reg = json.loads((EP / "EVAL_REGISTRY.json").read_text("utf-8"))
    specs = []
    for s in reg["stocks"]:
        if s.get("status") != "adopted":
            continue
        sid = s["stock_id"]
        core = {}
        p5 = EP / sid / "p5_pool.json"
        if p5.exists():
            core = json.loads(p5.read_text("utf-8")).get("core", {})
        if not core:  # fallback p6
            p6 = EP / sid / "p6_grid.json"
            if p6.exists():
                core = json.loads(p6.read_text("utf-8")).get("core", {})
        fl = s.get("champion_floor") or "≥0.5億"
        floor = 1e8 if "1億" in fl else 5e7
        specs.append({"sid": sid, "name": s.get("stock_name"), "core": list(core),
                      "floor": floor, "floor_label": fl,
                      "orig_med": s.get("med_radj_pct"), "orig_n": s.get("radj_n")})
    return specs


def l1h7(dates, opens, closes, facs, ix_open, ix_close, ixpos, sig_dates):
    """回傳每個 signal 日的 r_adj%（adj_close 校正）。"""
    out = []
    dpos = {d: i for i, d in enumerate(dates)}
    for sd in sig_dates:
        i = dpos.get(sd)
        if i is None or sd not in ixpos:
            continue
        e, x = i + 1, i + HOLD
        if x >= len(dates):
            continue
        j = ixpos[sd]
        if j + HOLD >= len(ix_open):
            continue
        aeo = opens[e] * (facs[e] if facs[e] > 0 else 1.0)
        axc = closes[x] * (facs[x] if facs[x] > 0 else 1.0)
        r_s = axc / aeo - 1 - COST
        r_ix = ix_close[j + HOLD] / ix_open[j + 1] - 1
        out.append((sd, (r_s - BETA * r_ix) * 100))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch-db", default=str(ROOT / "data/stocks.db"))
    args = ap.parse_args()
    bdb = args.branch_db
    conn = sqlite3.connect(f"file:{bdb}?mode=ro&immutable=1", uri=True)
    data_max = conn.execute("SELECT MAX(trade_date) FROM stock_broker_branch_daily").fetchone()[0]
    specs = load_specs()
    print(f"[SNAPSHOT] branch_db={Path(bdb).name} data_max={data_max} adopted={len(specs)}")

    sids = sorted({s["sid"] for s in specs})
    ph = ",".join("?" * len(sids))
    # 個股價格(open/close/adj_close)
    px = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,open,close,adj_close FROM stock_daily_bars
            WHERE source='finmind' AND close>0 AND trade_date>=? AND stock_id IN ({ph})""",
        conn, params=[START, *sids])
    px["stock_id"] = px.stock_id.astype(str)
    px["open"] = px["open"].where(px["open"] > 0, px["close"])
    px["fac"] = np.where(px["adj_close"].notna() & (px["adj_close"] > 0), px["adj_close"] / px["close"], 1.0)
    close_map = {(r.stock_id, r.trade_date): r.close for r in px.itertuples()}
    bars = {}
    for sid, g in px.sort_values("trade_date").groupby("stock_id"):
        bars[sid] = (g.trade_date.tolist(), g.open.tolist(), g.close.tolist(), g.fac.tolist())
    # IX
    ixr = conn.execute("""SELECT date,open,close FROM daily_bars WHERE code='IX0001'
        AND date>=? AND open>0 AND close>0 ORDER BY date,
        CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END""", (START,)).fetchall()
    ixd, ixo, ixc = [], [], []
    seen = set()
    for d, o, c in ixr:
        if d in seen:
            continue
        seen.add(d); ixd.append(d); ixo.append(float(o)); ixc.append(float(c))
    ixpos = {d: i for i, d in enumerate(ixd)}

    # 一次撈全部：44 股 × core 分點聯集（避免 44 次全表掃描）
    core_union = sorted({c for s in specs for c in s["core"]})
    cuph = ",".join("?" * len(core_union))
    tape = pd.read_sql_query(
        f"""SELECT trade_date, stock_id, securities_trader_id, net FROM stock_broker_branch_daily
            WHERE source='finmind' AND net>0 AND trade_date>=? AND stock_id IN ({ph})
              AND securities_trader_id IN ({cuph})""",
        conn, params=[START, *sids, *core_union])
    tape["stock_id"] = tape.stock_id.astype(str)
    tape_by_sid = {sid: g for sid, g in tape.groupby("stock_id")}

    rows = []
    for s in specs:
        sid, core, floor = s["sid"], s["core"], s["floor"]
        b = bars.get(sid)
        if not b or not core:
            rows.append({**s, "new_med": None, "new_n": 0, "verdict": "無價/無core"}); continue
        k = 1 if len(core) == 1 else 2
        tp = tape_by_sid.get(sid)
        if tp is None or tp.empty:
            rows.append({**s, "new_med": None, "new_n": 0, "verdict": "core無交易"}); continue
        tp = tp[tp["securities_trader_id"].isin(core)].copy()
        if tp.empty:
            rows.append({**s, "new_med": None, "new_n": 0, "verdict": "core無交易"}); continue
        # 每分點每日 net_amt = net × close
        tp["close"] = [close_map.get((sid, d)) for d in tp.trade_date]
        tp = tp[tp["close"].notna()]
        tp["net_amt"] = tp["net"] * tp["close"]
        big = tp[tp["net_amt"] >= floor]  # 個別分點當日 net 買額 ≥ floor
        # 每日「在場」家數（去重分點）
        by_day = big.groupby("trade_date")["securities_trader_id"].nunique().to_dict()
        alldays = sorted(set(tp.trade_date))
        # 回看1日共識：今日或昨日在場家數(取聯集分點) ≥ k
        dts = b[0]
        sig_dates = []
        big_by_day = big.groupby("trade_date")["securities_trader_id"].apply(set).to_dict()
        for idx, d in enumerate(dts):
            today = big_by_day.get(d, set())
            yday = big_by_day.get(dts[idx - 1], set()) if idx > 0 else set()
            if len(today | yday) >= k and len(today) >= 1:  # 今日至少1家出手且回看1日共識達 k
                sig_dates.append(d)
        res = l1h7(*b, ixo, ixc, ixpos, sig_dates)
        # dedup 5 cal
        res.sort()
        keep = []
        last = None
        for d, r in res:
            if last is None or (pd.Timestamp(d) - pd.Timestamp(last)).days > DEDUP:
                keep.append(r); last = d
        ra = np.array(keep)
        n = len(ra)
        new_med = float(np.median(ra)) if n else None
        if n < 6:
            v = "THIN薄樣本"
        elif new_med is None:
            v = "無訊號"
        elif new_med >= 2.0:
            v = "HOLD撐住"
        elif new_med >= 0:
            v = "WEAKEN轉弱"
        else:
            v = "FAIL翻負"
        rows.append({**s, "k": k, "new_med": None if new_med is None else round(new_med, 2),
                     "new_mean": round(float(np.mean(ra)), 2) if n else None,
                     "new_win": round(float((ra > 0).mean() * 100), 0) if n else None,
                     "new_n": n, "verdict": v})

    conn.close()
    df = pd.DataFrame(rows)
    outp = EP / "RETEST_ADOPTED44_new_data.csv"
    df.to_csv(outp, index=False)
    from collections import Counter
    print("[VERDICT 分布]", dict(Counter(df["verdict"])))
    show = df[["sid", "name", "n_core" if "n_core" in df else "core", "orig_med", "orig_n", "new_med", "new_n", "new_win", "verdict"]] if False else df
    print(df[["sid", "name", "orig_med", "orig_n", "new_med", "new_n", "new_win", "verdict"]].sort_values("new_med", na_position="last", ascending=False).to_string(index=False))
    print(f"[OUT] {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
