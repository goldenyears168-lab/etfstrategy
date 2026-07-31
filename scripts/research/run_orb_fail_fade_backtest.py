#!/usr/bin/env python3
"""ORB (opening-range breakout) failed-breakout fade · ~30m directional bias.

Research-role backtest of ``orb_fail_fade`` per the literature-review spec
(Crabel stretch/retrace + tradingpedia "Trading Failed Breakouts at Session
Opens"). Reuses the *exact* IS/OOS + gate framework already established for
``ta_30m_bias`` (see ``run_ta_30m_bias_backtest.py``) — same metric
(directed hit on sign(temp)==sign(fwd_30m)), same PIT discipline, same
gates. Does not import that script (keeps this file self-contained /
read-only) but mirrors its ``load_days`` / ``eval_day`` / ``summarize`` /
``stability_gates`` logic byte-for-byte in spirit so results are comparable.

Signal: ``orb_fail_fade_from_bars`` — defined locally here (mirrors the
style of ``fade_near_ext_from_bars`` in
``src/research/intraday_direction_thermometer.py``, reuses its ``Bar``
dataclass).

Gates (30m-bias standard, same as ta_30m_bias)
-----------------------------------------------
1. Pool OOS directed hit ≥ 70% and n_directed ≥ 500
2. ≥70% of names with OOS hit ≥ 55% and n ≥ 50 each
3. No single name > 40% of pool OOS n

IS ≤ 2025-09-30 / OOS > 2025-09-30. Champion selected on IS only (n≥200),
then OOS gates evaluated on the frozen IS champion — never picked post-hoc
from OOS.

Universe
--------
- Primary (gate claim): the project's 8-stock ta_30m_bias universe
  (2327,8046,3189,6451,2330,2303,2454,0050).
- Secondary (exploratory, reported honestly, NOT used for gate claim):
  the site's currently-live extra names not in that universe
  (6505,6147,3653,2492) — reported separately since they were never
  validated by the original research.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_orb_fail_fade_backtest.py
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.intraday_direction_thermometer import Bar  # noqa: E402

DB_PATH = ROOT / "data" / "stocks.db"

PRIMARY_STOCKS = ("2327", "8046", "3189", "6451", "2330", "2303", "2454", "0050")
EXTRA_STOCKS = ("6505", "6147", "3653", "2492")  # site-live, unvalidated
IS_END = "2025-09-30"
START = "2024-01-02"
HORIZON = 6  # ~30m @ 5m bars, same as ta_30m_bias
FLAT_EPS = 0.0005  # 0.05%, same as ta_30m_bias
MIN_BARS_PER_DAY = 30
N_OR = 6  # 09:00-09:30 opening range (primary per lit review)

MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
OOS_HIT_GATE = 70.0

OR_LOOKBACK_DAYS = 20  # for narrow-OR percentile filter (PIT: past days only)


# --------------------------------------------------------------------------
# Signal: orb_fail_fade_from_bars
# --------------------------------------------------------------------------


def orb_fail_fade_from_bars(
    prev: Sequence[Bar],
    *,
    n_or: int = N_OR,
    w: int = 2,
    r: float = 0.0,
    narrow_filter: bool = False,
    or_range_percentile: float | None = None,
    require_vol_light: bool = False,
    vol_ratio_thresh: float = 1.2,
) -> int:
    """PIT ORB failed-breakout fade bias from completed same-day 5m bars.

    ``prev`` = bars up to and including "now" (the decision bar), same
    session, already-completed only. Returns bias in {-1,0,+1}.

    Step 1: OR = first ``n_or`` bars' high/low.
    Step 2: breakout confirmed by *close* beyond OR (not wick).
    Step 3: fade fires if a breakout occurred within the last ``w`` bars
      (t-k, k=1..w) and the *current* close has moved back inside the OR.
    Step 4: optional retrace-depth filter ``r`` (Crabel-style 50% stretch):
      require the reversal from the post-breakout extreme to have retraced
      >= r of the distance from OR edge to that extreme.
    Step 5: optional narrow-OR filter — only fire when today's OR range is
      in the bottom half of the trailing ``OR_LOOKBACK_DAYS`` (passed in by
      caller, PIT-safe: computed from strictly prior days).
    Step 6: optional thin-breakout-volume filter — only fire when the
      triggering breakout bar's volume is < ``vol_ratio_thresh`` x the
      average volume of the OR bars.
    """
    n = len(prev)
    if n <= n_or:
        return 0
    if narrow_filter:
        if or_range_percentile is None or or_range_percentile > 0.5:
            return 0

    or_bars = prev[:n_or]
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)
    if or_high <= or_low:
        return 0
    or_vol_avg = (
        sum(float(b.volume or 0.0) for b in or_bars) / len(or_bars)
        if or_bars
        else 0.0
    )

    cur_idx = n - 1
    current = prev[cur_idx]
    win_start = max(n_or, cur_idx - w)
    candidates = list(range(win_start, cur_idx))  # bars strictly before "now"
    if not candidates:
        return 0

    # --- up-breakout -> fade short ---
    up_trigger_idx = None
    for idx in candidates:
        if prev[idx].close > or_high:
            up_trigger_idx = idx
            break
    fade_up = up_trigger_idx is not None and current.close < or_high

    # --- down-breakout -> fade long ---
    dn_trigger_idx = None
    for idx in candidates:
        if prev[idx].close < or_low:
            dn_trigger_idx = idx
            break
    fade_dn = dn_trigger_idx is not None and current.close > or_low

    if fade_up and fade_dn:
        return 0  # ambiguous within same short window; skip

    if fade_up:
        seg = prev[up_trigger_idx : cur_idx + 1]
        ext_high = max(b.high for b in seg)
        denom = ext_high - or_high
        if denom <= 0:
            return 0
        retrace_pct = (ext_high - current.close) / denom
        if r > 0 and retrace_pct < r:
            return 0
        if require_vol_light:
            trig_vol = float(prev[up_trigger_idx].volume or 0.0)
            if or_vol_avg <= 0 or trig_vol / or_vol_avg >= vol_ratio_thresh:
                return 0
        return -1

    if fade_dn:
        seg = prev[dn_trigger_idx : cur_idx + 1]
        ext_low = min(b.low for b in seg)
        denom = or_low - ext_low
        if denom <= 0:
            return 0
        retrace_pct = (current.close - ext_low) / denom
        if r > 0 and retrace_pct < r:
            return 0
        if require_vol_light:
            trig_vol = float(prev[dn_trigger_idx].volume or 0.0)
            if or_vol_avg <= 0 or trig_vol / or_vol_avg >= vol_ratio_thresh:
                return 0
        return 1

    return 0


# --------------------------------------------------------------------------
# Variant grid (pre-registered before any OOS look)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    name: str
    w: int
    r: float
    narrow_filter: bool
    require_vol_light: bool


def build_variants() -> tuple[Variant, ...]:
    variants: list[Variant] = []
    for w, r, narrow, vol in itertools.product(
        (1, 2, 3, 4), (0.0, 0.3, 0.5), (False, True), (False, True)
    ):
        name = f"w{w}_r{r:.1f}_narrow{int(narrow)}_vol{int(vol)}"
        variants.append(
            Variant(name=name, w=w, r=r, narrow_filter=narrow, require_vol_light=vol)
        )
    return tuple(variants)


VARIANTS: tuple[Variant, ...] = build_variants()


# --------------------------------------------------------------------------
# Data loading (mirrors run_ta_30m_bias_backtest.py load_days)
# --------------------------------------------------------------------------


def _minute_hhmm(minute: str) -> str:
    parts = str(minute).split(":")
    return f"{parts[0]}:{parts[1]}"


def load_days(
    conn: sqlite3.Connection, stock_id: str, *, start: str, end: str
) -> dict[str, list[Bar]]:
    rows = conn.execute(
        """
        SELECT trade_date, minute, open, high, low, close, volume
        FROM stock_kbar_5m
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
        if hm < 9 * 60 or hm > 13 * 60 + 25:
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


def or_range_of_day(bars: list[Bar], n_or: int = N_OR) -> float | None:
    if len(bars) <= n_or:
        return None
    or_bars = bars[:n_or]
    hi = max(b.high for b in or_bars)
    lo = min(b.low for b in or_bars)
    if hi <= lo:
        return None
    return hi - lo


def compute_narrow_percentiles(
    sorted_days: list[tuple[str, list[Bar]]], *, lookback: int = OR_LOOKBACK_DAYS
) -> dict[str, float | None]:
    """PIT-safe: percentile of today's OR range vs trailing ``lookback`` days
    (strictly prior days only, expanding then rolling window)."""
    hist: deque[float] = deque(maxlen=lookback)
    out: dict[str, float | None] = {}
    for td, bars in sorted_days:
        rng = or_range_of_day(bars)
        if rng is None or len(hist) < 5:
            out[td] = None
        else:
            n_le = sum(1 for x in hist if x <= rng)
            out[td] = n_le / len(hist)
        if rng is not None:
            hist.append(rng)
    return out


# --------------------------------------------------------------------------
# Eval (mirrors run_ta_30m_bias_backtest.py eval_day / summarize)
# --------------------------------------------------------------------------


def eval_day(
    bars: list[Bar],
    variant: Variant,
    *,
    horizon: int,
    or_pctile: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(bars)):
        if i + horizon >= len(bars):
            break
        prev = bars[: i + 1]
        temp = orb_fail_fade_from_bars(
            prev,
            n_or=N_OR,
            w=variant.w,
            r=variant.r,
            narrow_filter=variant.narrow_filter,
            or_range_percentile=or_pctile,
            require_vol_light=variant.require_vol_light,
        )
        if temp == 0:
            continue
        c0 = bars[i].close
        if not c0:
            continue
        fwd = bars[i + horizon].close / c0 - 1.0
        out.append({"temp": int(temp), "fwd": fwd, "hhmm": bars[i].ts.strftime("%H:%M")})
    return out


def summarize(rows: list[dict[str, Any]], *, flat_eps: float) -> dict[str, Any]:
    directed_hit = directed_tot = 0
    long_hit = 0
    signed_pnl: list[float] = []
    for r in rows:
        t = int(r["temp"])
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


def run_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    start: str,
    end: str,
    is_end: str,
    variants: Sequence[Variant],
    horizon: int,
    flat_eps: float,
) -> dict[str, Any]:
    days = load_days(conn, stock_id, start=start, end=end)
    if not days:
        return {"stock_id": stock_id, "error": "no_5m", "n_days": 0}
    sorted_days = sorted(days.items())
    pctile_by_day = compute_narrow_percentiles(sorted_days)

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    for td, bars in sorted_days:
        split = "IS" if td <= is_end else "OOS"
        or_pctile = pctile_by_day.get(td)
        for v in variants:
            by_var[v.name][split].extend(
                eval_day(bars, v, horizon=horizon, or_pctile=or_pctile)
            )

    out_vars: dict[str, Any] = {}
    for v in variants:
        is_rows = by_var[v.name]["IS"]
        oos_rows = by_var[v.name]["OOS"]
        out_vars[v.name] = {
            "IS": summarize(is_rows, flat_eps=flat_eps),
            "OOS": summarize(oos_rows, flat_eps=flat_eps),
        }
    return {
        "stock_id": stock_id,
        "n_days": len(days),
        "date_min": min(days),
        "date_max": max(days),
        "variants": out_vars,
    }


def pool_from_stocks(
    stock_results: list[dict[str, Any]], variant: str, split: str
) -> dict[str, Any]:
    n_dir = hit = n_sig = 0
    per_name: list[dict[str, Any]] = []
    for s in stock_results:
        if s.get("error"):
            continue
        m = s["variants"][variant][split]
        nd = int(m["n_directed"] or 0)
        nh = int(m.get("n_hit") or 0)
        ns = int(m["n_signals"] or 0)
        n_sig += ns
        n_dir += nd
        hit += nh
        per_name.append(
            {
                "stock_id": s["stock_id"],
                "n_directed": nd,
                "directed_hit_pct": m["directed_hit_pct"],
                "expectancy_signed_pct": m["expectancy_signed_pct"],
            }
        )

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    d_pct = pct(hit, n_dir)
    return {
        "n_signals": n_sig,
        "n_directed": n_dir,
        "n_hit": hit,
        "directed_hit_pct": d_pct,
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
        "per_name": per_name,
    }


def stability_gates(pool_oos: dict[str, Any]) -> dict[str, Any]:
    names = [p for p in pool_oos.get("per_name") or [] if (p.get("n_directed") or 0) > 0]
    n_names = len(names)
    pass_names = [
        p
        for p in names
        if (p.get("n_directed") or 0) >= MIN_NAME_N
        and (p.get("directed_hit_pct") or 0) >= NAME_HIT_FLOOR
    ]
    frac = (len(pass_names) / n_names) if n_names else 0.0
    max_share = 0.0
    max_sid = None
    nd_pool = int(pool_oos.get("n_directed") or 0)
    if nd_pool:
        for p in names:
            share = (p.get("n_directed") or 0) / nd_pool
            if share > max_share:
                max_share = share
                max_sid = p["stock_id"]
    hit = pool_oos.get("directed_hit_pct")
    gate_hit = bool(hit is not None and hit >= OOS_HIT_GATE and nd_pool >= MIN_OOS_N)
    gate_stab = bool(n_names > 0 and frac >= NAME_PASS_FRAC)
    gate_dom = bool(max_share <= MAX_NAME_SHARE)
    return {
        "gate_oos_hit_ge70_n500": gate_hit,
        "gate_name_stability": gate_stab,
        "gate_no_megacap_dom": gate_dom,
        "all_gates_pass": gate_hit and gate_stab and gate_dom,
        "name_pass_frac": round(frac, 3),
        "n_names": n_names,
        "n_names_pass": len(pass_names),
        "max_name_share": round(max_share, 3),
        "max_name_sid": max_sid,
        "names_pass": [p["stock_id"] for p in pass_names],
        "names_fail": [
            {"stock_id": p["stock_id"], "hit": p.get("directed_hit_pct"), "n": p.get("n_directed")}
            for p in names
            if p["stock_id"] not in {x["stock_id"] for x in pass_names}
        ],
    }


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    end = conn.execute("SELECT MAX(trade_date) FROM stock_kbar_5m").fetchone()[0]

    print(
        f"orb_fail_fade backtest: {len(VARIANTS)} variants x "
        f"{len(PRIMARY_STOCKS)} primary + {len(EXTRA_STOCKS)} extra stocks, "
        f"{START}->{end}, IS<={IS_END}",
        flush=True,
    )

    all_results: dict[str, dict[str, Any]] = {}
    for sid in PRIMARY_STOCKS + EXTRA_STOCKS:
        print(f"  eval {sid} ...", flush=True)
        all_results[sid] = run_stock(
            conn,
            sid,
            start=START,
            end=end,
            is_end=IS_END,
            variants=VARIANTS,
            horizon=HORIZON,
            flat_eps=FLAT_EPS,
        )

    primary_results = [all_results[s] for s in PRIMARY_STOCKS]
    extra_results = [all_results[s] for s in EXTRA_STOCKS]

    # --- Primary universe: IS champion selection + OOS gate claim ---
    pool_rows: list[dict[str, Any]] = []
    for v in VARIANTS:
        is_m = pool_from_stocks(primary_results, v.name, "IS")
        oos_m = pool_from_stocks(primary_results, v.name, "OOS")
        pool_rows.append(
            {
                "variant": v.name,
                "IS": {k: is_m[k] for k in is_m if k != "per_name"},
                "OOS": {k: oos_m[k] for k in oos_m if k != "per_name"},
                "_is_full": is_m,
                "_oos_full": oos_m,
            }
        )

    selectable = [
        r
        for r in pool_rows
        if (r["IS"].get("n_directed") or 0) >= 200
        and r["IS"].get("directed_hit_pct") is not None
    ]
    selectable.sort(
        key=lambda r: (
            -(r["IS"]["directed_hit_pct"] or 0),
            -(r["IS"]["n_directed"] or 0),
        )
    )
    if not selectable:
        print("NO IS-selectable variant (all n_directed<200 on IS). Aborting claim.")
        champ_row = pool_rows[0]
    else:
        champ_row = selectable[0]

    champ = {
        "variant": champ_row["variant"],
        "IS": champ_row["_is_full"],
        "OOS": champ_row["_oos_full"],
    }
    gates = stability_gates(champ["OOS"])

    best_oos = max(
        pool_rows,
        key=lambda r: (
            r["OOS"].get("directed_hit_pct") or 0,
            r["OOS"].get("n_directed") or 0,
        ),
    )

    leaderboard = sorted(
        pool_rows,
        key=lambda x: (
            -(x["OOS"].get("directed_hit_pct") or 0),
            -(x["OOS"].get("n_directed") or 0),
        ),
    )

    # --- Extra (site-live, unvalidated) universe: apply the SAME frozen
    # IS-champion variant (no re-selection) and report honestly. ---
    extra_is = pool_from_stocks(extra_results, champ["variant"], "IS")
    extra_oos = pool_from_stocks(extra_results, champ["variant"], "OOS")
    extra_gates = stability_gates(extra_oos)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layer": "research",
        "status": "observe_only",
        "metric": {
            "name": "directed_hit_rate_orb_fail_fade",
            "horizon": HORIZON,
            "flat_eps": FLAT_EPS,
            "is_end": IS_END,
            "start": START,
            "end": end,
            "n_or": N_OR,
            "primary_stocks": list(PRIMARY_STOCKS),
            "extra_stocks_site_live_unvalidated": list(EXTRA_STOCKS),
            "n_variants": len(VARIANTS),
        },
        "is_champion": {
            "variant": champ["variant"],
            "IS": {k: champ["IS"][k] for k in champ["IS"] if k != "per_name"},
            "OOS": champ["OOS"],
        },
        "gates_primary_universe": gates,
        "best_oos_exploratory_not_claim": {
            "variant": best_oos["variant"],
            "OOS": best_oos["OOS"],
            "IS": best_oos["IS"],
        },
        "extra_universe_same_frozen_variant": {
            "variant": champ["variant"],
            "IS": {k: extra_is[k] for k in extra_is if k != "per_name"},
            "OOS": extra_oos,
            "gates": extra_gates,
            "note": "2492 has only ~15 rows total in stock_kbar_5m (effectively no history); expect near-empty result for that name.",
        },
        "pool_leaderboard_top20_by_oos": [
            {
                "variant": r["variant"],
                "IS": r["IS"],
                "OOS": r["OOS"],
            }
            for r in leaderboard[:20]
        ],
    }

    out_json = ROOT / "reports" / "research" / "orb_fail_fade" / "orb_fail_fade_backtest.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("")
    print("=" * 70)
    print(f"IS champion variant: {champ['variant']}")
    print(f"  IS  hit={champ['IS'].get('directed_hit_pct')}% n={champ['IS'].get('n_directed')}")
    print(f"  OOS hit={champ['OOS'].get('directed_hit_pct')}% n={champ['OOS'].get('n_directed')}")
    print(f"Gates (primary 8-stock universe):")
    print(f"  gate_oos_hit_ge70_n500 : {gates['gate_oos_hit_ge70_n500']}")
    print(f"  gate_name_stability    : {gates['gate_name_stability']} "
          f"({gates['n_names_pass']}/{gates['n_names']}={gates['name_pass_frac']})")
    print(f"  gate_no_megacap_dom    : {gates['gate_no_megacap_dom']} "
          f"(max_share={gates['max_name_share']} sid={gates['max_name_sid']})")
    print(f"  ALL_GATES_PASS         : {gates['all_gates_pass']}")
    print("")
    print(f"Best OOS in grid (exploratory only, NOT the claim): "
          f"{best_oos['variant']} = {best_oos['OOS'].get('directed_hit_pct')}% "
          f"(n={best_oos['OOS'].get('n_directed')})")
    print("")
    print(f"Extra (site-live, unvalidated) universe on SAME frozen champion variant:")
    print(f"  IS  hit={extra_is.get('directed_hit_pct')}% n={extra_is.get('n_directed')}")
    print(f"  OOS hit={extra_oos.get('directed_hit_pct')}% n={extra_oos.get('n_directed')}")
    print(f"  per-name OOS: {extra_oos.get('per_name')}")
    print("=" * 70)
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
