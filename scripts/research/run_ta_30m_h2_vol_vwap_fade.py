#!/usr/bin/env python3
"""H2 · closed-30m RVOL and/or VWAP-distance filters on fade_idx_or_inside.

Hypothesis
----------
Adding **closed-30m relative volume** and/or **VWAP distance** filters to
champion ``fade_idx_or_inside`` improves OOS directed hit **and** keeps
n≥500 + multi-stock stability (majority of names OOS≥55%).

Distinct from prior tracks
--------------------------
- E4: TOD RVOL (same HH:MM across prior days) dry-up — thin-n, not claimable.
- E3: VWAP ±2σ rejection + ADX/OR skip — hurt hit vs champion.
- H2: same-session closed-30m window RVOL + simple |vs_VWAP| extreme
  stacked on idx OR-inside only (light IS-locked grid).

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_h2_vol_vwap_fade.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
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
# H2 hypothesis: majority of names; also report prior-track 70% stab.
NAME_PASS_FRAC_H2 = 0.50
NAME_PASS_FRAC_STRICT = 0.70
MAX_NAME_SHARE = 0.40
MIN_IS_N_SELECT = 200
MIN_RVOL_HIST = 3  # prior completed 30m windows same session
HIT_GATE = 60.0  # shared thermometer floor; beat-champion is separate claim


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    family: str = "h2"
    # Optional filters (None = off)
    rvol_min: float | None = None  # require closed-30m RVOL ≥
    rvol_max: float | None = None  # require closed-30m RVOL ≤
    vwap_abs_pct: float | None = None  # |vs_VWAP|% extreme agreeing with fade


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", "baseline"),
    Variant("fade_idx_or_inside", "fade_idx_or_inside", "baseline"),
    # Closed-30m RVOL high (exhaustion / confirmation)
    Variant("idx_rvol30_hi_1p2", "idx_filt", rvol_min=1.2),
    Variant("idx_rvol30_hi_1p5", "idx_filt", rvol_min=1.5),
    Variant("idx_rvol30_hi_2p0", "idx_filt", rvol_min=2.0),
    # Closed-30m RVOL low (dry-up)
    Variant("idx_rvol30_lo_0p5", "idx_filt", rvol_max=0.5),
    Variant("idx_rvol30_lo_0p7", "idx_filt", rvol_max=0.7),
    Variant("idx_rvol30_lo_1p0", "idx_filt", rvol_max=1.0),
    # VWAP distance extreme (agree with fade side)
    Variant("idx_vwap_ext_0p3", "idx_filt", vwap_abs_pct=0.3),
    Variant("idx_vwap_ext_0p5", "idx_filt", vwap_abs_pct=0.5),
    Variant("idx_vwap_ext_0p75", "idx_filt", vwap_abs_pct=0.75),
    Variant("idx_vwap_ext_1p0", "idx_filt", vwap_abs_pct=1.0),
    # Light combos
    Variant(
        "idx_rvol_hi_1p5_vwap_0p5",
        "idx_filt",
        rvol_min=1.5,
        vwap_abs_pct=0.5,
    ),
    Variant(
        "idx_rvol_lo_0p7_vwap_0p5",
        "idx_filt",
        rvol_max=0.7,
        vwap_abs_pct=0.5,
    ),
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


def _align_bench(stock_prev: Sequence[Bar], bench_day: Sequence[Bar]) -> list[Bar]:
    if not stock_prev or not bench_day:
        return []
    t_end = stock_prev[-1].ts
    return [b for b in bench_day if b.ts <= t_end]


def idx_or_inside(stock_prev: Sequence[Bar], bench_day: Sequence[Bar]) -> bool | None:
    bprev = _align_bench(stock_prev, bench_day)
    if len(bprev) < TA30M_OR_BARS:
        return None
    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return None
    or_hi, or_lo = or_hl
    bc = bprev[-1].close
    return bool(or_lo <= bc <= or_hi)


def closed_30m_rvol(
    prev: Sequence[Bar],
    *,
    horizon: int = TA30M_HORIZON_BARS,
    min_hist: int = MIN_RVOL_HIST,
) -> float | None:
    """Same-session RVOL: last H-bar volume sum / median of prior H-bar sums.

    PIT: only completed bars ``≤`` last; prior windows end strictly before
    the current window.
    """
    n = len(prev)
    if n < horizon + min_hist:
        return None
    vols = [float(b.volume or 0.0) for b in prev]
    cur = sum(vols[-horizon:])
    hist: list[float] = []
    # Prior completed windows ending at indices horizon-1 .. n-horizon-1
    for end in range(horizon - 1, n - horizon):
        wsum = sum(vols[end - horizon + 1 : end + 1])
        if wsum > 0:
            hist.append(wsum)
    if len(hist) < min_hist:
        return None
    med = float(statistics.median(hist))
    if med <= 0:
        return None
    return cur / med


def vwap_extreme_agrees(
    prev: Sequence[Bar], fade: int, *, min_abs_pct: float
) -> bool:
    """True if |close/VWAP-1|*100 ≥ thresh and side matches fade extension."""
    if fade == 0 or min_abs_pct <= 0:
        return False
    vwap = session_vwap(prev)
    if vwap is None or vwap <= 0:
        return False
    vs_pct = 100.0 * (prev[-1].close / vwap - 1.0)
    if fade < 0:
        return vs_pct >= min_abs_pct
    return vs_pct <= -min_abs_pct


def fade_temp(prev: Sequence[Bar]) -> int:
    layer = fade_near_ext_from_bars(prev, midday_only=True)
    if not layer.ready or layer.temp is None:
        return 0
    return int(layer.temp)


def signal_temp(
    stock_prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    variant: Variant,
) -> int:
    if variant.kind == "fade":
        return fade_temp(stock_prev)

    fade = fade_temp(stock_prev)
    if fade == 0:
        return 0

    inside = idx_or_inside(stock_prev, bench_day)
    if inside is not True:
        return 0

    if variant.kind == "fade_idx_or_inside":
        return fade

    # idx_filt: champion + optional RVOL / VWAP
    if variant.rvol_min is not None or variant.rvol_max is not None:
        rvol = closed_30m_rvol(stock_prev)
        if rvol is None:
            return 0
        if variant.rvol_min is not None and rvol < variant.rvol_min:
            return 0
        if variant.rvol_max is not None and rvol > variant.rvol_max:
            return 0

    if variant.vwap_abs_pct is not None:
        if not vwap_extreme_agrees(
            stock_prev, fade, min_abs_pct=variant.vwap_abs_pct
        ):
            return 0

    return fade


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
        out.append({"temp": int(temp), "fwd": fwd})
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
        out_vars[v.name] = {
            "IS": summarize(by_var[v.name]["IS"], flat_eps=flat_eps),
            "OOS": summarize(by_var[v.name]["OOS"], flat_eps=flat_eps),
        }
    return {
        "stock_id": stock_id,
        "n_days": len(days),
        "n_days_aligned_bench": n_aligned,
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
        "per_name": per_name,
    }


def stability_gates(
    pool_oos: dict[str, Any],
    *,
    hit_gate: float = HIT_GATE,
    name_pass_frac: float = NAME_PASS_FRAC_H2,
) -> dict[str, Any]:
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
    gate_hit = bool(hit is not None and hit >= hit_gate and nd_pool >= MIN_OOS_N)
    gate_stab = bool(n_names > 0 and frac >= name_pass_frac)
    gate_dom = bool(max_share <= MAX_NAME_SHARE)
    return {
        f"gate_oos_hit_ge{int(hit_gate)}_n500": gate_hit,
        "gate_name_stability": gate_stab,
        "gate_no_megacap_dom": gate_dom,
        "all_gates_pass": gate_hit and gate_stab and gate_dom,
        "name_pass_frac_req": name_pass_frac,
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


def leave_one_out_oos(
    stock_results: list[dict[str, Any]], variant: str
) -> list[dict[str, Any]]:
    sids = [s["stock_id"] for s in stock_results if not s.get("error")]
    out: list[dict[str, Any]] = []
    for drop in sids:
        kept = [
            s for s in stock_results if s.get("stock_id") != drop and not s.get("error")
        ]
        pool = pool_from_stocks(kept, variant, "OOS")
        out.append(
            {
                "drop": drop,
                "directed_hit_pct": pool["directed_hit_pct"],
                "n_directed": pool["n_directed"],
            }
        )
    return out


def write_md(payload: dict[str, Any], path: Path) -> None:
    m = payload["metric"]
    champ = payload["is_champion"]
    gates = payload["gates_h2"]
    gates_strict = payload["gates_strict"]
    champ_base = payload["champion_baseline"]
    lines = [
        "# H2 · Closed-30m RVOL / VWAP distance ∧ fade_idx_or_inside",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Hypothesis",
        "",
        "Adding **closed-30m relative volume** and/or **VWAP distance** filters "
        "to champion `fade_idx_or_inside` improves OOS directed hit **and** keeps "
        "n≥500 + multi-stock stability (majority of names OOS≥55%), vs champion "
        f"alone (~{champ_base['OOS']['directed_hit_pct']}% n={champ_base['OOS']['n_directed']} "
        "from TRACK_BETA_EMA).",
        "",
        "## Filters (PIT)",
        "",
        "- **Closed-30m RVOL**: `sum(vol[-6:]) / median(prior same-session "
        "6-bar volume sums)` (ends strictly before current window; min hist=3).",
        "- **VWAP extreme**: `|close/VWAP−1|×100 ≥ thresh` and side agrees with "
        "fade (near-high → above VWAP; near-low → below).",
        "- Base gate: midday `fade_near_ext` ∧ 0050 OR intact (`fade_idx_or_inside`).",
        "- Distinct from E4 TOD RVOL and E3 VWAP±2σ rejection stacks.",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: fade / RVOL / VWAP / idx OR from completed same-session bars `≤ i`.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; champion on **IS only**; claim on OOS.",
        f"- Universe (ex `{BENCH_5M}`): `{', '.join(m['stocks'])}`.",
        f"- Pre-registered variants: {m['n_variants']}.",
        f"- Window: **{m['start']} → {m['end']}**",
        f"- H2 gate: OOS≥**{int(HIT_GATE)}%** · n≥500 · majority "
        f"(≥{int(NAME_PASS_FRAC_H2*100)}%) names @≥{int(NAME_HIT_FLOOR)}% "
        f"(n≥{MIN_NAME_N}); also report strict ≥{int(NAME_PASS_FRAC_STRICT*100)}% "
        "name stab (prior tracks).",
        "",
        f"## Verdict: H2 filters beat champion + stable？ "
        f"**{payload['verdict']}**",
        "",
        f"- Champion baseline `fade_idx_or_inside` OOS: "
        f"**{champ_base['OOS']['directed_hit_pct']}%** "
        f"(n={champ_base['OOS']['n_directed']})",
        f"- IS champion (H2 grid): `{champ['name']}`",
        f"- Champion IS hit: **{champ['IS']['directed_hit_pct']}%** "
        f"(n={champ['IS']['n_directed']})",
        f"- Champion OOS hit: **{champ['OOS']['directed_hit_pct']}%** "
        f"(n={champ['OOS']['n_directed']})",
        f"- Δ OOS vs `fade_idx_or_inside`: **{payload['delta_vs_idx_oos_pp']}** pp",
        (
            f"- Best OOS exploratory: `{payload['best_oos_exploratory']['name']}` → "
            f"**{payload['best_oos_exploratory']['OOS']['directed_hit_pct']}%** "
            f"(n={payload['best_oos_exploratory']['OOS']['n_directed']})"
        ),
        (
            f"- Best OOS with n≥500: `{payload['best_thick_oos']['name']}` → "
            f"**{payload['best_thick_oos']['OOS']['directed_hit_pct']}%** "
            f"(n={payload['best_thick_oos']['OOS']['n_directed']})"
            if payload.get("best_thick_oos")
            else "- Best OOS with n≥500: **none**"
        ),
        f"- RVOL helped (best thick RVOL vs idx)? **{payload['rvol_helped']}**",
        f"- VWAP-ext helped (best thick VWAP vs idx)? **{payload['vwap_helped']}**",
        f"- Live adopt? **{payload['live_recommend']}**",
        "",
        "## Gates（IS champion OOS）",
        "",
        f"- H2 majority name stab: `{gates['all_gates_pass']}` "
        f"(name_pass={gates['n_names_pass']}/{gates['n_names']}="
        f"{gates['name_pass_frac']}; req≥{gates['name_pass_frac_req']})",
        f"- Strict 70% name stab (prior): `{gates_strict['all_gates_pass']}` "
        f"(name_pass={gates_strict['n_names_pass']}/{gates_strict['n_names']}="
        f"{gates_strict['name_pass_frac']})",
        f"- `gate_oos_hit_ge{int(HIT_GATE)}_n500`: "
        f"**{gates[f'gate_oos_hit_ge{int(HIT_GATE)}_n500']}**",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Pool leaderboard（OOS directed 排序）",
        "",
        "|variant|family|IS hit%|IS n|OOS hit%|OOS n|Δ vs idx|",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    idx_ref = champ_base["OOS"]["directed_hit_pct"]
    for row in payload["leaderboard"]:
        oos = row["OOS"]
        d_idx = (
            round(oos["directed_hit_pct"] - idx_ref, 2)
            if oos["directed_hit_pct"] is not None and idx_ref is not None
            else None
        )
        lines.append(
            f"|{row['name']}|{row['family']}|"
            f"{row['IS']['directed_hit_pct']}|{row['IS']['n_directed']}|"
            f"{oos['directed_hit_pct']}|{oos['n_directed']}|{d_idx}|"
        )

    lines += [
        "",
        f"## IS champion per-stock OOS · `{champ['name']}`",
        "",
        "|sid|OOS hit%|OOS n|E[signed]%|",
        "|---|---:|---:|---:|",
    ]
    for p in sorted(
        champ["OOS"].get("per_name") or [],
        key=lambda x: (-(x.get("directed_hit_pct") or 0), x["stock_id"]),
    ):
        lines.append(
            f"|{p['stock_id']}|{p.get('directed_hit_pct')}|"
            f"{p.get('n_directed')}|{p.get('expectancy_signed_pct')}|"
        )

    lines += [
        "",
        "## Champion baseline per-stock OOS · `fade_idx_or_inside`",
        "",
        "|sid|OOS hit%|OOS n|E[signed]%|",
        "|---|---:|---:|---:|",
    ]
    for p in sorted(
        champ_base["OOS"].get("per_name") or [],
        key=lambda x: (-(x.get("directed_hit_pct") or 0), x["stock_id"]),
    ):
        lines.append(
            f"|{p['stock_id']}|{p.get('directed_hit_pct')}|"
            f"{p.get('n_directed')}|{p.get('expectancy_signed_pct')}|"
        )

    loo = payload.get("leave_one_out_oos") or []
    if loo:
        lines += [
            "",
            f"## Leave-one-stock-out OOS · `{champ['name']}`",
            "",
            "|drop|OOS hit%|OOS n|",
            "|---|---:|---:|",
        ]
        for r in sorted(loo, key=lambda x: (x.get("directed_hit_pct") or 0)):
            lines.append(
                f"|{r['drop']}|{r.get('directed_hit_pct')}|{r.get('n_directed')}|"
            )

    lines += [
        "",
        "## Interpretation",
        "",
        payload["interpretation"],
        "",
        "## Claim",
        "",
        f"- `h2_support`: **{payload['claim']['h2_support']}**",
        f"- `beats_champion_oos_thick`: **{payload['claim']['beats_champion_oos_thick']}**",
        f"- `oos_stable_h2_majority`: **{payload['claim']['oos_stable_h2_majority']}**",
        f"- `oos_stable_strict_70`: **{payload['claim']['oos_stable_strict_70']}**",
        f"- `live_bias_updated`: **{payload['claim']['live_bias_updated']}**",
        f"- `order_adopted`: **{payload['claim']['order_adopted']}**",
        "",
        "## Artifacts",
        "",
        f"- JSON: `{payload['artifacts']['json']}`",
        f"- MD: `{payload['artifacts']['md']}`",
        f"- Runner: `scripts/research/run_ta_30m_h2_vol_vwap_fade.py`",
        "",
        f"Generated: `{payload['generated_at']}`",
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
    variants = VARIANTS

    conn = sqlite3.connect(str(args.db))
    print(f"load bench {BENCH_5M} …", flush=True)
    bench_days = load_days(conn, BENCH_5M, start=args.start, end=args.end)
    stock_results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"  run {sid} …", flush=True)
        stock_results.append(
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
        leaderboard.append(
            {
                "name": v.name,
                "family": v.family,
                "IS": pool_from_stocks(stock_results, v.name, "IS"),
                "OOS": pool_from_stocks(stock_results, v.name, "OOS"),
            }
        )
    by_name = {r["name"]: r for r in leaderboard}
    champ_base = by_name["fade_idx_or_inside"]
    idx_hit = champ_base["OOS"]["directed_hit_pct"]

    # IS lock: prefer thicker IS among non-baseline first; fall back to all.
    h2_rows = [r for r in leaderboard if r["family"] == "h2"]
    eligible = [
        r
        for r in h2_rows
        if (r["IS"].get("n_directed") or 0) >= MIN_IS_N_SELECT
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not eligible:
        eligible = [
            r
            for r in h2_rows
            if (r["IS"].get("n_directed") or 0) >= 100
            and r["IS"].get("directed_hit_pct") is not None
        ]
    if not eligible:
        eligible = [r for r in h2_rows if r["IS"].get("directed_hit_pct") is not None]
    if not eligible:
        eligible = [champ_base]

    champ_row = max(
        eligible, key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"])
    )
    gates_h2 = stability_gates(
        champ_row["OOS"], hit_gate=HIT_GATE, name_pass_frac=NAME_PASS_FRAC_H2
    )
    gates_strict = stability_gates(
        champ_row["OOS"], hit_gate=HIT_GATE, name_pass_frac=NAME_PASS_FRAC_STRICT
    )
    gates_base_h2 = stability_gates(
        champ_base["OOS"], hit_gate=HIT_GATE, name_pass_frac=NAME_PASS_FRAC_H2
    )

    best_oos = max(
        (r for r in leaderboard if r["OOS"].get("directed_hit_pct") is not None),
        key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"]["n_directed"]),
    )
    thick = [
        r
        for r in leaderboard
        if (r["OOS"].get("n_directed") or 0) >= MIN_OOS_N
        and r["OOS"].get("directed_hit_pct") is not None
    ]
    best_thick = (
        max(thick, key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"]["n_directed"]))
        if thick
        else None
    )

    def _helped(new: float | None, base: float | None, *, min_pp: float = 0.5) -> str:
        if new is None or base is None:
            return "UNKNOWN"
        d = new - base
        if d >= min_pp:
            return f"YES (+{d:.2f}pp vs idx {base}%)"
        if d > 0:
            return f"MARGINAL (+{d:.2f}pp)"
        return f"NO ({d:.2f}pp vs idx {base}%)"

    rvol_thick = [
        r
        for r in thick
        if r["name"].startswith("idx_rvol30_")
        and "vwap" not in r["name"]
    ]
    vwap_thick = [
        r
        for r in thick
        if r["name"].startswith("idx_vwap_ext_")
    ]
    best_rvol = (
        max(rvol_thick, key=lambda r: r["OOS"]["directed_hit_pct"])
        if rvol_thick
        else None
    )
    best_vwap = (
        max(vwap_thick, key=lambda r: r["OOS"]["directed_hit_pct"])
        if vwap_thick
        else None
    )
    rvol_helped = _helped(
        best_rvol["OOS"]["directed_hit_pct"] if best_rvol else None, idx_hit
    )
    vwap_helped = _helped(
        best_vwap["OOS"]["directed_hit_pct"] if best_vwap else None, idx_hit
    )

    delta_champ = None
    if champ_row["OOS"]["directed_hit_pct"] is not None and idx_hit is not None:
        delta_champ = round(champ_row["OOS"]["directed_hit_pct"] - idx_hit, 2)

    beats_thick = bool(
        best_thick
        and best_thick["name"] != "fade_idx_or_inside"
        and best_thick["name"] != "baseline_fade_near_ext"
        and (best_thick["OOS"]["directed_hit_pct"] or 0) > (idx_hit or 0)
        and stability_gates(
            best_thick["OOS"], hit_gate=HIT_GATE, name_pass_frac=NAME_PASS_FRAC_H2
        )["all_gates_pass"]
    )

    # Support if IS-locked H2 rule beats idx on OOS with n≥500 + H2 stab,
    # or any thick H2 rule clearly beats + stable (still report IS-lock honestly).
    is_champ_beats = bool(
        champ_row["family"] == "h2"
        and (champ_row["OOS"].get("n_directed") or 0) >= MIN_OOS_N
        and delta_champ is not None
        and delta_champ > 0
        and gates_h2["all_gates_pass"]
    )

    if is_champ_beats:
        verdict = "SUPPORT"
        h2_support = "support"
    elif beats_thick:
        verdict = "MIXED"
        h2_support = "mixed"
    elif (
        champ_row["family"] == "h2"
        and delta_champ is not None
        and delta_champ > 0
        and (champ_row["OOS"].get("n_directed") or 0) < MIN_OOS_N
    ):
        verdict = "MIXED"
        h2_support = "mixed"
    else:
        verdict = "REJECT"
        h2_support = "reject"

    live_recommend = (
        "NO — keep Live observe on bare `fade_idx_or_inside`; "
        "do not volume-/VWAP-gate fade30 from this pass."
        if h2_support != "support"
        else "CONDITIONAL — only after fresh holdout; not this pass alone."
    )

    loo = leave_one_out_oos(stock_results, champ_row["name"])
    loo_min = min((r["directed_hit_pct"] or 0) for r in loo) if loo else None

    interpretation = (
        f"Champion `fade_idx_or_inside` OOS={idx_hit}% "
        f"(n={champ_base['OOS']['n_directed']}; H2 majority gates="
        f"{gates_base_h2['all_gates_pass']}). "
        f"IS-locked H2 pick `{champ_row['name']}` OOS="
        f"{champ_row['OOS']['directed_hit_pct']}% "
        f"(n={champ_row['OOS']['n_directed']}; Δ={delta_champ}pp; "
        f"H2_gates={gates_h2['all_gates_pass']}; "
        f"strict70={gates_strict['all_gates_pass']}; "
        f"LOO min={loo_min}%). "
        f"Best thick RVOL: "
        f"{best_rvol['name'] + ' OOS=' + str(best_rvol['OOS']['directed_hit_pct']) + '%' if best_rvol else 'none (all thin)'}; "
        f"best thick VWAP: "
        f"{best_vwap['name'] + ' OOS=' + str(best_vwap['OOS']['directed_hit_pct']) + '%' if best_vwap else 'none (all thin)'}. "
        f"RVOL helped? {rvol_helped}. VWAP helped? {vwap_helped}. "
        "Same class risk as E3/E4: filters that lift point estimate often thin "
        "n below 500 or fail name stability. "
        f"Live: {live_recommend}"
    )

    leaderboard_sorted = sorted(
        leaderboard,
        key=lambda r: (
            -(r["OOS"]["directed_hit_pct"] or -1),
            -(r["OOS"]["n_directed"] or 0),
        ),
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "h2_vol_vwap_fade_30m.json"
    md_path = out_dir / "H2_VOL_VWAP_FADE.md"

    payload: dict[str, Any] = {
        "topic": "intraday-direction-thermometer · H2 closed-30m RVOL / VWAP",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metric": {
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": args.end,
            "stocks": list(stocks),
            "bench_5m": BENCH_5M,
            "n_variants": len(variants),
            "hit_gate": HIT_GATE,
            "name_pass_frac_h2": NAME_PASS_FRAC_H2,
            "name_pass_frac_strict": NAME_PASS_FRAC_STRICT,
            "variant_names": [v.name for v in variants],
            "rvol_def": "sum(vol[-6:]) / median(prior same-session 6-bar sums)",
            "vwap_def": "|close/VWAP-1|*100 ≥ thresh, side agrees with fade",
        },
        "verdict": verdict,
        "delta_vs_idx_oos_pp": delta_champ,
        "champion_baseline": {
            "name": "fade_idx_or_inside",
            "IS": champ_base["IS"],
            "OOS": champ_base["OOS"],
            "gates_h2": gates_base_h2,
        },
        "is_champion": {
            "name": champ_row["name"],
            "family": champ_row["family"],
            "IS": champ_row["IS"],
            "OOS": champ_row["OOS"],
        },
        "best_oos_exploratory": {
            "name": best_oos["name"],
            "OOS": best_oos["OOS"],
            "IS": best_oos["IS"],
        },
        "best_thick_oos": (
            {
                "name": best_thick["name"],
                "OOS": best_thick["OOS"],
                "IS": best_thick["IS"],
                "gates_h2": stability_gates(
                    best_thick["OOS"],
                    hit_gate=HIT_GATE,
                    name_pass_frac=NAME_PASS_FRAC_H2,
                ),
            }
            if best_thick
            else None
        ),
        "gates_h2": gates_h2,
        "gates_strict": gates_strict,
        "rvol_helped": rvol_helped,
        "vwap_helped": vwap_helped,
        "best_rvol_thick": (
            {"name": best_rvol["name"], "OOS": best_rvol["OOS"]} if best_rvol else None
        ),
        "best_vwap_thick": (
            {"name": best_vwap["name"], "OOS": best_vwap["OOS"]} if best_vwap else None
        ),
        "live_recommend": live_recommend,
        "leave_one_out_oos": loo,
        "leaderboard": leaderboard_sorted,
        "per_stock": stock_results,
        "interpretation": interpretation,
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
        },
        "claim": {
            "h2_support": h2_support,
            "beats_champion_oos_thick": beats_thick,
            "oos_stable_h2_majority": gates_h2["all_gates_pass"],
            "oos_stable_strict_70": gates_strict["all_gates_pass"],
            "live_bias_updated": False,
            "order_adopted": False,
        },
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, md_path)

    print(
        f"verdict={verdict} IS_champ={champ_row['name']} "
        f"IS={champ_row['IS']['directed_hit_pct']}% "
        f"OOS={champ_row['OOS']['directed_hit_pct']}% "
        f"Δvs_idx={delta_champ}pp gates_h2={gates_h2['all_gates_pass']}"
    )
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
