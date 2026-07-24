#!/usr/bin/env python3
"""Track E2 · Explicit failed-OR-break fade · ~30m directed-hit (research).

Event (PIT · first reclaim only)
--------------------------------
After 09:00–09:30 OR is set: same-bar rejection (wick beyond OR + close inside)
or first close back inside within N after a closed excursion. Signal = fade
opposite pierce (toward OR mid / VWAP). Optional weak poke RVOL ≤ 1.0 and
idx OR-inside AND.

Baselines: ``fade_near_ext``, ``fade_idx_or_inside`` (0050 proxy OR intact).
IS-lock once among fail-break family; single OOS claim read.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_track_e2_fail_break.py
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
    fade_near_ext_from_bars,
    opening_range_hl,
    or_fail_break_temp,
)

# Same pool as TRACK_VS_MARKET (0050 is intraday OR proxy, not a name).
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
MIN_IS_N_SELECT = 200


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str  # fade | fade_idx_or_inside | fail_break
    family: str  # baseline | fail_break
    reclaim_within: int = 2
    weak_vol_max_rvol: float | None = None
    midday_only: bool = False
    require_beyond_or_mid: bool = False
    require_vwap_side: bool = False
    and_idx_or_inside: bool = False


# Locked before any OOS champion claim.
VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", "baseline"),
    Variant("fade_idx_or_inside", "fade_idx_or_inside", "baseline"),
    Variant("fb_n1", "fail_break", "fail_break", reclaim_within=1),
    Variant("fb_n2", "fail_break", "fail_break", reclaim_within=2),
    Variant("fb_n3", "fail_break", "fail_break", reclaim_within=3),
    Variant(
        "fb_n2_weak1p0",
        "fail_break",
        "fail_break",
        reclaim_within=2,
        weak_vol_max_rvol=1.0,
    ),
    Variant(
        "fb_n2_weak1p0_midday",
        "fail_break",
        "fail_break",
        reclaim_within=2,
        weak_vol_max_rvol=1.0,
        midday_only=True,
    ),
    Variant(
        "fb_n2_weak1p0_beyond_mid",
        "fail_break",
        "fail_break",
        reclaim_within=2,
        weak_vol_max_rvol=1.0,
        require_beyond_or_mid=True,
    ),
    Variant(
        "fb_n2_weak1p0_vwap_side",
        "fail_break",
        "fail_break",
        reclaim_within=2,
        weak_vol_max_rvol=1.0,
        require_vwap_side=True,
    ),
    Variant(
        "fb_n2_weak1p0_and_idx_or_inside",
        "fail_break",
        "fail_break",
        reclaim_within=2,
        weak_vol_max_rvol=1.0,
        and_idx_or_inside=True,
    ),
    Variant(
        "fb_n3_weak1p0_and_idx_or_inside",
        "fail_break",
        "fail_break",
        reclaim_within=3,
        weak_vol_max_rvol=1.0,
        and_idx_or_inside=True,
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


def _idx_or_inside(bench_day: Sequence[Bar], n_stock: int) -> bool:
    if n_stock <= 0 or len(bench_day) < n_stock:
        return False
    bprev = bench_day[:n_stock]
    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return False
    or_hi, or_lo = or_hl
    bc = bprev[-1].close
    return or_lo <= bc <= or_hi


def signal_temp(
    stock_prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    variant: Variant,
) -> int:
    if variant.kind == "fade":
        layer = fade_near_ext_from_bars(stock_prev, midday_only=True)
        if not layer.ready or layer.temp is None:
            return 0
        return int(layer.temp)

    if variant.kind == "fade_idx_or_inside":
        layer = fade_near_ext_from_bars(stock_prev, midday_only=True)
        if not layer.ready or layer.temp is None or int(layer.temp) == 0:
            return 0
        if not _idx_or_inside(bench_day, len(stock_prev)):
            return 0
        return int(layer.temp)

    temp = or_fail_break_temp(
        stock_prev,
        reclaim_within=variant.reclaim_within,
        weak_vol_max_rvol=variant.weak_vol_max_rvol,
        midday_only=variant.midday_only,
        require_beyond_or_mid=variant.require_beyond_or_mid,
        require_vwap_side=variant.require_vwap_side,
    )
    if temp == 0:
        return 0
    if variant.and_idx_or_inside and not _idx_or_inside(bench_day, len(stock_prev)):
        return 0
    return temp


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
    gates = payload["gates"]
    lines = [
        "# Track E2 · Explicit failed-OR-break fade · ~30m",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Hypothesis",
        "",
        "Event-defined fail-break (wick beyond OR → close back inside within N, "
        "weak poke volume preferred) should beat continuous `fade_near_ext` and "
        "match or lift `fade_idx_or_inside` (~70.7% prior on 0050 proxy).",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: stock (+bench for idx filter) uses completed same-session bars `≤ i`.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; **fail_break family** champion on IS only; "
        "one OOS claim read.",
        f"- Universe (ex `{BENCH_5M}` proxy): `{', '.join(m['stocks'])}`.",
        f"- Pre-registered variants: {m['n_variants']}.",
        f"- Window: **{m['start']} → {m['end']}**",
        "",
        f"## Verdict: OOS≥70% + stable？ "
        f"**{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- IS champion (fail_break*): `{champ['name']}`",
        f"- Champion IS hit: **{champ['IS']['directed_hit_pct']}%** "
        f"(n={champ['IS']['n_directed']})",
        f"- Champion OOS hit: **{champ['OOS']['directed_hit_pct']}%** "
        f"(n={champ['OOS']['n_directed']})",
        f"- Baseline `fade_near_ext` OOS: "
        f"**{payload['baseline_fade']['OOS']['directed_hit_pct']}%** "
        f"(n={payload['baseline_fade']['OOS']['n_directed']})",
        f"- Baseline `fade_idx_or_inside` OOS: "
        f"**{payload['baseline_idx']['OOS']['directed_hit_pct']}%** "
        f"(n={payload['baseline_idx']['OOS']['n_directed']})",
        f"- Beats fade_idx_or_inside on OOS? **{payload['beats_idx_or_inside']}**",
        f"- Helps toward 70%? **{payload['helps_to_70']}**",
        "",
        "## Gates（IS-locked fail-break champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Pool leaderboard（OOS directed 排序）",
        "",
        "|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|Δ vs idx_or|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    fade_ref = payload["baseline_fade"]["OOS"]["directed_hit_pct"] or 0.0
    idx_ref = payload["baseline_idx"]["OOS"]["directed_hit_pct"] or 0.0
    for row in payload["leaderboard"]:
        oos = row["OOS"]
        hit = oos["directed_hit_pct"]
        vs_fade = round(hit - fade_ref, 2) if hit is not None else None
        vs_idx = round(hit - idx_ref, 2) if hit is not None else None
        lines.append(
            f"|{row['name']}|{row['family']}|"
            f"{row['IS']['directed_hit_pct']}|{row['IS']['n_directed']}|"
            f"{hit}|{oos['n_directed']}|"
            f"{oos['edge_vs_coin_pp']}|{vs_fade}|{vs_idx}|"
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
        "## Interpretation",
        "",
        payload["interpretation"],
        "",
        "## Artifacts",
        "",
        f"- JSON: `{payload['artifacts']['json']}`",
        f"- MD: `{payload['artifacts']['md']}`",
        f"- Runner: `scripts/research/run_ta_30m_track_e2_fail_break.py`",
        f"- Helper: `or_fail_break_temp` in "
        "`src/research/intraday_direction_thermometer.py`",
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
        is_pool = pool_from_stocks(stock_results, v.name, "IS")
        oos_pool = pool_from_stocks(stock_results, v.name, "OOS")
        leaderboard.append(
            {
                "name": v.name,
                "family": v.family,
                "IS": is_pool,
                "OOS": oos_pool,
            }
        )

    fb_eligible = [
        r
        for r in leaderboard
        if r["family"] == "fail_break"
        and (r["IS"].get("n_directed") or 0) >= MIN_IS_N_SELECT
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not fb_eligible:
        fb_eligible = [
            r
            for r in leaderboard
            if r["family"] == "fail_break"
            and r["IS"].get("directed_hit_pct") is not None
        ]
    if not fb_eligible:
        raise SystemExit("no fail_break variants with IS hits")

    champ_row = max(
        fb_eligible,
        key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"]),
    )
    gates = stability_gates(champ_row["OOS"])

    baseline_fade = next(r for r in leaderboard if r["name"] == "baseline_fade_near_ext")
    baseline_idx = next(r for r in leaderboard if r["name"] == "fade_idx_or_inside")

    champ_oos = champ_row["OOS"]["directed_hit_pct"]
    idx_oos = baseline_idx["OOS"]["directed_hit_pct"]
    beats = (
        champ_oos is not None
        and idx_oos is not None
        and champ_oos > idx_oos
        and (champ_row["OOS"]["n_directed"] or 0) >= 100
    )
    helps = (
        "YES — OOS≥70% + stability gates pass"
        if gates["all_gates_pass"]
        else (
            "PARTIAL — hit≥70% but stability/n fail"
            if gates["gate_oos_hit_ge70_n500"] and not gates["all_gates_pass"]
            else "NO — fail-break IS champ does not clear OOS≥70%+stability"
        )
    )

    interpretation = (
        f"IS-locked fail-break champion `{champ_row['name']}` → "
        f"OOS directed hit {champ_oos}% (n={champ_row['OOS']['n_directed']}). "
        f"Baselines: fade_near_ext OOS {baseline_fade['OOS']['directed_hit_pct']}% "
        f"(n={baseline_fade['OOS']['n_directed']}); "
        f"fade_idx_or_inside OOS {idx_oos}% "
        f"(n={baseline_idx['OOS']['n_directed']}). "
        "Event is first-reclaim / rejection only (not sticky post-pierce spam). "
        "Across the pre-registered fail_break grid, IS+OOS directed hits cluster "
        "~37–43% — **anti-edge vs fade** (pierce-then-reclaim more often continues "
        "the break over fwd~30m on this TW 5m pool). Weak-vol / mid / VWAP / "
        "idx-OR-inside ANDs do not lift fade above chance. "
        "**Kill E2 fade path** vs continuous `fade_near_ext` / `fade_idx_or_inside`. "
        "Research only — no Live 70% claim, no Order."
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "track_e2_fail_break.json"
    md_path = args.out_dir / "TRACK_E2_FAIL_BREAK.md"
    payload: dict[str, Any] = {
        "metric": {
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": args.end,
            "stocks": list(stocks),
            "n_variants": len(variants),
            "bench": BENCH_5M,
        },
        "is_champion": champ_row,
        "gates": gates,
        "baseline_fade": baseline_fade,
        "baseline_idx": baseline_idx,
        "beats_idx_or_inside": bool(beats),
        "helps_to_70": helps,
        "leaderboard": sorted(
            leaderboard,
            key=lambda r: (
                -(r["OOS"]["directed_hit_pct"] or -1),
                -(r["OOS"]["n_directed"] or 0),
            ),
        ),
        "stocks": stock_results,
        "interpretation": interpretation,
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
        },
        "claim": {
            "oos_ge_70_stable": gates["all_gates_pass"],
            "research_only": True,
            "not_order": True,
        },
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, md_path)
    print(
        f"champ={champ_row['name']} IS={champ_row['IS']['directed_hit_pct']}% "
        f"OOS={champ_oos}% gates={gates['all_gates_pass']}",
        flush=True,
    )
    print(f"wrote {md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
