#!/usr/bin/env python3
"""H3: beta-adjusted residual short-mom vs raw short-mom (research).

Hypothesis
----------
Stock 1–2m return minus β×0050 1–2m return predicts **forward 5–15 minutes**
better than raw short-mom. Gate: OOS directed hit ≥55%, n≥1000, multi-name
stability.

Prior TRACK_BETA_EMA on fade30 (~30m) residual filters hurt / coin-flip;
this retests residual on the **Live short-mom horizon** (1m bars · fwd 5–15m).

Baselines: raw_mom, mkt_mom, resid_beta1 (β≡1), always-long null.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_h3_residual_short_mom.py
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
    SHORT_BETA_LOOKBACK,
    SHORT_MOM_EPS_PCT,
    SHORT_RESID_EPS_PCT,
    Bar,
)

# Swing-eval liquid names (0050 is bench only).
DEFAULT_STOCKS = (
    "2327",
    "8046",
    "3189",
    "6451",
    "2330",
    "2303",
    "2454",
)
BENCH_1M = "0050"
DEFAULT_START = "2024-01-02"
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0003  # 3bp — 1m noise floor for 5–15m moves
MIN_BARS_PER_DAY = 120
MIN_OOS_N = 1000
MIN_NAME_N = 80
NAME_HIT_FLOOR = 52.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
HIT_GATE = 55.0
HORIZONS = (5, 10, 15)  # 1m bars → minutes
MOM_BARS_OPTIONS = (1, 2)
SKIP_OPEN_BARS = 5  # skip first ~5m after open


@dataclass(frozen=True)
class Variant:
    name: str
    key: str  # field on short_residual_mom_from_bars
    mom_bars: int
    family: str = "signal"


def _variants() -> tuple[Variant, ...]:
    out: list[Variant] = []
    for mb in MOM_BARS_OPTIONS:
        out.extend(
            [
                Variant(f"raw_mom_{mb}", "raw_mom", mb, "baseline"),
                Variant(f"mkt_mom_{mb}", "mkt_mom", mb, "baseline"),
                Variant(f"resid_mom_{mb}", "resid_mom", mb, "h3"),
                Variant(f"resid_beta1_mom_{mb}", "resid_beta1_mom", mb, "baseline"),
            ]
        )
    return tuple(out)


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


def align_day(
    stock: Sequence[Bar], bench: Sequence[Bar]
) -> tuple[list[Bar], list[Bar]]:
    """Inner-join by timestamp; keep chronological order."""
    bmap = {b.ts: b for b in bench}
    s_out: list[Bar] = []
    b_out: list[Bar] = []
    for s in stock:
        b = bmap.get(s.ts)
        if b is None:
            continue
        s_out.append(s)
        b_out.append(b)
    return s_out, b_out


def _sign_eps(x: float, eps: float) -> int:
    if x > eps:
        return 1
    if x < -eps:
        return -1
    return 0


def _rolling_beta_at(
    s_rets: Sequence[float],
    b_rets: Sequence[float],
    *,
    end_i: int,
    lookback: int,
) -> float | None:
    """OLS beta using returns with indices (end_i-lookback+1 .. end_i) inclusive.

    ``end_i`` is the index of the last 1-bar return (from bar end_i → end_i+1
    in the close array, i.e. after close index ``end_i+1``).
    """
    if end_i + 1 < lookback:
        return None
    s = list(s_rets[end_i - lookback + 1 : end_i + 1])
    b = list(b_rets[end_i - lookback + 1 : end_i + 1])
    n = len(s)
    if n < max(10, lookback // 2):
        return None
    mean_b = sum(b) / n
    mean_s = sum(s) / n
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_b <= 1e-18:
        return None
    cov = sum((s[i] - mean_s) * (b[i] - mean_b) for i in range(n))
    return cov / var_b


def eval_day_aligned(
    stock: list[Bar],
    bench: list[Bar],
    *,
    mom_bars: int,
    horizons: Sequence[int],
    beta_lookback: int,
    skip_open: int,
    mom_eps_pct: float = SHORT_MOM_EPS_PCT,
    resid_eps_pct: float = SHORT_RESID_EPS_PCT,
) -> dict[str, dict[int, list[dict[str, Any]]]]:
    """Return {variant_key: {H: rows}} for one day (aligned 1m).

    Fast path: operate on close/return arrays (same PIT math as helper).
    """
    keys = ("raw_mom", "mkt_mom", "resid_mom", "resid_beta1_mom")
    out: dict[str, dict[int, list[dict[str, Any]]]] = {
        k: {h: [] for h in horizons} for k in keys
    }
    n = len(stock)
    if n != len(bench) or n < beta_lookback + 2:
        return out
    sc = [b.close for b in stock]
    bc = [b.close for b in bench]
    s_rets = [
        (sc[i] / sc[i - 1] - 1.0) if sc[i - 1] else 0.0 for i in range(1, n)
    ]
    b_rets = [
        (bc[i] / bc[i - 1] - 1.0) if bc[i - 1] else 0.0 for i in range(1, n)
    ]
    mb = int(mom_bars)
    max_h = max(horizons)
    # bar index i (0-based); need mb prior closes and beta_lookback returns.
    i0 = max(skip_open, beta_lookback, mb)
    for i in range(i0, n - max_h):
        s0, s1 = sc[i - mb], sc[i]
        b0, b1 = bc[i - mb], bc[i]
        if not s0 or not b0:
            continue
        raw_pct = 100.0 * (s1 / s0 - 1.0)
        mkt_pct = 100.0 * (b1 / b0 - 1.0)
        # Last return index used for beta ends at i-1 (return from i-1→i).
        beta = _rolling_beta_at(
            s_rets, b_rets, end_i=i - 1, lookback=beta_lookback
        )
        if beta is None:
            continue
        # Multi-bar residual matches rolling_beta_residual: r_s - beta * r_b
        resid_pct = raw_pct - 100.0 * beta * (b1 / b0 - 1.0)
        resid_b1 = raw_pct - mkt_pct
        signs = {
            "raw_mom": _sign_eps(raw_pct, mom_eps_pct),
            "mkt_mom": _sign_eps(mkt_pct, mom_eps_pct),
            "resid_mom": _sign_eps(resid_pct, resid_eps_pct),
            "resid_beta1_mom": _sign_eps(resid_b1, resid_eps_pct),
        }
        c0 = sc[i]
        if not c0:
            continue
        for h in horizons:
            fwd = sc[i + h] / c0 - 1.0
            for k in keys:
                temp = signs[k]
                if temp == 0:
                    continue
                out[k][h].append({"temp": temp, "fwd": fwd})
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


def stability_gates(
    pool: dict[str, Any],
    per_name: dict[str, dict[str, Any]],
    *,
    hit_gate: float,
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
    horizons: Sequence[int],
    flat_eps: float,
    beta_lookback: int,
    skip_open: int,
) -> dict[str, Any]:
    end_s = end or "2099-12-31"
    print(f"loading bench {BENCH_1M} 1m …", flush=True)
    bench_days = load_days_1m(conn, BENCH_1M, start=start, end=end_s)
    print(f"  bench days={len(bench_days)}", flush=True)

    variants = _variants()
    # Accumulate rows: variant_name -> split -> rows; also per stock.
    pool: dict[str, dict[str, dict[int, list[dict[str, Any]]]]] = {
        v.name: {"IS": {h: [] for h in horizons}, "OOS": {h: [] for h in horizons}}
        for v in variants
    }
    per_stock: dict[str, dict[str, dict[str, dict[int, list[dict[str, Any]]]]]] = {}

    for sid in stocks:
        print(f"stock {sid} …", flush=True)
        days = load_days_1m(conn, sid, start=start, end=end_s)
        per_stock[sid] = {
            v.name: {
                "IS": {h: [] for h in horizons},
                "OOS": {h: [] for h in horizons},
            }
            for v in variants
        }
        n_al = 0
        for td, sbars in sorted(days.items()):
            bbars = bench_days.get(td)
            if not bbars:
                continue
            s_al, b_al = align_day(sbars, bbars)
            if len(s_al) < MIN_BARS_PER_DAY:
                continue
            n_al += 1
            split = "IS" if td <= is_end else "OOS"
            for mb in MOM_BARS_OPTIONS:
                day_pack = eval_day_aligned(
                    s_al,
                    b_al,
                    mom_bars=mb,
                    horizons=horizons,
                    beta_lookback=beta_lookback,
                    skip_open=skip_open,
                )
                for v in variants:
                    if v.mom_bars != mb:
                        continue
                    for h in horizons:
                        rows = day_pack[v.key][h]
                        pool[v.name][split][h].extend(rows)
                        per_stock[sid][v.name][split][h].extend(rows)
        print(f"  aligned_days={n_al}", flush=True)

    # Summaries
    leaderboard: list[dict[str, Any]] = []
    for v in variants:
        for h in horizons:
            is_sum = summarize(pool[v.name]["IS"][h], flat_eps=flat_eps)
            oos_sum = summarize(pool[v.name]["OOS"][h], flat_eps=flat_eps)
            name_oos = {
                sid: summarize(per_stock[sid][v.name]["OOS"][h], flat_eps=flat_eps)
                for sid in stocks
            }
            gates = stability_gates(oos_sum, name_oos, hit_gate=HIT_GATE)
            leaderboard.append(
                {
                    "name": v.name,
                    "family": v.family,
                    "mom_bars": v.mom_bars,
                    "horizon_m": h,
                    "IS": is_sum,
                    "OOS": oos_sum,
                    "gates": gates,
                    "per_name_oos": name_oos,
                }
            )

    return {
        "variants": [v.name for v in variants],
        "horizons": list(horizons),
        "stocks": list(stocks),
        "leaderboard": leaderboard,
        "bench": BENCH_1M,
    }


def _md_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| variant | H(m) | OOS hit% | OOS n | vs coin | vs AL | IS hit% | gates |",
        "|---|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in rows:
        o = r["OOS"]
        i = r["IS"]
        g = "PASS" if r["gates"]["all_gates_pass"] else "fail"
        lines.append(
            f"| `{r['name']}` | {r['horizon_m']} | "
            f"{o.get('directed_hit_pct')} | {o.get('n_directed')} | "
            f"{o.get('edge_vs_coin_pp')} | {o.get('edge_vs_always_long_pp')} | "
            f"{i.get('directed_hit_pct')} | {g} |"
        )
    return "\n".join(lines)


def write_report(payload: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "h3_residual_short_mom.json"
    md_path = out_dir / "H3_RESIDUAL_SHORT_MOM.md"

    lb = payload["leaderboard"]
    # Best OOS resid vs raw by horizon
    by_h: dict[int, dict[str, Any]] = {}
    for h in payload["horizons"]:
        cand = [r for r in lb if r["horizon_m"] == h]
        resid = [r for r in cand if r["family"] == "h3"]
        raw = [r for r in cand if r["name"].startswith("raw_mom_")]
        best_resid = max(
            resid,
            key=lambda r: (
                r["OOS"].get("directed_hit_pct") or -1,
                r["OOS"].get("n_directed") or 0,
            ),
        )
        best_raw = max(
            raw,
            key=lambda r: (
                r["OOS"].get("directed_hit_pct") or -1,
                r["OOS"].get("n_directed") or 0,
            ),
        )
        by_h[h] = {"best_resid": best_resid, "best_raw": best_raw}

    any_pass = any(r["gates"]["all_gates_pass"] for r in lb if r["family"] == "h3")
    # Residual must beat raw on same mom_bars+horizon for support
    beat_raw_count = 0
    compare_n = 0
    for r in lb:
        if r["family"] != "h3":
            continue
        mb = r["mom_bars"]
        h = r["horizon_m"]
        raw = next(
            (
                x
                for x in lb
                if x["name"] == f"raw_mom_{mb}" and x["horizon_m"] == h
            ),
            None,
        )
        if raw is None:
            continue
        compare_n += 1
        rh = r["OOS"].get("directed_hit_pct")
        raw_h = raw["OOS"].get("directed_hit_pct")
        if rh is not None and raw_h is not None and rh > raw_h + 0.5:
            beat_raw_count += 1

    if any_pass and beat_raw_count >= max(1, compare_n // 2):
        verdict = "SUPPORT"
        verdict_note = (
            "At least one residual variant clears OOS≥55% / n≥1000 / name "
            "stability and residual beats raw on ≥ half of (mom_bars×H) cells."
        )
    else:
        # Surface anti-momentum if residual/raw sit well below coin.
        resid_hits = [
            r["OOS"].get("directed_hit_pct")
            for r in lb
            if r["family"] == "h3" and r["OOS"].get("directed_hit_pct") is not None
        ]
        mean_hit = sum(resid_hits) / len(resid_hits) if resid_hits else None
        if mean_hit is not None and mean_hit < 45.0:
            verdict = "REJECT"
            verdict_note = (
                f"Residual short-mom is **anti-predictive** on fwd 5–15m "
                f"(mean OOS directed ≈{mean_hit:.1f}% ≪ 50%). It beats raw "
                f"by >0.5pp on {beat_raw_count}/{compare_n} cells but never "
                f"approaches the ≥55% gate. Same qualitative failure mode as "
                f"TRACK_BETA_EMA on fade30: β-residual is not a free follow "
                f"edge — here both raw and residual look like short-horizon "
                f"mean-reversion (fade short-mom ≈ 58–65% if inverted)."
            )
        elif any(
            (r["OOS"].get("directed_hit_pct") or 0) >= HIT_GATE
            and (r["OOS"].get("n_directed") or 0) >= MIN_OOS_N
            for r in lb
            if r["family"] == "h3"
        ):
            verdict = "REJECT (gates incomplete)"
            verdict_note = (
                "Residual reaches hit/n on some cells but fails multi-name "
                "stability and/or does not clearly beat raw short-mom."
            )
        else:
            verdict = "REJECT"
            verdict_note = (
                "Residual short-mom does not clear OOS directed ≥55% with n≥1000. "
                "Aligns with TRACK_BETA_EMA: beta residual is not a free edge "
                "on this path."
            )

    sorted_lb = sorted(
        lb,
        key=lambda r: (
            -(r["OOS"].get("directed_hit_pct") or -1),
            -(r["OOS"].get("n_directed") or 0),
        ),
    )

    best_cells = []
    for h, pack in by_h.items():
        br = pack["best_resid"]
        bw = pack["best_raw"]
        best_cells.append(
            f"- **H={h}m**: best resid `{br['name']}` OOS "
            f"{br['OOS'].get('directed_hit_pct')}% n={br['OOS'].get('n_directed')} "
            f"(gates={'PASS' if br['gates']['all_gates_pass'] else 'fail'}); "
            f"best raw `{bw['name']}` OOS {bw['OOS'].get('directed_hit_pct')}% "
            f"n={bw['OOS'].get('n_directed')}"
        )

    # Live implication: only suggest residual columns if residual is useful
    # as a *follow* signal (above coin and beating raw), not merely "less bad".
    resid_edges = [
        r["OOS"].get("edge_vs_coin_pp")
        for r in lb
        if r["family"] == "h3" and r["OOS"].get("edge_vs_coin_pp") is not None
    ]
    raw_edges = [
        r["OOS"].get("edge_vs_coin_pp")
        for r in lb
        if r["name"].startswith("raw_mom_")
        and r["OOS"].get("edge_vs_coin_pp") is not None
    ]
    mean_resid = sum(resid_edges) / len(resid_edges) if resid_edges else None
    mean_raw = sum(raw_edges) / len(raw_edges) if raw_edges else None
    if (
        mean_resid is not None
        and mean_resid >= 5.0
        and mean_raw is not None
        and mean_resid > mean_raw + 0.5
    ):
        live_impl = (
            "Residual short-mom shows a usable lift vs raw on this sample — "
            "consider **observe-only** residual columns beside Live "
            "`mom_1bar_pct` / `mom_2bar_pct`; do **not** Order-graduate."
        )
    elif mean_resid is not None and mean_resid <= -5.0:
        live_impl = (
            "Keep Live short-mom as **raw** 1–2m diagnostic columns "
            "(`mom_1bar_pct` / `mom_2bar_pct`). Do **not** add β-residual "
            "short-mom as a follow-signal for 5–15m. Both raw and residual "
            "are mean-reverting on this horizon (directed hit ≪ 50%); if "
            "Live uses short-mom for action, treat it as a **fade / exhaustion** "
            "hint, not continuation — and still research-only until a separate "
            "fade gate is frozen."
        )
    else:
        live_impl = (
            "Keep Live short-mom as **raw** 1–2m columns "
            "(`mom_1bar_pct` / `mom_2bar_pct`). Do **not** replace with "
            "β-residual short-mom for Live decisions; residual did not earn "
            "a clear OOS follow-edge on 5–15m forwards."
        )

    meta = payload["metric"]
    md = f"""# H3 · Residual short momentum (fwd 5–15m)

**Verdict: {verdict}**

{verdict_note}

## Setup

| Item | Value |
|---|---|
| Bars | `stock_kbar_1m` · bench `{meta['bench']}` |
| Universe | {", ".join(meta["stocks"])} |
| Window | {meta["start"]} → {meta["end"] or "max"} · IS ≤ {meta["is_end"]} |
| Mom lookback | 1 and 2 completed 1m bars (Live-aligned) |
| β | rolling OLS · lookback={meta["beta_lookback"]} 1-bar returns · same session PIT |
| Forward | H ∈ {{{", ".join(str(x) for x in meta["horizons"])}}} minutes |
| Flat eps | {meta["flat_eps"]} ({meta["flat_eps"]*10000:.1f} bp) |
| Skip open | first {meta["skip_open"]} 1m bars |
| Gate | OOS directed ≥{HIT_GATE}% · n≥{MIN_OOS_N} · ≥{int(NAME_PASS_FRAC*100)}% names ≥{NAME_HIT_FLOOR}% · max name share ≤{int(MAX_NAME_SHARE*100)}% |

PIT: signal at bar `i` uses only completed same-session bars `≤ i`.
No Order · research only. Prior fade30 `TRACK_BETA_EMA` residual filters
are **out of scope** here (different horizon / path).

## Best OOS by horizon

{chr(10).join(best_cells)}

Residual beat raw by >0.5pp on **{beat_raw_count}/{compare_n}** (mom_bars×H) cells.

## Leaderboard (OOS sorted)

{_md_table(sorted_lb)}

## Implication for Live short-mom columns

{live_impl}

## Repro

```bash
PYTHONPATH=src .venv/bin/python scripts/research/run_h3_residual_short_mom.py
```

Artifacts: `h3_residual_short_mom.json` · this file.
"""

    payload_out = {
        **payload,
        "verdict": verdict,
        "verdict_note": verdict_note,
        "live_implication": live_impl,
        "best_by_horizon": {
            str(h): {
                "resid": {
                    "name": p["best_resid"]["name"],
                    "OOS": p["best_resid"]["OOS"],
                    "gates_pass": p["best_resid"]["gates"]["all_gates_pass"],
                },
                "raw": {
                    "name": p["best_raw"]["name"],
                    "OOS": p["best_raw"]["OOS"],
                },
            }
            for h, p in by_h.items()
        },
        "resid_beat_raw_cells": f"{beat_raw_count}/{compare_n}",
    }
    # Drop heavy per_name row lists from JSON leaderboard copies for size —
    # keep gate per_name summary only.
    slim_lb = []
    for r in sorted_lb:
        slim = {k: v for k, v in r.items() if k != "per_name_oos"}
        slim_lb.append(slim)
    payload_out["leaderboard"] = slim_lb

    json_path.write_text(
        json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--db", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--flat-eps", type=float, default=FLAT_EPS)
    ap.add_argument("--beta-lookback", type=int, default=SHORT_BETA_LOOKBACK)
    ap.add_argument("--skip-open", type=int, default=SKIP_OPEN_BARS)
    ap.add_argument(
        "--horizons",
        default=",".join(str(h) for h in HORIZONS),
        help="comma-separated forward minutes on 1m bars",
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
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())

    conn = sqlite3.connect(args.db)
    try:
        result = run(
            conn,
            stocks=stocks,
            start=args.start,
            end=args.end,
            is_end=args.is_end,
            horizons=horizons,
            flat_eps=args.flat_eps,
            beta_lookback=args.beta_lookback,
            skip_open=args.skip_open,
        )
    finally:
        conn.close()

    result["metric"] = {
        "start": args.start,
        "end": args.end,
        "is_end": args.is_end,
        "flat_eps": args.flat_eps,
        "beta_lookback": args.beta_lookback,
        "skip_open": args.skip_open,
        "horizons": list(horizons),
        "stocks": list(stocks),
        "bench": BENCH_1M,
        "mom_eps_pct": SHORT_MOM_EPS_PCT,
        "resid_eps_pct": SHORT_RESID_EPS_PCT,
        "hit_gate": HIT_GATE,
        "min_oos_n": MIN_OOS_N,
    }
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    result["topic"] = "H3 residual short momentum · fwd 5–15m"

    json_path, md_path = write_report(result, args.out_dir)
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")

    # Console summary
    print("\n=== OOS summary (resid vs raw) ===")
    for h in horizons:
        rows = [r for r in result["leaderboard"] if r["horizon_m"] == h]
        for r in sorted(rows, key=lambda x: x["name"]):
            if not (
                r["name"].startswith("raw_mom_") or r["family"] == "h3"
            ):
                continue
            o = r["OOS"]
            print(
                f"  H={h} {r['name']:22s} hit={o.get('directed_hit_pct')}% "
                f"n={o.get('n_directed')} AL={o.get('always_long_pct')}% "
                f"gates={'Y' if r['gates']['all_gates_pass'] else 'n'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
