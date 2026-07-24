#!/usr/bin/env python3
"""E4 · TOD RVOL dry-up ∧ fade / fade_idx_or_inside · ~30m (research only).

Fixes prior exploratory ``fade_dryup_05`` OOS~68% **peek risk** (session-mean
dry-up was not IS champion; best-OOS was reported as if selected).

Protocol
--------
- Directed hit vs fwd ~30m (H=6 @ 5m); flat_eps=0.05%.
- IS ≤2025-09-30 / OOS >; **champion on IS only**; **one OOS claim**.
- PIT: completed bars ≤ i; TOD average uses **prior trading days only**.

TOD RVOL
--------
``rvol = bar.volume / median(same HH:MM volume over prior L days)``.
Dry-up when ``rvol ≤ dry_max`` (thresholds pre-registered; freeze via IS).

AND layers
----------
- ``fade_near_ext`` (midday)
- ``fade_idx_or_inside`` (0050 OR intact) — same definition as TRACK_VS_MARKET

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_track_e4_tod_rvol.py
"""

from __future__ import annotations

import argparse
import json
import statistics
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
FLAT_EPS_LOOSE = 0.001
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
MIN_IS_N_SELECT = 200
MIN_TOD_HIST = 5


@dataclass(frozen=True)
class Variant:
    name: str
    family: str  # baseline | fade_tod | idx_tod
    kind: str  # fade | idx_inside | fade_tod_dry | idx_tod_dry
    dry_max: float = 1.0
    tod_lookback: int = 20


# Pre-registered before any OOS champion claim (small grid · IS lock).
VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "baseline", "fade"),
    Variant("baseline_fade_idx_or_inside", "baseline", "idx_inside"),
    # fade ∧ TOD dry-up
    Variant("fade_tod_dry_0p5_L20", "fade_tod", "fade_tod_dry", dry_max=0.5, tod_lookback=20),
    Variant("fade_tod_dry_0p7_L20", "fade_tod", "fade_tod_dry", dry_max=0.7, tod_lookback=20),
    Variant("fade_tod_dry_1p0_L20", "fade_tod", "fade_tod_dry", dry_max=1.0, tod_lookback=20),
    Variant("fade_tod_dry_0p5_L10", "fade_tod", "fade_tod_dry", dry_max=0.5, tod_lookback=10),
    Variant("fade_tod_dry_0p7_L10", "fade_tod", "fade_tod_dry", dry_max=0.7, tod_lookback=10),
    # fade_idx_or_inside ∧ TOD dry-up (main E4 hypothesis)
    Variant("idx_tod_dry_0p5_L20", "idx_tod", "idx_tod_dry", dry_max=0.5, tod_lookback=20),
    Variant("idx_tod_dry_0p7_L20", "idx_tod", "idx_tod_dry", dry_max=0.7, tod_lookback=20),
    Variant("idx_tod_dry_1p0_L20", "idx_tod", "idx_tod_dry", dry_max=1.0, tod_lookback=20),
    Variant("idx_tod_dry_0p5_L10", "idx_tod", "idx_tod_dry", dry_max=0.5, tod_lookback=10),
    Variant("idx_tod_dry_0p7_L10", "idx_tod", "idx_tod_dry", dry_max=0.7, tod_lookback=10),
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


def build_tod_vol_index(days: dict[str, list[Bar]]) -> dict[str, dict[str, float]]:
    """trade_date -> HH:MM -> volume."""
    out: dict[str, dict[str, float]] = {}
    for td, bars in days.items():
        slot: dict[str, float] = {}
        for b in bars:
            hhmm = b.ts.strftime("%H:%M")
            slot[hhmm] = float(b.volume or 0.0)
        out[td] = slot
    return out


def tod_rvol(
    *,
    td: str,
    hhmm: str,
    vol: float,
    sorted_dates: Sequence[str],
    date_pos: dict[str, int],
    tod_index: dict[str, dict[str, float]],
    lookback: int,
    min_hist: int = MIN_TOD_HIST,
) -> float | None:
    """PIT relative volume vs same clock-bar median over prior days."""
    pos = date_pos.get(td)
    if pos is None or pos <= 0 or vol < 0:
        return None
    hist: list[float] = []
    for i in range(pos - 1, -1, -1):
        d = sorted_dates[i]
        v = tod_index.get(d, {}).get(hhmm)
        if v is not None and v > 0:
            hist.append(float(v))
        if len(hist) >= lookback:
            break
    if len(hist) < min_hist:
        return None
    med = float(statistics.median(hist))
    if med <= 0:
        return None
    return vol / med


def idx_or_inside(stock_prev: Sequence[Bar], bench_day: Sequence[Bar]) -> bool | None:
    """True if 0050 OR still intact at aligned PIT time; None if not ready."""
    if len(stock_prev) < TA30M_OR_BARS or not bench_day:
        return None
    t_end = stock_prev[-1].ts
    bprev = [b for b in bench_day if b.ts <= t_end]
    if len(bprev) < TA30M_OR_BARS:
        return None
    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return None
    or_hi, or_lo = or_hl
    bc = bprev[-1].close
    return or_lo <= bc <= or_hi


def fade_temp(prev: Sequence[Bar]) -> int:
    layer = fade_near_ext_from_bars(prev, midday_only=True)
    if not layer.ready or layer.temp is None:
        return 0
    return int(layer.temp)


def signal_temp(
    prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    variant: Variant,
    *,
    rvol: float | None,
) -> int:
    kind = variant.kind
    if kind == "fade":
        return fade_temp(prev)

    fade = fade_temp(prev)
    if fade == 0:
        return 0

    if kind == "idx_inside":
        inside = idx_or_inside(prev, bench_day)
        if inside is None:
            return 0
        return fade if inside else 0

    if kind in {"fade_tod_dry", "idx_tod_dry"}:
        if rvol is None or rvol <= 0:
            return 0
        if rvol > variant.dry_max:
            return 0
        if kind == "fade_tod_dry":
            return fade
        inside = idx_or_inside(prev, bench_day)
        if inside is None:
            return 0
        return fade if inside else 0

    raise ValueError(f"unknown kind: {kind}")


def eval_day(
    stock_bars: list[Bar],
    bench_bars: list[Bar],
    variant: Variant,
    *,
    td: str,
    sorted_dates: Sequence[str],
    date_pos: dict[str, int],
    tod_index: dict[str, dict[str, float]],
    horizon: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    need_rvol = variant.kind in {"fade_tod_dry", "idx_tod_dry"}
    for i in range(len(stock_bars)):
        if i + horizon >= len(stock_bars):
            break
        prev = stock_bars[: i + 1]
        rvol: float | None = None
        if need_rvol:
            b = stock_bars[i]
            rvol = tod_rvol(
                td=td,
                hhmm=b.ts.strftime("%H:%M"),
                vol=float(b.volume or 0.0),
                sorted_dates=sorted_dates,
                date_pos=date_pos,
                tod_index=tod_index,
                lookback=variant.tod_lookback,
            )
        temp = signal_temp(prev, bench_bars, variant, rvol=rvol)
        if temp == 0:
            continue
        c0 = stock_bars[i].close
        if not c0:
            continue
        fwd = stock_bars[i + horizon].close / c0 - 1.0
        out.append({"temp": int(temp), "fwd": fwd, "rvol": rvol})
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

    tod_index = build_tod_vol_index(days)
    sorted_dates = sorted(days.keys())
    date_pos = {d: i for i, d in enumerate(sorted_dates)}

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    for td, bars in sorted(days.items()):
        bench = bench_days.get(td) or []
        if not bench:
            continue
        split = "IS" if td <= is_end else "OOS"
        for v in variants:
            by_var[v.name][split].extend(
                eval_day(
                    bars,
                    bench,
                    v,
                    td=td,
                    sorted_dates=sorted_dates,
                    date_pos=date_pos,
                    tod_index=tod_index,
                    horizon=horizon,
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
    fade = payload["baselines"]["fade_near_ext"]
    idx = payload["baselines"]["fade_idx_or_inside"]
    prior = payload.get("prior_peek_note") or {}
    lines = [
        "# TRACK E4 · TOD RVOL dry-up ∧ idx_or_inside · ~30m",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Goal",
        "",
        "- Replace exploratory session-mean `fade_dryup_05` (OOS~68%, **peek risk**) "
        "with **time-of-day RVOL** dry-up.",
        "- AND with `fade_near_ext` and/or `fade_idx_or_inside`.",
        "- **Freeze thresholds on IS only**; single OOS claim.",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp ∈ {±1}` 且 "
        f"`|fwd| ≥ {m['flat_eps']}`；hit iff `sign(temp)==sign(fwd)`。",
        f"- **Forward**: `close[i+{m['horizon']}]/close[i]-1`（≈30 分 @ 5m）。",
        "- **TOD RVOL**: `vol / median(same HH:MM over prior L days)`；"
        f"min hist={MIN_TOD_HIST}；PIT（不含當日／未來日）。",
        "- **PIT**: fade / index OR 僅用當日已完成 bars `≤ i`。",
        f"- **IS / OOS**: IS ≤ `{m['is_end']}`；OOS `>`。選冠軍只看 IS；"
        "**主閘只認一次 OOS**。",
        f"- Bench: `{m['bench']}` 5m OR-inside（同 TRACK_VS_MARKET）。",
        f"- Universe (ex bench): `{', '.join(m['stocks'])}`",
        f"- Window: **{m['start']} → {m['end']}**",
        f"- Pre-registered variants: {len(payload['pool_leaderboard'])}",
        "",
        f"## Verdict: OOS≥70% + stable？ **{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- IS champion: `{champ['variant']}`",
        f"- Champion IS: **{champ['IS'].get('directed_hit_pct')}%** "
        f"(n={champ['IS'].get('n_directed')})",
        f"- Champion OOS: **{champ['OOS'].get('directed_hit_pct')}%** "
        f"(n={champ['OOS'].get('n_directed')})",
        f"- Best OOS in grid (**exploratory · not claim**): "
        f"`{payload['best_oos']['variant']}` → "
        f"**{payload['best_oos']['OOS'].get('directed_hit_pct')}%** "
        f"(n={payload['best_oos']['OOS'].get('n_directed')})",
        "",
        "## Baselines",
        "",
        f"- `fade_near_ext` OOS: **{fade['OOS'].get('directed_hit_pct')}%** "
        f"(n={fade['OOS'].get('n_directed')})",
        f"- `fade_idx_or_inside` OOS: **{idx['OOS'].get('directed_hit_pct')}%** "
        f"(n={idx['OOS'].get('n_directed')})",
        "",
        "## Prior peek note（session dry-up）",
        "",
        f"- Prior exploratory `fade_dryup_05` OOS≈{prior.get('oos_hit', 68)}% "
        f"(n≈{prior.get('oos_n', 796)}) was **best-OOS**, not IS champion "
        f"(`fade_ud_agree` IS-pick OOS≈59.8%) — peek risk.",
        "- E4 uses TOD median RVOL + IS-locked pick to close that gap.",
        "",
        "## Gates（IS champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Pool leaderboard（OOS directed 排序）",
        "",
        "|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload["pool_leaderboard"]:
        i, o = row["IS"], row["OOS"]
        lines.append(
            f"|{row['variant']}|{row['family']}|"
            f"{i.get('directed_hit_pct')}|{i.get('n_directed')}|"
            f"{o.get('directed_hit_pct')}|{o.get('n_directed')}|"
            f"{o.get('edge_vs_coin_pp')}|"
        )
    lines += [
        "",
        f"## IS champion per-stock OOS · `{champ['variant']}`",
        "",
        "|sid|OOS hit%|OOS n|E[signed]%|",
        "|---|---:|---:|---:|",
    ]
    for p in sorted(
        champ["OOS"].get("per_name") or [],
        key=lambda x: -(x.get("directed_hit_pct") or 0),
    ):
        lines.append(
            f"|{p['stock_id']}|{p.get('directed_hit_pct')}|"
            f"{p.get('n_directed')}|{p.get('expectancy_signed_pct')}|"
        )
    lines += [
        "",
        "## Sensitivity · flat_eps=0.10%",
        "",
    ]
    sens = payload.get("sensitivity_flat_eps_0p10") or {}
    if sens:
        lines.append(
            f"- Same IS champion OOS @ flat 0.10%: "
            f"**{sens.get('directed_hit_pct')}%** (n={sens.get('n_directed')})"
        )
    else:
        lines.append("- (not run)")
    lines += [
        "",
        "## Honest note",
        "",
        payload.get("failure_note_zh") or "—",
        "",
        "## Do not",
        "",
        "- 未過閘寫入 `strategy.yaml` / Order live",
        "- 用格子最佳 OOS 當宣稱（即先前 dry-up peek）",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Runner: `scripts/research/run_ta_30m_track_e4_tod_rvol.py`",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="")
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--horizon", type=int, default=TA30M_HORIZON_BARS)
    ap.add_argument("--flat-eps", type=float, default=FLAT_EPS)
    ap.add_argument(
        "--out-json",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "track_e4_tod_rvol.json"
        ),
    )
    ap.add_argument(
        "--out-md",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "TRACK_E4_TOD_RVOL.md"
        ),
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    end = args.end or conn.execute(
        "SELECT MAX(trade_date) FROM stock_kbar_5m"
    ).fetchone()[0]
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    variants = VARIANTS

    print(f"load bench {BENCH_5M} ...", flush=True)
    bench_days = load_days(conn, BENCH_5M, start=args.start, end=end)

    results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"eval {sid} {args.start}→{end} IS≤{args.is_end} ...", flush=True)
        results.append(
            run_stock(
                conn,
                sid,
                bench_days,
                start=args.start,
                end=end,
                is_end=args.is_end,
                variants=variants,
                horizon=args.horizon,
                flat_eps=args.flat_eps,
            )
        )

    pool_rows: list[dict[str, Any]] = []
    for v in variants:
        is_m = pool_from_stocks(results, v.name, "IS")
        oos_m = pool_from_stocks(results, v.name, "OOS")
        pool_rows.append(
            {
                "variant": v.name,
                "family": v.family,
                "IS": {k: is_m[k] for k in is_m if k != "per_name"},
                "OOS": {k: oos_m[k] for k in oos_m if k != "per_name"},
                "_is_full": is_m,
                "_oos_full": oos_m,
            }
        )

    selectable = [
        r
        for r in pool_rows
        if (r["IS"].get("n_directed") or 0) >= MIN_IS_N_SELECT
        and r["IS"].get("directed_hit_pct") is not None
    ]
    selectable.sort(
        key=lambda r: (
            -(r["IS"]["directed_hit_pct"] or 0),
            -(r["IS"]["n_directed"] or 0),
        )
    )
    champ_row = selectable[0] if selectable else pool_rows[0]
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

    fade_row = next(r for r in pool_rows if r["variant"] == "baseline_fade_near_ext")
    idx_row = next(
        r for r in pool_rows if r["variant"] == "baseline_fade_idx_or_inside"
    )

    print(f"sensitivity flat_eps={FLAT_EPS_LOOSE} on champion...", flush=True)
    champ_v = next(v for v in variants if v.name == champ["variant"])
    sens_rows_oos: list[dict[str, Any]] = []
    for s in results:
        if s.get("error"):
            continue
        days = load_days(conn, s["stock_id"], start=args.start, end=end)
        tod_index = build_tod_vol_index(days)
        sorted_dates = sorted(days.keys())
        date_pos = {d: i for i, d in enumerate(sorted_dates)}
        for td, bars in days.items():
            if td <= args.is_end:
                continue
            bench = bench_days.get(td) or []
            if not bench:
                continue
            sens_rows_oos.extend(
                eval_day(
                    bars,
                    bench,
                    champ_v,
                    td=td,
                    sorted_dates=sorted_dates,
                    date_pos=date_pos,
                    tod_index=tod_index,
                    horizon=args.horizon,
                )
            )
    sens = summarize(sens_rows_oos, flat_eps=FLAT_EPS_LOOSE)

    oos_hit = champ["OOS"].get("directed_hit_pct")
    best_hit = best_oos["OOS"].get("directed_hit_pct")
    fade_oos = fade_row["OOS"].get("directed_hit_pct")
    idx_oos = idx_row["OOS"].get("directed_hit_pct")
    failure_note = (
        f"IS 冠軍 `{champ['variant']}` OOS={oos_hit}% "
        f"(n={champ['OOS'].get('n_directed')})；"
        f"格子最佳 OOS（exploratory）`{best_oos['variant']}`={best_hit}% "
        f"(n={best_oos['OOS'].get('n_directed')})。"
        f" 裸 fade OOS={fade_oos}%；fade_idx_or_inside OOS={idx_oos}%。"
        " 閘門只認 IS 冠軍之一次 OOS；不採 best-OOS 當宣稱（修 dry-up peek）。"
    )

    leaderboard = []
    for r in sorted(
        pool_rows,
        key=lambda x: (
            -(x["OOS"].get("directed_hit_pct") or 0),
            -(x["OOS"].get("n_directed") or 0),
        ),
    ):
        leaderboard.append(
            {
                "variant": r["variant"],
                "family": r["family"],
                "IS": r["IS"],
                "OOS": r["OOS"],
            }
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layer": "research",
        "track": "e4_tod_rvol",
        "status": "observe_only",
        "metric": {
            "name": "directed_hit_rate_ta30m_e4_tod_rvol",
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": end,
            "stocks": stocks,
            "bench": BENCH_5M,
        },
        "is_champion": {
            "variant": champ["variant"],
            "IS": {k: champ["IS"][k] for k in champ["IS"] if k != "per_name"},
            "OOS": champ["OOS"],
        },
        "best_oos": {
            "variant": best_oos["variant"],
            "OOS": best_oos["OOS"],
            "IS": best_oos["IS"],
            "note": "exploratory_not_claim",
        },
        "baselines": {
            "fade_near_ext": {
                "variant": "baseline_fade_near_ext",
                "IS": fade_row["IS"],
                "OOS": fade_row["OOS"],
            },
            "fade_idx_or_inside": {
                "variant": "baseline_fade_idx_or_inside",
                "IS": idx_row["IS"],
                "OOS": idx_row["OOS"],
            },
        },
        "prior_peek_note": {
            "variant": "fade_dryup_05",
            "oos_hit": 68.09,
            "oos_n": 796,
            "was_is_champion": False,
            "is_champion_was": "fade_ud_agree",
        },
        "gates": gates,
        "pool_leaderboard": leaderboard,
        "per_stock": [
            {
                "stock_id": s["stock_id"],
                "n_days": s.get("n_days"),
                "date_min": s.get("date_min"),
                "date_max": s.get("date_max"),
                "error": s.get("error"),
                "champion": (s.get("variants") or {}).get(champ["variant"]),
            }
            for s in results
        ],
        "sensitivity_flat_eps_0p10": sens,
        "failure_note_zh": failure_note,
    }

    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, out_md)

    print(
        f"VERDICT gates={gates['all_gates_pass']} "
        f"champ={champ['variant']} "
        f"IS={champ['IS'].get('directed_hit_pct')}% "
        f"OOS={champ['OOS'].get('directed_hit_pct')}% "
        f"n={champ['OOS'].get('n_directed')} "
        f"best_oos={best_oos['variant']}:{best_oos['OOS'].get('directed_hit_pct')}% "
        f"fade={fade_oos}% idx={idx_oos}%",
        flush=True,
    )
    print(f"wrote {out_md}", flush=True)
    print(f"wrote {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
