#!/usr/bin/env python3
"""Additional overnight waves: frozen-rule H14, sector-exclusions, multi-slot."""
from __future__ import annotations

import json
import importlib.util
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "research")]
spec = importlib.util.spec_from_file_location(
    "mvp", ROOT / "scripts/research/run_abc_rolling_top10_l1h7_mvp.py"
)
mvp = importlib.util.module_from_spec(spec)
sys.modules["mvp"] = mvp
assert spec.loader is not None
spec.loader.exec_module(mvp)

CACHE = ROOT / "data/scratch/overnight_cache"
OUT = sorted(
    (ROOT / "reports/research/branch-footprint-screen").glob("overnight_hunt_*"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)[0]
WEIGHT = {"2330", "2317", "2454", "2308", "2881", "2412", "3045", "2303", "2382", "2357"}
FIXED = {
    "918X": (2e8, 0.4, "群益金鼎-台北"),
    "9661": (2e8, 0.4, "富邦新店"),
    "9875": (3e8, 0.5, "元大-土城永寧"),
    "981j": (3e8, 0.3, "元大-士林"),
    "9217": (1.5e8, 0.25, "凱基松山"),
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] WMORE {msg}"
    print(line, flush=True)
    (OUT / "run.log").open("a", encoding="utf-8").write(line + "\n")


def summarize(legs, min_n=8):
    if len(legs) < min_n:
        return dict(
            n=len(legs),
            ok=False,
            excess_med=np.nan,
            excess_mean=np.nan,
            win=np.nan,
            max1=np.nan,
            slot_n=0,
            slot_med=np.nan,
            slot_dd=np.nan,
            slot_compound=np.nan,
        )
    xs = np.array([x["excess_pct"] for x in legs], float)
    ctr = Counter(x["stock_id"] for x in legs)
    taken, busy = [], None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    txs = np.array([x["excess_pct"] for x in taken], float)
    trs = np.array([x["stock_pct"] for x in taken], float) / 100
    eq = np.cumprod(1 + trs)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min() * 100)
    return dict(
        n=len(legs),
        ok=True,
        excess_med=float(np.median(xs)),
        excess_mean=float(np.mean(xs)),
        win=float((xs > 0).mean() * 100),
        max1=max(ctr.values()) / len(legs),
        slot_n=len(taken),
        slot_med=float(np.median(txs)),
        slot_dd=dd,
        slot_compound=float((eq[-1] - 1) * 100),
    )


def main() -> int:
    events = json.loads((CACHE / "events_all.json").read_text())
    cal = json.loads((CACHE / "calendar.json").read_text())
    di = {d: i for i, d in enumerate(cal)}
    conn = mvp.connect_readonly(ROOT / "data/scratch/rolling_mvp_snapshot.db")
    try:
        bench = mvp.load_benchmark(conn, cal[0], cal[-1])
        ohlc = mvp.load_stock_ohlc(conn, cal[0], cal[-1])
    finally:
        conn.close()

    def oc(sid, d, hold):
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
        return dict(
            signal_date=d,
            entry_date=ed,
            exit_date=xd,
            stock_id=sid,
            stock_pct=sr * 100,
            excess_pct=(sr - br) * 100,
        )

    rows = []
    # Frozen-rule solos (no adaptive pick) — closest to deploy
    log("frozen solos")
    for hold in (7, 10, 14, 20):
        for bid, (a, s, name) in FIXED.items():
            for xw in (True, False):
                legs = []
                for e in events[bid]:
                    if e["buy_amt"] < a or e["buy_share"] < s:
                        continue
                    if xw and e["stock_id"] in WEIGHT:
                        continue
                    o = oc(e["stock_id"], e["trade_date"], hold)
                    if o:
                        legs.append({**o, "branch": bid, "buy_amt": e["buy_amt"]})
                st = summarize(legs)
                rows.append(
                    dict(
                        kind="frozen_solo",
                        branch=name,
                        hold=hold,
                        xw=xw,
                        rule=f">={a/1e8:g}億∩{int(s*100)}%",
                        **st,
                    )
                )
                if st["ok"]:
                    log(
                        f"frozen {name} H{hold} xw={xw}: n={st['n']} med={st['excess_med']:+.2f} "
                        f"slot={st['slot_med']:+.2f}"
                    )

    # Multi-slot: allow up to K concurrent non-overlapping only per branch identity
    log("multi-slot OR maxamt")
    for hold in (10, 14):
        for k_slots in (1, 2, 3):
            for xw in (True, False):
                cands = []
                for bid, (a, s, name) in FIXED.items():
                    for e in events[bid]:
                        if e["buy_amt"] < a or e["buy_share"] < s:
                            continue
                        if xw and e["stock_id"] in WEIGHT:
                            continue
                        o = oc(e["stock_id"], e["trade_date"], hold)
                        if o:
                            cands.append({**o, "branch": bid, "buy_amt": e["buy_amt"]})
                # greedy fill up to k_slots
                cands = sorted(cands, key=lambda z: (z["entry_date"], -z["buy_amt"]))
                active = []  # (exit_date)
                taken = []
                for lg in cands:
                    active = [x for x in active if x >= lg["entry_date"]]
                    if len(active) >= k_slots:
                        continue
                    taken.append(lg)
                    active.append(lg["exit_date"])
                    active.sort()
                st = summarize(taken, min_n=8)
                rows.append(
                    dict(
                        kind=f"multislot_k{k_slots}",
                        branch="TOP5fixed",
                        hold=hold,
                        xw=xw,
                        rule="fixed",
                        **st,
                    )
                )
                if st["ok"]:
                    log(
                        f"multislot k={k_slots} H{hold} xw={xw}: n={st['n']} med={st['excess_med']:+.2f} "
                        f"compound={st['slot_compound']:+.1f} dd={st['slot_dd']:+.0f}"
                    )

    # Time split OOS: fit nothing, just evaluate second half with frozen rules
    log("half-sample OOS frozen")
    mid = cal[len(cal) // 2]
    for hold in (10, 14):
        for bid, (a, s, name) in FIXED.items():
            for xw in (True,):
                legs = []
                for e in events[bid]:
                    if e["trade_date"] < mid:
                        continue
                    if e["buy_amt"] < a or e["buy_share"] < s:
                        continue
                    if xw and e["stock_id"] in WEIGHT:
                        continue
                    o = oc(e["stock_id"], e["trade_date"], hold)
                    if o:
                        legs.append(o)
                st = summarize(legs, min_n=6)
                rows.append(
                    dict(
                        kind="half_oos_frozen",
                        branch=name,
                        hold=hold,
                        xw=xw,
                        rule=f">={a/1e8:g}億∩{int(s*100)}%",
                        **st,
                    )
                )
                log(
                    f"halfOOS {name} H{hold}: n={st['n']} med={st['excess_med']:+.2f}"
                )

    cdf = pd.DataFrame(rows)
    cdf.to_csv(OUT / "wave_more_results.csv", index=False)
    cdf2 = cdf.sort_values(["excess_med", "n"], ascending=[False, False])
    md = [
        "# Wave more (frozen / multislot / half-OOS)\n\n",
        f"- out: `{OUT.name}`\n",
        f"- mid_split: `{mid}`\n\n",
        "## Top 30\n\n",
        "|kind|branch|hold|xw|n|excess_med|win|slot_med|slot_dd|compound|\n",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|\n",
    ]
    for r in cdf2[cdf2.ok == True].head(30).itertuples():  # noqa: E712
        md.append(
            f"|{r.kind}|{r.branch}|{r.hold}|{r.xw}|{int(r.n)}|{r.excess_med:+.2f}|"
            f"{r.win:.0f}|{r.slot_med:+.2f}|{r.slot_dd:+.0f}|{r.slot_compound:+.1f}|\n"
        )
    (OUT / "WAVE_MORE.md").write_text("".join(md), encoding="utf-8")
    log(f"DONE -> {OUT/'WAVE_MORE.md'}")
    print(cdf2[cdf2.ok == True].head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
