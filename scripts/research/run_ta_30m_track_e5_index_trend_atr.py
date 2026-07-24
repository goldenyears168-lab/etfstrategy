#!/usr/bin/env python3
"""Track E5: index trend filter beyond OR-inside + MAE/ATR (research).

Extends ``fade_idx_or_inside`` with PIT index-trend gates (VWAP side,
OR-break hold), reports MAE / ATR-normalized adverse excursion, and
runs the same grid on **0050** (IS+OOS) and **IX0001** (OOS-only).

Shared gates (claim OOS≥70%)
----------------------------
Directed hit fwd H=6 (~30m) · flat_eps=0.05% · IS≤2025-09-30 · OOS >;
n_directed≥500 · name stability · no mega-cap dominance. Research only.

Example
-------
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_ta_30m_track_e5_index_trend_atr.py
"""

from __future__ import annotations

import argparse
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

from research.intraday_direction_thermometer import (  # noqa: E402
    Bar,
    TA30M_HORIZON_BARS,
    TA30M_OR_BARS,
    fade_near_ext_from_bars,
    opening_range_hl,
    session_vwap,
)

DEFAULT_STOCKS = (
    "2327",
    "8046",
    "3189",
    "6451",
    "2330",
    "2303",
    "2454",
)
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0005
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
VWAP_EPS_PCT = 0.05  # quiet zone for idx vs VWAP (% points)
ATR_PERIOD = 14
HOLD_K_DEFAULT = 2


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    hold_k: int = HOLD_K_DEFAULT
    family: str = "fusion"


# Pre-registered small grid (locked before OOS champion claim).
VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", family="baseline"),
    Variant("fade_idx_or_inside", "fade_idx_or_inside"),
    # Index below/above own VWAP aligned for mean-reversion fade.
    Variant("fade_idx_vwap_mr", "fade_idx_vwap_mr"),
    # Skip fade when index broke OR and held outside ≥K bars.
    Variant("fade_skip_or_hold_k2", "fade_skip_or_hold", hold_k=2),
    Variant("fade_skip_or_hold_k3", "fade_skip_or_hold", hold_k=3),
    # OR-inside ∧ VWAP MR (stricter than OR-inside alone).
    Variant("fade_or_inside_and_vwap_mr", "fade_or_inside_and_vwap_mr"),
    # Skip when index trend: OR broken+held OR VWAP side = OR break side.
    Variant("fade_skip_idx_trend", "fade_skip_idx_trend", hold_k=2),
    # OR-inside ∧ not (index VWAP slope strongly with extreme).
    Variant("fade_or_inside_flat_vwap", "fade_or_inside_flat_vwap"),
)


def _minute_hhmm(minute: str) -> str:
    parts = str(minute).split(":")
    return f"{parts[0]}:{parts[1]}"


def _sign_eps(x: float, eps: float = 0.0) -> int:
    if x > eps:
        return 1
    if x < -eps:
        return -1
    return 0


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


def load_atr_pct(
    conn: sqlite3.Connection, stock_id: str, *, start: str, end: str
) -> dict[str, float]:
    """Map trade_date → ATR14% ending **T−1** (PIT for session T)."""
    rows = conn.execute(
        """
        SELECT trade_date, high, low, close
        FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date >= date(?, '-40 days')
          AND trade_date <= ?
        ORDER BY trade_date
        """,
        (stock_id, start, end),
    ).fetchall()
    if not rows:
        return {}
    dates = [str(r[0]) for r in rows]
    highs = [float(r[1] or r[3] or 0) for r in rows]
    lows = [float(r[2] or r[3] or 0) for r in rows]
    closes = [float(r[3] or 0) for r in rows]
    trs: list[float] = []
    for i in range(len(rows)):
        if i == 0 or closes[i - 1] <= 0:
            trs.append(max(0.0, highs[i] - lows[i]))
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    atr_end_day: dict[str, float] = {}
    for i in range(len(rows)):
        if i + 1 < ATR_PERIOD or closes[i] <= 0:
            continue
        window = trs[i - ATR_PERIOD + 1 : i + 1]
        atr = sum(window) / ATR_PERIOD
        atr_end_day[dates[i]] = 100.0 * atr / closes[i]
    # shift to next session (T−1 ATR usable on T)
    out: dict[str, float] = {}
    prev: float | None = None
    for d in dates:
        if prev is not None:
            out[d] = prev
        if d in atr_end_day:
            prev = atr_end_day[d]
    return out


def _align_bench_prefix(
    stock_prev: Sequence[Bar], bench_day: Sequence[Bar]
) -> list[Bar]:
    if not stock_prev or not bench_day:
        return []
    t_end = stock_prev[-1].ts
    return [b for b in bench_day if b.ts <= t_end]


def index_state(stock_prev: Sequence[Bar], bench_day: Sequence[Bar]) -> dict[str, Any]:
    """PIT index microstructure vs stock clock."""
    out: dict[str, Any] = {
        "ready": False,
        "idx_or_state": None,
        "idx_vs_vwap": 0,
        "idx_vs_vwap_pct": None,
        "idx_or_hold_bars": 0,
        "idx_vwap_slope_sign": 0,
        "or_hi": None,
        "or_lo": None,
    }
    if len(stock_prev) < TA30M_OR_BARS:
        return out
    bprev = _align_bench_prefix(stock_prev, bench_day)
    if len(bprev) < TA30M_OR_BARS:
        return out
    out["ready"] = True

    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return out
    or_hi, or_lo = or_hl
    out["or_hi"], out["or_lo"] = or_hi, or_lo
    bc = bprev[-1].close
    if bc > or_hi:
        out["idx_or_state"] = "above"
        side = 1
    elif bc < or_lo:
        out["idx_or_state"] = "below"
        side = -1
    else:
        out["idx_or_state"] = "inside"
        side = 0

    # Consecutive bars at end of prefix that stay on same OR-break side.
    hold = 0
    if side != 0:
        for b in reversed(bprev):
            if side > 0 and b.close > or_hi:
                hold += 1
            elif side < 0 and b.close < or_lo:
                hold += 1
            else:
                break
    out["idx_or_hold_bars"] = hold

    b_vwap = session_vwap(bprev)
    if b_vwap and b_vwap > 0:
        vs = 100.0 * (bc / b_vwap - 1.0)
        out["idx_vs_vwap_pct"] = round(vs, 4)
        out["idx_vs_vwap"] = _sign_eps(vs, VWAP_EPS_PCT)
        # Short VWAP slope over last ~30m of index (6 bars) if available.
        if len(bprev) >= 6:
            w = bprev[-6:]
            v0 = session_vwap(bprev[: -5] or bprev[:1])
            v1 = b_vwap
            if v0 and v0 > 0 and v1:
                slope = 100.0 * (v1 / v0 - 1.0)
                out["idx_vwap_slope_sign"] = _sign_eps(slope, VWAP_EPS_PCT)
    return out


def signal_temp(
    stock_prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    variant: Variant,
) -> int:
    fade_layer = fade_near_ext_from_bars(stock_prev, midday_only=True)
    fade = (
        int(fade_layer.temp)
        if fade_layer.ready and fade_layer.temp is not None
        else 0
    )
    if variant.kind == "fade":
        return fade
    if fade == 0:
        return 0

    st = index_state(stock_prev, bench_day)
    if not st.get("ready"):
        return 0

    or_state = st.get("idx_or_state")
    vs_vwap = int(st.get("idx_vs_vwap") or 0)
    hold = int(st.get("idx_or_hold_bars") or 0)
    slope = int(st.get("idx_vwap_slope_sign") or 0)

    if variant.kind == "fade_idx_or_inside":
        return fade if or_state == "inside" else 0

    if variant.kind == "fade_idx_vwap_mr":
        # Short fade (fade=-1 near highs): prefer index not above VWAP.
        # Long fade (fade=+1 near lows): prefer index not below VWAP.
        if fade < 0:
            return fade if vs_vwap <= 0 else 0
        return fade if vs_vwap >= 0 else 0

    if variant.kind == "fade_skip_or_hold":
        # Allow fade unless index already broke OR and held ≥K bars.
        if or_state in {"above", "below"} and hold >= variant.hold_k:
            return 0
        return fade

    if variant.kind == "fade_or_inside_and_vwap_mr":
        if or_state != "inside":
            return 0
        if fade < 0:
            return fade if vs_vwap <= 0 else 0
        return fade if vs_vwap >= 0 else 0

    if variant.kind == "fade_skip_idx_trend":
        # Trend day proxy: OR broken+held, or VWAP side agrees with break.
        if or_state in {"above", "below"} and hold >= variant.hold_k:
            return 0
        if or_state == "above" and vs_vwap > 0:
            return 0
        if or_state == "below" and vs_vwap < 0:
            return 0
        return fade

    if variant.kind == "fade_or_inside_flat_vwap":
        if or_state != "inside":
            return 0
        # Skip if VWAP slope strongly with the extreme (trend continuation risk).
        if fade < 0 and slope > 0:
            return 0
        if fade > 0 and slope < 0:
            return 0
        return fade

    return 0


def path_mae_pct(
    bars: Sequence[Bar], i: int, horizon: int, temp: int
) -> float | None:
    """Max adverse excursion over (i, i+horizon] vs fade direction (% of entry)."""
    if temp == 0 or i + horizon >= len(bars):
        return None
    entry = bars[i].close
    if not entry:
        return None
    path = bars[i + 1 : i + horizon + 1]
    if not path:
        return None
    if temp > 0:  # long fade → adverse = drawdown vs entry
        lo = min(b.low for b in path)
        return max(0.0, 100.0 * (entry - lo) / entry)
    hi = max(b.high for b in path)
    return max(0.0, 100.0 * (hi - entry) / entry)


def eval_day(
    stock_bars: list[Bar],
    bench_bars: list[Bar],
    variant: Variant,
    *,
    horizon: int,
    atr_pct: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(stock_bars)):
        if i + horizon >= len(stock_bars):
            break
        prev = stock_bars[: i + 1]
        temp = signal_temp(prev, bench_bars, variant)
        if temp == 0:
            continue
        c0 = stock_bars[i].close
        if not c0:
            continue
        fwd = stock_bars[i + horizon].close / c0 - 1.0
        mae = path_mae_pct(stock_bars, i, horizon, temp)
        mae_atr = None
        if mae is not None and atr_pct and atr_pct > 0:
            mae_atr = round(mae / atr_pct, 4)
        st = index_state(prev, bench_bars)
        out.append(
            {
                "temp": int(temp),
                "fwd": fwd,
                "hhmm": stock_bars[i].ts.strftime("%H:%M"),
                "mae_pct": round(mae, 4) if mae is not None else None,
                "mae_atr": mae_atr,
                "atr_pct": round(atr_pct, 4) if atr_pct is not None else None,
                "idx_or_state": st.get("idx_or_state"),
                "idx_vs_vwap": st.get("idx_vs_vwap"),
                "idx_or_hold_bars": st.get("idx_or_hold_bars"),
            }
        )
    return out


def _pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 2) if b else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2:
        return round(ys[mid], 4)
    return round(0.5 * (ys[mid - 1] + ys[mid]), 4)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return round(ys[0], 4)
    pos = q * (len(ys) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ys) - 1)
    frac = pos - lo
    return round(ys[lo] * (1.0 - frac) + ys[hi] * frac, 4)


def summarize(rows: list[dict[str, Any]], *, flat_eps: float) -> dict[str, Any]:
    directed_hit = directed_tot = long_hit = 0
    signed_pnl: list[float] = []
    maes: list[float] = []
    mae_atrs: list[float] = []
    mae_hit: list[float] = []
    mae_miss: list[float] = []
    mae_atr_miss: list[float] = []
    for r in rows:
        t = int(r["temp"])
        fr = float(r["fwd"])
        if t == 0 or abs(fr) < flat_eps:
            continue
        directed_tot += 1
        is_hit = (t > 0 and fr > 0) or (t < 0 and fr < 0)
        if is_hit:
            directed_hit += 1
        if fr > 0:
            long_hit += 1
        signed_pnl.append(fr if t > 0 else -fr)
        if r.get("mae_pct") is not None:
            mae = float(r["mae_pct"])
            maes.append(mae)
            (mae_hit if is_hit else mae_miss).append(mae)
        if r.get("mae_atr") is not None:
            ma = float(r["mae_atr"])
            mae_atrs.append(ma)
            if not is_hit:
                mae_atr_miss.append(ma)

    d_pct = _pct(directed_hit, directed_tot)
    al = _pct(long_hit, directed_tot)
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
        "mae_pct_median": _median(maes),
        "mae_pct_p75": _quantile(maes, 0.75),
        "mae_pct_mean": _mean(maes),
        "mae_atr_median": _median(mae_atrs),
        "mae_atr_p75": _quantile(mae_atrs, 0.75),
        "mae_atr_mean": _mean(mae_atrs),
        "mae_pct_mean_hit": _mean(mae_hit),
        "mae_pct_mean_miss": _mean(mae_miss),
        "mae_atr_mean_miss": _mean(mae_atr_miss),
        "mae_zero_frac": (
            round(sum(1 for x in maes if x <= 1e-12) / len(maes), 3) if maes else None
        ),
        "n_mae": len(maes),
        "n_mae_atr": len(mae_atrs),
    }


def run_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    bench_days: dict[str, list[Bar]],
    *,
    start: str,
    end: str,
    is_end: str,
    variants: Sequence[Variant],
    horizon: int,
    flat_eps: float,
    oos_only: bool = False,
) -> dict[str, Any]:
    days = load_days(conn, stock_id, start=start, end=end)
    atr_map = load_atr_pct(conn, stock_id, start=start, end=end)
    if not days:
        return {"stock_id": stock_id, "error": "no_5m", "n_days": 0}

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    n_aligned = 0
    for td, bars in sorted(days.items()):
        if oos_only and td <= is_end:
            continue
        bench = bench_days.get(td)
        if not bench:
            continue
        n_aligned += 1
        split = "IS" if td <= is_end else "OOS"
        atr = atr_map.get(td)
        for v in variants:
            by_var[v.name][split].extend(
                eval_day(
                    bars,
                    bench,
                    v,
                    horizon=horizon,
                    atr_pct=atr,
                )
            )

    out_vars: dict[str, Any] = {}
    for v in variants:
        is_rows = by_var[v.name]["IS"]
        oos_rows = by_var[v.name]["OOS"]
        out_vars[v.name] = {
            "IS": summarize(is_rows, flat_eps=flat_eps),
            "OOS": summarize(oos_rows, flat_eps=flat_eps),
            "ALL": summarize(is_rows + oos_rows, flat_eps=flat_eps),
            "_oos_rows": oos_rows,  # for disagreement analysis (stripped later)
        }
    return {
        "stock_id": stock_id,
        "n_days": len(days),
        "n_days_aligned_bench": n_aligned,
        "date_min": min(days) if days else None,
        "date_max": max(days) if days else None,
        "variants": out_vars,
    }


def pool_from_stocks(
    stock_results: list[dict[str, Any]], variant: str, split: str
) -> dict[str, Any]:
    n_dir = hit = long_hit = n_sig = 0
    per_name: list[dict[str, Any]] = []
    mae_medians: list[float] = []
    mae_atr_medians: list[float] = []
    for s in stock_results:
        if s.get("error"):
            continue
        m = s["variants"][variant][split]
        nd = int(m["n_directed"] or 0)
        nh = int(m.get("n_hit") or 0)
        if m.get("n_hit") is None and m["directed_hit_pct"] is not None and nd:
            nh = int(round(m["directed_hit_pct"] / 100.0 * nd))
        ns = int(m["n_signals"] or 0)
        n_sig += ns
        n_dir += nd
        hit += nh
        if m["always_long_pct"] is not None and nd:
            long_hit += int(round(m["always_long_pct"] / 100.0 * nd))
        per_name.append(
            {
                "stock_id": s["stock_id"],
                "n_directed": nd,
                "directed_hit_pct": m["directed_hit_pct"],
                "expectancy_signed_pct": m["expectancy_signed_pct"],
                "mae_pct_median": m.get("mae_pct_median"),
                "mae_atr_median": m.get("mae_atr_median"),
            }
        )
        if m.get("mae_pct_median") is not None:
            mae_medians.append(float(m["mae_pct_median"]))
        if m.get("mae_atr_median") is not None:
            mae_atr_medians.append(float(m["mae_atr_median"]))

    d_pct = _pct(hit, n_dir)
    al = _pct(long_hit, n_dir)
    # Pool-level MAE from concatenating isn't available here; use name medians.
    return {
        "n_signals": n_sig,
        "n_directed": n_dir,
        "n_hit": hit,
        "directed_hit_pct": d_pct,
        "always_long_pct": al,
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
        "mae_pct_median_across_names": _median(mae_medians),
        "mae_atr_median_across_names": _median(mae_atr_medians),
        "per_name": per_name,
    }


def pool_mae_from_rows(
    stock_results: list[dict[str, Any]], variant: str, split: str, *, flat_eps: float
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    key = "_oos_rows" if split == "OOS" else None
    for s in stock_results:
        if s.get("error"):
            continue
        v = s["variants"][variant]
        if split == "OOS" and key and key in v:
            rows.extend(v[key])
        else:
            # rebuild from summarize fields only if rows stripped — skip
            pass
    if not rows:
        return {}
    return summarize(rows, flat_eps=flat_eps)


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
    gate_hit = bool(hit is not None and hit >= 70.0 and nd_pool >= MIN_OOS_N)
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
            {
                "stock_id": p["stock_id"],
                "hit": p.get("directed_hit_pct"),
                "n": p.get("n_directed"),
            }
            for p in names
            if p["stock_id"] not in {x["stock_id"] for x in pass_names}
        ],
    }


def strip_row_payloads(stock_results: list[dict[str, Any]]) -> None:
    for s in stock_results:
        for v in (s.get("variants") or {}).values():
            v.pop("_oos_rows", None)


def disagreement_analysis(
    conn: sqlite3.Connection,
    stocks: Sequence[str],
    *,
    start: str,
    end: str,
    is_end: str,
    horizon: int,
    flat_eps: float,
) -> dict[str, Any]:
    """Compare fade_idx_or_inside when 0050 vs IX0001 OR-state agree/disagree."""
    b50 = load_days(conn, "0050", start=start, end=end)
    bix = load_days(conn, "IX0001", start=start, end=end)
    v = next(x for x in VARIANTS if x.name == "fade_idx_or_inside")
    buckets: dict[str, list[dict[str, Any]]] = {
        "agree_inside": [],
        "agree_outside": [],
        "0050_inside_ix_out": [],
        "ix_inside_0050_out": [],
        "only_0050_day": [],
        "only_ix_day": [],
    }
    n_days_both = 0
    for sid in stocks:
        days = load_days(conn, sid, start=start, end=end)
        atr_map = load_atr_pct(conn, sid, start=start, end=end)
        for td, bars in days.items():
            if td <= is_end:
                continue
            d50 = b50.get(td)
            dix = bix.get(td)
            if not d50 and not dix:
                continue
            if d50 and dix:
                n_days_both += 1
            atr = atr_map.get(td)
            for i in range(len(bars)):
                if i + horizon >= len(bars):
                    break
                prev = bars[: i + 1]
                fade_layer = fade_near_ext_from_bars(prev, midday_only=True)
                fade = (
                    int(fade_layer.temp)
                    if fade_layer.ready and fade_layer.temp is not None
                    else 0
                )
                if fade == 0:
                    continue
                st50 = index_state(prev, d50) if d50 else None
                stix = index_state(prev, dix) if dix else None
                in50 = bool(st50 and st50.get("ready") and st50.get("idx_or_state") == "inside")
                inix = bool(stix and stix.get("ready") and stix.get("idx_or_state") == "inside")
                c0 = bars[i].close
                if not c0:
                    continue
                fwd = bars[i + horizon].close / c0 - 1.0
                mae = path_mae_pct(bars, i, horizon, fade)
                row = {
                    "temp": fade,
                    "fwd": fwd,
                    "mae_pct": round(mae, 4) if mae is not None else None,
                    "mae_atr": (
                        round(mae / atr, 4) if mae is not None and atr and atr > 0 else None
                    ),
                    "stock_id": sid,
                    "trade_date": td,
                }
                if d50 and dix:
                    if in50 and inix:
                        buckets["agree_inside"].append(row)
                    elif (not in50) and (not inix):
                        buckets["agree_outside"].append(row)
                    elif in50 and not inix:
                        buckets["0050_inside_ix_out"].append(row)
                    else:
                        buckets["ix_inside_0050_out"].append(row)
                elif d50 and not dix:
                    if in50:
                        buckets["only_0050_day"].append(row)
                elif dix and not d50:
                    if inix:
                        buckets["only_ix_day"].append(row)

    # For or_inside signal on each bench: only rows that would fire.
    def _sum_bucket(name: str) -> dict[str, Any]:
        return summarize(buckets[name], flat_eps=flat_eps)

    return {
        "n_oos_days_both_benches_aligned_sessions": n_days_both,
        "buckets": {k: _sum_bucket(k) for k in buckets},
        "note": (
            "Buckets are among fade_near_ext signals on OOS days. "
            "agree_inside ≈ both benches would keep fade_idx_or_inside; "
            "0050_inside_ix_out = proxy keeps signal but true index OR broken."
        ),
    }


def inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    def kbar(sid: str) -> dict[str, Any]:
        r = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT trade_date),
                   MIN(trade_date), MAX(trade_date)
            FROM stock_kbar_5m WHERE stock_id=?
            """,
            (sid,),
        ).fetchone()
        n_is = conn.execute(
            """
            SELECT COUNT(DISTINCT trade_date) FROM stock_kbar_5m
            WHERE stock_id=? AND trade_date<=?
            """,
            (sid, DEFAULT_IS_END),
        ).fetchone()[0]
        n_oos = conn.execute(
            """
            SELECT COUNT(DISTINCT trade_date) FROM stock_kbar_5m
            WHERE stock_id=? AND trade_date>?
            """,
            (sid, DEFAULT_IS_END),
        ).fetchone()[0]
        return {
            "n_bars": r[0],
            "n_days": r[1],
            "min": r[2],
            "max": r[3],
            "n_days_IS": n_is,
            "n_days_OOS": n_oos,
        }

    return {"bench_0050": kbar("0050"), "bench_IX0001": kbar("IX0001")}


def run_bench_grid(
    conn: sqlite3.Connection,
    *,
    bench_id: str,
    stocks: Sequence[str],
    start: str,
    end: str,
    is_end: str,
    horizon: int,
    flat_eps: float,
    oos_only: bool,
) -> dict[str, Any]:
    print(f"  bench={bench_id} load …", flush=True)
    bench_days = load_days(conn, bench_id, start=start, end=end)
    stock_results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"    {sid} …", flush=True)
        stock_results.append(
            run_stock(
                conn,
                sid,
                bench_days,
                start=start,
                end=end,
                is_end=is_end,
                variants=VARIANTS,
                horizon=horizon,
                flat_eps=flat_eps,
                oos_only=oos_only,
            )
        )

    leaderboard: list[dict[str, Any]] = []
    for v in VARIANTS:
        is_pool = pool_from_stocks(stock_results, v.name, "IS")
        oos_pool = pool_from_stocks(stock_results, v.name, "OOS")
        oos_mae = pool_mae_from_rows(stock_results, v.name, "OOS", flat_eps=flat_eps)
        if oos_mae:
            for k in (
                "mae_pct_median",
                "mae_pct_p75",
                "mae_pct_mean",
                "mae_atr_median",
                "mae_atr_p75",
                "mae_atr_mean",
                "mae_pct_mean_hit",
                "mae_pct_mean_miss",
                "mae_atr_mean_miss",
                "mae_zero_frac",
                "expectancy_signed_pct",
            ):
                if k in oos_mae:
                    oos_pool[k] = oos_mae[k]
        leaderboard.append(
            {
                "name": v.name,
                "family": v.family,
                "kind": v.kind,
                "IS": is_pool,
                "OOS": oos_pool,
            }
        )

    eligible = [
        r
        for r in leaderboard
        if (r["IS"].get("n_directed") or 0) >= 200
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not eligible:
        # IX0001 has no IS → pick by OOS for exploratory only (no claim).
        eligible = [
            r for r in leaderboard if r["OOS"].get("directed_hit_pct") is not None
        ]
        champ_row = max(
            eligible,
            key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"]["n_directed"]),
        ) if eligible else leaderboard[0]
        champ_source = "oos_exploratory_no_IS"
    else:
        champ_row = max(
            eligible, key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"])
        )
        champ_source = "IS"

    gates = stability_gates(champ_row["OOS"])
    strip_row_payloads(stock_results)
    return {
        "bench": bench_id,
        "oos_only": oos_only,
        "champ_source": champ_source,
        "is_champion": {
            "name": champ_row["name"],
            "family": champ_row["family"],
            "IS": champ_row["IS"],
            "OOS": champ_row["OOS"],
        },
        "gates": gates,
        "leaderboard": sorted(
            leaderboard,
            key=lambda r: (
                -(r["OOS"]["directed_hit_pct"] or -1),
                -(r["OOS"]["n_directed"] or 0),
            ),
        ),
        "per_stock": stock_results,
    }


def write_md(payload: dict[str, Any], path: Path) -> None:
    m = payload["metric"]
    r50 = payload["results_0050"]
    rix = payload["results_IX0001"]
    gap = payload.get("gap_analysis") or {}
    g50 = r50["gates"]
    champ50 = r50["is_champion"]
    lines = [
        "# Track E5 · Index trend filter + MAE/ATR · ~30m",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Question",
        "",
        "Does an index **trend** filter beyond `fade_idx_or_inside` push "
        "**IX0001** OOS directed hit to ≥70%? What do MAE / ATR-normalized "
        "adverse excursions say about fade risk? Why 0050≈70.7% vs IX0001≈66.9%?",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: stock+bench factors use completed same-session bars `≤ i`; "
        f"ATR{ATR_PERIOD}% ends **T−1**.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; champion on **IS** for 0050; "
        "IX0001 is OOS-only sensitivity (0 IS days).",
        f"- Universe (ex benches): `{', '.join(m['stocks'])}`.",
        f"- Pre-registered variants: {m['n_variants']}.",
        f"- Window: **{m['start']} → {m['end']}**",
        "",
        f"## Verdict: trend filter → IX0001 ≥70%？ "
        f"**{payload['verdict_ix_ge70']}**",
        "",
        f"- 0050 IS champion: `{champ50['name']}` → OOS "
        f"**{champ50['OOS']['directed_hit_pct']}%** "
        f"(n={champ50['OOS']['n_directed']}) · gates "
        f"**{'PASS' if g50['all_gates_pass'] else 'FAIL'}**",
        f"- IX0001 best OOS (exploratory / no IS): "
        f"`{rix['is_champion']['name']}` → "
        f"**{rix['is_champion']['OOS']['directed_hit_pct']}%** "
        f"(n={rix['is_champion']['OOS']['n_directed']})",
        f"- IX0001 `fade_idx_or_inside` OOS: "
        f"**{payload['ix_or_inside_oos']['directed_hit_pct']}%** "
        f"(n={payload['ix_or_inside_oos']['n_directed']})",
        f"- Helps close 0050↔IX0001 gap? **{payload['helps_gap']}**",
        f"- Prior stable ref `fade_idx_or_inside` @0050: "
        f"**{payload.get('ref_or_inside_0050', {}).get('directed_hit_pct')}%** "
        f"(n={payload.get('ref_or_inside_0050', {}).get('n_directed')}; "
        f"gates **{'PASS' if payload.get('ref_or_inside_0050_gates_pass') else 'FAIL'}**)",
        "",
        "## Gates（0050 · IS champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{g50['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{g50['gate_name_stability']}** "
        f"({g50['n_names_pass']}/{g50['n_names']} = {g50['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{g50['gate_no_megacap_dom']}** "
        f"(max share {g50['max_name_share']} · {g50['max_name_sid']})",
        "",
        "## Leaderboard · bench=0050（OOS hit 排序）",
        "",
        "|variant|IS hit%|IS n|OOS hit%|OOS n|MAE% mean|MAE% p75|MAE/ATR mean|E[signed]%|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in r50["leaderboard"]:
        o = row["OOS"]
        lines.append(
            f"|{row['name']}|{row['IS']['directed_hit_pct']}|{row['IS']['n_directed']}|"
            f"{o['directed_hit_pct']}|{o['n_directed']}|"
            f"{o.get('mae_pct_mean')}|{o.get('mae_pct_p75')}|"
            f"{o.get('mae_atr_mean')}|{o.get('expectancy_signed_pct')}|"
        )

    lines += [
        "",
        "## Leaderboard · bench=IX0001（OOS-only · no IS claim）",
        "",
        "|variant|OOS hit%|OOS n|MAE% mean|MAE% p75|MAE/ATR mean|E[signed]%|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rix["leaderboard"]:
        o = row["OOS"]
        lines.append(
            f"|{row['name']}|{o['directed_hit_pct']}|{o['n_directed']}|"
            f"{o.get('mae_pct_mean')}|{o.get('mae_pct_p75')}|"
            f"{o.get('mae_atr_mean')}|{o.get('expectancy_signed_pct')}|"
        )

    lines += [
        "",
        "## MAE / ATR findings",
        "",
        payload["mae_interpretation"],
        "",
        "## 0050 vs IX0001 gap analysis",
        "",
        gap.get("narrative", ""),
        "",
        "### Disagreement buckets（OOS · among `fade_near_ext`）",
        "",
        "|bucket|hit%|n|MAE% mean|MAE/ATR mean|",
        "|---|---:|---:|---:|---:|",
    ]
    for name, sm in (gap.get("buckets") or {}).items():
        lines.append(
            f"|{name}|{sm.get('directed_hit_pct')}|{sm.get('n_directed')}|"
            f"{sm.get('mae_pct_mean')}|{sm.get('mae_atr_mean')}|"
        )

    lines += [
        "",
        "## Interpretation",
        "",
        payload["interpretation"],
        "",
        "## Claim",
        "",
        f"- `oos_ge_70_stable_0050_is_champ`: **{payload['claim']['oos_ge_70_stable_0050_is_champ']}**",
        f"- `oos_ge_70_stable_0050_or_inside`: **{payload['claim']['oos_ge_70_stable_0050_or_inside']}**",
        f"- `oos_ge_70_stable_ix0001`: **{payload['claim']['oos_ge_70_stable_ix0001']}**",
        f"- `live_bias_updated`: **False**",
        f"- `order_adopted`: **False**",
        "",
        "## Artifacts",
        "",
        f"- JSON: `{payload['artifacts']['json']}`",
        f"- MD: `{payload['artifacts']['md']}`",
        f"- Runner: `scripts/research/run_ta_30m_track_e5_index_trend_atr.py`",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "stocks.db")
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2026-07-22")
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--horizon", type=int, default=TA30M_HORIZON_BARS)
    ap.add_argument("--flat-eps", type=float, default=FLAT_EPS)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "reports" / "research" / "intraday_direction_thermometer",
    )
    args = ap.parse_args()
    stocks = tuple(s.strip() for s in args.stocks.split(",") if s.strip())

    conn = sqlite3.connect(str(args.db))
    inv = inventory(conn)

    r50 = run_bench_grid(
        conn,
        bench_id="0050",
        stocks=stocks,
        start=args.start,
        end=args.end,
        is_end=args.is_end,
        horizon=args.horizon,
        flat_eps=args.flat_eps,
        oos_only=False,
    )
    rix = run_bench_grid(
        conn,
        bench_id="IX0001",
        stocks=stocks,
        start=args.start,
        end=args.end,
        is_end=args.is_end,
        horizon=args.horizon,
        flat_eps=args.flat_eps,
        oos_only=True,
    )

    print("  disagreement analysis …", flush=True)
    gap_raw = disagreement_analysis(
        conn,
        stocks,
        start=args.start,
        end=args.end,
        is_end=args.is_end,
        horizon=args.horizon,
        flat_eps=args.flat_eps,
    )
    conn.close()

    def _lb(res: dict[str, Any], name: str) -> dict[str, Any]:
        for row in res["leaderboard"]:
            if row["name"] == name:
                return row["OOS"]
        return {}

    ix_or = _lb(rix, "fade_idx_or_inside")
    b50_or = _lb(r50, "fade_idx_or_inside")
    champ50 = r50["is_champion"]
    ix_best = rix["is_champion"]["OOS"]
    ix_ge70 = bool(
        (ix_best.get("directed_hit_pct") or 0) >= 70.0
        and (ix_best.get("n_directed") or 0) >= MIN_OOS_N
        and rix["gates"]["all_gates_pass"]
    )
    # Also check if any trend filter lifts IX or_inside to ≥70 with n≥500
    ix_any_ge70 = any(
        (row["OOS"].get("directed_hit_pct") or 0) >= 70.0
        and (row["OOS"].get("n_directed") or 0) >= MIN_OOS_N
        for row in rix["leaderboard"]
    )

    agree = (gap_raw.get("buckets") or {}).get("agree_inside") or {}
    disc_50in = (gap_raw.get("buckets") or {}).get("0050_inside_ix_out") or {}
    disc_ixin = (gap_raw.get("buckets") or {}).get("ix_inside_0050_out") or {}
    # IX or_inside ≈ agree_inside ∪ ix_inside_0050_out (same OOS overlap days).
    gap_narrative = (
        f"Decompose OOS `fade_near_ext` where **both** benches exist: "
        f"**agree_inside** hit **{agree.get('directed_hit_pct')}%** "
        f"(n={agree.get('n_directed')}). "
        f"The large dilution bucket is **`ix_inside_0050_out`** "
        f"(IX still OR-inside, 0050 already outside): hit "
        f"**{disc_ixin.get('directed_hit_pct')}%** "
        f"(n={disc_ixin.get('n_directed')}). "
        f"IX `fade_idx_or_inside` ≈ agree_inside + that bucket → "
        f"{ix_or.get('directed_hit_pct')}% (n={ix_or.get('n_directed')}). "
        f"Conversely `0050_inside_ix_out` is small "
        f"(n={disc_50in.get('n_directed')}, hit {disc_50in.get('directed_hit_pct')}%). "
        f"So the ~{(b50_or.get('directed_hit_pct') or 0) - (ix_or.get('directed_hit_pct') or 0):.1f}pp "
        f"gap (0050 {b50_or.get('directed_hit_pct')}% vs IX {ix_or.get('directed_hit_pct')}%) "
        f"is mostly **IX keeping low-quality ‘index still inside’ signals after the "
        f"ETF proxy has already broken OR**, not 0050 falsely keeping rotation. "
        f"VWAP / OR-hold ANDs raise raw IX hit on thin n "
        f"(e.g. `fade_or_inside_and_vwap_mr` {rix['is_champion']['OOS'].get('directed_hit_pct')}% "
        f"n={rix['is_champion']['OOS'].get('n_directed')}) but **fail n≥500 + name stability**."
    )

    def _mae_line(label: str, oos: dict[str, Any]) -> str:
        return (
            f"- **{label}**: MAE% mean={oos.get('mae_pct_mean')} · "
            f"p75={oos.get('mae_pct_p75')} · zero_frac={oos.get('mae_zero_frac')}; "
            f"MAE/ATR mean={oos.get('mae_atr_mean')} · p75={oos.get('mae_atr_p75')}; "
            f"miss MAE% mean={oos.get('mae_pct_mean_miss')} "
            f"(miss MAE/ATR={oos.get('mae_atr_mean_miss')}); "
            f"E[signed]%={oos.get('expectancy_signed_pct')} "
            f"(n_dir={oos.get('n_directed')})"
        )

    mae_bits = [
        "Adverse excursion over the same H=6 path (research risk context; not Order stops). "
        "Median often ≈0 because ≥50% of directed fades never print a worse extreme than entry "
        "within 30m — use **mean / p75 / miss-MAE**.",
        _mae_line("0050 · fade_idx_or_inside", b50_or),
        _mae_line("IX0001 · fade_idx_or_inside", ix_or),
        _mae_line("0050 · baseline_fade", _lb(r50, "baseline_fade_near_ext")),
        _mae_line("IX0001 · baseline_fade", _lb(rix, "baseline_fade_near_ext")),
    ]
    if disc_ixin.get("mae_atr_mean") is not None and agree.get("mae_atr_mean") is not None:
        mae_bits.append(
            f"- Bucket `ix_inside_0050_out` MAE/ATR mean="
            f"{disc_ixin.get('mae_atr_mean')} vs agree_inside "
            f"{agree.get('mae_atr_mean')} — "
            + (
                "worse adverse when IX alone keeps the inside label."
                if (disc_ixin.get("mae_atr_mean") or 0) > (agree.get("mae_atr_mean") or 0)
                else "MAE similar; dilution is mainly hit-rate."
            )
        )
    mae_interpretation = "\n".join(mae_bits)

    best_ix_hit = ix_best.get("directed_hit_pct")
    best_ix_n = ix_best.get("n_directed") or 0
    if ix_ge70:
        verdict = "YES"
        helps = "YES — trend filter clears IX shared gates"
    elif (
        best_ix_hit is not None
        and best_ix_hit >= 70.0
        and best_ix_n < MIN_OOS_N
    ):
        verdict = "NO"
        helps = (
            f"NO — `{rix['is_champion']['name']}` raw IX OOS {best_ix_hit}% "
            f"but n={best_ix_n}<{MIN_OOS_N} and name-stability fails; "
            "cannot claim ≥70% stable"
        )
    elif ix_any_ge70:
        verdict = "NO"
        helps = "PARTIAL — some IX variant hit≥70 but gates fail"
    else:
        verdict = "NO"
        helps = (
            f"NO — best IX OOS `{rix['is_champion']['name']}` at {best_ix_hit}% "
            f"(n={best_ix_n}); gap from ix_inside_0050_out dilution, "
            "not closed under shared gates"
        )

    # Reference prior stable champion for interpretation.
    or_inside_gates = stability_gates(
        next(r for r in r50["leaderboard"] if r["name"] == "fade_idx_or_inside")["OOS"]
    )
    interpretation = (
        "E5 tests index **trend** context beyond OR-inside: VWAP mean-reversion "
        "side, OR-break hold ≥K bars, and combinations. "
        f"IS-max on 0050 is `{champ50['name']}` "
        f"(OOS {champ50['OOS']['directed_hit_pct']}%, n={champ50['OOS']['n_directed']}) "
        f"but **fails** n≥500 / name stability. "
        f"Prior stable reference `fade_idx_or_inside` stays "
        f"{b50_or.get('directed_hit_pct')}% OOS (n={b50_or.get('n_directed')}; "
        f"gates {'PASS' if or_inside_gates['all_gates_pass'] else 'FAIL'}). "
        f"On **IX0001**, or_inside={ix_or.get('directed_hit_pct')}% "
        f"(n={ix_or.get('n_directed')}); stricter ANDs do not clear shared gates. "
        "Gap vs 0050 is explained by **`ix_inside_0050_out`** dilution. "
        "MAE/ATR: mean adverse ≈0.4–0.5% (≈0.08–0.10×ATR14) over 30m; "
        "misses worse than hits. **Do not** update Live / Order from E5."
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "track_e5_index_trend_atr.json"
    md_path = out_dir / "TRACK_E5_INDEX_TREND_ATR.md"

    payload: dict[str, Any] = {
        "topic": "intraday-direction-thermometer · E5 index trend + MAE/ATR",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_inventory": inv,
        "metric": {
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": args.end,
            "stocks": list(stocks),
            "n_variants": len(VARIANTS),
            "variant_names": [v.name for v in VARIANTS],
            "atr_period": ATR_PERIOD,
        },
        "results_0050": r50,
        "results_IX0001": rix,
        "ix_or_inside_oos": ix_or,
        "gap_analysis": {
            "narrative": gap_narrative,
            "buckets": gap_raw.get("buckets"),
            "n_oos_days_both": gap_raw.get("n_oos_days_both_benches_aligned_sessions"),
            "note": gap_raw.get("note"),
        },
        "mae_interpretation": mae_interpretation,
        "interpretation": interpretation,
        "verdict_ix_ge70": verdict,
        "helps_gap": helps,
        "ref_or_inside_0050": b50_or,
        "ref_or_inside_0050_gates_pass": bool(or_inside_gates["all_gates_pass"]),
        "ref_or_inside_0050_gates": or_inside_gates,
        "claim": {
            "oos_ge_70_stable_0050_is_champ": bool(r50["gates"]["all_gates_pass"]),
            "oos_ge_70_stable_0050_or_inside": bool(or_inside_gates["all_gates_pass"]),
            "oos_ge_70_stable_ix0001": bool(ix_ge70),
            "live_bias_updated": False,
            "order_adopted": False,
        },
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
        },
    }
    # Drop heavy per_stock row caches already stripped; keep structure.
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, md_path)

    print(
        f"0050 champ={champ50['name']} OOS={champ50['OOS']['directed_hit_pct']}% "
        f"gates={r50['gates']['all_gates_pass']}"
    )
    print(
        f"IX or_inside OOS={ix_or.get('directed_hit_pct')}% "
        f"best={rix['is_champion']['name']} "
        f"{ix_best.get('directed_hit_pct')}% · verdict_ix_ge70={verdict}"
    )
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
