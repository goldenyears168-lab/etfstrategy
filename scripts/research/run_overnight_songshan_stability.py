#!/usr/bin/env python3
"""Stability checks for champion frozen rule: 凱基松山 ≥1.5億∩25% xWeight H10/H14/H20."""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
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
WEIGHT = {"2330", "2317", "2454", "2308", "2881", "2412", "3045", "2303", "2382"}
BID, A, S = "9217", 1.5e8, 0.25


def main() -> int:
    events = json.loads((CACHE / "events_all.json").read_text())[BID]
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
            ym=d[:7],
        )

    lines = ["# 凱基松山 champion stability\n\n", f"- generated: {datetime.now().isoformat(timespec='seconds')}\n\n"]
    for hold in (10, 14, 20):
        legs = []
        for e in events:
            if e["buy_amt"] < A or e["buy_share"] < S:
                continue
            if e["stock_id"] in WEIGHT:
                continue
            o = oc(e["stock_id"], e["trade_date"], hold)
            if o:
                legs.append({**o, "buy_amt": e["buy_amt"], "buy_share": e["buy_share"]})
        xs = np.array([x["excess_pct"] for x in legs], float)
        # bootstrap median
        rng = np.random.default_rng(42)
        boots = []
        for _ in range(2000):
            sample = rng.choice(xs, size=len(xs), replace=True)
            boots.append(float(np.median(sample)))
        lo, hi = np.quantile(boots, [0.05, 0.95])
        by_ym = defaultdict(list)
        for lg in legs:
            by_ym[lg["ym"]].append(lg["excess_pct"])
        month_rows = []
        for ym in sorted(by_ym):
            arr = np.array(by_ym[ym])
            month_rows.append(
                dict(ym=ym, n=len(arr), med=float(np.median(arr)), mean=float(np.mean(arr)), win=float((arr > 0).mean() * 100))
            )
        mdf = pd.DataFrame(month_rows)
        pos_months = int((mdf["med"] > 0).sum())
        lines.append(f"## Hold {hold}\n\n")
        lines.append(
            f"- N={len(legs)} excess_med={float(np.median(xs)):+.2f}% mean={float(np.mean(xs)):+.2f}% "
            f"win={(xs>0).mean()*100:.0f}%\n"
        )
        lines.append(f"- bootstrap median 90% CI: [{lo:+.2f}%, {hi:+.2f}%]\n")
        lines.append(f"- months with med>0: {pos_months}/{len(mdf)}\n\n")
        lines.append("|ym|n|med|mean|win|\n|---|---:|---:|---:|---:|\n")
        for r in mdf.itertuples():
            lines.append(f"|{r.ym}|{r.n}|{r.med:+.2f}|{r.mean:+.2f}|{r.win:.0f}|\n")
        lines.append("\n")
        pd.DataFrame(legs).to_csv(OUT / f"songshan_H{hold}_legs.csv", index=False)
        print(
            f"H{hold}: n={len(legs)} med={float(np.median(xs)):+.2f} CI=[{lo:+.2f},{hi:+.2f}] "
            f"pos_months={pos_months}/{len(mdf)}",
            flush=True,
        )

    (OUT / "SONGSHAN_STABILITY.md").write_text("".join(lines), encoding="utf-8")
    print("WROTE", OUT / "SONGSHAN_STABILITY.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
