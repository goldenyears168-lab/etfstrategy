#!/usr/bin/env python3
"""2026 第二次處置股 × stable Top30 分點 · 自適應大戶門檻 · L1H7.

Research only. Protocol (frozen for this run):
  - Branches: abc_rolling_top30_stability Top30 (by epoch hits)
  - Events: branch day buy of a stock inside 2026「第二次處置」
    window [p_start, p_end], with buy_amt=buy×close and
    buy_share = that stock's share of the branch's day buy book
    (NOT restricted to day Top1 — Top1 was too sparse)
  - Adaptive whale rule: abs∩share grid, prefer N∈[n_lo,n_hi],
    maximize L1H7 excess median (stock − β×IX0001 − 30bps)
  - Hold: L1H7 = T+1 open → H7 close
  - Dedupe: same stock within 5 calendar days → keep first

Example:
  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_second_disp_top30_l1h7.py --shard 0 --shards 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "research")]

import run_abc_rolling_top10_l1h7_mvp as mvp  # noqa: E402

OUT = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "second_disp_top30_l1h7"
)
STAB = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "abc_rolling_top30_stability.csv"
)
WHALE100 = OUT / "whale100_universe.json"
TWSE_CACHE = OUT / "twse_second_disp_2026.json"

SOURCE = mvp.SOURCE
COST = mvp.COST_RATE
BETA = 1.15
ABS_GRID = mvp.ABS_GRID
# Disposition buys are often smaller than general Top1 whale prints
ABS_GRID_BALANCED = (
    0.1e8,
    0.15e8,
    0.2e8,
    0.3e8,
    0.5e8,
    0.7e8,
    1e8,
    1.5e8,
    2e8,
    3e8,
    5e8,
    8e8,
    10e8,
)
# Lower share floors: disposition names are rarely a branch's day-book majority
SHARE_GRID = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50)
HOLD = 7
START = "2024-07-01"
END = "2026-07-22"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def roc_to_iso(s: str) -> str:
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911}-{m}-{d}"


def fetch_twse_second_2026() -> list[dict]:
    if TWSE_CACHE.exists():
        return json.loads(TWSE_CACHE.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    url = (
        "https://www.twse.com.tw/rwd/zh/announcement/punish"
        "?response=json&startDate=20260101&endDate=20260723"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode())
    rows = []
    for row in payload.get("data") or []:
        if row[7] != "第二次處置":
            continue
        a, b = row[6].replace("～", "~").split("~")
        sid = str(row[2])
        if len(sid) != 4 or not sid.isdigit():
            continue
        rows.append(
            {
                "announce": roc_to_iso(row[1]),
                "sid": sid,
                "name": row[3],
                "p_start": roc_to_iso(a),
                "p_end": roc_to_iso(b),
            }
        )
    TWSE_CACHE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def merge_episodes(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["sid"]].append(r)
    out: list[dict] = []
    for sid, lst in by.items():
        lst.sort(key=lambda x: x["announce"])
        cur = None
        for e in lst:
            if cur is None:
                cur = {
                    "sid": sid,
                    "name": e["name"],
                    "announce0": e["announce"],
                    "p_start": e["p_start"],
                    "p_end": e["p_end"],
                    "n_ann": 1,
                }
                continue
            gap = (
                datetime.fromisoformat(e["announce"])
                - datetime.fromisoformat(cur["p_end"])
            ).days
            if e["announce"] <= cur["p_end"] or gap <= 5:
                cur["n_ann"] += 1
                cur["p_end"] = max(cur["p_end"], e["p_end"])
            else:
                out.append(cur)
                cur = {
                    "sid": sid,
                    "name": e["name"],
                    "announce0": e["announce"],
                    "p_start": e["p_start"],
                    "p_end": e["p_end"],
                    "n_ann": 1,
                }
        if cur:
            out.append(cur)
    return out


def load_top30() -> list[dict[str, str]]:
    import csv

    rows = list(csv.DictReader(STAB.open(encoding="utf-8")))
    rows.sort(
        key=lambda r: (
            -int(r["n_epochs_in_top30"]),
            float(r["avg_rank_when_in"]),
        )
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "tier": r.get("tier") or "",
            "n_epochs": int(r["n_epochs_in_top30"]),
        }
        for r in rows[:30]
    ]


def load_branches(universe: str, top_n: int) -> list[dict[str, str]]:
    """universe: top30 | whale100 | path to json list[{id,name,...}]."""
    if universe in ("top30", "30"):
        return load_top30()[:top_n]
    path = WHALE100 if universe in ("whale100", "100") else Path(universe)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "branches" in raw:
        raw = raw["branches"]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        bid = str(item["id"])
        if bid in seen:
            continue
        seen.add(bid)
        out.append(
            {
                "id": bid,
                "name": str(item.get("name") or bid),
                "tier": str(item.get("tier") or ""),
                "n_epochs": int(item.get("n_epochs") or 0),
            }
        )
        if len(out) >= top_n:
            break
    if not out:
        raise ValueError(f"empty branch universe: {path}")
    return out


def disposition_day_set(episodes: list[dict]) -> dict[str, set[str]]:
    """sid -> set of calendar dates covered by any episode [p_start,p_end]."""
    out: dict[str, set[str]] = defaultdict(set)
    for ep in episodes:
        # store range markers; membership checked vs calendar later
        out[ep["sid"]].add(f"{ep['p_start']}|{ep['p_end']}|{ep['announce0']}")
    return out


def date_in_ranges(day: str, ranges: set[str]) -> str | None:
    """Return announce0 of matching episode, else None."""
    for item in ranges:
        ps, pe, ann = item.split("|")
        if ps <= day <= pe:
            return ann
    return None


def l1h7_excess(
    stock_id: str,
    signal_date: str,
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark: dict[str, tuple[float, float]],
) -> dict | None:
    si = date_index.get(signal_date)
    if si is None:
        return None
    ei = si + 1
    xi = ei + HOLD - 1
    if xi >= len(calendar):
        return None
    ed, xd = calendar[ei], calendar[xi]
    er, xr = ohlc.get((stock_id, ed)), ohlc.get((stock_id, xd))
    be, bx = benchmark.get(ed), benchmark.get(xd)
    if not er or not xr or not be or not bx:
        return None
    stock_r = xr[1] / er[0] - 1.0 - COST
    bench_r = bx[1] / be[0] - 1.0
    adj = stock_r - BETA * bench_r
    return {
        "signal_date": signal_date,
        "stock_id": stock_id,
        "entry_date": ed,
        "exit_date": xd,
        "stock_pct": stock_r * 100.0,
        "bench_pct": bench_r * 100.0,
        "excess_pct": adj * 100.0,
    }


def choose_adaptive(
    events: list[dict],
    n_lo: int,
    n_hi: int,
    abs_only: bool = False,
    *,
    abs_grid: tuple[float, ...] | None = None,
    target_n: int | None = None,
) -> tuple[float, float, list[dict]] | None:
    """Pick abs∩share rule.

    If target_n is set (balanced mode):
      1) prefer n in [n_lo, n_hi]
      2) among those, maximize median excess (then mean, n)
      3) else minimize |n - target_n|, then maximize median
    Else (legacy): minimize distance to [n_lo,n_hi], then max median.
    """
    best = None
    best_key = None
    grid = abs_grid if abs_grid is not None else ABS_GRID
    share_grid = (0.0,) if abs_only else SHARE_GRID
    mid = target_n if target_n is not None else (n_lo + n_hi) // 2
    for abs_floor in grid:
        for share_floor in share_grid:
            legs = [
                e
                for e in events
                if e["buy_amt"] >= abs_floor
                and (abs_only or e["buy_share"] >= share_floor)
            ]
            if not legs:
                continue
            xs = [e["excess_pct"] for e in legs]
            n = len(xs)
            in_band = n_lo <= n <= n_hi
            if n < n_lo:
                dist = n_lo - n
            elif n > n_hi:
                dist = n - n_hi
            else:
                dist = 0
            if target_n is not None:
                # Prefer in-band strongly; then closeness to target; then edge
                key = (
                    1 if in_band else 0,
                    -abs(n - mid),
                    -dist,
                    median(xs),
                    mean(xs),
                    n,
                    abs_floor,
                    share_floor,
                )
            else:
                key = (
                    -dist,
                    median(xs),
                    mean(xs),
                    n,
                    abs_floor,
                    share_floor,
                )
            if best_key is None or key > best_key:
                best_key = key
                best = (abs_floor, share_floor, legs)
    return best


def episode_dedupe(legs: list[dict], cal_days: int = 5) -> list[dict]:
    """Keep first signal per stock within cal_days (by signal_date)."""
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for leg in sorted(legs, key=lambda x: (x["stock_id"], x["signal_date"])):
        by_sid[leg["stock_id"]].append(leg)
    out: list[dict] = []
    for sid, lst in by_sid.items():
        last = None
        for leg in lst:
            if last is None:
                out.append(leg)
                last = leg
                continue
            gap = (
                datetime.fromisoformat(leg["signal_date"])
                - datetime.fromisoformat(last["signal_date"])
            ).days
            if gap > cal_days:
                out.append(leg)
                last = leg
    return out


def summarize(legs: list[dict]) -> dict:
    if not legs:
        return {"n": 0}
    xs = [x["excess_pct"] for x in legs]
    ss = [x["stock_pct"] for x in legs]
    return {
        "n": len(legs),
        "excess_med": round(median(xs), 3),
        "excess_mean": round(mean(xs), 3),
        "excess_win": round(100.0 * mean(1 if x > 0 else 0 for x in xs), 1),
        "stock_med": round(median(ss), 3),
        "stock_mean": round(mean(ss), 3),
        "n_stocks": len({x["stock_id"] for x in legs}),
    }


def load_disp_buys(
    conn,
    branch_ids: list[str],
    disp_sids: list[str],
    start: str,
    end: str,
) -> dict[str, list[dict]]:
    """All priced buys of disposition stocks by branches (not Top1-only)."""
    if not branch_ids or not disp_sids:
        return {}
    bq = ",".join("?" for _ in branch_ids)
    sq = ",".join("?" for _ in disp_sids)
    sql = f"""
        WITH priced AS (
          SELECT b.securities_trader_id AS branch_id,
                 b.trade_date,
                 b.stock_id,
                 b.buy AS buy_shares,
                 b.buy * p.close AS buy_amt
          FROM stock_broker_branch_daily b
          JOIN stock_daily_bars p
            ON p.stock_id=b.stock_id
           AND p.trade_date=b.trade_date
           AND p.source=?
          WHERE b.source=?
            AND b.securities_trader_id IN ({bq})
            AND b.trade_date BETWEEN ? AND ?
            AND b.buy>0 AND p.close>0
            AND length(b.stock_id)=4
            AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
        ),
        with_share AS (
          SELECT *,
                 SUM(buy_amt) OVER (
                   PARTITION BY branch_id, trade_date
                 ) AS day_buy_amt
          FROM priced
        )
        SELECT branch_id, trade_date, stock_id, buy_shares, buy_amt,
               buy_amt/day_buy_amt AS buy_share
        FROM with_share
        WHERE day_buy_amt>0
          AND stock_id IN ({sq})
        ORDER BY branch_id, trade_date, stock_id
    """
    params = (SOURCE, SOURCE, *branch_ids, start, end, *disp_sids)
    rows = mvp.query_with_retry(conn, sql, params)
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        out[str(row[0])].append(
            {
                "branch_id": str(row[0]),
                "trade_date": str(row[1]),
                "stock_id": str(row[2]),
                "buy_shares": float(row[3]),
                "buy_amt": float(row[4]),
                "buy_share": float(row[5]),
            }
        )
    return dict(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--n-lo", type=int, default=3)
    ap.add_argument("--n-hi", type=int, default=30)
    ap.add_argument("--dedupe-days", type=int, default=5)
    ap.add_argument("--abs-only", action="store_true", help="ignore share floor")
    ap.add_argument(
        "--balanced-n",
        action="store_true",
        help="equalize N across branches: prefer [n_lo,n_hi] near --target-n; use finer abs grid",
    )
    ap.add_argument("--target-n", type=int, default=10, help="balanced-n target size")
    ap.add_argument(
        "--universe",
        default="top30",
        help="top30 | whale100 | path to json",
    )
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--tag", default="", help="output filename tag")
    args = ap.parse_args()

    if args.balanced_n and args.n_lo == 3 and args.n_hi == 30:
        # Sensible default band when user asks for comparable N
        args.n_lo, args.n_hi = 8, 12
        log(f"balanced-n: default band → N∈[{args.n_lo},{args.n_hi}] target={args.target_n}")

    OUT.mkdir(parents=True, exist_ok=True)
    db = args.db
    if db is None:
        for p in (ROOT / "data/stocks.db", ROOT / "data/scratch/rolling_mvp_snapshot.db"):
            if p.exists():
                db = p
                break
    if db is None:
        raise FileNotFoundError("no stocks.db")

    branches = load_branches(args.universe, args.top_n)
    shard_branches = [
        b for i, b in enumerate(branches) if i % args.shards == args.shard
    ]
    log(
        f"universe={args.universe} top_n={len(branches)} "
        f"shard {args.shard}/{args.shards} branches={len(shard_branches)}"
    )

    disp_rows = fetch_twse_second_2026()
    episodes = merge_episodes(disp_rows)
    ranges = disposition_day_set(episodes)
    disp_sids = sorted(ranges.keys())
    log(f"2026 second-disp episodes={len(episodes)} sids={len(disp_sids)}")

    conn = mvp.connect_readonly(db)
    calendar = mvp.load_calendar(conn, START, END)
    # trim to latest with H7 room
    calendar = [d for d in calendar if d <= END]
    date_index = {d: i for i, d in enumerate(calendar)}
    benchmark = mvp.load_benchmark(conn, START, END)
    ohlc = mvp.load_stock_ohlc(conn, START, END)
    branch_ids = [b["id"] for b in shard_branches]
    t0 = time.time()
    disp_buys = load_disp_buys(conn, branch_ids, disp_sids, START, END)
    log(
        f"loaded disp buys in {time.time()-t0:.1f}s "
        f"rows={sum(len(v) for v in disp_buys.values())}"
    )

    results: list[dict] = []
    all_legs: list[dict] = []

    for b in shard_branches:
        bid, bname = b["id"], b["name"]
        cand: list[dict] = []
        for ev in disp_buys.get(bid, []):
            day = ev["trade_date"]
            ann = date_in_ranges(day, ranges[ev["stock_id"]])
            if ann is None:
                continue
            out = l1h7_excess(
                ev["stock_id"], day, calendar, date_index, ohlc, benchmark
            )
            if out is None:
                continue
            cand.append(
                {
                    **out,
                    "branch_id": bid,
                    "branch_name": bname,
                    "buy_amt": ev["buy_amt"],
                    "buy_share": ev["buy_share"],
                    "buy_shares": ev["buy_shares"],
                    "disp_announce0": ann,
                }
            )
        raw_n = len(cand)
        if raw_n == 0:
            results.append(
                {
                    "branch_id": bid,
                    "branch_name": bname,
                    "raw_n": 0,
                    "status": "no_disp_buy",
                }
            )
            continue
        # Dedupe before adaptive so N targets unique episodes
        cand_d = episode_dedupe(cand, args.dedupe_days)
        picked = choose_adaptive(
            cand_d,
            args.n_lo,
            args.n_hi,
            abs_only=args.abs_only,
            abs_grid=ABS_GRID_BALANCED if args.balanced_n else ABS_GRID,
            target_n=args.target_n if args.balanced_n else None,
        )
        if picked is None:
            results.append(
                {
                    "branch_id": bid,
                    "branch_name": bname,
                    "raw_n": raw_n,
                    "status": "no_rule",
                }
            )
            continue
        abs_f, share_f, legs = picked
        legs_d = legs  # already deduped upstream
        stats = summarize(legs_d)
        if args.abs_only:
            rule = f">={abs_f/1e8:g}億"
        else:
            rule = f">={abs_f/1e8:g}億∩{round(share_f*100):g}%"
        row = {
            "branch_id": bid,
            "branch_name": bname,
            "raw_n": raw_n,
            "status": "ok",
            "abs_floor": abs_f,
            "share_floor": share_f,
            "rule": rule,
            "n_before_dedupe": len(legs),
            **stats,
            "legs": legs_d,
        }
        results.append(row)
        all_legs.extend(legs_d)
        log(
            f"{bid} {bname} raw={raw_n} rule={row['rule']} "
            f"n={stats['n']} med={stats.get('excess_med')} "
            f"win={stats.get('excess_win')}"
        )

    tag = args.tag or (
        ("bal_abs" if args.abs_only else "bal_abssh")
        if args.balanced_n
        else ("abs" if args.abs_only else "abssh")
    )
    if args.universe in ("whale100", "100"):
        uni = "w100"
    elif args.universe in ("top30", "30"):
        uni = "t30"
    else:
        # custom json path → short stem tag (e.g. whale300_extra_branches → w300x)
        stem = Path(args.universe).stem.lower()
        uni = "w300x" if "300" in stem else stem[:12]
    shard_path = OUT / f"shard_{args.shard}_of_{args.shards}_{uni}_v3_{tag}.json"
    adapt = (
        f"balanced abs-only target_n={args.target_n} band=[{args.n_lo},{args.n_hi}]"
        if args.balanced_n and args.abs_only
        else (
            f"balanced abs∩share target_n={args.target_n} band=[{args.n_lo},{args.n_hi}]"
            if args.balanced_n
            else (
                f"abs-only prefer N∈[{args.n_lo},{args.n_hi}]"
                if args.abs_only
                else f"abs∩share(≥5%) prefer N∈[{args.n_lo},{args.n_hi}]"
            )
        )
    )
    payload = {
        "protocol": {
            "branches": f"{args.universe} top_n={len(branches)}",
            "event": "any day buy of stock inside 2026 第二次處置 [p_start,p_end]",
            "hold": "L1H7",
            "cost_bps": 30,
            "beta": BETA,
            "bench": "IX0001",
            "adaptive": adapt,
            "dedupe_cal_days": args.dedupe_days,
            "window": f"{START}..{END}",
            "version": f"v3_{uni}_{tag}",
            "balanced_n": bool(args.balanced_n),
            "target_n": args.target_n if args.balanced_n else None,
            "n_band": [args.n_lo, args.n_hi],
        },
        "shard": args.shard,
        "shards": args.shards,
        "n_episodes": len(episodes),
        "n_branches_universe": len(branches),
        "results": [
            {k: v for k, v in r.items() if k != "legs"} for r in results
        ],
        "legs": all_legs,
    }
    shard_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"wrote {shard_path}")


if __name__ == "__main__":
    main()
