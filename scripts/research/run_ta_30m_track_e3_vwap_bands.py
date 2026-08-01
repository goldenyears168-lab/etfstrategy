#!/usr/bin/env python3
"""Track E3: VWAP 2σ + rejection candle + ADX/wide-OR skip ∧ fade_idx_or_inside.

Research only · PIT · shared ~30m directed-hit gates (IS≤2025-09-30).

Pre-registered stack (PRO_METHODS E3)
-------------------------------------
1. Session VWAP ± 2σ bands from completed 5m bars (volume-weighted σ of TP).
2. Rejection candle at the band (poke beyond band, close back inside + wick).
3. Skip if session ADX(14)@5m > 25 **or** OR width > 0.75× ATR(14).
4. AND with the **idx OR-inside** gate from ``fade_idx_or_inside``
   (0050 OR intact). Note: raw ``fade_near_ext`` ∩ VWAP-rejection is
   nearly empty (different extreme definitions), so primary E3 does **not**
   require fade_near_ext coincidence; optional variants test fade_idx +
   ADX/OR skip and fade_idx + |z|≥2 stretch.

Baselines (frozen refs): ``baseline_fade_near_ext``, ``fade_idx_or_inside``.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_track_e3_vwap_bands.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import os
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
BENCH_5M = "0050"
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0005
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
MIN_IS_N_SELECT = 100

VWAP_SIGMA = 2.0
ADX_PERIOD = 14
ADX_SKIP = 25.0
ATR_PERIOD = 14
OR_ATR_SKIP = 0.75
MIN_BAND_BARS = 8  # need history for σ / ADX / ATR


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    family: str  # baseline | e3
    # kinds: fade | fade_idx_or_inside | vwap2s_rej | vwap2s_rej_skip
    #        e3_vwap_idx | e3_full | fade_idx_skip | fade_idx_at_2s


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", "baseline"),
    Variant("fade_idx_or_inside", "fade_idx_or_inside", "baseline"),
    Variant("vwap2s_rej", "vwap2s_rej", "e3"),
    Variant("vwap2s_rej_skip", "vwap2s_rej_skip", "e3"),
    Variant("e3_vwap_idx", "e3_vwap_idx", "e3"),
    Variant("e3_full", "e3_full", "e3"),
    Variant("fade_idx_skip", "fade_idx_skip", "e3"),
    Variant("fade_idx_at_2s", "fade_idx_at_2s", "e3"),
)


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


def _align_bench_prefix(
    stock_prev: Sequence[Bar], bench_day: Sequence[Bar]
) -> list[Bar]:
    if not stock_prev or not bench_day:
        return []
    t_end = stock_prev[-1].ts
    return [b for b in bench_day if b.ts <= t_end]


def idx_or_inside(stock_prev: Sequence[Bar], bench_day: Sequence[Bar]) -> bool | None:
    """True iff 0050 OR still intact (close inside OR) on aligned prefix."""
    bprev = _align_bench_prefix(stock_prev, bench_day)
    if len(bprev) < TA30M_OR_BARS:
        return None
    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return None
    or_hi, or_lo = or_hl
    bc = bprev[-1].close
    return or_lo <= bc <= or_hi


def session_vwap_sigma(prev: Sequence[Bar]) -> tuple[float, float] | None:
    """Session VWAP and volume-weighted σ of typical price vs VWAP (PIT)."""
    vwap = session_vwap(prev)
    if vwap is None or vwap <= 0 or len(prev) < 2:
        return None
    wsum = 0.0
    ssq = 0.0
    for b in prev:
        tp = (b.high + b.low + b.close) / 3.0
        w = float(b.volume or 0.0)
        if w <= 0:
            w = 1.0
        d = tp - vwap
        wsum += w
        ssq += w * d * d
    if wsum <= 0:
        return None
    var = ssq / wsum
    # Floor σ so thin early session doesn't explode z-scores.
    sigma = max(math.sqrt(var), vwap * 0.0005)
    return vwap, sigma


def _wilder_atr_adx(
    prev: Sequence[Bar], *, period: int = ADX_PERIOD
) -> tuple[float | None, float | None]:
    """Return (ATR, ADX) on completed prefix; None if not enough bars."""
    n = len(prev)
    need = period * 2 + 1
    if n < need:
        return None, None
    trs: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    for i in range(1, n):
        h, l, c_prev = prev[i].high, prev[i].low, prev[i - 1].close
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        up = h - prev[i - 1].high
        down = prev[i - 1].low - l
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)
    if len(trs) < period:
        return None, None

    # Wilder seed: SMA of first ``period`` TR / DM, then recursive smooth.
    atr_s = sum(trs[:period])
    plus_s = sum(plus_dms[:period])
    minus_s = sum(minus_dms[:period])
    atr_avg = atr_s / period
    dx_vals: list[float] = []

    def _dx(atr_sum: float, plus_sum: float, minus_sum: float) -> float:
        atr_a = atr_sum / period
        if atr_a <= 1e-12:
            return 0.0
        pdi = 100.0 * (plus_sum / period) / atr_a
        mdi = 100.0 * (minus_sum / period) / atr_a
        denom = pdi + mdi
        return 100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0

    dx_vals.append(_dx(atr_s, plus_s, minus_s))
    for i in range(period, len(trs)):
        atr_s = atr_s - atr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dms[i]
        minus_s = minus_s - minus_s / period + minus_dms[i]
        atr_avg = atr_s / period
        dx_vals.append(_dx(atr_s, plus_s, minus_s))

    if len(dx_vals) < period:
        return atr_avg, None
    adx = sum(dx_vals[:period]) / period
    for i in range(period, len(dx_vals)):
        adx = (adx * (period - 1) + dx_vals[i]) / period
    return atr_avg, adx


def or_too_wide(prev: Sequence[Bar], atr: float | None) -> bool | None:
    """True if OR width > OR_ATR_SKIP × ATR(14). None if OR/ATR not ready."""
    or_hl = opening_range_hl(prev)
    if or_hl is None or atr is None or atr <= 0:
        return None
    or_hi, or_lo = or_hl
    width = or_hi - or_lo
    return width > OR_ATR_SKIP * atr


def rejection_at_vwap_band(prev: Sequence[Bar]) -> int:
    """+1 lower-band rejection (fade long); -1 upper; 0 none. PIT."""
    if len(prev) < MIN_BAND_BARS:
        return 0
    band = session_vwap_sigma(prev)
    if band is None:
        return 0
    vwap, sigma = band
    upper = vwap + VWAP_SIGMA * sigma
    lower = vwap - VWAP_SIGMA * sigma
    b = prev[-1]
    rng = b.high - b.low
    if rng <= 0:
        return 0
    upper_wick = b.high - max(b.open, b.close)
    lower_wick = min(b.open, b.close) - b.low
    body = abs(b.close - b.open)

    # Upper: poke ≥ +2σ, close back inside, upper wick ≥ body (rejection).
    if b.high >= upper and b.close < upper and upper_wick >= max(body, rng * 0.35):
        return -1
    # Lower: poke ≤ −2σ, close back inside, lower wick ≥ body.
    if b.low <= lower and b.close > lower and lower_wick >= max(body, rng * 0.35):
        return 1
    return 0


def close_z_vs_vwap(prev: Sequence[Bar]) -> float | None:
    """Signed (close − VWAP) / σ; None if band not ready."""
    if len(prev) < MIN_BAND_BARS:
        return None
    band = session_vwap_sigma(prev)
    if band is None:
        return None
    vwap, sigma = band
    if sigma <= 0:
        return None
    return (prev[-1].close - vwap) / sigma


def trend_or_wide_skip(prev: Sequence[Bar]) -> bool:
    """Skip fade when ADX high or OR too wide (ATR-based). True = skip."""
    atr, adx = _wilder_atr_adx(prev, period=ADX_PERIOD)
    if adx is not None and adx > ADX_SKIP:
        return True
    wide = or_too_wide(prev, atr if atr is not None else None)
    if wide is True:
        return True
    # If ADX/ATR not ready yet, do not skip (conservative: allow signal
    # only when filters can be evaluated would thin early session too hard;
    # E3 midday fade usually has enough bars).
    return False


def signal_temp(
    stock_prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    variant: Variant,
) -> int:
    kind = variant.kind

    fade_layer = fade_near_ext_from_bars(stock_prev, midday_only=True)
    fade = (
        int(fade_layer.temp)
        if fade_layer.ready and fade_layer.temp is not None
        else 0
    )

    if kind == "fade":
        return fade

    if kind == "fade_idx_or_inside":
        if fade == 0:
            return 0
        inside = idx_or_inside(stock_prev, bench_day)
        return fade if inside is True else 0

    rej = rejection_at_vwap_band(stock_prev)
    inside = idx_or_inside(stock_prev, bench_day)

    if kind == "vwap2s_rej":
        return rej

    if kind == "vwap2s_rej_skip":
        if rej == 0:
            return 0
        return 0 if trend_or_wide_skip(stock_prev) else rej

    if kind == "e3_vwap_idx":
        # VWAP±2σ rejection ∧ 0050 OR intact (idx gate from fade_idx_or_inside).
        if rej == 0 or inside is not True:
            return 0
        return rej

    if kind == "e3_full":
        # Primary: rejection ∧ ADX/OR skip ∧ idx OR-inside.
        if rej == 0 or inside is not True:
            return 0
        if trend_or_wide_skip(stock_prev):
            return 0
        return rej

    if kind == "fade_idx_skip":
        # Optional: existing champion + ADX/wide-OR skip only.
        if fade == 0 or inside is not True:
            return 0
        if trend_or_wide_skip(stock_prev):
            return 0
        return fade

    if kind == "fade_idx_at_2s":
        # Optional: fade_idx_or_inside only when |z|≥2 (stretch, no rej K).
        if fade == 0 or inside is not True:
            return 0
        z = close_z_vs_vwap(stock_prev)
        if z is None or abs(z) < VWAP_SIGMA:
            return 0
        # Direction must agree with stretch side.
        if fade < 0 and z < VWAP_SIGMA:
            return 0
        if fade > 0 and z > -VWAP_SIGMA:
            return 0
        return fade

    return 0


def eval_day(
    stock_bars: list[Bar],
    bench_bars: list[Bar],
    variant: Variant,
    *,
    horizon: int,
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
        out.append(
            {
                "temp": int(temp),
                "fwd": fwd,
                "hhmm": stock_bars[i].ts.strftime("%H:%M"),
            }
        )
    return out


def summarize(rows: list[dict[str, Any]], *, flat_eps: float) -> dict[str, Any]:
    directed_hit = directed_tot = long_hit = 0
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
        "edge_vs_always_long_pp": (
            round(d_pct - al, 2) if d_pct is not None and al is not None else None
        ),
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
) -> dict[str, Any]:
    days = load_days(conn, stock_id, start=start, end=end)
    if not days:
        return {"stock_id": stock_id, "error": "no_5m", "n_days": 0}

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    n_aligned = 0
    for td, bars in sorted(days.items()):
        bench = bench_days.get(td)
        if not bench:
            continue
        n_aligned += 1
        split = "IS" if td <= is_end else "OOS"
        for v in variants:
            by_var[v.name][split].extend(
                eval_day(bars, bench, v, horizon=horizon)
            )

    out_vars: dict[str, Any] = {}
    for v in variants:
        is_rows = by_var[v.name]["IS"]
        oos_rows = by_var[v.name]["OOS"]
        out_vars[v.name] = {
            "IS": summarize(is_rows, flat_eps=flat_eps),
            "OOS": summarize(oos_rows, flat_eps=flat_eps),
            "ALL": summarize(is_rows + oos_rows, flat_eps=flat_eps),
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
            }
        )

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    d_pct = pct(hit, n_dir)
    al = pct(long_hit, n_dir)
    return {
        "n_signals": n_sig,
        "n_directed": n_dir,
        "n_hit": hit,
        "directed_hit_pct": d_pct,
        "always_long_pct": al,
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
        "edge_vs_always_long_pp": (
            round(d_pct - al, 2) if d_pct is not None and al is not None else None
        ),
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


def write_md(payload: dict[str, Any], path: Path) -> None:
    m = payload["metric"]
    champ = payload["is_champion"]
    e3 = payload["e3_full"]
    gates = payload["gates_e3_full"]
    fade = payload["baselines"]["baseline_fade_near_ext"]
    idx = payload["baselines"]["fade_idx_or_inside"]
    lines = [
        "# Track E3 · VWAP 2σ + 拒絕K + ADX／寬OR skip ∧ fade_idx_or_inside",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Hypothesis",
        "",
        "- Midday fade at session VWAP ±2σ with rejection candle.",
        "- Skip when ADX(14)@5m > 25 or OR width > 0.75× ATR(14).",
        "- AND with **idx OR-inside** (0050) from `fade_idx_or_inside`.",
        "- Note: `fade_near_ext` ∩ VWAP-rejection is nearly empty → "
        "primary `e3_full` uses rejection∧skip∧idx-OR, not fade_near_ext coincidence.",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: VWAP/σ/ADX/ATR/OR/rejection use completed same-session bars `≤ i`.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; IS-lock once; claim on OOS.",
        f"- Universe (ex `{BENCH_5M}`): `{', '.join(m['stocks'])}`.",
        f"- Bench for OR-inside: **`{BENCH_5M}` 5m**.",
        f"- Window: **{m['start']} → {m['end']}**",
        f"- Pre-registered: `{', '.join(m['variant_names'])}`.",
        "",
        f"## Verdict: OOS≥70% + stable？ "
        f"**{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- Pre-registered E3 full (`e3_full`) OOS: "
        f"**{e3['OOS']['directed_hit_pct']}%** (n={e3['OOS']['n_directed']})",
        f"- E3 full IS: **{e3['IS']['directed_hit_pct']}%** "
        f"(n={e3['IS']['n_directed']})",
        f"- Baseline fade OOS: **{fade['OOS']['directed_hit_pct']}%** "
        f"(n={fade['OOS']['n_directed']})",
        f"- Baseline `fade_idx_or_inside` OOS: "
        f"**{idx['OOS']['directed_hit_pct']}%** (n={idx['OOS']['n_directed']})",
        f"- Δ vs fade (OOS): **{payload['delta_vs_fade_pp']}** pp",
        f"- Δ vs idx_or_inside (OOS): **{payload['delta_vs_idx_pp']}** pp",
        f"- IS champion (grid): `{champ['name']}` · "
        f"IS {champ['IS']['directed_hit_pct']}% · "
        f"OOS {champ['OOS']['directed_hit_pct']}%",
        f"- Helps toward 70%? **{payload['helps_to_70']}**",
        "",
        "## Exploratory note（not claim）",
        "",
        f"- Grid IS-top `{champ['name']}` OOS "
        f"**{champ['OOS']['directed_hit_pct']}%** "
        f"(n={champ['OOS']['n_directed']}) — "
        f"gates_all={payload['gates_is_champion']['all_gates_pass']} "
        f"(hit≥70∧n≥500={payload['gates_is_champion']['gate_oos_hit_ge70_n500']}; "
        f"name_stab={payload['gates_is_champion']['gate_name_stability']}).",
        "- Higher point estimate than `fade_idx_or_inside` does **not** clear "
        "shared n/stability floors; do not treat as new champion.",
        "",
        "## Gates（`e3_full` OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Pool leaderboard（OOS directed 排序）",
        "",
        "|variant|family|IS hit%|IS n|OOS hit%|OOS n|Δ vs fade|Δ vs idx|",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    fade_oos = fade["OOS"]["directed_hit_pct"]
    idx_oos = idx["OOS"]["directed_hit_pct"]
    for r in payload["leaderboard"]:
        oos = r["OOS"]["directed_hit_pct"]
        d_f = (
            round(oos - fade_oos, 2)
            if oos is not None and fade_oos is not None
            else None
        )
        d_i = (
            round(oos - idx_oos, 2)
            if oos is not None and idx_oos is not None
            else None
        )
        lines.append(
            f"|{r['name']}|{r['family']}|"
            f"{r['IS']['directed_hit_pct']}|{r['IS']['n_directed']}|"
            f"{r['OOS']['directed_hit_pct']}|{r['OOS']['n_directed']}|"
            f"{d_f}|{d_i}|"
        )

    lines.extend(
        [
            "",
            "## `e3_full` per-stock OOS",
            "",
            "|sid|OOS hit%|OOS n|E[signed]%|",
            "|---|---:|---:|---:|",
        ]
    )
    for p in sorted(
        e3["OOS"].get("per_name") or [],
        key=lambda x: (-(x.get("directed_hit_pct") or 0), x["stock_id"]),
    ):
        lines.append(
            f"|{p['stock_id']}|{p['directed_hit_pct']}|"
            f"{p['n_directed']}|{p['expectancy_signed_pct']}|"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            payload["interpretation"],
            "",
            "## Artifacts",
            "",
            f"- JSON: `{payload['artifacts']['json']}`",
            f"- MD: `{payload['artifacts']['md']}`",
            f"- Runner: `{payload['artifacts']['runner']}`",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=(Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or ROOT) / "data" / "stocks.db"))
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="")
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--horizon", type=int, default=TA30M_HORIZON_BARS)
    ap.add_argument("--flat-eps", type=float, default=FLAT_EPS)
    ap.add_argument(
        "--stocks",
        default=",".join(DEFAULT_STOCKS),
        help="Comma-separated stock_ids (0050 used as bench, excluded)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT
        / "reports"
        / "research"
        / "intraday_direction_thermometer",
    )
    args = ap.parse_args()

    stocks = tuple(s.strip() for s in args.stocks.split(",") if s.strip())
    stocks = tuple(s for s in stocks if s != BENCH_5M)
    variants = VARIANTS

    conn = sqlite3.connect(str(args.db))
    if not args.end:
        r = conn.execute(
            "SELECT MAX(trade_date) FROM stock_kbar_5m WHERE stock_id=?",
            (BENCH_5M,),
        ).fetchone()
        args.end = str(r[0] or "2026-07-22")

    print(f"E3 VWAP bands · load {BENCH_5M} …", flush=True)
    bench_days = load_days(conn, BENCH_5M, start=args.start, end=args.end)
    print(f"  bench days={len(bench_days)}", flush=True)

    results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"  {sid} …", flush=True)
        results.append(
            run_stock(
                conn,
                sid,
                bench_days,
                start=args.start,
                end=args.end,
                is_end=args.is_end,
                variants=variants,
                horizon=args.horizon,
                flat_eps=args.flat_eps,
            )
        )
    conn.close()

    leaderboard: list[dict[str, Any]] = []
    for v in variants:
        is_pool = pool_from_stocks(results, v.name, "IS")
        oos_pool = pool_from_stocks(results, v.name, "OOS")
        leaderboard.append(
            {
                "name": v.name,
                "family": v.family,
                "IS": is_pool,
                "OOS": oos_pool,
            }
        )

    by_name = {r["name"]: r for r in leaderboard}
    fade_row = by_name["baseline_fade_near_ext"]
    idx_row = by_name["fade_idx_or_inside"]
    e3_row = by_name["e3_full"]
    gates_e3 = stability_gates(e3_row["OOS"])

    eligible = [
        r
        for r in leaderboard
        if (r["IS"].get("n_directed") or 0) >= MIN_IS_N_SELECT
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not eligible:
        eligible = [r for r in leaderboard if r["IS"].get("directed_hit_pct") is not None]
    champ = max(
        eligible,
        key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"]),
    )
    gates_champ = stability_gates(champ["OOS"])

    fade_oos = fade_row["OOS"]["directed_hit_pct"]
    idx_oos = idx_row["OOS"]["directed_hit_pct"]
    e3_oos = e3_row["OOS"]["directed_hit_pct"]
    delta_fade = (
        round(e3_oos - fade_oos, 2)
        if e3_oos is not None and fade_oos is not None
        else None
    )
    delta_idx = (
        round(e3_oos - idx_oos, 2)
        if e3_oos is not None and idx_oos is not None
        else None
    )

    if gates_e3["all_gates_pass"]:
        helps = "YES — e3_full clears OOS≥70% + stability"
    elif e3_oos is not None and idx_oos is not None and e3_oos > idx_oos + 1.0:
        helps = (
            f"PARTIAL — e3_full OOS {e3_oos}% > idx {idx_oos}% "
            f"(Δ{delta_idx}pp) but gates fail "
            f"(n={e3_row['OOS']['n_directed']})"
        )
    elif e3_oos is not None and idx_oos is not None and e3_oos >= idx_oos - 1.0:
        helps = (
            f"NO lift vs idx_or_inside — e3_full OOS {e3_oos}% vs "
            f"idx {idx_oos}% (Δ{delta_idx}pp); n={e3_row['OOS']['n_directed']}"
        )
    else:
        helps = (
            f"NO — e3_full OOS {e3_oos}% < idx {idx_oos}% "
            f"(Δ{delta_idx}pp); stack thins or hurts"
        )

    interpretation = (
        f"Unfiltered fade OOS {fade_oos}% (n={fade_row['OOS']['n_directed']}); "
        f"`fade_idx_or_inside` OOS {idx_oos}% "
        f"(n={idx_row['OOS']['n_directed']}). "
        f"Pre-registered `e3_full` (VWAP±2σ rejection ∧ ADX/OR skip ∧ "
        f"0050 OR-inside) OOS {e3_oos}% "
        f"(n={e3_row['OOS']['n_directed']}; IS {e3_row['IS']['directed_hit_pct']}% "
        f"n={e3_row['IS']['n_directed']}). "
        f"Δ vs fade {delta_fade}pp · Δ vs idx {delta_idx}pp. "
        "Standalone VWAP-rejection is ~coin or worse; idx-OR gate helps fade30 "
        "more than VWAP-band structure. `fade_near_ext` ∩ rejection is nearly "
        "empty (different extremes) — documented, not forced. "
        "Research only — do not Order-graduate; Live observe unchanged."
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "track_e3_vwap_bands.json"
    md_path = out_dir / "TRACK_E3_VWAP_BANDS.md"
    runner_rel = "scripts/research/run_ta_30m_track_e3_vwap_bands.py"

    leaderboard_sorted = sorted(
        leaderboard,
        key=lambda r: (
            -(r["OOS"]["directed_hit_pct"] or -1),
            -(r["OOS"]["n_directed"] or 0),
        ),
    )

    payload: dict[str, Any] = {
        "topic": "intraday-direction-thermometer · track E3 VWAP bands",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layer": "research",
        "status": "observe_only",
        "metric": {
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": args.end,
            "stocks": list(stocks),
            "bench_5m": BENCH_5M,
            "n_variants": len(variants),
            "variant_names": [v.name for v in variants],
            "vwap_sigma": VWAP_SIGMA,
            "adx_skip": ADX_SKIP,
            "or_atr_skip": OR_ATR_SKIP,
            "adx_period": ADX_PERIOD,
            "atr_period": ATR_PERIOD,
        },
        "baselines": {
            "baseline_fade_near_ext": fade_row,
            "fade_idx_or_inside": idx_row,
        },
        "e3_full": e3_row,
        "gates_e3_full": gates_e3,
        "gates_is_champion": gates_champ,
        "is_champion": {
            "name": champ["name"],
            "family": champ["family"],
            "IS": champ["IS"],
            "OOS": champ["OOS"],
        },
        "delta_vs_fade_pp": delta_fade,
        "delta_vs_idx_pp": delta_idx,
        "helps_to_70": helps,
        "leaderboard": leaderboard_sorted,
        "per_stock": [
            {
                "stock_id": s["stock_id"],
                "n_days": s.get("n_days"),
                "date_min": s.get("date_min"),
                "date_max": s.get("date_max"),
                "error": s.get("error"),
                "e3_full": (s.get("variants") or {}).get("e3_full"),
            }
            for s in results
        ],
        "interpretation": interpretation,
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
            "runner": runner_rel,
        },
    }

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, md_path)

    print(
        f"VERDICT e3_full_gates={gates_e3['all_gates_pass']} "
        f"e3_oos={e3_oos}% n={e3_row['OOS']['n_directed']} "
        f"fade={fade_oos}% idx={idx_oos}% "
        f"Δfade={delta_fade} Δidx={delta_idx} "
        f"champ={champ['name']}",
        flush=True,
    )
    print(f"wrote {md_path}", flush=True)
    print(f"wrote {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
