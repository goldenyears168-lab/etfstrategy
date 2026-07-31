#!/usr/bin/env python3
"""1-2m fade_mom · universe-generalization check for Live TA dynamic universe.

Background
----------
`run_h3_residual_short_mom.py` established (REJECT for continuation, but
strong for **fade**): 1-2 completed-1m-bar raw momentum is *anti-predictive*
of fwd 5/10/15m direction on 7 names (2327,8046,3189,6451,2330,2303,2454).
Inverting the sign (fade) turns the ~35-40% continuation hit rate into a
~58-65% fade hit rate — see H3_RESIDUAL_SHORT_MOM.md, "fade short-mom ≈
58-65% if inverted".

But Live TA's actual displayed universe is **dynamic** (holdings ∪ watchlist
∪ extras). On 2026-07-28 it showed 8046,6505,6147,3653,3189,2492,2327 — of
which 6505 (台塑化), 6147 (頎邦), 3653 (健策), 2492 (華新科) were never
backtested.

This script
-----------
Runs the exact same fade_mom construction (raw 1-2 bar momentum sign,
inverted → predicted direction; forward 5/10/15m; flat_eps=3bp; IS<=2025-09-30
/ OOS>2025-09-30; same gate thresholds as run_h3_residual_short_mom.py) on
the union of the original 7-name research pool + the 4 untested Live-only
names, and reports each of the 4 new names' OOS hit-rate/n individually.

PIT: signal at bar i uses only bars <= i (own-stock closes only — no bench
needed, matches ops_live_ta.py's mom_meta_from_1m_bars, which is single
stock, not beta-adjusted).

Gate (identical to run_h3_residual_short_mom.py / task spec):
  OOS directed hit >= 55.0%, n >= 1000, >=70% of names with n>=80 have
  OOS hit >= 52.0%, max single-name share of pooled OOS n <= 40%.

Usage
-----
  PYTHONPATH=src .venv/bin/python scripts/research/study_1_2m_fade_universe_generalization.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.intraday_direction_thermometer import Bar  # noqa: E402

# Original 7-name research pool (already validated for fade_mom).
ORIGINAL_STOCKS = ("2327", "8046", "3189", "6451", "2330", "2303", "2454")
# 4 names shown live on 2026-07-28 that were never backtested.
NEW_STOCKS = ("6505", "6147", "3653", "2492")
ALL_STOCKS = ORIGINAL_STOCKS + NEW_STOCKS

DEFAULT_START = "2024-01-02"
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0003  # 3bp
MIN_BARS_PER_DAY = 120
MIN_OOS_N = 1000
MIN_NAME_N = 80
NAME_HIT_FLOOR = 52.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
HIT_GATE = 55.0
HORIZONS = (5, 10, 15)
MOM_BARS_OPTIONS = (1, 2)
SKIP_OPEN_BARS = 5
MOM_EPS_PCT = 0.02  # % points, matches SHORT_MOM_EPS_PCT


def _minute_hhmm(minute: str) -> str:
    parts = str(minute).split(":")
    return f"{parts[0]}:{parts[1]}"


def load_days_1m(
    conn: sqlite3.Connection, stock_id: str, *, start: str, end: str
) -> dict[str, list[Bar]]:
    rows = conn.execute(
        """
        SELECT trade_date, minute, open, high, low, close, volume
        FROM stock_kbar_1m
        WHERE stock_id = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, minute
        """,
        (stock_id, start, end),
    ).fetchall()
    by_day: dict[str, list[Bar]] = defaultdict(list)
    for td, minute, o, h, l, c, v in rows:
        if c is None:
            continue
        hhmm = _minute_hhmm(minute)
        hm = int(hhmm[:2]) * 60 + int(hhmm[3:5])
        if hm < 9 * 60 or hm > 13 * 60 + 24:
            continue
        ts = datetime.strptime(f"{td} {hhmm}", "%Y-%m-%d %H:%M")
        by_day[str(td)].append(
            Bar(
                ts=ts,
                open=float(o or c),
                high=float(h or c),
                low=float(l or c),
                close=float(c),
                volume=float(v or 0),
            )
        )
    return {d: bars for d, bars in by_day.items() if len(bars) >= MIN_BARS_PER_DAY}


def _sign_eps(x: float, eps: float) -> int:
    if x > eps:
        return 1
    if x < -eps:
        return -1
    return 0


@dataclass(frozen=True)
class Variant:
    name: str
    mom_bars: int
    fade: bool  # True = predicted dir is -sign(mom); False = continuation (baseline)


def _variants() -> tuple[Variant, ...]:
    out: list[Variant] = []
    for mb in MOM_BARS_OPTIONS:
        out.append(Variant(f"cont_mom_{mb}", mb, fade=False))
        out.append(Variant(f"fade_mom_{mb}", mb, fade=True))
    return tuple(out)


def eval_day(
    bars: list[Bar],
    *,
    mom_bars: int,
    horizons: Sequence[int],
    skip_open: int,
    mom_eps_pct: float = MOM_EPS_PCT,
) -> dict[int, list[dict[str, Any]]]:
    """PIT: signal at bar i from closes[i-mom_bars..i]; fwd from close[i]."""
    n = len(bars)
    out: dict[int, list[dict[str, Any]]] = {h: [] for h in horizons}
    if n < mom_bars + 2:
        return out
    closes = [b.close for b in bars]
    mb = int(mom_bars)
    max_h = max(horizons)
    i0 = max(skip_open, mb)
    for i in range(i0, n - max_h):
        c0, c1 = closes[i - mb], closes[i]
        if not c0:
            continue
        raw_pct = 100.0 * (c1 / c0 - 1.0)
        temp = _sign_eps(raw_pct, mom_eps_pct)
        if temp == 0:
            continue
        px0 = closes[i]
        if not px0:
            continue
        for h in horizons:
            fwd = closes[i + h] / px0 - 1.0
            out[h].append({"cont_temp": temp, "fwd": fwd})
    return out


def summarize(
    rows: list[dict[str, Any]], *, flat_eps: float, fade: bool
) -> dict[str, Any]:
    directed_hit = directed_tot = long_hit = 0
    signed_pnl: list[float] = []
    for r in rows:
        cont_t = int(r["cont_temp"])
        t = -cont_t if fade else cont_t
        fr = float(r["fwd"])
        if t == 0 or abs(fr) < flat_eps:
            continue
        directed_tot += 1
        if (t > 0 and fr > 0) or (t < 0 and fr < 0):
            directed_hit += 1
        if fr > 0:
            long_hit += 1
        signed_pnl.append(fr if t > 0 else -fr)

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    d_pct = pct(directed_hit, directed_tot)
    al = pct(long_hit, directed_tot)
    return {
        "n_signals": len(rows),
        "n_directed": directed_tot,
        "n_hit": directed_hit,
        "directed_hit_pct": d_pct,
        "always_long_pct": al,
        "expectancy_signed_pct": (
            round(100.0 * sum(signed_pnl) / len(signed_pnl), 4) if signed_pnl else None
        ),
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
    }


def stability_gates(
    pool: dict[str, Any], per_name: dict[str, dict[str, Any]], *, hit_gate: float
) -> dict[str, Any]:
    n = int(pool.get("n_directed") or 0)
    hit = pool.get("directed_hit_pct")
    name_hits = []
    shares = []
    for sid, sm in per_name.items():
        nn = int(sm.get("n_directed") or 0)
        hh = sm.get("directed_hit_pct")
        if nn >= MIN_NAME_N and hh is not None:
            name_hits.append((sid, hh, nn))
        if n > 0 and nn > 0:
            shares.append((sid, nn / n))
    pass_names = [x for x in name_hits if x[1] >= NAME_HIT_FLOOR]
    frac = (len(pass_names) / len(name_hits)) if name_hits else 0.0
    max_share = max((s for _, s in shares), default=0.0)
    max_sid = max(shares, key=lambda x: x[1])[0] if shares else None
    gates = {
        "oos_n_ge": n >= MIN_OOS_N,
        "oos_hit_ge": hit is not None and hit >= hit_gate,
        "name_floor_frac": round(frac, 3),
        "name_floor_pass": frac >= NAME_PASS_FRAC and len(name_hits) >= 3,
        "max_name_share": round(max_share, 3),
        "max_name_share_ok": max_share <= MAX_NAME_SHARE,
        "max_name": max_sid,
        "per_name": [
            {"stock_id": s, "hit": h, "n": nn} for s, h, nn in sorted(name_hits)
        ],
        "names_below_min_n": sorted(
            sid for sid, sm in per_name.items() if int(sm.get("n_directed") or 0) < MIN_NAME_N
        ),
    }
    gates["all_gates_pass"] = bool(
        gates["oos_n_ge"]
        and gates["oos_hit_ge"]
        and gates["name_floor_pass"]
        and gates["max_name_share_ok"]
    )
    return gates


def run(
    conn: sqlite3.Connection,
    *,
    stocks: Sequence[str],
    start: str,
    end: str | None,
    is_end: str,
) -> dict[str, Any]:
    end_s = end or "2099-12-31"
    variants = _variants()
    pool: dict[str, dict[str, dict[int, list[dict[str, Any]]]]] = {
        v.name: {"IS": {h: [] for h in HORIZONS}, "OOS": {h: [] for h in HORIZONS}}
        for v in variants
    }
    per_stock: dict[str, dict[str, dict[str, dict[int, list[dict[str, Any]]]]]] = {}
    day_counts: dict[str, int] = {}

    for sid in stocks:
        print(f"stock {sid} loading …", flush=True)
        days = load_days_1m(conn, sid, start=start, end=end_s)
        day_counts[sid] = len(days)
        per_stock[sid] = {
            v.name: {"IS": {h: [] for h in HORIZONS}, "OOS": {h: [] for h in HORIZONS}}
            for v in variants
        }
        for td, bars in sorted(days.items()):
            if len(bars) < MIN_BARS_PER_DAY:
                continue
            split = "IS" if td <= is_end else "OOS"
            for mb in MOM_BARS_OPTIONS:
                day_pack = eval_day(
                    bars, mom_bars=mb, horizons=HORIZONS, skip_open=SKIP_OPEN_BARS
                )
                for v in variants:
                    if v.mom_bars != mb:
                        continue
                    for h in HORIZONS:
                        rows = day_pack[h]
                        pool[v.name][split][h].extend(rows)
                        per_stock[sid][v.name][split][h].extend(rows)
        print(f"  {sid}: days={len(days)}", flush=True)

    leaderboard: list[dict[str, Any]] = []
    for v in variants:
        for h in HORIZONS:
            is_sum = summarize(pool[v.name]["IS"][h], flat_eps=FLAT_EPS, fade=v.fade)
            oos_sum = summarize(pool[v.name]["OOS"][h], flat_eps=FLAT_EPS, fade=v.fade)
            name_oos = {
                sid: summarize(
                    per_stock[sid][v.name]["OOS"][h], flat_eps=FLAT_EPS, fade=v.fade
                )
                for sid in stocks
            }
            gates = stability_gates(oos_sum, name_oos, hit_gate=HIT_GATE)
            leaderboard.append(
                {
                    "name": v.name,
                    "mom_bars": v.mom_bars,
                    "fade": v.fade,
                    "horizon_m": h,
                    "IS": is_sum,
                    "OOS": oos_sum,
                    "gates": gates,
                    "per_name_oos": name_oos,
                }
            )

    return {
        "stocks": list(stocks),
        "day_counts": day_counts,
        "start": start,
        "is_end": is_end,
        "leaderboard": leaderboard,
    }


def main() -> None:
    db_path = ROOT / "data" / "stocks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        payload = run(
            conn,
            stocks=ALL_STOCKS,
            start=DEFAULT_START,
            end=None,
            is_end=DEFAULT_IS_END,
        )
    finally:
        conn.close()

    out_dir = ROOT / "reports" / "research" / "intraday_direction_thermometer"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "fade_mom_1_2m_universe_generalization.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nwrote {json_path}")

    print("\n=== day counts (1m bar coverage) ===")
    for sid, n in payload["day_counts"].items():
        tag = " [NEW]" if sid in NEW_STOCKS else ""
        print(f"  {sid}{tag}: {n} trading days")

    print("\n=== fade_mom leaderboard (OOS, pooled 11 names) ===")
    print(f"{'variant':<14}{'H':>4}{'OOS hit%':>10}{'OOS n':>10}{'IS hit%':>10}  gates")
    for r in payload["leaderboard"]:
        if not r["fade"]:
            continue
        o, i, g = r["OOS"], r["IS"], r["gates"]
        status = "PASS" if g["all_gates_pass"] else "fail"
        print(
            f"{r['name']:<14}{r['horizon_m']:>4}{o.get('directed_hit_pct'):>10}"
            f"{o.get('n_directed'):>10}{i.get('directed_hit_pct'):>10}  {status}"
        )

    print("\n=== NEW stocks (6505/6147/3653/2492) individual OOS fade_mom hit rates ===")
    for r in payload["leaderboard"]:
        if not r["fade"]:
            continue
        print(f"\n-- {r['name']} H={r['horizon_m']}m --")
        for sid in NEW_STOCKS:
            sm = r["per_name_oos"].get(sid, {})
            print(
                f"  {sid}: hit={sm.get('directed_hit_pct')}% n={sm.get('n_directed')}"
            )


if __name__ == "__main__":
    main()
