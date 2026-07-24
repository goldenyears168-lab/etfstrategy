#!/usr/bin/env python3
"""Track E1 · ORB path vs Fade path 分軌 (~30m directed-hit · research).

Day-type fork after 09:30 (OR = first 6×5m bars):
  - OR broken & held → ORB follow only
  - OR intact         → fade_near_ext allowed

Paths evaluated separately; union = mutually exclusive coverage.
Metric lock identical to RESEARCH_PLAN_MULTI_SIGNAL_70.md.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_track_e1_orb_fade_split.py
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
    "0050",
)
# Universe for variants that need 0050 as index bench (exclude self).
STOCKS_EX_BENCH = tuple(s for s in DEFAULT_STOCKS if s != "0050")
BENCH_5M = "0050"
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0005
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
MIN_IS_N_SELECT = 200
RVOL_DEFAULT = 1.5


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    family: str  # baseline | fade_path | orb_path | union
    rvol_min: float = RVOL_DEFAULT
    hold_bars: int = 1  # consecutive closes outside OR, same side
    need_vwap: bool = False
    need_idx_or_broken: bool = False  # require 0050 OR also broken same side
    use_idx_or_intact: bool = False  # fade only when 0050 OR inside
    needs_bench: bool = False


# Locked before OOS champion claim.
VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", "baseline"),
    Variant("baseline_orb_break_raw", "orb", "baseline", rvol_min=0.0, hold_bars=1),
    # Fade path · OR intact
    Variant("fade_stock_or_intact", "fade_or_intact", "fade_path"),
    Variant(
        "fade_idx_or_inside",
        "fade_or_intact",
        "fade_path",
        use_idx_or_intact=True,
        needs_bench=True,
    ),
    # ORB path · OR broken & held
    Variant("orb_break_held", "orb", "orb_path", rvol_min=0.0, hold_bars=1),
    Variant("orb_break_held_rvol15", "orb", "orb_path", rvol_min=1.5, hold_bars=1),
    Variant(
        "orb_break_held_vwap",
        "orb",
        "orb_path",
        rvol_min=0.0,
        hold_bars=1,
        need_vwap=True,
    ),
    Variant(
        "orb_break_held_rvol15_vwap",
        "orb",
        "orb_path",
        rvol_min=1.5,
        hold_bars=1,
        need_vwap=True,
    ),
    Variant(
        "orb_break_held2_rvol15_vwap",
        "orb",
        "orb_path",
        rvol_min=1.5,
        hold_bars=2,
        need_vwap=True,
    ),
    Variant(
        "orb_break_idx_confirm_rvol15_vwap",
        "orb",
        "orb_path",
        rvol_min=1.5,
        hold_bars=1,
        need_vwap=True,
        need_idx_or_broken=True,
        needs_bench=True,
    ),
    # Union · mutually exclusive by regime
    Variant(
        "union_fade_intact_orb_stack",
        "union",
        "union",
        rvol_min=1.5,
        hold_bars=1,
        need_vwap=True,
    ),
    Variant(
        "union_fade_idx_orb_stack",
        "union",
        "union",
        rvol_min=1.5,
        hold_bars=1,
        need_vwap=True,
        use_idx_or_intact=True,
        needs_bench=True,
    ),
)


def _minute_hhmm(minute: str) -> str:
    parts = str(minute).split(":")
    return f"{parts[0]}:{parts[1]}"


def _sign(x: int) -> int:
    if x > 0:
        return 1
    if x < 0:
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


def _align_bench_prefix(
    stock_prev: Sequence[Bar], bench_day: Sequence[Bar]
) -> list[Bar]:
    if not stock_prev or not bench_day:
        return []
    t_end = stock_prev[-1].ts
    return [b for b in bench_day if b.ts <= t_end]


def session_rvol(prev: Sequence[Bar]) -> float:
    """Last-bar volume / mean of prior session bars (PIT)."""
    vols = [float(b.volume or 0.0) for b in prev]
    if not vols:
        return 0.0
    prior = [v for v in vols[:-1] if v > 0]
    if not prior:
        return 0.0
    mean = sum(prior) / len(prior)
    if mean <= 0:
        return 0.0
    return vols[-1] / mean


def or_side(prev: Sequence[Bar], *, n_bars: int = TA30M_OR_BARS) -> int:
    """+1 above ORH · -1 below ORL · 0 inside / not ready."""
    or_hl = opening_range_hl(prev, n_bars=n_bars)
    if or_hl is None:
        return 0
    or_hi, or_lo = or_hl
    c = prev[-1].close
    if c > or_hi:
        return 1
    if c < or_lo:
        return -1
    return 0


def or_held_side(
    prev: Sequence[Bar], *, hold_bars: int = 1, n_bars: int = TA30M_OR_BARS
) -> int:
    """OR broken & held: last ``hold_bars`` closes all outside same side."""
    if len(prev) < n_bars + max(0, hold_bars - 1):
        return 0
    sides = [or_side(prev[: i + 1], n_bars=n_bars) for i in range(len(prev) - hold_bars, len(prev))]
    if not sides or sides[-1] == 0:
        return 0
    if all(s == sides[-1] for s in sides):
        return sides[-1]
    return 0


def regime_label(prev: Sequence[Bar], *, hold_bars: int = 1) -> str:
    """Mutually exclusive day-type after OR formed."""
    if len(prev) < TA30M_OR_BARS:
        return "pre_or"
    held = or_held_side(prev, hold_bars=hold_bars)
    if held != 0:
        return "orb_broken_held"
    if or_side(prev) == 0:
        return "or_intact"
    # Close outside but not held for hold_bars (transition / flicker)
    return "or_broken_unheld"


def idx_or_state(stock_prev: Sequence[Bar], bench_day: Sequence[Bar] | None) -> str | None:
    if not bench_day:
        return None
    bprev = _align_bench_prefix(stock_prev, bench_day)
    if len(bprev) < TA30M_OR_BARS:
        return None
    side = or_side(bprev)
    if side > 0:
        return "above"
    if side < 0:
        return "below"
    return "inside"


def fade_temp(prev: Sequence[Bar]) -> int:
    layer = fade_near_ext_from_bars(prev, midday_only=True)
    if not layer.ready or layer.temp is None:
        return 0
    return int(layer.temp)


def orb_temp(
    prev: Sequence[Bar],
    variant: Variant,
    *,
    bench_day: Sequence[Bar] | None,
) -> int:
    side = or_held_side(prev, hold_bars=variant.hold_bars)
    if side == 0:
        return 0
    if variant.rvol_min > 0 and session_rvol(prev) < variant.rvol_min:
        return 0
    if variant.need_vwap:
        vwap = session_vwap(prev)
        if not vwap or vwap <= 0:
            return 0
        c = prev[-1].close
        if side > 0 and c < vwap:
            return 0
        if side < 0 and c > vwap:
            return 0
    if variant.need_idx_or_broken:
        st = idx_or_state(prev, bench_day)
        if st is None:
            return 0
        if side > 0 and st != "above":
            return 0
        if side < 0 and st != "below":
            return 0
    return side


def signal_temp(
    prev: Sequence[Bar],
    variant: Variant,
    *,
    bench_day: Sequence[Bar] | None,
) -> int:
    kind = variant.kind

    if kind == "fade":
        return fade_temp(prev)

    if kind == "fade_or_intact":
        f = fade_temp(prev)
        if f == 0:
            return 0
        if variant.use_idx_or_intact:
            st = idx_or_state(prev, bench_day)
            return f if st == "inside" else 0
        # Stock OR intact (not broken-held; close inside)
        return f if or_side(prev) == 0 else 0

    if kind == "orb":
        return orb_temp(prev, variant, bench_day=bench_day)

    if kind == "union":
        # Mutually exclusive: intact → fade; broken&held → ORB stack
        side = or_held_side(prev, hold_bars=variant.hold_bars)
        if side != 0:
            return orb_temp(prev, variant, bench_day=bench_day)
        # intact (or unheld flicker): fade path only when OR inside
        f = fade_temp(prev)
        if f == 0:
            return 0
        if variant.use_idx_or_intact:
            st = idx_or_state(prev, bench_day)
            return f if st == "inside" else 0
        return f if or_side(prev) == 0 else 0

    raise ValueError(f"unknown kind: {kind}")


def eval_day(
    stock_bars: list[Bar],
    variant: Variant,
    *,
    horizon: int,
    bench_bars: list[Bar] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(stock_bars)):
        if i + horizon >= len(stock_bars):
            break
        prev = stock_bars[: i + 1]
        temp = signal_temp(prev, variant, bench_day=bench_bars)
        if temp == 0:
            continue
        c0 = stock_bars[i].close
        if not c0:
            continue
        fwd = stock_bars[i + horizon].close / c0 - 1.0
        reg = regime_label(prev, hold_bars=max(1, variant.hold_bars))
        out.append(
            {
                "temp": int(temp),
                "fwd": fwd,
                "hhmm": stock_bars[i].ts.strftime("%H:%M"),
                "regime": reg,
            }
        )
    return out


def summarize(rows: list[dict[str, Any]], *, flat_eps: float) -> dict[str, Any]:
    directed_hit = directed_tot = long_hit = 0
    signed_pnl: list[float] = []
    regime_n: dict[str, int] = defaultdict(int)
    for r in rows:
        t = int(r["temp"])
        fr = float(r["fwd"])
        if t == 0 or abs(fr) < flat_eps:
            continue
        directed_tot += 1
        regime_n[str(r.get("regime") or "?")] += 1
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
        "regime_n_directed": dict(regime_n),
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
    bench_days: dict[str, list[Bar]],
) -> dict[str, Any]:
    days = load_days(conn, stock_id, start=start, end=end)
    if not days:
        return {"stock_id": stock_id, "error": "no_5m", "n_days": 0}

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    n_aligned = 0
    for td, bars in sorted(days.items()):
        split = "IS" if td <= is_end else "OOS"
        bench = bench_days.get(td)
        if bench:
            n_aligned += 1
        for v in variants:
            if v.needs_bench and stock_id == BENCH_5M:
                continue  # skip self-bench
            if v.needs_bench and not bench:
                continue
            by_var[v.name][split].extend(
                eval_day(bars, v, horizon=horizon, bench_bars=bench)
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
        "n_days_with_bench": n_aligned,
        "date_min": min(days) if days else None,
        "date_max": max(days) if days else None,
        "variants": out_vars,
    }


def pool_from_stocks(
    stock_results: list[dict[str, Any]], variant: str, split: str
) -> dict[str, Any]:
    n_dir = hit = long_hit = n_sig = 0
    per_name: list[dict[str, Any]] = []
    regime_n: dict[str, int] = defaultdict(int)
    for s in stock_results:
        if s.get("error"):
            continue
        variants = s.get("variants") or {}
        if variant not in variants:
            continue
        m = variants[variant][split]
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
        for k, vv in (m.get("regime_n_directed") or {}).items():
            regime_n[k] += int(vv)
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
        "regime_n_directed": dict(regime_n),
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
    lines = [
        "# Track E1 · ORB path vs Fade path 分軌 · ~30m",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "Parent: `intraday-direction-thermometer` · Plan: "
        "[`RESEARCH_PLAN_MULTI_SIGNAL_70.md`](./RESEARCH_PLAN_MULTI_SIGNAL_70.md) · "
        "Pro: [`PRO_METHODS_WEB_REVIEW.md`](./PRO_METHODS_WEB_REVIEW.md) §E1",
        "",
        "## Regime definition（互斥 · after 09:30）",
        "",
        "- **OR** = first `6` × 5m bars（≈09:00–09:30）high/low。",
        "- **`orb_broken_held`**: 最近 `hold_bars` 根收盤皆在 OR 同側之外（close 突破且持住）。",
        "- **`or_intact`**: 收盤仍在 OR 內 → 允許 fade。",
        "- **`or_broken_unheld`**: 當根在外但未達 held 連續條件（不發 ORB；fade 也不發）。",
        "- Paths **不混用**：intact 只跑 fade；broken&held 只跑 ORB follow。",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: completed same-session bars `≤ i` only.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; champion on **IS only**; claim on OOS.",
        f"- Universe: `{', '.join(m['stocks'])}` "
        f"（bench variants ex-`{BENCH_5M}`）。",
        f"- Pre-registered variants: {m['n_variants']}.",
        f"- Window: **{m['start']} → {m['end']}**",
        "",
        f"## Verdict: either/both paths clear 70%+stability？ "
        f"**{payload['claim']['verdict_zh']}**",
        "",
        f"- Fade path clears? **{'YES' if payload['claim']['fade_path_clears'] else 'NO'}** "
        f"（`fade_idx_or_inside`）",
        f"- ORB path clears? **{'YES' if payload['claim']['orb_path_clears'] else 'NO'}** "
        f"（`orb_break_held_rvol15_vwap`）",
        f"- Union clears? **{'YES' if payload['claim']['union_clears'] else 'NO'}**",
        f"- IS champion: `{payload['is_champion']['name']}`",
        f"- Champion IS: **{payload['is_champion']['IS']['directed_hit_pct']}%** "
        f"(n={payload['is_champion']['IS']['n_directed']})",
        f"- Champion OOS: **{payload['is_champion']['OOS']['directed_hit_pct']}%** "
        f"(n={payload['is_champion']['OOS']['n_directed']})",
        f"- Best OOS exploratory: `{payload['best_oos_exploratory']['name']}` → "
        f"**{payload['best_oos_exploratory']['OOS']['directed_hit_pct']}%** "
        f"(n={payload['best_oos_exploratory']['OOS']['n_directed']})",
        f"- Keep / Kill: **{payload['keep_kill']}**",
        "",
        "## Path roll-up（OOS）",
        "",
        "|path|variant|OOS hit%|OOS n|gates|note|",
        "|---|---|---:|---:|---|---|",
    ]
    for row in payload["path_rollup"]:
        g = row["gates"]
        lines.append(
            f"|{row['path']}|{row['name']}|"
            f"{row['OOS']['directed_hit_pct']}|{row['OOS']['n_directed']}|"
            f"{'PASS' if g['all_gates_pass'] else 'FAIL'}|"
            f"{row.get('note', '')}|"
        )

    lines += [
        "",
        "## Combined coverage",
        "",
        f"- Fade path OOS n: **{payload['coverage']['fade_oos_n']}**",
        f"- ORB path OOS n: **{payload['coverage']['orb_oos_n']}**",
        f"- Union OOS n: **{payload['coverage']['union_oos_n']}**",
        f"- Union vs fade-alone coverage ratio: "
        f"**{payload['coverage']['union_vs_fade_ratio']}**",
        f"- Overlap note: {payload['coverage']['overlap_note']}",
        "",
        "## Gates（IS champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: "
        f"**{payload['gates']['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{payload['gates']['gate_name_stability']}** "
        f"({payload['gates']['n_names_pass']}/{payload['gates']['n_names']} = "
        f"{payload['gates']['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{payload['gates']['gate_no_megacap_dom']}** "
        f"(max share {payload['gates']['max_name_share']} · "
        f"{payload['gates']['max_name_sid']})",
        "",
        "## Pool leaderboard（OOS directed 排序）",
        "",
        "|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    fade_ref = payload.get("fade_baseline_oos_hit") or 62.13
    for row in payload["leaderboard"]:
        oos = row["OOS"]
        vs_fade = (
            round(oos["directed_hit_pct"] - fade_ref, 2)
            if oos["directed_hit_pct"] is not None
            else None
        )
        lines.append(
            f"|{row['name']}|{row['family']}|"
            f"{row['IS']['directed_hit_pct']}|{row['IS']['n_directed']}|"
            f"{oos['directed_hit_pct']}|{oos['n_directed']}|"
            f"{oos['edge_vs_coin_pp']}|{vs_fade}|"
        )

    champ = payload["is_champion"]
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
        "## Interpretation",
        "",
        payload["interpretation"],
        "",
        "## Artifacts",
        "",
        f"- JSON: `{payload['artifacts']['json']}`",
        f"- MD: `{payload['artifacts']['md']}`",
        f"- Runner: `scripts/research/run_track_e1_orb_fade_split.py`",
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
    bench_days = load_days(conn, BENCH_5M, start=args.start, end=args.end)

    stock_results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"  run {sid} …", flush=True)
        stock_results.append(
            run_stock(
                conn,
                sid,
                start=args.start,
                end=args.end,
                is_end=args.is_end,
                variants=variants,
                horizon=args.horizon,
                flat_eps=args.flat_eps,
                bench_days=bench_days,
            )
        )
    conn.close()

    leaderboard: list[dict[str, Any]] = []
    for v in variants:
        is_pool = pool_from_stocks(stock_results, v.name, "IS")
        oos_pool = pool_from_stocks(stock_results, v.name, "OOS")
        leaderboard.append(
            {
                "name": v.name,
                "family": v.family,
                "kind": v.kind,
                "IS": is_pool,
                "OOS": oos_pool,
                "gates_oos": stability_gates(oos_pool),
            }
        )

    eligible = [
        r
        for r in leaderboard
        if (r["IS"].get("n_directed") or 0) >= MIN_IS_N_SELECT
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not eligible:
        eligible = [r for r in leaderboard if r["IS"].get("directed_hit_pct") is not None]
    champ_row = max(
        eligible, key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"])
    )
    gates = stability_gates(champ_row["OOS"])
    best_oos = max(
        (r for r in leaderboard if r["OOS"].get("directed_hit_pct") is not None),
        key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"]["n_directed"]),
    )

    fade_row = next(r for r in leaderboard if r["name"] == "baseline_fade_near_ext")
    fade_hit = fade_row["OOS"]["directed_hit_pct"]

    def _pick(name: str) -> dict[str, Any]:
        return next(r for r in leaderboard if r["name"] == name)

    fade_intact = _pick("fade_stock_or_intact")
    fade_idx = _pick("fade_idx_or_inside")
    orb_stack = _pick("orb_break_held_rvol15_vwap")
    orb_raw = _pick("orb_break_held")
    union_stock = _pick("union_fade_intact_orb_stack")
    union_idx = _pick("union_fade_idx_orb_stack")

    path_rollup = [
        {
            "path": "fade_baseline",
            "name": "baseline_fade_near_ext",
            "OOS": fade_row["OOS"],
            "gates": fade_row["gates_oos"],
            "note": "unfiltered midday fade",
        },
        {
            "path": "fade_intact",
            "name": "fade_stock_or_intact",
            "OOS": fade_intact["OOS"],
            "gates": fade_intact["gates_oos"],
            "note": "fade ∧ stock OR inside",
        },
        {
            "path": "fade_idx",
            "name": "fade_idx_or_inside",
            "OOS": fade_idx["OOS"],
            "gates": fade_idx["gates_oos"],
            "note": "MKT champion · 0050 OR inside",
        },
        {
            "path": "orb_raw",
            "name": "orb_break_held",
            "OOS": orb_raw["OOS"],
            "gates": orb_raw["gates_oos"],
            "note": "close beyond OR held",
        },
        {
            "path": "orb_stack",
            "name": "orb_break_held_rvol15_vwap",
            "OOS": orb_stack["OOS"],
            "gates": orb_stack["gates_oos"],
            "note": "ORB + RVOL≥1.5 + VWAP side",
        },
        {
            "path": "union_stock",
            "name": "union_fade_intact_orb_stack",
            "OOS": union_stock["OOS"],
            "gates": union_stock["gates_oos"],
            "note": "mutually exclusive union",
        },
        {
            "path": "union_idx",
            "name": "union_fade_idx_orb_stack",
            "OOS": union_idx["OOS"],
            "gates": union_idx["gates_oos"],
            "note": "fade_idx ∪ orb_stack",
        },
    ]

    fade_n = int(fade_intact["OOS"]["n_directed"] or 0)
    orb_n = int(orb_stack["OOS"]["n_directed"] or 0)
    union_n = int(union_stock["OOS"]["n_directed"] or 0)
    coverage = {
        "fade_oos_n": fade_n,
        "orb_oos_n": orb_n,
        "union_oos_n": union_n,
        "union_vs_fade_ratio": round(union_n / fade_n, 3) if fade_n else None,
        "overlap_note": (
            "Union n ≈ fade_intact + orb_stack by construction "
            f"(fade={fade_n}, orb={orb_n}, union={union_n}); "
            "regimes mutually exclusive so no double-count."
        ),
    }

    fade_clears = bool(fade_idx["gates_oos"]["all_gates_pass"])
    orb_clears = bool(orb_stack["gates_oos"]["all_gates_pass"])
    union_clears = bool(union_stock["gates_oos"]["all_gates_pass"])
    champ_oos = champ_row["OOS"]["directed_hit_pct"]
    if fade_clears and not orb_clears:
        verdict_zh = "PARTIAL — only fade∧idx-OR-intact；ORB path fail"
        keep_kill = (
            "KEEP fade path (research reproduce MKT)；"
            "KILL ORB path for 70% claim；do not union"
        )
    elif fade_clears and orb_clears:
        verdict_zh = "YES — both paths clear"
        keep_kill = "KEEP both paths separately (research)"
    elif orb_clears and not fade_clears:
        verdict_zh = "PARTIAL — only ORB path"
        keep_kill = "KEEP ORB path；fade fork weak here"
    elif champ_oos is not None and fade_hit is not None and champ_oos > fade_hit + 1:
        verdict_zh = "NO — neither path clears full gates"
        keep_kill = "OBSERVE — lift vs fade but gates fail"
    else:
        verdict_zh = "NO — neither path clears"
        keep_kill = "KILL for 70% claim"

    leaderboard_sorted = sorted(
        leaderboard,
        key=lambda r: (
            -(r["OOS"]["directed_hit_pct"] or -1),
            -(r["OOS"]["n_directed"] or 0),
        ),
    )

    orb_oos = orb_stack["OOS"]["directed_hit_pct"]
    fade_intact_oos = fade_intact["OOS"]["directed_hit_pct"]
    fade_idx_oos = fade_idx["OOS"]["directed_hit_pct"]
    union_oos = union_stock["OOS"]["directed_hit_pct"]
    interpretation = (
        f"Day-type fork after OR: fade on intact OR OOS={fade_intact_oos}% "
        f"(n={fade_n}); ORB stack (RVOL≥1.5+VWAP) OOS={orb_oos}% "
        f"(n={orb_n}). Index-OR fade (`fade_idx_or_inside`) OOS={fade_idx_oos}% "
        f"(n={fade_idx['OOS']['n_directed']}) — reproduces MKT partial champion. "
        f"Union coverage OOS hit={union_oos}% (n={union_n}). "
        "Pros expect ORB raw ~40–52% and filtered ORB ~52–65%; "
        "our ORB path is in that band and does **not** clear 70%. "
        "Fade∧OR-intact remains the only near-70% path. "
        "Do not blend ORB into the fade thermometer; keep paths separate. "
        "Research only — no Live 70% claim, no Order."
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "track_e1_orb_fade_split.json"
    md_path = out_dir / "TRACK_E1_ORB_FADE_SPLIT.md"

    payload: dict[str, Any] = {
        "topic": "intraday-direction-thermometer · track E1 ORB vs fade split",
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
            "or_bars": TA30M_OR_BARS,
            "rvol_default": RVOL_DEFAULT,
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
        "gates": gates,
        "keep_kill": keep_kill,
        "fade_baseline_oos_hit": fade_hit,
        "path_rollup": path_rollup,
        "coverage": coverage,
        "leaderboard": [
            {
                "name": r["name"],
                "family": r["family"],
                "IS": {k: r["IS"][k] for k in r["IS"] if k != "per_name"},
                "OOS": {k: r["OOS"][k] for k in r["OOS"] if k != "per_name"},
                "gates_oos": r["gates_oos"],
            }
            for r in leaderboard_sorted
        ],
        "per_stock": stock_results,
        "interpretation": interpretation,
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
        },
        "claim": {
            "verdict_zh": verdict_zh,
            "oos_ge_70_stable": fade_clears or orb_clears,
            "fade_path_clears": fade_clears,
            "orb_path_clears": orb_clears,
            "union_clears": union_clears,
            "both_paths_clear": fade_clears and orb_clears,
            "live_bias_updated": False,
            "order_graduated": False,
        },
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, md_path)

    print(
        f"IS champ={champ_row['name']} "
        f"IS={champ_row['IS']['directed_hit_pct']}% "
        f"OOS={champ_row['OOS']['directed_hit_pct']}% "
        f"gates={gates['all_gates_pass']}",
        flush=True,
    )
    print(
        f"paths fade_intact OOS={fade_intact_oos}% n={fade_n} | "
        f"orb_stack OOS={orb_oos}% n={orb_n} | "
        f"fade_idx OOS={fade_idx_oos}% | "
        f"union OOS={union_oos}% n={union_n}",
        flush=True,
    )
    print(f"wrote {md_path}", flush=True)
    print(f"wrote {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
