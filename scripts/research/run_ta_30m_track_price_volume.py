#!/usr/bin/env python3
"""Track: 價量 (price–volume) · ~30m directed bias (research only).

Data
----
``stock_kbar_5m.volume`` in ``data/stocks.db`` (PIT research path). Live
observe uses Yahoo Chart 5m volume via ``ops_live_ta.fetch_yahoo_5m_bars`` —
same OHLCV shape; this track does **not** call Yahoo.

Pre-registered metric (shared protocol)
--------------------------------------
Directed hit vs fwd ~30m (H=6 @ 5m); flat_eps=0.05%; IS ≤2025-09-30 / OOS >;
select champion on IS only; claim gates on OOS once.

Features (PIT · completed bars ≤ i)
-----------------------------------
- vol_surge: last vol / session mean so far
- up_down: signed up-volume vs down-volume over lookback
- breakout_vol: OR break + surge
- dry_up: last vol ≤ fraction of session mean (fade confirmation)

Small grid · freeze IS top · one OOS read · fade×volume combos.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_track_price_volume.py
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
    ta30m_factors_from_bars,
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


@dataclass(frozen=True)
class Variant:
    name: str
    family: str  # baseline | vol | fade_vol
    kind: str
    # kind params
    surge_min: float = 1.5
    dry_max: float = 0.7
    ud_lookback: int = 6
    require_ud: str = ""  # "" | "agree" | "against"


# Locked before any OOS champion read.
VARIANTS: tuple[Variant, ...] = (
    # --- shared nulls ---
    Variant("baseline_fade_near_ext", "baseline", "fade"),
    Variant("baseline_mom30", "baseline", "mom30"),
    # --- standalone volume ---
    Variant("vol_surge_mom_1p5", "vol", "surge_mom", surge_min=1.5),
    Variant("vol_surge_mom_2p0", "vol", "surge_mom", surge_min=2.0),
    Variant("updown_vol_L6", "vol", "updown", ud_lookback=6),
    Variant("or_break_vol_surge_1p5", "vol", "or_surge", surge_min=1.5),
    Variant("or_break_vol_surge_2p0", "vol", "or_surge", surge_min=2.0),
    # --- fade × volume (lift test toward 70%) ---
    Variant("fade_dryup_07", "fade_vol", "fade_dry", dry_max=0.7),
    Variant("fade_dryup_05", "fade_vol", "fade_dry", dry_max=0.5),
    Variant("fade_no_surge_1p2", "fade_vol", "fade_no_surge", surge_min=1.2),
    Variant("fade_no_surge_1p5", "fade_vol", "fade_no_surge", surge_min=1.5),
    Variant("fade_ud_agree", "fade_vol", "fade_ud", require_ud="agree", ud_lookback=6),
    Variant(
        "fade_ud_against",
        "fade_vol",
        "fade_ud",
        require_ud="against",
        ud_lookback=6,
    ),
    Variant(
        "fade_dryup07_ud_against",
        "fade_vol",
        "fade_dry_ud",
        dry_max=0.7,
        require_ud="against",
        ud_lookback=6,
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


def pv_state(prev: Sequence[Bar], *, ud_lookback: int = 6) -> dict[str, Any]:
    """PIT price–volume helpers from completed session bars."""
    n = len(prev)
    last = prev[-1]
    vols = [float(b.volume or 0.0) for b in prev]
    prior = [v for v in vols[:-1] if v > 0]
    sess_mean = (sum(prior) / len(prior)) if prior else (vols[-1] if vols[-1] > 0 else 0.0)
    vol_ratio = (vols[-1] / sess_mean) if sess_mean > 0 else 0.0

    L = min(ud_lookback, n)
    w = list(prev[-L:])
    up_v = sum(float(b.volume or 0.0) for b in w if b.close >= b.open)
    dn_v = sum(float(b.volume or 0.0) for b in w if b.close < b.open)
    if up_v > dn_v * 1.05:
        ud = 1
    elif dn_v > up_v * 1.05:
        ud = -1
    else:
        ud = 0

    return {
        "vol_ratio": vol_ratio,
        "sess_mean": sess_mean,
        "ud": ud,
        "up_v": up_v,
        "dn_v": dn_v,
        "bars": n,
    }


def signal_temp(prev: Sequence[Bar], variant: Variant) -> int:
    kind = variant.kind
    if kind == "fade":
        layer = fade_near_ext_from_bars(prev, midday_only=True)
        if not layer.ready or layer.temp is None:
            return 0
        return int(layer.temp)

    if kind == "mom30":
        fac = ta30m_factors_from_bars(prev)
        return int(fac.get("mom30") or 0) if fac.get("ready") else 0

    pv = pv_state(prev, ud_lookback=variant.ud_lookback)
    fac = ta30m_factors_from_bars(prev)

    if kind == "surge_mom":
        if not fac.get("ready"):
            return 0
        if pv["vol_ratio"] < variant.surge_min:
            return 0
        return int(fac.get("mom30") or 0)

    if kind == "updown":
        if pv["bars"] < max(6, variant.ud_lookback):
            return 0
        return int(pv["ud"])

    if kind == "or_surge":
        if not fac.get("ready") or int(fac.get("bars_elapsed") or 0) < TA30M_OR_BARS:
            return 0
        if pv["vol_ratio"] < variant.surge_min:
            return 0
        return int(fac.get("or_break") or 0)

    # fade × volume families
    layer = fade_near_ext_from_bars(prev, midday_only=True)
    if not layer.ready or layer.temp is None or int(layer.temp) == 0:
        return 0
    fade = int(layer.temp)

    if kind == "fade_dry":
        if pv["vol_ratio"] <= 0 or pv["sess_mean"] <= 0:
            return 0
        return fade if pv["vol_ratio"] <= variant.dry_max else 0

    if kind == "fade_no_surge":
        if pv["sess_mean"] <= 0:
            return 0
        return fade if pv["vol_ratio"] < variant.surge_min else 0

    if kind == "fade_ud":
        ud = int(pv["ud"])
        if ud == 0:
            return 0
        if variant.require_ud == "agree" and ud == fade:
            return fade
        if variant.require_ud == "against" and ud == -fade:
            return fade
        return 0

    if kind == "fade_dry_ud":
        if pv["vol_ratio"] <= 0 or pv["sess_mean"] <= 0:
            return 0
        if pv["vol_ratio"] > variant.dry_max:
            return 0
        ud = int(pv["ud"])
        if ud == 0:
            return 0
        if variant.require_ud == "against" and ud == -fade:
            return fade
        if variant.require_ud == "agree" and ud == fade:
            return fade
        return 0

    raise ValueError(f"unknown kind: {kind}")


def eval_day(
    bars: list[Bar], variant: Variant, *, horizon: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(bars)):
        if i + horizon >= len(bars):
            break
        prev = bars[: i + 1]
        temp = signal_temp(prev, variant)
        if temp == 0:
            continue
        c0 = bars[i].close
        if not c0:
            continue
        fwd = bars[i + horizon].close / c0 - 1.0
        out.append({"temp": int(temp), "fwd": fwd})
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
        "edge_vs_always_long_pp": (
            round(d_pct - al, 2) if d_pct is not None and al is not None else None
        ),
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

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    for td, bars in sorted(days.items()):
        split = "IS" if td <= is_end else "OOS"
        for v in variants:
            by_var[v.name][split].extend(eval_day(bars, v, horizon=horizon))

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
    fade = payload["fade_baseline"]
    lift = payload["volume_lift_fade"]
    lines = [
        "# Track · 價量 (price–volume) · ~30m directed bias",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Data path",
        "",
        "- Research PIT: `stock_kbar_5m.volume` in `data/stocks.db` "
        f"（universe 量能覆蓋 ≈98–100%；窗 {m['start']}→{m['end']}）。",
        "- Live observe mirror: Yahoo Chart 5m OHLCV via "
        "`ops_live_ta.fetch_yahoo_5m_bars` / `compute_ta_30m_snapshot` "
        "（本 track 不打 Yahoo）。",
        "",
        "## Metric（共用協議）",
        "",
        "- **Directed hit (~30m)**: `temp ∈ {±1}` 且 "
        f"`|fwd| ≥ {m['flat_eps']}`；hit iff `sign(temp)==sign(fwd)`。",
        f"- **Forward**: `close[i+{m['horizon']}]/close[i]-1`（≈30 分 @ 5m）。",
        "- **PIT**: 僅用當日已完成 bars `≤ i`。",
        f"- **IS / OOS**: IS ≤ `{m['is_end']}`；OOS `>`。選冠軍只看 IS；"
        "**主閘只認一次 OOS**。",
        "- **對照**: `fade_near_ext` ~62%；`mom30` ~45%；目標 OOS≥70% + 穩定。",
        f"- Universe: `{', '.join(m['stocks'])}`",
        f"- Pre-registered variants: {len(payload['pool_leaderboard'])}",
        "",
        f"## Verdict: OOS≥70% + stable？ **{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- IS champion: `{champ['variant']}`",
        f"- Champion IS: **{champ['IS'].get('directed_hit_pct')}%** "
        f"(n={champ['IS'].get('n_directed')})",
        f"- Champion OOS: **{champ['OOS'].get('directed_hit_pct')}%** "
        f"(n={champ['OOS'].get('n_directed')})",
        f"- Best OOS in grid (exploratory): `{payload['best_oos']['variant']}` → "
        f"**{payload['best_oos']['OOS'].get('directed_hit_pct')}%** "
        f"(n={payload['best_oos']['OOS'].get('n_directed')})",
        "",
        "## Does volume lift fade to 70%?",
        "",
        f"- Fade baseline OOS: **{fade['OOS'].get('directed_hit_pct')}%** "
        f"(n={fade['OOS'].get('n_directed')})",
        f"- Best fade×volume OOS: `{lift['best_variant']}` → "
        f"**{lift['best_oos_hit']}%** (n={lift['best_oos_n']})",
        f"- Δ vs fade baseline: **{lift['delta_pp']} pp**",
        f"- Reaches 70%? **{lift['reaches_70']}**",
        "",
        lift.get("note_zh") or "",
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
        "- 事後擴格子再挑 OOS 冠軍當宣稱",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Runner: `scripts/research/run_ta_30m_track_price_volume.py`",
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
            / "track_price_volume.json"
        ),
    )
    ap.add_argument(
        "--out-md",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "TRACK_PRICE_VOLUME.md"
        ),
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    end = args.end or conn.execute(
        "SELECT MAX(trade_date) FROM stock_kbar_5m"
    ).fetchone()[0]
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    variants = VARIANTS

    results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"eval {sid} {args.start}→{end} IS≤{args.is_end} ...", flush=True)
        results.append(
            run_stock(
                conn,
                sid,
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
    fade_vol_rows = [r for r in pool_rows if r["family"] == "fade_vol"]
    best_fv = max(
        fade_vol_rows,
        key=lambda r: (
            r["OOS"].get("directed_hit_pct") or 0,
            r["OOS"].get("n_directed") or 0,
        ),
    ) if fade_vol_rows else fade_row
    fade_oos = fade_row["OOS"].get("directed_hit_pct") or 0.0
    best_fv_oos = best_fv["OOS"].get("directed_hit_pct") or 0.0
    delta = round(best_fv_oos - fade_oos, 2)
    reaches = bool(best_fv_oos >= 70.0 and (best_fv["OOS"].get("n_directed") or 0) >= MIN_OOS_N)
    lift = {
        "best_variant": best_fv["variant"],
        "best_oos_hit": best_fv_oos,
        "best_oos_n": best_fv["OOS"].get("n_directed"),
        "fade_oos_hit": fade_oos,
        "delta_pp": delta,
        "reaches_70": "YES" if reaches else "NO",
        "note_zh": (
            f"價量濾網相對裸 fade 改變 {delta:+.2f} pp；"
            + (
                "達 pool OOS≥70%。"
                if reaches
                else "未達 70%（或 n 不足）；dry-up／無放量多為縮樣，未穩定抬升命中。"
            )
        ),
    }

    print(f"sensitivity flat_eps={FLAT_EPS_LOOSE} on champion...", flush=True)
    sens_rows_oos: list[dict[str, Any]] = []
    champ_v = next(v for v in variants if v.name == champ["variant"])
    for s in results:
        if s.get("error"):
            continue
        days = load_days(conn, s["stock_id"], start=args.start, end=end)
        for td, bars in days.items():
            if td <= args.is_end:
                continue
            sens_rows_oos.extend(eval_day(bars, champ_v, horizon=args.horizon))
    sens = summarize(sens_rows_oos, flat_eps=FLAT_EPS_LOOSE)

    oos_hit = champ["OOS"].get("directed_hit_pct")
    best_hit = best_oos["OOS"].get("directed_hit_pct")
    failure_note = (
        f"IS 冠軍 `{champ['variant']}` OOS={oos_hit}% "
        f"(n={champ['OOS'].get('n_directed')})；"
        f"格子最佳 OOS `{best_oos['variant']}`={best_hit}% "
        f"(n={best_oos['OOS'].get('n_directed')})。"
        f" 裸 fade OOS={fade_oos}%；最佳 fade×volume "
        f"`{lift['best_variant']}`={best_fv_oos}% (Δ{delta:+.2f}pp)。"
        " 放量追動能／OR+surge 仍貼近或低於擲幣；量能未能把 fade 抬到 70%。"
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
        "track": "price_volume",
        "status": "observe_only",
        "metric": {
            "name": "directed_hit_rate_ta30m_price_volume",
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": end,
            "stocks": stocks,
        },
        "data_path": {
            "research": "stock_kbar_5m.volume",
            "live_observe": "ops_live_ta.fetch_yahoo_5m_bars",
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
        },
        "fade_baseline": {
            "variant": "baseline_fade_near_ext",
            "IS": fade_row["IS"],
            "OOS": fade_row["OOS"],
        },
        "volume_lift_fade": lift,
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
        f"fade_lift={lift['best_variant']}:{lift['best_oos_hit']}% "
        f"reaches70={lift['reaches_70']}",
        flush=True,
    )
    print(f"wrote {out_md}", flush=True)
    print(f"wrote {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
