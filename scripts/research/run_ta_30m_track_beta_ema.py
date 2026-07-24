#!/usr/bin/env python3
"""Track: 5m EMA + rolling beta residual filters on fade30 (research).

Goal (user gate this pass)
--------------------------
Raise ~30m directed-hit with **OOS ≥60%**, n≥500, and multi-stock stability.
Prior research already has ``fade_idx_or_inside`` @ ~70.7% (0050 proxy, 70% gate).
This track tests features that were **not** in the 30m path: 5m EMA9/21 and
rolling beta vs 0050 + residual.

Baselines (frozen refs): ``baseline_fade_near_ext``, ``fade_idx_or_inside``.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_track_beta_ema.py
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
    ema_factors_from_bars,
    fade_near_ext_from_bars,
    opening_range_hl,
    rolling_beta_residual,
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
HIT_GATE = 60.0  # this pass (user); prior MKT used 70


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    family: str = "fusion"


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", "baseline"),
    Variant("fade_idx_or_inside", "fade_idx_or_inside", "baseline"),
    # Standalone EMA / residual (expect ~coin)
    Variant("baseline_price_vs_ema9", "price_vs_ema_fast", "baseline"),
    Variant("baseline_ema_cross", "ema_cross", "baseline"),
    Variant("baseline_resid_sign", "resid_sign", "baseline"),
    # Fade ∧ EMA
    Variant("fade_ema_ext_slow", "fade_ema_ext_slow"),  # fade when extended vs EMA21
    Variant("fade_ema_cross_agree", "fade_ema_cross_agree"),  # fade side == ema_cross
    Variant("fade_ema_cross_against", "fade_ema_cross_against"),
    Variant("fade_ema_flat_cross", "fade_ema_flat_cross"),  # |ema9-ema21| quiet
    # Fade ∧ beta / residual
    Variant("fade_resid_against", "fade_resid_against"),  # residual opposes fade? no: same as ext
    Variant("fade_resid_agree_ext", "fade_resid_agree_ext"),  # residual same sign as fade opp = ext
    Variant("fade_high_beta", "fade_high_beta"),
    Variant("fade_low_beta", "fade_low_beta"),
    # Stack on prior champion
    Variant("fade_idx_and_ema_ext", "fade_idx_and_ema_ext"),
    Variant("fade_idx_and_resid_ext", "fade_idx_and_resid_ext"),
    Variant("fade_idx_and_ema_flat", "fade_idx_and_ema_flat"),
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


def _idx_or_inside(stock_prev: Sequence[Bar], bench_day: Sequence[Bar]) -> bool | None:
    bprev = _align_bench(stock_prev, bench_day)
    if len(bprev) < TA30M_OR_BARS:
        return None
    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return None
    or_hi, or_lo = or_hl
    bc = bprev[-1].close
    return bool(or_lo <= bc <= or_hi)


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
    ema = ema_factors_from_bars(stock_prev)
    bprev = _align_bench(stock_prev, bench_day)
    beta = rolling_beta_residual(stock_prev, bprev) if bprev else {"ready": False}
    inside = _idx_or_inside(stock_prev, bench_day)

    if variant.kind == "fade":
        return fade
    if variant.kind == "fade_idx_or_inside":
        if fade == 0 or inside is not True:
            return 0
        return fade
    if variant.kind == "price_vs_ema_fast":
        return int(ema["price_vs_ema_fast"]) if ema.get("ready") else 0
    if variant.kind == "ema_cross":
        return int(ema["ema_cross"]) if ema.get("ready") else 0
    if variant.kind == "resid_sign":
        return int(beta["resid_sign"]) if beta.get("ready") else 0

    if fade == 0:
        return 0

    # Extension vs EMA21: short fade when above slow EMA; long when below.
    if variant.kind == "fade_ema_ext_slow":
        if not ema.get("ready"):
            return 0
        vs = int(ema["price_vs_ema_slow"])
        # fade=-1 (near high) needs price above EMA; fade=+1 needs below
        if fade < 0 and vs > 0:
            return fade
        if fade > 0 and vs < 0:
            return fade
        return 0

    if variant.kind == "fade_ema_cross_agree":
        if not ema.get("ready"):
            return 0
        return fade if int(ema["ema_cross"]) == fade else 0

    if variant.kind == "fade_ema_cross_against":
        if not ema.get("ready"):
            return 0
        return fade if int(ema["ema_cross"]) == -fade else 0

    if variant.kind == "fade_ema_flat_cross":
        if not ema.get("ready"):
            return 0
        return fade if int(ema["ema_cross"]) == 0 else 0

    # near high → fade short (−1); resid>0 = stock beat beta*mkt → exhausted up
    if variant.kind == "fade_resid_agree_ext":
        if not beta.get("ready"):
            return 0
        return fade if int(beta["resid_sign"]) == -fade else 0
    if variant.kind == "fade_resid_against":
        # residual already pointing fade way (pre-MR) — optional filter
        if not beta.get("ready"):
            return 0
        return fade if int(beta["resid_sign"]) == fade else 0

    if variant.kind == "fade_high_beta":
        if not beta.get("ready") or not beta.get("beta_abs_ge1"):
            return 0
        return fade

    if variant.kind == "fade_low_beta":
        if not beta.get("ready") or not beta.get("beta_abs_lt1"):
            return 0
        return fade

    if variant.kind == "fade_idx_and_ema_ext":
        if inside is not True or not ema.get("ready"):
            return 0
        vs = int(ema["price_vs_ema_slow"])
        if fade < 0 and vs > 0:
            return fade
        if fade > 0 and vs < 0:
            return fade
        return 0

    if variant.kind == "fade_idx_and_resid_ext":
        if inside is not True or not beta.get("ready"):
            return 0
        return fade if int(beta["resid_sign"]) == -fade else 0

    if variant.kind == "fade_idx_and_ema_flat":
        if inside is not True or not ema.get("ready"):
            return 0
        return fade if int(ema["ema_cross"]) == 0 else 0

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
        is_rows = by_var[v.name]["IS"]
        oos_rows = by_var[v.name]["OOS"]
        out_vars[v.name] = {
            "IS": summarize(is_rows, flat_eps=flat_eps),
            "OOS": summarize(oos_rows, flat_eps=flat_eps),
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


def stability_gates(pool_oos: dict[str, Any], *, hit_gate: float = HIT_GATE) -> dict[str, Any]:
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
    gate_stab = bool(n_names > 0 and frac >= NAME_PASS_FRAC)
    gate_dom = bool(max_share <= MAX_NAME_SHARE)
    return {
        f"gate_oos_hit_ge{int(hit_gate)}_n500": gate_hit,
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


def leave_one_out_oos(
    stock_results: list[dict[str, Any]], variant: str
) -> list[dict[str, Any]]:
    sids = [s["stock_id"] for s in stock_results if not s.get("error")]
    out: list[dict[str, Any]] = []
    for drop in sids:
        kept = [s for s in stock_results if s.get("stock_id") != drop and not s.get("error")]
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
    gates = payload["gates"]
    gate_key = f"gate_oos_hit_ge{int(HIT_GATE)}_n500"
    lines = [
        "# Track · 5m EMA + rolling beta residual · ~30m",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Question",
        "",
        "Do **5m EMA9/21** and **rolling beta vs 0050 + residual** raise "
        f"fade30 OOS directed hit to ≥{int(HIT_GATE)}% with multi-stock "
        "stability? (Features absent from prior 30m path; daily WMA was "
        "killed in RRG track.)",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: EMA/beta/fade from completed same-session bars `≤ i` only.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; champion on **IS only**; claim on OOS.",
        f"- Universe (ex `{BENCH_5M}`): `{', '.join(m['stocks'])}`.",
        f"- Pre-registered variants: {m['n_variants']}.",
        f"- Window: **{m['start']} → {m['end']}**",
        f"- Gate this pass: OOS≥**{int(HIT_GATE)}%** · n≥500 · name stab "
        f"≥{int(NAME_PASS_FRAC*100)}% names @≥{int(NAME_HIT_FLOOR)}% (n≥{MIN_NAME_N}).",
        "",
        f"## Verdict: new EMA/beta rule clears OOS≥{int(HIT_GATE)}% + stable？ "
        f"**{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        (
            f"- **Best claimable (n≥500)**: `{payload['best_thick_oos']['name']}` "
            f"OOS **{payload['best_thick_oos']['OOS']['directed_hit_pct']}%** "
            f"(n={payload['best_thick_oos']['OOS']['n_directed']}); "
            f"gates **{payload['best_thick_oos']['gates']['all_gates_pass']}** "
            "— reproduced prior 大盤 OR-inside filter; **not** a new EMA/beta lift."
            if payload.get("best_thick_oos")
            else "- Best claimable (n≥500): **none**"
        ),
        f"- IS champion (this track): `{champ['name']}`",
        f"- Champion IS hit: **{champ['IS']['directed_hit_pct']}%** "
        f"(n={champ['IS']['n_directed']})",
        f"- Champion OOS hit: **{champ['OOS']['directed_hit_pct']}%** "
        f"(n={champ['OOS']['n_directed']})",
        f"- Best OOS exploratory: `{payload['best_oos_exploratory']['name']}` → "
        f"**{payload['best_oos_exploratory']['OOS']['directed_hit_pct']}%** "
        f"(n={payload['best_oos_exploratory']['OOS']['n_directed']})",
        (
            f"- Best OOS with n≥500: `{payload['best_thick_oos']['name']}` → "
            f"**{payload['best_thick_oos']['OOS']['directed_hit_pct']}%** "
            f"(n={payload['best_thick_oos']['OOS']['n_directed']})"
            if payload.get("best_thick_oos")
            else "- Best OOS with n≥500: **none**"
        ),
        f"- Did EMA help? **{payload['ema_helped']}**",
        f"- Did beta/residual help? **{payload['beta_helped']}**",
        f"- Did 大盤 (idx OR) help? **{payload['idx_helped']}**",
        "",
        "### Note on thin-n peaks",
        "",
        "`fade_idx_and_ema_flat` / `fade_ema_flat_cross` can print OOS hit ≫70% "
        "but **n≪500** and megacap-dominated — **not claimable** (same class as "
        "E4 TOD dry-up). Stacking EMA/beta on idx **thins n** below 500 and "
        "hurts name stability vs bare `fade_idx_or_inside`.",
        "",
        "## Gates（IS champion OOS）",
        "",
        f"- `{gate_key}`: **{gates[gate_key]}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Pool leaderboard（OOS directed 排序）",
        "",
        "|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    fade_ref = None
    for row in payload["leaderboard"]:
        if row["name"] == "baseline_fade_near_ext":
            fade_ref = row["OOS"]["directed_hit_pct"]
            break
    if fade_ref is None:
        fade_ref = 64.0
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
        f"- `oos_ge_60_stable_is_champ`: **{payload['claim']['oos_ge_60_stable_is_champ']}**",
        f"- `oos_ge_60_stable_best_thick`: **{payload['claim']['oos_ge_60_stable_best_thick']}** "
        f"(`{payload['claim'].get('best_thick_name')}`)",
        f"- `live_bias_updated`: **{payload['claim']['live_bias_updated']}**",
        f"- `order_adopted`: **{payload['claim']['order_adopted']}**",
        "",
        "## Artifacts",
        "",
        f"- JSON: `{payload['artifacts']['json']}`",
        f"- MD: `{payload['artifacts']['md']}`",
        f"- Runner: `scripts/research/run_ta_30m_track_beta_ema.py`",
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

    eligible = [
        r
        for r in leaderboard
        if (r["IS"].get("n_directed") or 0) >= MIN_OOS_N
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not eligible:
        # Fallback: still prefer thicker IS samples over sparse peek rules.
        eligible = [
            r
            for r in leaderboard
            if (r["IS"].get("n_directed") or 0) >= 200
            and r["IS"].get("directed_hit_pct") is not None
        ]
    if not eligible:
        eligible = [r for r in leaderboard if r["IS"].get("directed_hit_pct") is not None]
    champ_row = max(
        eligible, key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"])
    )
    gates = stability_gates(champ_row["OOS"], hit_gate=HIT_GATE)
    # Exploratory OOS peak (may be thin-n — not a claim).
    best_oos = max(
        (r for r in leaderboard if r["OOS"].get("directed_hit_pct") is not None),
        key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"]["n_directed"]),
    )
    # Best among OOS n≥500 (stability candidate pool).
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
    by_name = {r["name"]: r for r in leaderboard}
    fade_hit = by_name["baseline_fade_near_ext"]["OOS"]["directed_hit_pct"]
    idx_hit = by_name["fade_idx_or_inside"]["OOS"]["directed_hit_pct"]
    ema_ext = by_name["fade_ema_ext_slow"]["OOS"]["directed_hit_pct"]
    resid = by_name["fade_resid_agree_ext"]["OOS"]["directed_hit_pct"]
    idx_ema = by_name["fade_idx_and_ema_ext"]["OOS"]["directed_hit_pct"]
    idx_resid = by_name["fade_idx_and_resid_ext"]["OOS"]["directed_hit_pct"]

    def _helped(new: float | None, base: float | None, *, min_pp: float = 1.0) -> str:
        if new is None or base is None:
            return "UNKNOWN"
        d = new - base
        if d >= min_pp:
            return f"YES (+{d:.2f}pp vs base {base}%)"
        if d > 0:
            return f"MARGINAL (+{d:.2f}pp)"
        return f"NO ({d:.2f}pp vs base {base}%)"

    ema_helped = _helped(ema_ext, fade_hit)
    beta_helped = _helped(resid, fade_hit)
    idx_helped = _helped(idx_hit, fade_hit)
    # Stacking: did EMA/beta beat idx alone?
    ema_vs_idx = _helped(idx_ema, idx_hit, min_pp=0.5)
    beta_vs_idx = _helped(idx_resid, idx_hit, min_pp=0.5)

    loo = leave_one_out_oos(stock_results, champ_row["name"])
    loo_min = min((r["directed_hit_pct"] or 0) for r in loo) if loo else None

    interpretation = (
        f"Standalone EMA / residual signs are expected ~coin "
        f"(price_vs_ema9 OOS={by_name['baseline_price_vs_ema9']['OOS']['directed_hit_pct']}%; "
        f"resid OOS={by_name['baseline_resid_sign']['OOS']['directed_hit_pct']}%). "
        f"Fade∧EMA-ext OOS={ema_ext}% · fade∧resid-ext OOS={resid}% vs "
        f"unfiltered fade OOS={fade_hit}%. "
        f"Prior 大盤 filter `fade_idx_or_inside` OOS={idx_hit}% remains the "
        f"strong lift. Stacking EMA/residual on idx: "
        f"idx∧ema_ext OOS={idx_ema}% ({ema_vs_idx}); "
        f"idx∧resid OOS={idx_resid}% ({beta_vs_idx}). "
        f"IS champion `{champ_row['name']}` OOS="
        f"{champ_row['OOS']['directed_hit_pct']}% n={champ_row['OOS']['n_directed']}; "
        f"gates_pass={gates['all_gates_pass']}; "
        f"leave-one-out min OOS={loo_min}%. "
        "Do not Order-graduate; Live observe unchanged unless a fresh holdout "
        "confirms a rule that beats `fade_idx_or_inside` on gates."
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
    json_path = out_dir / "track_beta_ema_30m.json"
    md_path = out_dir / "TRACK_BETA_EMA.md"

    payload: dict[str, Any] = {
        "topic": "intraday-direction-thermometer · track beta+EMA",
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
            "variant_names": [v.name for v in variants],
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
                "gates": stability_gates(best_thick["OOS"], hit_gate=HIT_GATE),
            }
            if best_thick
            else None
        ),
        "gates": gates,
        "ema_helped": ema_helped,
        "beta_helped": beta_helped,
        "idx_helped": idx_helped,
        "ema_vs_idx": ema_vs_idx,
        "beta_vs_idx": beta_vs_idx,
        "leave_one_out_oos": loo,
        "leaderboard": leaderboard_sorted,
        "per_stock": stock_results,
        "interpretation": interpretation,
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
        },
        "claim": {
            "oos_ge_60_stable_is_champ": gates["all_gates_pass"],
            "oos_ge_60_stable_best_thick": bool(
                best_thick
                and stability_gates(best_thick["OOS"], hit_gate=HIT_GATE)[
                    "all_gates_pass"
                ]
            ),
            "best_thick_name": best_thick["name"] if best_thick else None,
            "live_bias_updated": False,
            "order_adopted": False,
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
        f"gates={gates['all_gates_pass']}"
    )
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
