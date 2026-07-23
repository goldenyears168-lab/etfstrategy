#!/usr/bin/env python3
"""Overnight hypothesis sweep from CORE16 event JSON cache (no heavy SQL)."""
from __future__ import annotations

import importlib.util
import json
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
    "mvp", ROOT / "scripts/research/run_abc_rolling_top10_l1h7_mvp.py"
)
mvp = importlib.util.module_from_spec(spec)
sys.modules["mvp"] = mvp
assert spec.loader is not None
spec.loader.exec_module(mvp)

CACHE = ROOT / "data/scratch/overnight_cache"
OUT = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / f"overnight_hunt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)
OUT.mkdir(parents=True, exist_ok=True)

CORE16 = [
    ("9875", "元大-土城永寧"),
    ("918X", "群益金鼎-台北"),
    ("9661", "富邦新店"),
    ("981j", "元大-士林"),
    ("779Z", "國票安和"),
    ("962A", "富邦-南港"),
    ("585b", "統一-內湖"),
    ("7008", "兆豐-三重"),
    ("779D", "國票-台南"),
    ("980h", "元大-台北"),
    ("989g", "元大-嘉義"),
    ("700F", "兆豐-虎尾"),
    ("5389", "第一金-中山"),
    ("9217", "凱基松山"),
    ("5383", "第一金-高雄"),
    ("9238", "凱基-士林"),
]
TOP4 = CORE16[:4]
WEIGHT = {"2330", "2317", "2454", "2308", "2881", "2412", "3045"}
ABS_GRID = mvp.ABS_GRID
SHARE_GRID = mvp.SHARE_GRID
HOLD_GRID = (3, 5, 7, 10, 14)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    (OUT / "run.log").open("a", encoding="utf-8").write(line + "\n")


def summarize(legs, min_n=8):
    if len(legs) < min_n:
        return dict(
            n=len(legs),
            ok=False,
            excess_med=np.nan,
            excess_mean=np.nan,
            stock_med=np.nan,
            win=np.nan,
            max1=np.nan,
            slot_n=0,
            slot_excess_med=np.nan,
            slot_dd=np.nan,
        )
    xs = np.array([x["excess_pct"] for x in legs], float)
    rs = np.array([x["stock_pct"] for x in legs], float)
    ctr = Counter(x["stock_id"] for x in legs)
    taken, busy = [], None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    if taken:
        txs = np.array([x["excess_pct"] for x in taken], float)
        trs = np.array([x["stock_pct"] for x in taken], float) / 100.0
        eq = np.cumprod(1.0 + trs)
        dd = float((eq / np.maximum.accumulate(eq) - 1.0).min() * 100.0)
        smed = float(np.median(txs))
        sn = len(taken)
    else:
        dd = smed = np.nan
        sn = 0
    return dict(
        n=len(legs),
        ok=True,
        excess_med=float(np.median(xs)),
        excess_mean=float(np.mean(xs)),
        stock_med=float(np.median(rs)),
        win=float((xs > 0).mean() * 100),
        max1=max(ctr.values()) / len(legs),
        slot_n=sn,
        slot_excess_med=smed,
        slot_dd=dd,
    )


def save(name, meta, legs, min_n=8):
    st = summarize(legs, min_n)
    row = {"hypothesis": name, **meta, **st}
    path = OUT / "all_results.csv"
    pd.DataFrame([row]).to_csv(path, mode="a", header=not path.exists(), index=False)
    tag = ""
    if st["ok"] and st["excess_med"] >= 10:
        tag = " ★★★ +10%"
        pd.DataFrame(legs).to_csv(OUT / f"legs_{name}.csv", index=False)
    elif st["ok"] and st["excess_med"] >= 5:
        tag = " ★ +5%"
        pd.DataFrame(legs).to_csv(OUT / f"legs_{name}.csv", index=False)
    log(
        f"{name}: n={st['n']} med={st['excess_med']:+.2f}% mean={st['excess_mean']:+.2f}% "
        f"win={st['win']:.0f}%{tag}"
    )
    return row


def outcome(sid, d, hold, cal, di, ohlc, bench):
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
    sr = xr[1] / er[0] - 1.0 - mvp.COST_RATE
    br = bx[1] / be[0] - 1.0
    return dict(
        signal_date=d,
        entry_date=ed,
        exit_date=xd,
        stock_id=sid,
        stock_pct=sr * 100,
        excess_pct=(sr - br) * 100,
    )


def main() -> int:
    t0 = time.time()
    log(f"OUT={OUT}")
    events_path = CACHE / "events_all.json"
    if not events_path.exists():
        # assemble from per-branch files
        events = {}
        for bid, _ in CORE16:
            p = CACHE / f"events_{bid}.json"
            if not p.exists():
                raise SystemExit(f"missing {p}; run build_overnight_core16_events_cache.py first")
            events[bid] = json.loads(p.read_text(encoding="utf-8"))
        events_path.write_text(json.dumps(events), encoding="utf-8")
    else:
        events = json.loads(events_path.read_text(encoding="utf-8"))
    cal = json.loads((CACHE / "calendar.json").read_text(encoding="utf-8"))
    di = {d: i for i, d in enumerate(cal)}
    log(f"events loaded branches={len(events)}")

    db = ROOT / "data/scratch/rolling_mvp_snapshot.db"
    conn = mvp.connect_readonly(db)
    try:
        bench = mvp.load_benchmark(conn, cal[0], cal[-1])
        ohlc = mvp.load_stock_ohlc(conn, cal[0], cal[-1])
    finally:
        conn.close()
    log(f"ohlc={len(ohlc)}")

    # outcome cache
    oc_cache = {}
    needed = 0
    for hold in HOLD_GRID:
        for bid, _ in CORE16:
            for e in events[bid]:
                key = (e["stock_id"], e["trade_date"], hold)
                if key not in oc_cache:
                    oc_cache[key] = outcome(
                        e["stock_id"], e["trade_date"], hold, cal, di, ohlc, bench
                    )
                    needed += 1
    log(f"outcomes computed keys={needed}")

    # free ohlc
    del ohlc

    # H1 solo adaptive
    log("=== H1 solo ===")
    for hold in HOLD_GRID:
        for n_lo, n_hi in ((5, 12), (8, 20), (15, 30), (20, 40)):
            for xw in (False, True):
                for bid, bname in CORE16:
                    best = None
                    for a in ABS_GRID:
                        for s in SHARE_GRID:
                            legs = []
                            for e in events[bid]:
                                if e["buy_amt"] < a or e["buy_share"] < s:
                                    continue
                                if xw and e["stock_id"] in WEIGHT:
                                    continue
                                oc = oc_cache.get((e["stock_id"], e["trade_date"], hold))
                                if oc:
                                    legs.append({**oc, "buy_amt": e["buy_amt"]})
                            n = len(legs)
                            if not (n_lo <= n <= n_hi):
                                continue
                            med = float(np.median([x["excess_pct"] for x in legs]))
                            mean = float(np.mean([x["excess_pct"] for x in legs]))
                            cand = (med, mean, n, a, s, legs)
                            if best is None or cand[:2] > best[:2]:
                                best = cand
                    if not best:
                        continue
                    med, mean, n, a, s, legs = best
                    save(
                        f"H1_{bid}_H{hold}_N{n_lo}-{n_hi}{'_xW' if xw else ''}",
                        dict(
                            family="H1_solo",
                            branch=bname,
                            hold=hold,
                            n_lo=n_lo,
                            n_hi=n_hi,
                            exclude_weights=xw,
                            rule=f">={a/1e8:g}億∩{int(round(s*100))}%",
                        ),
                        legs,
                    )

    # H2 consensus
    log("=== H2 consensus ===")
    for pool_name, pool in (("top4", TOP4), ("core8", CORE16[:8]), ("core16", CORE16)):
        pids = [b[0] for b in pool]
        for hold in (5, 7, 10, 14):
            for min_b in range(2, min(5, len(pids) + 1)):
                for a in (5e7, 1e8, 1.5e8, 2e8, 3e8, 5e8, 8e8):
                    for s in (0.2, 0.25, 0.3, 0.4, 0.5):
                        for xw in (False, True):
                            day_map = defaultdict(lambda: defaultdict(set))
                            day_amt = defaultdict(float)
                            for bid in pids:
                                for e in events[bid]:
                                    if e["buy_amt"] < a or e["buy_share"] < s:
                                        continue
                                    if xw and e["stock_id"] in WEIGHT:
                                        continue
                                    day_map[e["trade_date"]][e["stock_id"]].add(bid)
                                    day_amt[(e["trade_date"], e["stock_id"])] = max(
                                        day_amt[(e["trade_date"], e["stock_id"])],
                                        e["buy_amt"],
                                    )
                            legs = []
                            for d, stocks in day_map.items():
                                for sid, bids in stocks.items():
                                    if len(bids) < min_b:
                                        continue
                                    oc = oc_cache.get((sid, d, hold))
                                    if oc:
                                        legs.append(
                                            {
                                                **oc,
                                                "n_branches": len(bids),
                                                "buy_amt": day_amt[(d, sid)],
                                            }
                                        )
                            if len(legs) < 5:
                                continue
                            save(
                                f"H2_{pool_name}_m{min_b}_H{hold}_a{a/1e8:g}_s{int(s*100)}{'_xW' if xw else ''}",
                                dict(
                                    family="H2_consensus",
                                    pool=pool_name,
                                    min_branches=min_b,
                                    hold=hold,
                                    abs=a,
                                    share=s,
                                    exclude_weights=xw,
                                ),
                                legs,
                                min_n=5,
                            )

    # H3 rotation Top4
    log("=== H3 rotation ===")
    for hold in (5, 7, 10):
        for lookback in (5, 8, 12):
            for a in (1e8, 2e8, 3e8):
                for s in (0.3, 0.4, 0.5):
                    for xw in (False, True):
                        by_b_d = {b[0]: {} for b in TOP4}
                        for bid, _ in TOP4:
                            for e in events[bid]:
                                if e["buy_amt"] < a or e["buy_share"] < s:
                                    continue
                                if xw and e["stock_id"] in WEIGHT:
                                    continue
                                by_b_d[bid][e["trade_date"]] = e
                        hist = {b[0]: [] for b in TOP4}
                        legs = []
                        for d in cal:
                            for bid, _ in TOP4:
                                for pd0, e in by_b_d[bid].items():
                                    oc = oc_cache.get((e["stock_id"], pd0, hold))
                                    if oc and oc["exit_date"] == d:
                                        hist[bid].append(oc["excess_pct"])
                            scores = []
                            for bid, _ in TOP4:
                                recent = hist[bid][-lookback:]
                                if len(recent) < max(3, lookback // 3):
                                    continue
                                scores.append(
                                    (float(np.median(recent)), float(np.mean(recent)), bid)
                                )
                            if not scores:
                                continue
                            scores.sort(reverse=True)
                            best = scores[0][2]
                            e = by_b_d[best].get(d)
                            if not e:
                                continue
                            oc = oc_cache.get((e["stock_id"], d, hold))
                            if oc:
                                legs.append({**oc, "branch_id": best, "buy_amt": e["buy_amt"]})
                        if len(legs) >= 8:
                            save(
                                f"H3_rot_lb{lookback}_H{hold}_a{a/1e8:g}_s{int(s*100)}{'_xW' if xw else ''}",
                                dict(
                                    family="H3_rotation",
                                    lookback=lookback,
                                    hold=hold,
                                    abs=a,
                                    share=s,
                                    exclude_weights=xw,
                                ),
                                legs,
                            )

    # H4 walk-forward solo Top4
    log("=== H4 walk-forward ===")
    for hold in (7,):
        for train in (40, 60, 80):
            for n_lo, n_hi in ((5, 15), (8, 20), (15, 30)):
                for xw in (False, True):
                    for bid, bname in TOP4:
                        dated = {e["trade_date"]: e for e in events[bid]}
                        dates = [d for d in cal if d in dated]
                        oos = []
                        i = train
                        while i < len(dates):
                            train_dates = set(dates[i - train : i])
                            test_dates = set(dates[i : min(i + train, len(dates))])
                            last_train = max(train_dates)
                            best = None
                            for a in ABS_GRID:
                                for s in SHARE_GRID:
                                    xs = []
                                    for d in train_dates:
                                        e = dated[d]
                                        if e["buy_amt"] < a or e["buy_share"] < s:
                                            continue
                                        if xw and e["stock_id"] in WEIGHT:
                                            continue
                                        oc = oc_cache.get((e["stock_id"], d, hold))
                                        if oc is None or oc["exit_date"] > last_train:
                                            continue
                                        xs.append(oc["excess_pct"])
                                    n = len(xs)
                                    if not (n_lo <= n <= n_hi):
                                        continue
                                    med = float(np.median(xs))
                                    mean = float(np.mean(xs))
                                    cand = (med, mean, n, a, s)
                                    if best is None or cand[:2] > best[:2]:
                                        best = cand
                            if best:
                                _, _, _, a, s = best
                                for d in sorted(test_dates):
                                    e = dated[d]
                                    if e["buy_amt"] < a or e["buy_share"] < s:
                                        continue
                                    if xw and e["stock_id"] in WEIGHT:
                                        continue
                                    oc = oc_cache.get((e["stock_id"], d, hold))
                                    if oc:
                                        oos.append({**oc, "buy_amt": e["buy_amt"]})
                            i += train
                        save(
                            f"H4_wf_{bid}_tr{train}_H{hold}_N{n_lo}-{n_hi}{'_xW' if xw else ''}",
                            dict(
                                family="H4_walkforward",
                                branch=bname,
                                train=train,
                                hold=hold,
                                n_lo=n_lo,
                                n_hi=n_hi,
                                exclude_weights=xw,
                            ),
                            oos,
                            min_n=6,
                        )

    # H5 union intensity Top4 with frozen strong rules from N15-30
    log("=== H5 intensity union ===")
    strong = {"9875": (3e8, 0.5), "918X": (2e8, 0.4), "9661": (2e8, 0.4), "981j": (3e8, 0.3)}
    for hold in (5, 7, 10, 14):
        for xw in (False, True):
            by_day = {}
            for bid, (a, s) in strong.items():
                for e in events[bid]:
                    if e["buy_amt"] < a or e["buy_share"] < s:
                        continue
                    if xw and e["stock_id"] in WEIGHT:
                        continue
                    oc = oc_cache.get((e["stock_id"], e["trade_date"], hold))
                    if not oc:
                        continue
                    row = {**oc, "branch_id": bid, "buy_amt": e["buy_amt"]}
                    cur = by_day.get(e["trade_date"])
                    if cur is None or row["buy_amt"] > cur["buy_amt"]:
                        by_day[e["trade_date"]] = row
            legs = [by_day[d] for d in sorted(by_day)]
            save(
                f"H5_intensity_H{hold}{'_xW' if xw else ''}",
                dict(family="H5_intensity", hold=hold, exclude_weights=xw),
                legs,
            )

    # H6 consensus + min intensity
    log("=== H6 cons+intensity ===")
    for hold in (5, 7, 10):
        for min_b in (2, 3):
            for a in (1e8, 2e8, 3e8):
                for s in (0.3, 0.4, 0.5):
                    for amin in (2e8, 3e8, 5e8, 8e8):
                        day_map = defaultdict(lambda: defaultdict(set))
                        day_amt = defaultdict(float)
                        for bid, _ in TOP4:
                            for e in events[bid]:
                                if e["buy_amt"] < a or e["buy_share"] < s:
                                    continue
                                day_map[e["trade_date"]][e["stock_id"]].add(bid)
                                day_amt[(e["trade_date"], e["stock_id"])] = max(
                                    day_amt[(e["trade_date"], e["stock_id"])], e["buy_amt"]
                                )
                        legs = []
                        for d, stocks in day_map.items():
                            for sid, bids in stocks.items():
                                if len(bids) < min_b or day_amt[(d, sid)] < amin:
                                    continue
                                oc = oc_cache.get((sid, d, hold))
                                if oc:
                                    legs.append({**oc, "n_branches": len(bids), "buy_amt": day_amt[(d, sid)]})
                        if len(legs) >= 5:
                            save(
                                f"H6_consint_m{min_b}_H{hold}_a{a/1e8:g}_s{int(s*100)}_amin{amin/1e8:g}",
                                dict(
                                    family="H6_cons_intensity",
                                    min_branches=min_b,
                                    hold=hold,
                                    abs=a,
                                    share=s,
                                    amin=amin,
                                ),
                                legs,
                                min_n=5,
                            )

    # H7 walk-forward consensus Top4
    log("=== H7 wf consensus ===")
    dates = sorted({e["trade_date"] for bid, _ in TOP4 for e in events[bid]})
    for hold in (5, 7, 10):
        for train in (40, 60):
            oos = []
            i = train
            while i < len(dates):
                train_set = set(dates[i - train : i])
                test_set = set(dates[i : min(i + train, len(dates))])
                last_train = max(train_set)
                best = None
                for min_b in (2, 3):
                    for a in (5e7, 1e8, 2e8, 3e8, 5e8):
                        for s in (0.2, 0.3, 0.4, 0.5):
                            for xw in (False, True):
                                day_map = defaultdict(lambda: defaultdict(set))
                                for bid, _ in TOP4:
                                    for e in events[bid]:
                                        if e["trade_date"] not in train_set:
                                            continue
                                        if e["buy_amt"] < a or e["buy_share"] < s:
                                            continue
                                        if xw and e["stock_id"] in WEIGHT:
                                            continue
                                        day_map[e["trade_date"]][e["stock_id"]].add(bid)
                                xs = []
                                for d, stocks in day_map.items():
                                    for sid, bids in stocks.items():
                                        if len(bids) < min_b:
                                            continue
                                        oc = oc_cache.get((sid, d, hold))
                                        if oc is None or oc["exit_date"] > last_train:
                                            continue
                                        xs.append(oc["excess_pct"])
                                n = len(xs)
                                if not (5 <= n <= 25):
                                    continue
                                med = float(np.median(xs))
                                mean = float(np.mean(xs))
                                cand = (med, mean, n, min_b, a, s, xw)
                                if best is None or cand[:2] > best[:2]:
                                    best = cand
                if best:
                    _, _, _, min_b, a, s, xw = best
                    day_map = defaultdict(lambda: defaultdict(set))
                    for bid, _ in TOP4:
                        for e in events[bid]:
                            if e["trade_date"] not in test_set:
                                continue
                            if e["buy_amt"] < a or e["buy_share"] < s:
                                continue
                            if xw and e["stock_id"] in WEIGHT:
                                continue
                            day_map[e["trade_date"]][e["stock_id"]].add(bid)
                    for d, stocks in day_map.items():
                        for sid, bids in stocks.items():
                            if len(bids) < min_b:
                                continue
                            oc = oc_cache.get((sid, d, hold))
                            if oc:
                                oos.append({**oc, "n_branches": len(bids)})
                i += train
            save(
                f"H7_wfcons_tr{train}_H{hold}",
                dict(family="H7_wf_consensus", train=train, hold=hold),
                oos,
                min_n=6,
            )

    df = pd.read_csv(OUT / "all_results.csv")
    df = df.sort_values(["excess_med", "n"], ascending=[False, False])
    df.to_csv(OUT / "all_results_ranked.csv", index=False)
    ge10 = df[(df["ok"] == True) & (df["excess_med"] >= 10) & (df["n"] >= 8)]  # noqa: E712
    ge5 = df[(df["ok"] == True) & (df["excess_med"] >= 5) & (df["n"] >= 10)]  # noqa: E712
    top = df[df["ok"] == True].head(40)  # noqa: E712

    lines = [
        "# Overnight branch rotation hunt (cache-backed)\n\n",
        f"- out: `{OUT.name}`\n",
        f"- runtime_min: {(time.time()-t0)/60:.1f}\n",
        f"- rows: {len(df)}\n",
        f"- excess_med>=10 & N>=8: **{len(ge10)}**\n",
        f"- excess_med>=5 & N>=10: **{len(ge5)}**\n\n",
        "## Top 30\n\n",
        "|hypothesis|n|excess_med|excess_mean|win|max1|slot_med|slot_dd|\n",
        "|---|---:|---:|---:|---:|---:|---:|---:|\n",
    ]
    for r in top.head(30).itertuples():
        lines.append(
            f"|{r.hypothesis}|{int(r.n)}|{r.excess_med:+.2f}|{r.excess_mean:+.2f}|"
            f"{r.win:.0f}|{r.max1*100:.0f}%|{r.slot_excess_med:+.2f}|{r.slot_dd:+.0f}|\n"
        )
    if len(ge10):
        lines.append("\n## ★★★ +10% protocols\n\n")
        for r in ge10.itertuples():
            lines.append(
                f"- `{r.hypothesis}` n={int(r.n)} med={r.excess_med:+.2f} mean={r.excess_mean:+.2f}\n"
            )
    else:
        lines.append(
            "\n## No protocol reached excess_med >= +10% with N>=8.\n\n"
            "Best feasible methods are listed above. Prefer walk-forward / consensus over in-sample solo.\n"
        )
    (OUT / "FINAL_SUMMARY.md").write_text("".join(lines), encoding="utf-8")
    log(f"DONE rows={len(df)} ge10={len(ge10)} ge5={len(ge5)}")
    print(top.head(20)[["hypothesis", "n", "excess_med", "excess_mean", "win"]].to_string(index=False))
    print("OUT", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
