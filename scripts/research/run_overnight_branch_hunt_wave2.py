#!/usr/bin/env python3
"""Wave-2 overnight: denser consensus / combo / hold sweeps after wave-1.

Focuses on protocols that can realistically approach high excess medians:
- multi-branch same-day consensus with rising min_branches
- pick best of Top4 by same-day buy intensity
- require both high abs AND high share
- optional exclude mega-caps
- walk-forward consensus (fit floors on past window)

Writes into an existing overnight_hunt_* dir if OVERNIGHT_DIR env set, else new dir.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "research")]
spec = importlib.util.spec_from_file_location(
    "mvp_w2", ROOT / "scripts/research/run_abc_rolling_top10_l1h7_mvp.py"
)
mvp = importlib.util.module_from_spec(spec)
sys.modules["mvp_w2"] = mvp
assert spec.loader is not None
spec.loader.exec_module(mvp)

TOP4 = [("9875", "元大-土城永寧"), ("918X", "群益金鼎-台北"), ("9661", "富邦新店"), ("981j", "元大-士林")]
CORE8 = TOP4 + [
    ("779Z", "國票安和"),
    ("962A", "富邦-南港"),
    ("585b", "統一-內湖"),
    ("9217", "凱基松山"),
]
WEIGHT = {"2330", "2317", "2454", "2308", "2881", "2412", "3045"}


def log(out: Path, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] W2 {msg}"
    print(line, flush=True)
    (out / "run.log").open("a", encoding="utf-8").write(line + "\n")


def summarize(legs, min_n=8):
    if len(legs) < min_n:
        return dict(n=len(legs), ok=False, excess_med=np.nan, excess_mean=np.nan, win=np.nan, max1=np.nan, slot_n=0, slot_excess_med=np.nan, slot_dd=np.nan)
    xs = np.array([x["excess_pct"] for x in legs], float)
    ctr = Counter(x["stock_id"] for x in legs)
    taken, busy = [], None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    txs = np.array([x["excess_pct"] for x in taken], float) if taken else np.array([])
    trs = np.array([x["stock_pct"] for x in taken], float) / 100 if taken else np.array([])
    if len(trs):
        eq = np.cumprod(1 + trs)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min() * 100)
        smed = float(np.median(txs))
    else:
        dd, smed = np.nan, np.nan
    return dict(
        n=len(legs),
        ok=True,
        excess_med=float(np.median(xs)),
        excess_mean=float(np.mean(xs)),
        win=float((xs > 0).mean() * 100),
        max1=max(ctr.values()) / len(legs),
        slot_n=len(taken),
        slot_excess_med=smed,
        slot_dd=dd,
    )


def outcome(sid, d, cal, di, ohlc, bench, hold):
    si = di.get(d)
    if si is None:
        return None
    ei, xi = si + 1, si + hold
    if xi >= len(cal):
        return None
    ed, xd = cal[ei], cal[xi]
    er, xr = ohlc.get((sid, ed)), ohlc.get((sid, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None
    sr = xr[1] / er[0] - 1 - mvp.COST_RATE
    br = bx[1] / be[0] - 1
    return dict(signal_date=d, entry_date=ed, exit_date=xd, stock_id=sid, stock_pct=sr * 100, excess_pct=(sr - br) * 100)


def save(out, name, meta, legs, min_n=8):
    st = summarize(legs, min_n)
    row = {"hypothesis": name, "wave": 2, **meta, **st}
    path = out / "all_results.csv"
    pd.DataFrame([row]).to_csv(path, mode="a", header=not path.exists(), index=False)
    tag = ""
    if st["ok"] and st["excess_med"] >= 10:
        tag = " ★★★"
        pd.DataFrame(legs).to_csv(out / f"legs_{name}.csv", index=False)
    elif st["ok"] and st["excess_med"] >= 5:
        tag = " ★"
        pd.DataFrame(legs).to_csv(out / f"legs_{name}.csv", index=False)
    log(out, f"{name}: n={st['n']} med={st['excess_med']:+.2f}% mean={st['excess_mean']:+.2f}%{tag}")
    return row


def main() -> int:
    out_env = os.environ.get("OVERNIGHT_DIR")
    if out_env:
        out = Path(out_env)
    else:
        # attach to newest hunt dir if present
        parent = ROOT / "reports/research/branch-footprint-screen"
        dirs = sorted(parent.glob("overnight_hunt_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = dirs[0] if dirs else parent / f"overnight_hunt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        out.mkdir(parents=True, exist_ok=True)

    log(out, f"OUT={out}")
    db = ROOT / "data/scratch/rolling_mvp_snapshot.db"
    if not db.exists():
        db = ROOT / "data/stocks.db"
    ids = [b[0] for b in CORE8]
    conn = mvp.connect_readonly(db)
    try:
        end = str(mvp.query_with_retry(conn, "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source=?", (mvp.SOURCE,))[0][0])
        cal = mvp.load_calendar(conn, mvp.WINDOW_START, end)
        bench = mvp.load_benchmark(conn, cal[0], cal[-1])
        ohlc = mvp.load_stock_ohlc(conn, cal[0], cal[-1])
        buys = mvp.load_top_buys(conn, ids, cal[0], cal[-1])
    finally:
        conn.close()
    di = {d: i for i, d in enumerate(cal)}

    # events
    events = {bid: [] for bid in ids}
    for bid, dated in buys.items():
        for d in sorted(dated):
            e = dated[d]
            events[bid].append(e)

    # outcomes cache
    cache = {}
    for hold in (3, 5, 7, 10, 14, 20):
        for bid in ids:
            for e in events[bid]:
                k = (e.stock_id, e.trade_date, hold)
                if k not in cache:
                    cache[k] = outcome(e.stock_id, e.trade_date, cal, di, ohlc, bench, hold)

    # denser consensus grid
    log(out, "dense consensus grid")
    for pool_name, pool in (("top4", TOP4), ("core8", CORE8)):
        pids = [b[0] for b in pool]
        for hold in (3, 5, 7, 10, 14):
            for min_b in range(2, min(5, len(pids) + 1)):
                for a in [3e7, 5e7, 7e7, 1e8, 1.5e8, 2e8, 3e8, 5e8, 8e8, 1e9]:
                    for s in [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6]:
                        for xw in (False, True):
                            day_map = defaultdict(lambda: defaultdict(set))
                            day_amt = defaultdict(float)
                            for bid in pids:
                                for e in events[bid]:
                                    if e.buy_amt < a or e.buy_share < s:
                                        continue
                                    if xw and e.stock_id in WEIGHT:
                                        continue
                                    day_map[e.trade_date][e.stock_id].add(bid)
                                    day_amt[(e.trade_date, e.stock_id)] = max(
                                        day_amt[(e.trade_date, e.stock_id)], e.buy_amt
                                    )
                            legs = []
                            for d, stocks in day_map.items():
                                for sid, bids in stocks.items():
                                    if len(bids) < min_b:
                                        continue
                                    oc = cache.get((sid, d, hold))
                                    if not oc:
                                        continue
                                    legs.append({**oc, "n_branches": len(bids), "buy_amt": day_amt[(d, sid)]})
                            if len(legs) < 5:
                                continue
                            save(
                                out,
                                f"W2_cons_{pool_name}_m{min_b}_H{hold}_a{a/1e8:g}_s{int(s*100)}{'_xW' if xw else ''}",
                                dict(family="W2_consensus", pool=pool_name, min_branches=min_b, hold=hold, abs=a, share=s, exclude_weights=xw),
                                legs,
                                min_n=5,
                            )

    # best-of-day among top4 by buy_amt under each branch's N15-30 frozen-ish strong floors
    log(out, "intensity pick among top4")
    strong_rules = {
        "9875": (3e8, 0.5),
        "918X": (2e8, 0.4),
        "9661": (2e8, 0.4),
        "981j": (3e8, 0.3),
    }
    for hold in (5, 7, 10, 14):
        for xw in (False, True):
            by_day = {}
            for bid, (a, s) in strong_rules.items():
                for e in events[bid]:
                    if e.buy_amt < a or e.buy_share < s:
                        continue
                    if xw and e.stock_id in WEIGHT:
                        continue
                    oc = cache.get((e.stock_id, e.trade_date, hold))
                    if not oc:
                        continue
                    cur = by_day.get(e.trade_date)
                    row = {**oc, "branch_id": bid, "buy_amt": e.buy_amt}
                    if cur is None or row["buy_amt"] > cur["buy_amt"]:
                        by_day[e.trade_date] = row
            legs = [by_day[d] for d in sorted(by_day)]
            save(
                out,
                f"W2_intensity_top4_H{hold}{'_xW' if xw else ''}",
                dict(family="W2_intensity", hold=hold, exclude_weights=xw),
                legs,
            )

    # require consensus AND intensity (max branch buy >= floor)
    log(out, "consensus+intensity")
    for hold in (5, 7, 10):
        for min_b in (2, 3):
            for a in (1e8, 2e8, 3e8, 5e8):
                for s in (0.25, 0.3, 0.4, 0.5):
                    for amin in (2e8, 3e8, 5e8, 8e8):
                        day_map = defaultdict(lambda: defaultdict(set))
                        day_amt = defaultdict(float)
                        for bid, _ in TOP4:
                            for e in events[bid]:
                                if e.buy_amt < a or e.buy_share < s:
                                    continue
                                day_map[e.trade_date][e.stock_id].add(bid)
                                day_amt[(e.trade_date, e.stock_id)] = max(day_amt[(e.trade_date, e.stock_id)], e.buy_amt)
                        legs = []
                        for d, stocks in day_map.items():
                            for sid, bids in stocks.items():
                                if len(bids) < min_b:
                                    continue
                                if day_amt[(d, sid)] < amin:
                                    continue
                                oc = cache.get((sid, d, hold))
                                if not oc:
                                    continue
                                legs.append({**oc, "n_branches": len(bids), "buy_amt": day_amt[(d, sid)]})
                        if len(legs) < 5:
                            continue
                        save(
                            out,
                            f"W2_consint_m{min_b}_H{hold}_a{a/1e8:g}_s{int(s*100)}_amin{amin/1e8:g}",
                            dict(family="W2_cons_intensity", min_branches=min_b, hold=hold, abs=a, share=s, amin=amin),
                            legs,
                            min_n=5,
                        )

    # refresh ranked summary
    df = pd.read_csv(out / "all_results.csv")
    df = df.sort_values(["excess_med", "n"], ascending=[False, False])
    df.to_csv(out / "all_results_ranked.csv", index=False)
    ge10 = df[(df.get("ok") == True) & (df["excess_med"] >= 10) & (df["n"] >= 8)]  # noqa: E712
    top = df[df.get("ok") == True].head(30)  # noqa: E712
    md = [f"# Wave2 append\n\n- rows_now: {len(df)}\n- ge10_n>=8: {len(ge10)}\n\n", "## Top30\n\n"]
    md.append("|hypothesis|n|excess_med|excess_mean|win|\n|---|---:|---:|---:|---:|\n")
    for r in top.itertuples():
        md.append(f"|{r.hypothesis}|{int(r.n)}|{r.excess_med:+.2f}|{r.excess_mean:+.2f}|{r.win:.0f}|\n")
    (out / "SUMMARY_W2.md").write_text("".join(md), encoding="utf-8")
    log(out, f"DONE wave2 rows={len(df)} ge10={len(ge10)}")
    print(top.head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
