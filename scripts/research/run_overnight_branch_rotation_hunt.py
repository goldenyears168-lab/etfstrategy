#!/usr/bin/env python3
"""Overnight hunt: multi-hypothesis branch rotation / consensus toward high excess median.

Runs against a local SQLite snapshot (or stocks.db). Writes incremental results under
reports/research/branch-footprint-screen/overnight_hunt_YYYYMMDD/.

Target (aspirational): stable L1H7 excess median >= +10%. If unreachable under honest
walk-forward / minimum-N constraints, report the best feasible protocols with evidence.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "research")]

spec = importlib.util.spec_from_file_location(
    "mvp_overnight", ROOT / "scripts/research/run_abc_rolling_top10_l1h7_mvp.py"
)
mvp = importlib.util.module_from_spec(spec)
sys.modules["mvp_overnight"] = mvp
assert spec.loader is not None
spec.loader.exec_module(mvp)

OUT_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / f"overnight_hunt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Priority sleeves from prior N15-30 solo cards + stable Top30 set
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
WEIGHT_STOCKS = {"2330", "2317", "2454", "2308", "2881", "2882", "2891", "2412", "3045"}

HOLD_GRID = (3, 5, 7, 10, 14)
N_BANDS = ((5, 12), (8, 20), (15, 30), (20, 40))
ABS_GRID = mvp.ABS_GRID
SHARE_GRID = mvp.SHARE_GRID
COST = mvp.COST_RATE
SOURCE = mvp.SOURCE


@dataclass
class Event:
    branch_id: str
    trade_date: str
    stock_id: str
    buy_amt: float
    buy_share: float


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    (OUT_DIR / "run.log").open("a", encoding="utf-8").write(line + "\n")


def pick_db() -> Path:
    for p in (
        ROOT / "data/scratch/rolling_mvp_snapshot.db",
        ROOT / "data/stocks.db",
    ):
        if p.exists():
            return p
    raise FileNotFoundError("no stocks db")


def outcome(
    stock_id: str,
    signal_date: str,
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict,
    benchmark: dict,
    hold: int,
) -> dict | None:
    si = date_index.get(signal_date)
    if si is None:
        return None
    ei = si + 1
    xi = ei + hold - 1
    if xi >= len(calendar):
        return None
    ed, xd = calendar[ei], calendar[xi]
    er, xr = ohlc.get((stock_id, ed)), ohlc.get((stock_id, xd))
    be, bx = benchmark.get(ed), benchmark.get(xd)
    if not er or not xr or not be or not bx:
        return None
    stock_r = xr[1] / er[0] - 1.0 - COST
    bench_r = bx[1] / be[0] - 1.0
    return {
        "signal_date": signal_date,
        "entry_date": ed,
        "exit_date": xd,
        "stock_id": stock_id,
        "stock_pct": stock_r * 100.0,
        "bench_pct": bench_r * 100.0,
        "excess_pct": (stock_r - bench_r) * 100.0,
    }


def summarize(legs: list[dict], min_n: int = 8) -> dict:
    if len(legs) < min_n:
        return {
            "n": len(legs),
            "ok": False,
            "excess_med": np.nan,
            "excess_mean": np.nan,
            "stock_med": np.nan,
            "stock_mean": np.nan,
            "win": np.nan,
            "max1": np.nan,
            "slot_n": 0,
            "slot_excess_med": np.nan,
            "slot_compound": np.nan,
            "slot_dd": np.nan,
            "uniq": 0,
        }
    xs = np.array([x["excess_pct"] for x in legs], float)
    rs = np.array([x["stock_pct"] for x in legs], float)
    ctr = Counter(x["stock_id"] for x in legs)
    # single-slot non-overlap
    taken = []
    busy = None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    if taken:
        txs = np.array([x["excess_pct"] for x in taken], float)
        trs = np.array([x["stock_pct"] for x in taken], float) / 100.0
        eq = np.cumprod(1.0 + trs)
        dd = (eq / np.maximum.accumulate(eq) - 1.0) * 100.0
        slot = {
            "slot_n": len(taken),
            "slot_excess_med": float(np.median(txs)),
            "slot_compound": float((eq[-1] - 1.0) * 100.0),
            "slot_dd": float(dd.min()),
        }
    else:
        slot = {
            "slot_n": 0,
            "slot_excess_med": np.nan,
            "slot_compound": np.nan,
            "slot_dd": np.nan,
        }
    return {
        "n": len(legs),
        "ok": True,
        "excess_med": float(np.median(xs)),
        "excess_mean": float(np.mean(xs)),
        "stock_med": float(np.median(rs)),
        "stock_mean": float(np.mean(rs)),
        "win": float((xs > 0).mean() * 100.0),
        "max1": max(ctr.values()) / len(legs),
        "uniq": len(ctr),
        **slot,
    }


def choose_rule(
    events: list[Event],
    outcomes_by_key: dict[tuple[str, str, int], dict | None],
    hold: int,
    n_lo: int,
    n_hi: int,
    exclude_weights: bool,
) -> tuple[float, float, int, float, float, list[dict]] | None:
    best = None
    for a in ABS_GRID:
        for s in SHARE_GRID:
            legs = []
            for ev in events:
                if ev.buy_amt < a or ev.buy_share < s:
                    continue
                if exclude_weights and ev.stock_id in WEIGHT_STOCKS:
                    continue
                oc = outcomes_by_key.get((ev.stock_id, ev.trade_date, hold))
                if oc is None:
                    continue
                legs.append({**oc, "branch_id": ev.branch_id, "buy_amt": ev.buy_amt, "buy_share": ev.buy_share})
            n = len(legs)
            if not (n_lo <= n <= n_hi):
                continue
            med = float(np.median([x["excess_pct"] for x in legs]))
            mean = float(np.mean([x["excess_pct"] for x in legs]))
            cand = (med, mean, n, a, s, legs)
            if best is None or cand[:2] > best[:2]:
                best = cand
    return best


def walk_forward_fixed_rule(
    events: list[Event],
    outcomes_by_key: dict,
    calendar: list[str],
    hold: int,
    train_days: int,
    n_lo: int,
    n_hi: int,
    exclude_weights: bool,
) -> list[dict]:
    """Re-fit abs∩share every train_days on past window; apply next train_days OOS."""
    date_index = {d: i for i, d in enumerate(calendar)}
    by_date: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        by_date[ev.trade_date].append(ev)
    dates = [d for d in calendar if d in by_date]
    if len(dates) < train_days * 2:
        return []
    oos_legs: list[dict] = []
    start_i = train_days
    while start_i < len(dates):
        train_dates = set(dates[start_i - train_days : start_i])
        test_dates = set(dates[start_i : min(start_i + train_days, len(dates))])
        train_events = [e for d in train_dates for e in by_date[d]]
        # fit on train only using outcomes with exit <= last train date
        last_train = max(train_dates)
        best = None
        for a in ABS_GRID:
            for s in SHARE_GRID:
                xs = []
                for ev in train_events:
                    if ev.buy_amt < a or ev.buy_share < s:
                        continue
                    if exclude_weights and ev.stock_id in WEIGHT_STOCKS:
                        continue
                    oc = outcomes_by_key.get((ev.stock_id, ev.trade_date, hold))
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
        if best is not None:
            _, _, _, a, s = best
            for d in sorted(test_dates):
                for ev in by_date[d]:
                    if ev.buy_amt < a or ev.buy_share < s:
                        continue
                    if exclude_weights and ev.stock_id in WEIGHT_STOCKS:
                        continue
                    oc = outcomes_by_key.get((ev.stock_id, ev.trade_date, hold))
                    if oc is None:
                        continue
                    oos_legs.append(
                        {
                            **oc,
                            "branch_id": ev.branch_id,
                            "buy_amt": ev.buy_amt,
                            "buy_share": ev.buy_share,
                            "rule_abs": a,
                            "rule_share": s,
                        }
                    )
        start_i += train_days
    return oos_legs


def consensus_legs(
    branch_events: dict[str, list[Event]],
    outcomes_by_key: dict,
    hold: int,
    min_branches: int,
    abs_floor: float,
    share_floor: float,
    exclude_weights: bool,
    branch_ids: list[str] | None = None,
) -> list[dict]:
    """Same stock appears as Top1 under threshold for >= min_branches on same day."""
    ids = branch_ids or list(branch_events)
    day_stock_branches: dict[tuple[str, str], set[str]] = defaultdict(set)
    day_stock_amt: dict[tuple[str, str], float] = defaultdict(float)
    for bid in ids:
        for ev in branch_events[bid]:
            if ev.buy_amt < abs_floor or ev.buy_share < share_floor:
                continue
            if exclude_weights and ev.stock_id in WEIGHT_STOCKS:
                continue
            key = (ev.trade_date, ev.stock_id)
            day_stock_branches[key].add(bid)
            day_stock_amt[key] = max(day_stock_amt[key], ev.buy_amt)
    legs = []
    for (d, sid), bids in day_stock_branches.items():
        if len(bids) < min_branches:
            continue
        oc = outcomes_by_key.get((sid, d, hold))
        if oc is None:
            continue
        legs.append(
            {
                **oc,
                "n_branches": len(bids),
                "branches": ",".join(sorted(bids)),
                "buy_amt": day_stock_amt[(d, sid)],
            }
        )
    return sorted(legs, key=lambda x: x["signal_date"])


def rotate_by_recent_edge(
    branch_events: dict[str, list[Event]],
    outcomes_by_key: dict,
    calendar: list[str],
    hold: int,
    lookback: int,
    abs_floor: float,
    share_floor: float,
    exclude_weights: bool,
    branch_ids: list[str],
) -> list[dict]:
    """Each day follow only the branch with best recent realized excess median."""
    date_index = {d: i for i, d in enumerate(calendar)}
    # precompute per-branch threshold events
    by_branch_date: dict[str, dict[str, Event]] = {b: {} for b in branch_ids}
    for bid in branch_ids:
        for ev in branch_events[bid]:
            if ev.buy_amt < abs_floor or ev.buy_share < share_floor:
                continue
            if exclude_weights and ev.stock_id in WEIGHT_STOCKS:
                continue
            by_branch_date[bid][ev.trade_date] = ev

    # realized excess history by branch (completed exits)
    hist: dict[str, list[tuple[str, float]]] = {b: [] for b in branch_ids}
    legs: list[dict] = []
    for d in calendar:
        # update hist with outcomes whose exit_date == d
        for bid in branch_ids:
            for past_d, ev in list(by_branch_date[bid].items()):
                oc = outcomes_by_key.get((ev.stock_id, past_d, hold))
                if oc and oc["exit_date"] == d:
                    hist[bid].append((d, oc["excess_pct"]))

        # score branches on lookback completed trades
        scores = []
        for bid in branch_ids:
            recent = [x for t, x in hist[bid] if True][-lookback:]
            if len(recent) < max(3, lookback // 3):
                continue
            scores.append((float(np.median(recent)), float(np.mean(recent)), bid))
        if not scores:
            continue
        scores.sort(reverse=True)
        best_bid = scores[0][2]
        ev = by_branch_date[best_bid].get(d)
        if ev is None:
            continue
        oc = outcomes_by_key.get((ev.stock_id, d, hold))
        if oc is None:
            continue
        legs.append({**oc, "branch_id": best_bid, "buy_amt": ev.buy_amt, "buy_share": ev.buy_share})
    return legs


def save_result(name: str, meta: dict, legs: list[dict], min_n: int = 8) -> dict:
    stats = summarize(legs, min_n=min_n)
    row = {"hypothesis": name, **meta, **stats}
    # append to master csv
    path = OUT_DIR / "all_results.csv"
    pd.DataFrame([row]).to_csv(path, mode="a", header=not path.exists(), index=False)
    if legs and stats.get("excess_med", -999) >= 5.0:
        pd.DataFrame(legs).to_csv(OUT_DIR / f"legs_{name}.csv", index=False)
    hit = ""
    if stats["ok"] and stats["excess_med"] >= 10:
        hit = " ★★★ +10%"
    elif stats["ok"] and stats["excess_med"] >= 5:
        hit = " ★ +5%"
    log(
        f"{name}: n={stats['n']} excess_med={stats['excess_med']:+.2f}% "
        f"mean={stats['excess_mean']:+.2f}% win={stats['win']:.0f}% "
        f"max1={stats['max1']*100 if stats['ok'] else float('nan'):.0f}%{hit}"
    )
    return row


def main() -> int:
    t0 = time.time()
    log(f"OUT_DIR={OUT_DIR}")
    db = pick_db()
    log(f"db={db}")

    uni = {x["id"]: x for x in mvp.load_universe(mvp.DEFAULT_UNIVERSE)}
    # Lean mode: CORE16 only (avoid OOM from loading 50+ deep tapes + full OHLC).
    load_ids = [b[0] for b in CORE16]

    conn = mvp.connect_readonly(db)
    try:
        end = str(
            mvp.query_with_retry(
                conn, "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source=?", (SOURCE,)
            )[0][0]
        )
        calendar = mvp.load_calendar(conn, mvp.WINDOW_START, end)
        benchmark = mvp.load_benchmark(conn, calendar[0], calendar[-1])
        ohlc = mvp.load_stock_ohlc(conn, calendar[0], calendar[-1])
        log(f"cal={len(calendar)} ohlc={len(ohlc)} end={end}")
        log(f"loading topbuys for {len(load_ids)} branches (CORE16 lean)")
        top_buys = mvp.load_top_buys(conn, load_ids, calendar[0], calendar[-1])
        log(f"topbuys loaded branches={len(top_buys)}")
    finally:
        conn.close()

    branch_events: dict[str, list[Event]] = {}
    for bid, dated in top_buys.items():
        branch_events[bid] = [
            Event(bid, d, dated[d].stock_id, dated[d].buy_amt, dated[d].buy_share)
            for d in sorted(dated)
        ]
    names = {b[0]: b[1] for b in CORE16}
    for bid in branch_events:
        if bid not in names:
            names[bid] = uni.get(bid, {}).get("name", bid)

    # precompute outcomes for needed (stock,date,hold)
    needed: set[tuple[str, str, int]] = set()
    for hold in HOLD_GRID:
        for events in branch_events.values():
            for ev in events:
                needed.add((ev.stock_id, ev.trade_date, hold))
    log(f"computing outcomes keys={len(needed)}")
    date_index = {d: i for i, d in enumerate(calendar)}
    outcomes_by_key: dict[tuple[str, str, int], dict | None] = {}
    for i, (sid, d, hold) in enumerate(needed):
        outcomes_by_key[(sid, d, hold)] = outcome(sid, d, calendar, date_index, ohlc, benchmark, hold)
        if (i + 1) % 20000 == 0:
            log(f"outcomes {i+1}/{len(needed)}")

    results: list[dict] = []

    # ---- H1: solo adaptive per core branch ----
    log("=== H1 solo adaptive ===")
    for hold in HOLD_GRID:
        for n_lo, n_hi in N_BANDS:
            for exclude in (False, True):
                for bid, bname in CORE16:
                    events = branch_events.get(bid, [])
                    picked = choose_rule(events, outcomes_by_key, hold, n_lo, n_hi, exclude)
                    if not picked:
                        continue
                    med, mean, n, a, s, legs = picked
                    results.append(
                        save_result(
                            f"H1_solo_{bid}_H{hold}_N{n_lo}-{n_hi}{'_xW' if exclude else ''}",
                            {
                                "family": "H1_solo",
                                "branch": bname,
                                "hold": hold,
                                "n_lo": n_lo,
                                "n_hi": n_hi,
                                "exclude_weights": exclude,
                                "rule": f">={a/1e8:g}億∩{round(s*100):g}%",
                            },
                            legs,
                        )
                    )

    # ---- H2: Top4 / Top6 consensus ----
    log("=== H2 consensus ===")
    for hold in (5, 7, 10):
        for min_b in (2, 3, 4):
            for a in (3e7, 5e7, 1e8, 1.5e8, 2e8, 3e8, 5e8):
                for s in (0.12, 0.2, 0.25, 0.3, 0.4, 0.5):
                    for exclude in (False, True):
                        for label, ids in (
                            ("top4", [b[0] for b in TOP4]),
                            ("core8", [b[0] for b in CORE16[:8]]),
                            ("core16", [b[0] for b in CORE16]),
                        ):
                            legs = consensus_legs(
                                branch_events,
                                outcomes_by_key,
                                hold,
                                min_b,
                                a,
                                s,
                                exclude,
                                ids,
                            )
                            if len(legs) < 5:
                                continue
                            results.append(
                                save_result(
                                    f"H2_cons_{label}_m{min_b}_H{hold}_a{a/1e8:g}_s{round(s*100)}{'_xW' if exclude else ''}",
                                    {
                                        "family": "H2_consensus",
                                        "pool": label,
                                        "min_branches": min_b,
                                        "hold": hold,
                                        "abs": a,
                                        "share": s,
                                        "exclude_weights": exclude,
                                    },
                                    legs,
                                    min_n=5,
                                )
                            )

    # ---- H3: rotate by recent edge among Top4 ----
    log("=== H3 rotation ===")
    for hold in (5, 7, 10):
        for lookback in (5, 8, 12):
            for a in (5e7, 1e8, 2e8, 3e8):
                for s in (0.2, 0.3, 0.4, 0.5):
                    for exclude in (False, True):
                        legs = rotate_by_recent_edge(
                            branch_events,
                            outcomes_by_key,
                            calendar,
                            hold,
                            lookback,
                            a,
                            s,
                            exclude,
                            [b[0] for b in TOP4],
                        )
                        if len(legs) < 8:
                            continue
                        results.append(
                            save_result(
                                f"H3_rot_lb{lookback}_H{hold}_a{a/1e8:g}_s{round(s*100)}{'_xW' if exclude else ''}",
                                {
                                    "family": "H3_rotation",
                                    "lookback": lookback,
                                    "hold": hold,
                                    "abs": a,
                                    "share": s,
                                    "exclude_weights": exclude,
                                },
                                legs,
                            )
                        )

    # ---- H4: walk-forward OOS for Top4 solos ----
    log("=== H4 walk-forward OOS ===")
    for hold in (7,):
        for train in (40, 60, 80):
            for n_lo, n_hi in ((5, 15), (8, 20), (15, 30)):
                for exclude in (False, True):
                    for bid, bname in TOP4:
                        legs = walk_forward_fixed_rule(
                            branch_events.get(bid, []),
                            outcomes_by_key,
                            calendar,
                            hold,
                            train,
                            n_lo,
                            n_hi,
                            exclude,
                        )
                        results.append(
                            save_result(
                                f"H4_wf_{bid}_tr{train}_H{hold}_N{n_lo}-{n_hi}{'_xW' if exclude else ''}",
                                {
                                    "family": "H4_walkforward",
                                    "branch": bname,
                                    "train_days": train,
                                    "hold": hold,
                                    "n_lo": n_lo,
                                    "n_hi": n_hi,
                                    "exclude_weights": exclude,
                                },
                                legs,
                                min_n=6,
                            )
                        )

    # ---- H5: extreme only — very high abs, tiny N, report honestly ----
    log("=== H5 extreme ----")
    for hold in (5, 7, 10):
        for n_lo, n_hi in ((5, 12), (8, 15)):
            for exclude in (True, False):
                # pool: any of top4 extreme hits, dedupe by day taking max buy_amt
                pool_legs = []
                for bid, bname in TOP4:
                    picked = choose_rule(
                        branch_events.get(bid, []),
                        outcomes_by_key,
                        hold,
                        n_lo,
                        n_hi,
                        exclude,
                    )
                    if picked:
                        _, _, _, a, s, legs = picked
                        for lg in legs:
                            pool_legs.append({**lg, "branch": bname, "rule_abs": a, "rule_share": s})
                # dedupe same day+stock keep first
                seen = set()
                uniq = []
                for lg in sorted(pool_legs, key=lambda x: (-x["buy_amt"], x["signal_date"])):
                    k = (lg["signal_date"], lg["stock_id"])
                    if k in seen:
                        continue
                    seen.add(k)
                    uniq.append(lg)
                results.append(
                    save_result(
                        f"H5_extreme_pool_H{hold}_N{n_lo}-{n_hi}{'_xW' if exclude else ''}",
                        {
                            "family": "H5_extreme_pool",
                            "hold": hold,
                            "n_lo": n_lo,
                            "n_hi": n_hi,
                            "exclude_weights": exclude,
                        },
                        uniq,
                        min_n=5,
                    )
                )

    # ---- H6: OR union Top4 with fixed strong rule, single slot ----
    log("=== H6 union OR ===")
    for hold in (5, 7, 10):
        for a in (1e8, 2e8, 3e8, 5e8):
            for s in (0.3, 0.4, 0.5):
                for exclude in (False, True):
                    legs = []
                    for bid, _ in TOP4:
                        for ev in branch_events.get(bid, []):
                            if ev.buy_amt < a or ev.buy_share < s:
                                continue
                            if exclude and ev.stock_id in WEIGHT_STOCKS:
                                continue
                            oc = outcomes_by_key.get((ev.stock_id, ev.trade_date, hold))
                            if oc is None:
                                continue
                            legs.append({**oc, "branch_id": bid, "buy_amt": ev.buy_amt})
                    # one per day: max buy_amt
                    by_day: dict[str, dict] = {}
                    for lg in legs:
                        cur = by_day.get(lg["signal_date"])
                        if cur is None or lg["buy_amt"] > cur["buy_amt"]:
                            by_day[lg["signal_date"]] = lg
                    uniq = [by_day[d] for d in sorted(by_day)]
                    results.append(
                        save_result(
                            f"H6_union_H{hold}_a{a/1e8:g}_s{round(s*100)}{'_xW' if exclude else ''}",
                            {
                                "family": "H6_union",
                                "hold": hold,
                                "abs": a,
                                "share": s,
                                "exclude_weights": exclude,
                            },
                            uniq,
                        )
                    )

    # ---- H7: confirm persistence — same stock Top1 two consecutive days ----
    log("=== H7 persistence ===")
    for hold in (5, 7, 10):
        for a in (5e7, 1e8, 2e8):
            for s in (0.2, 0.3, 0.4):
                for exclude in (False, True):
                    for bid, bname in TOP4 + CORE16[4:8]:
                        dated = {e.trade_date: e for e in branch_events.get(bid, [])}
                        legs = []
                        for d, ev in sorted(dated.items()):
                            if ev.buy_amt < a or ev.buy_share < s:
                                continue
                            if exclude and ev.stock_id in WEIGHT_STOCKS:
                                continue
                            di = date_index.get(d)
                            if di is None or di == 0:
                                continue
                            prev = calendar[di - 1]
                            pev = dated.get(prev)
                            if pev is None or pev.stock_id != ev.stock_id:
                                continue
                            if pev.buy_amt < a or pev.buy_share < s:
                                continue
                            oc = outcomes_by_key.get((ev.stock_id, d, hold))
                            if oc is None:
                                continue
                            legs.append({**oc, "branch_id": bid})
                        if len(legs) < 5:
                            continue
                        results.append(
                            save_result(
                                f"H7_persist_{bid}_H{hold}_a{a/1e8:g}_s{round(s*100)}{'_xW' if exclude else ''}",
                                {
                                    "family": "H7_persist",
                                    "branch": bname,
                                    "hold": hold,
                                    "abs": a,
                                    "share": s,
                                    "exclude_weights": exclude,
                                },
                                legs,
                                min_n=5,
                            )
                        )

    # rank and write summary
    df = pd.read_csv(OUT_DIR / "all_results.csv")
    df = df.sort_values(["excess_med", "n"], ascending=[False, False])
    df.to_csv(OUT_DIR / "all_results_ranked.csv", index=False)
    top = df[df["ok"] == True].head(40)  # noqa: E712
    ge10 = df[(df["ok"] == True) & (df["excess_med"] >= 10) & (df["n"] >= 8)]  # noqa: E712
    ge5 = df[(df["ok"] == True) & (df["excess_med"] >= 5) & (df["n"] >= 10)]  # noqa: E712

    lines = []
    lines.append("# Overnight branch rotation hunt\n\n")
    lines.append(f"- dir: `{OUT_DIR.name}`\n")
    lines.append(f"- runtime_min: {(time.time()-t0)/60:.1f}\n")
    lines.append(f"- experiments_rows: {len(df)}\n")
    lines.append(f"- hits_excess_med>=10 & N>=8: {len(ge10)}\n")
    lines.append(f"- hits_excess_med>=5 & N>=10: {len(ge5)}\n\n")
    lines.append("## Top 25 by excess median (ok only)\n\n")
    lines.append(
        "|hypothesis|n|excess_med|excess_mean|win|max1|slot_n|slot_med|slot_dd|\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    for r in top.head(25).itertuples():
        lines.append(
            f"|{r.hypothesis}|{int(r.n)}|{r.excess_med:+.2f}|{r.excess_mean:+.2f}|"
            f"{r.win:.0f}|{r.max1*100:.0f}%|{int(r.slot_n)}|{r.slot_excess_med:+.2f}|{r.slot_dd:+.0f}|\n"
        )
    if len(ge10):
        lines.append("\n## ★ Protocols with excess_med >= +10% and N>=8\n\n")
        for r in ge10.itertuples():
            lines.append(f"- `{r.hypothesis}` n={int(r.n)} med={r.excess_med:+.2f} mean={r.excess_mean:+.2f}\n")
    else:
        lines.append(
            "\n## No protocol hit excess_med >= +10% with N>=8 under tested grids.\n"
            "Best feasible candidates listed above; see ranked CSV for full sweep.\n"
        )
    (OUT_DIR / "SUMMARY.md").write_text("".join(lines), encoding="utf-8")
    log(f"DONE rows={len(df)} ge10={len(ge10)} ge5={len(ge5)} -> {OUT_DIR}")
    print(top.head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
