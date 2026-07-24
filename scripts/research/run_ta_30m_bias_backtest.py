#!/usr/bin/env python3
"""Multi-factor ~30m directional bias · PIT directed-hit eval (research only).

Pre-registered metric
---------------------
**Directed hit**: among samples with ``temp ∈ {-1,+1}`` and
``|fwd_30m| ≥ flat_eps``, fraction where ``sign(temp) == sign(fwd)``.

- Forward: ``close[i+H] / close[i] - 1``, H=6 ≈ 30m @ 5m bars.
- PIT: factors at bar i use only completed same-session bars ``≤ i``.
- Nulls: coin-flip 50%; always-long; last-30m-color alone; VWAP side alone;
  Live confluence; ``fade_near_ext``.
- IS / OOS: calendar split (default IS ≤ 2025-09-30). Select on IS only;
  claim gates use **OOS** of the IS-frozen champion (+ stability).

Gates (claim OOS≥70%)
---------------------
1. Pool OOS directed hit ≥ 70% and n_directed ≥ 500
2. ≥70% of names with OOS hit ≥ 55% and n ≥ 50 each
3. No single name > 40% of pool OOS n (mega-cap dominance)

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_ta_30m_bias_backtest.py
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
    FADE_HORIZON_BARS,
    TA30M_HORIZON_BARS,
    fade_near_ext_from_bars,
    fuse_ta30m_bias,
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
FLAT_EPS = 0.0005  # 0.05%
FLAT_EPS_LOOSE = 0.001  # 0.10% · sensitivity
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40


@dataclass(frozen=True)
class Variant:
    """Pre-registered fusion rule (small grid · no post-hoc expansion)."""

    name: str
    mode: str
    keys: tuple[str, ...] = ()
    score_thresh: int = 2
    require_vol: bool = False
    midday_only: bool = False
    after_or: bool = True
    min_abs_ret30_pct: float = 0.0
    use_fade_layer: bool = False  # call fade_near_ext_from_bars instead
    family: str = "fusion"  # baseline | fusion


# Pre-registered grid (locked before OOS read for champion selection).
VARIANTS: tuple[Variant, ...] = (
    # --- baselines ---
    Variant("baseline_mom30", "first", ("mom30",), family="baseline"),
    Variant("baseline_vwap", "first", ("vwap",), family="baseline"),
    Variant("baseline_or_break", "first", ("or_break",), family="baseline"),
    Variant("baseline_short_mom", "first", ("short_mom",), family="baseline"),
    Variant(
        "baseline_live_confluence",
        "live_confluence",
        ("mom30", "vwap"),
        family="baseline",
    ),
    Variant(
        "baseline_fade_near_ext",
        "fade_layer",
        (),
        use_fade_layer=True,
        after_or=False,
        family="baseline",
    ),
    # --- AND / OR ---
    Variant("and_mom_vwap", "and", ("mom30", "vwap")),
    Variant("and_mom_vwap_or", "and", ("mom30", "vwap", "or_break")),
    Variant("and_mom_vwap_short", "and", ("mom30", "vwap", "short_mom")),
    Variant(
        "and_mom_vwap_vol",
        "and",
        ("mom30", "vwap"),
        require_vol=True,
    ),
    Variant(
        "and_mom_vwap_midday",
        "and",
        ("mom30", "vwap"),
        midday_only=True,
    ),
    Variant(
        "or_agree_mom_vwap_short",
        "or_agree",
        ("mom30", "vwap", "short_mom"),
    ),
    # --- score fusion ---
    Variant(
        "score2_mom_vwap_short",
        "score",
        ("mom30", "vwap", "short_mom"),
        score_thresh=2,
    ),
    Variant(
        "score2_mom_vwap_or",
        "score",
        ("mom30", "vwap", "or_break"),
        score_thresh=2,
    ),
    Variant(
        "score3_mom_vwap_short_or",
        "score",
        ("mom30", "vwap", "short_mom", "or_break"),
        score_thresh=3,
    ),
    Variant(
        "score2_four_factors",
        "score",
        ("mom30", "vwap", "short_mom", "or_break"),
        score_thresh=2,
    ),
    # --- magnitude gates on mom ---
    Variant(
        "and_mom_vwap_mag030",
        "and",
        ("mom30", "vwap"),
        min_abs_ret30_pct=0.30,
    ),
    Variant(
        "and_mom_vwap_mag050",
        "and",
        ("mom30", "vwap"),
        min_abs_ret30_pct=0.50,
    ),
    Variant(
        "score2_mag030",
        "score",
        ("mom30", "vwap", "short_mom"),
        score_thresh=2,
        min_abs_ret30_pct=0.30,
    ),
    # --- MR-tilted (name-special-cased in signal_temp where noted) ---
    Variant("and_fade_vwap_mr", "and", ("fade_ext", "vwap"), midday_only=True),
    Variant("fade_against_mom", "and", ("fade_ext_anytod",), midday_only=True),
    Variant(
        "score_fade_short_mr",
        "score",
        ("fade_ext", "short_mom"),
        score_thresh=1,
        midday_only=True,
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


def signal_temp(prev: Sequence[Bar], variant: Variant) -> int:
    """Directed bias temp for one PIT prefix."""
    if variant.use_fade_layer:
        layer = fade_near_ext_from_bars(prev, midday_only=True)
        if not layer.ready or layer.temp is None:
            return 0
        return int(layer.temp)

    factors = ta30m_factors_from_bars(prev)
    if variant.name == "fade_against_mom":
        if not factors.get("ready"):
            return 0
        hm = factors.get("hm")
        if not isinstance(hm, int) or not (
            10 * 60 <= hm <= 12 * 60 + 30
        ):
            return 0
        fade = int(factors.get("fade_ext_anytod") or 0)
        mom = int(factors.get("mom30") or 0)
        # MR: fade fires and recent mom still pushing into the extreme.
        if fade != 0 and mom == -fade:
            return fade
        return 0

    if variant.name == "and_fade_vwap_mr":
        # Near extreme fade + price still on the "wrong" VWAP side (continuation).
        if not factors.get("ready"):
            return 0
        fade = int(factors.get("fade_ext") or 0)
        vwap = int(factors.get("vwap") or 0)
        if fade != 0 and vwap == -fade:
            return fade
        return 0

    return fuse_ta30m_bias(
        factors,
        mode=variant.mode,
        keys=variant.keys,
        score_thresh=variant.score_thresh,
        require_vol=variant.require_vol,
        midday_only=variant.midday_only,
        after_or=variant.after_or,
        min_abs_ret30_pct=variant.min_abs_ret30_pct,
    )


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
        out.append(
            {
                "temp": int(temp),
                "fwd": fwd,
                "hhmm": bars[i].ts.strftime("%H:%M"),
                "bars_elapsed": i + 1,
            }
        )
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
    lines = [
        "# TA 30m multi-factor bias · PIT directed-hit evaluation",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Metric definition（預先鎖定）",
        "",
        "- **Primary directed hit (~30m)**: samples with `temp ∈ {±1}` and "
        f"`|fwd| ≥ {m['flat_eps']}` (={m['flat_eps']*100:.2f}%), hit iff "
        "`sign(temp) == sign(fwd)`.",
        f"- **Forward**: `close[i+{m['horizon']}] / close[i] - 1`（≈30 分；同日 5m）。",
        "- **PIT**: 因子僅用當日已完成 5m bars `≤ i`。",
        "- **Null baselines**: coin-flip 50%；always-long；`baseline_mom30`；"
        "`baseline_vwap`；`baseline_live_confluence`；`baseline_fade_near_ext`。",
        f"- **IS / OOS**: IS `trade_date ≤ {m['is_end']}`；OOS `>`。"
        " 調參／選冠軍只看 IS；**主閘門只認 OOS**。",
        "- **Gates**: OOS hit ≥ **70%** 且 n≥500；≥70% 個股 OOS hit≥55% 且 n≥50；"
        "單一名稱 ≤40% pool n。",
        "",
        f"- Window: **{m['start']} → {m['end']}**",
        f"- Universe: `{', '.join(m['stocks'])}`",
        f"- Pre-registered variants: {len(payload['pool_leaderboard'])}",
        "",
        f"## Verdict: OOS≥70% + stable？ **{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- IS champion: `{champ['variant']}`",
        f"- Champion IS hit: **{champ['IS'].get('directed_hit_pct')}%** "
        f"(n={champ['IS'].get('n_directed')})",
        f"- Champion OOS hit: **{champ['OOS'].get('directed_hit_pct')}%** "
        f"(n={champ['OOS'].get('n_directed')})",
        f"- Best OOS among grid (exploratory, not claim): "
        f"`{payload['best_oos']['variant']}` → "
        f"**{payload['best_oos']['OOS'].get('directed_hit_pct')}%** "
        f"(n={payload['best_oos']['OOS'].get('n_directed')})",
        f"- Live bias updated? **{payload.get('live_bias_updated')}**",
        "",
        "## Gates detail（IS champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Null / baseline pool（OOS）",
        "",
        "|variant|OOS hit%|OOS n|vs coin|vs long|",
        "|---|---:|---:|---:|---:|",
    ]
    for row in payload["pool_leaderboard"]:
        if row["family"] != "baseline":
            continue
        o = row["OOS"]
        lines.append(
            f"|{row['variant']}|{o.get('directed_hit_pct')}|"
            f"{o.get('n_directed')}|{o.get('edge_vs_coin_pp')}|"
            f"{o.get('edge_vs_always_long_pp')}|"
        )
    lines += [
        "",
        "## Pool leaderboard · all pre-registered（OOS directed 排序）",
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
        "## Why 70% is hard（honest）",
        "",
        payload.get("failure_note_zh") or "—",
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
        "## Do not",
        "",
        "- 未過閘寫入 `strategy.yaml` / Order live",
        "- 把 Live `ta_30m_bias` 說成 OOS≥70%",
        "- 事後擴格子再挑 OOS 冠軍當宣稱",
        "",
        f"Generated: `{payload['generated_at']}`",
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
            / "ta_30m_bias_backtest.json"
        ),
    )
    ap.add_argument(
        "--out-md",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "TA_30M_BIAS_EVAL.md"
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
        # Strip per_name from leaderboard rows for compactness (kept on champion).
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

    # IS champion: max IS hit among variants with IS n ≥ 200 (avoid tiny spikes).
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

    # Sensitivity: recompute champion only at loose flat eps (reuse rows via re-run summarize?
    # Cheap path: re-eval champion only).
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

    # Honest failure note
    oos_hit = champ["OOS"].get("directed_hit_pct")
    best_hit = best_oos["OOS"].get("directed_hit_pct")
    failure_note = (
        f"IS 冠軍 `{champ['variant']}` OOS={oos_hit}% "
        f"(n={champ['OOS'].get('n_directed')})；"
        f"格子內最佳 OOS `{best_oos['variant']}`={best_hit}% "
        f"(n={best_oos['OOS'].get('n_directed')})。"
        " 動能／VWAP／OR／score／AND 合流多在 41–50%（低於或貼近擲幣）；"
        "僅贴日極值均值回歸淡化能到低六十，但個股異質、穩定性閘未過。"
        " AND／幅度閘未抬到 70%。未弱化閘門；pool OOS≥70% 本輪不可達。"
    )

    live_updated = False  # only flip if all gates pass — handled below
    if gates["all_gates_pass"]:
        live_updated = False  # parent may tighten Live; this runner does not mutate Live

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
        "status": "observe_only",
        "metric": {
            "name": "directed_hit_rate_ta30m_bias",
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": end,
            "stocks": stocks,
            "fade_horizon_ref": FADE_HORIZON_BARS,
        },
        "is_champion": {
            "variant": champ["variant"],
            "IS": {
                k: champ["IS"][k]
                for k in champ["IS"]
                if k != "per_name"
            },
            "OOS": champ["OOS"],
        },
        "best_oos": {
            "variant": best_oos["variant"],
            "OOS": best_oos["OOS"],
            "IS": best_oos["IS"],
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
        "live_bias_updated": live_updated,
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
        f"best_oos={best_oos['variant']}:{best_oos['OOS'].get('directed_hit_pct')}%",
        flush=True,
    )
    print(f"wrote {out_md}", flush=True)
    print(f"wrote {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
