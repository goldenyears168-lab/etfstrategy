#!/usr/bin/env python3
"""Which of the 16 cells (session x PV8) carry this strategy, and which
diverge most from each other? Raw causal_engine.simulate() (not the
order-layer replay -- this is a descriptive cell-attribution breakdown,
fast enough to run directly) on the current LIVE PAPER_RECIPE, across the
same 22-day corrected full-day (00:00-23:59) window used tonight
(2026-08-09).

Does NOT touch src/order/, config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from copy import deepcopy

sys.path.insert(0, "src")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import PV8  # noqa: E402
from tmf_channel.cache_store import load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})


def sess_of_hhmm(hm: str) -> str:
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def main():
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    by_cell = defaultdict(list)  # (sess, regime_e) -> [pnl, pnl, ...]
    all_trades_n = 0
    per_day_net = {}

    for day in JULY_DAYS + AUG_DAYS:
        source = SOURCE_FOR_DAY[day]
        rows = load_day(day, source=source)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]
        H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]
        C = [float(r["c"]) for r in rows]
        V = [float(r.get("v") or 0) for r in rows]
        T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        per_day_net[day] = round(sum(t["pnl"] for t in trades), 1)
        for t in trades:
            all_trades_n += 1
            hm = str(t.get("et") or t.get("xt") or "")[11:16]
            sess = sess_of_hhmm(hm)
            regime = str(t.get("regime_e") or "?")
            by_cell[(sess, regime)].append(float(t["pnl"]))

    print(f"total trades across {len(JULY_DAYS)+len(AUG_DAYS)} days: {all_trades_n}\n")
    print(f"{'cell':22s} {'n':>5s} {'net':>10s} {'mean':>8s} {'std':>8s} {'win%':>6s}")
    rows_out = []
    for sess in ("day", "night"):
        for reg in PV8:
            key = (sess, reg)
            pnls = by_cell.get(key, [])
            if not pnls:
                print(f"{sess+'|'+reg:22s} {0:5d} {'--':>10s}")
                continue
            n = len(pnls)
            net = sum(pnls)
            mean = st.mean(pnls)
            std = st.stdev(pnls) if n > 1 else 0.0
            wr = 100.0 * sum(1 for p in pnls if p > 0) / n
            print(f"{sess+'|'+reg:22s} {n:5d} {net:10.1f} {mean:8.1f} {std:8.1f} {wr:5.1f}%")
            rows_out.append(dict(cell=f"{sess}|{reg}", n=n, net=round(net, 1),
                                  mean=round(mean, 1), std=round(std, 1), win_rate=round(wr, 1)))

    out_path = "reports/research/channel_lab/tmf_cell_breakdown_22day_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(per_day_net=per_day_net, cells=rows_out), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
