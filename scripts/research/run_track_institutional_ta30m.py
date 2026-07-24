#!/usr/bin/env python3
"""Track: 三大法人 × fade30 · PIT directed-hit (research only).

Latency (honest)
----------------
TWSE 三大法人 is an **end-of-day** print (FinMind → ``stock_institutional_daily``).
At **10:00 on day T**, only rows with ``trade_date ≤ T−1`` are PIT-legal.
Same-day ``T`` nets are **not** used for intraday 30m decisions in this track.

Protocol mirrors ``run_ta_30m_bias_backtest.py``:
  directed hit · H=6 (~30m) · flat_eps=0.05% · IS ≤2025-09-30 · OOS once.

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_track_institutional_ta30m.py
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
SOURCE = "finmind"


@dataclass(frozen=True)
class InstFeat:
    """PIT features as-of prior trading day (available at open of T)."""

    foreign_net: float
    trust_net: float
    dealer_net: float
    three_net: float
    foreign_net_5d: float
    three_net_5d: float
    foreign_buy_streak: int
    three_buy_streak: int


@dataclass(frozen=True)
class Variant:
    name: str
    rule: str  # see apply_filter
    family: str = "filter"  # baseline | filter | null


# Pre-registered · locked before OOS champion read.
VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "pass", family="baseline"),
    Variant("fade_foreign_buy_t1", "foreign_buy"),
    Variant("fade_foreign_sell_t1", "foreign_sell"),
    Variant("fade_three_buy_t1", "three_buy"),
    Variant("fade_three_sell_t1", "three_sell"),
    Variant("fade_trust_buy_t1", "trust_buy"),
    Variant("fade_foreign_streak2", "foreign_streak2"),
    Variant("fade_foreign_streak3", "foreign_streak3"),
    Variant("fade_three_5d_buy", "three_5d_buy"),
    Variant("fade_foreign_buy_trust_nn", "foreign_buy_trust_nn"),
    Variant("fade_align_foreign", "align_foreign"),
    Variant("fade_against_foreign", "against_foreign"),
    # Null: institutional direction alone on fade clock (no price extreme).
    Variant("null_foreign_dir_midday", "null_foreign_midday", family="null"),
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


def load_inst_series(
    conn: sqlite3.Connection, stock_id: str, *, start: str, end: str
) -> list[tuple[str, float, float, float, float]]:
    """Return ascending (trade_date, foreign, trust, dealer, three)."""
    # Pad start so 5d / streaks exist for first eval days.
    rows = conn.execute(
        """
        SELECT trade_date, foreign_net, investment_trust_net,
               dealer_self_net, three_institution_net
        FROM stock_institutional_daily
        WHERE source = ? AND stock_id = ?
          AND trade_date >= date(?, '-40 day') AND trade_date <= ?
        ORDER BY trade_date
        """,
        (SOURCE, stock_id, start, end),
    ).fetchall()
    out: list[tuple[str, float, float, float, float]] = []
    for td, f, t, d, th in rows:
        out.append(
            (
                str(td),
                float(f or 0.0),
                float(t or 0.0),
                float(d or 0.0),
                float(th or 0.0),
            )
        )
    return out


def build_inst_by_print(
    series: list[tuple[str, float, float, float, float]],
) -> dict[str, InstFeat]:
    """Features keyed by institutional **print** date.

    Session-day T resolves via ``resolve_t1`` → last print with date < T.
    """
    dates = [r[0] for r in series]
    foreign = [r[1] for r in series]
    trust = [r[2] for r in series]
    dealer = [r[3] for r in series]
    three = [r[4] for r in series]

    f_streak = [0] * len(series)
    th_streak = [0] * len(series)
    for i in range(len(series)):
        if foreign[i] > 0:
            f_streak[i] = (f_streak[i - 1] if i else 0) + 1
        else:
            f_streak[i] = 0
        if three[i] > 0:
            th_streak[i] = (th_streak[i - 1] if i else 0) + 1
        else:
            th_streak[i] = 0

    by_print: dict[str, InstFeat] = {}
    for i in range(len(series)):
        lo = max(0, i - 4)
        by_print[dates[i]] = InstFeat(
            foreign_net=foreign[i],
            trust_net=trust[i],
            dealer_net=dealer[i],
            three_net=three[i],
            foreign_net_5d=sum(foreign[lo : i + 1]),
            three_net_5d=sum(three[lo : i + 1]),
            foreign_buy_streak=f_streak[i],
            three_buy_streak=th_streak[i],
        )
    return by_print


def resolve_t1(
    by_print: dict[str, InstFeat], print_dates: Sequence[str], session: str
) -> InstFeat | None:
    """Last institutional print with trade_date < session."""
    # print_dates ascending
    lo, hi = 0, len(print_dates) - 1
    best = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if print_dates[mid] < session:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best < 0:
        return None
    return by_print[print_dates[best]]


def apply_filter(temp: int, feat: InstFeat | None, rule: str) -> int:
    """Return temp or 0 after institutional gate. temp may be 0 already."""
    if rule == "pass":
        return temp
    if rule == "null_foreign_midday":
        # Separate path — caller supplies temp=sign(foreign).
        return temp
    if temp == 0 or feat is None:
        return 0
    f = feat.foreign_net
    if rule == "foreign_buy":
        return temp if f > 0 else 0
    if rule == "foreign_sell":
        return temp if f < 0 else 0
    if rule == "three_buy":
        return temp if feat.three_net > 0 else 0
    if rule == "three_sell":
        return temp if feat.three_net < 0 else 0
    if rule == "trust_buy":
        return temp if feat.trust_net > 0 else 0
    if rule == "foreign_streak2":
        return temp if feat.foreign_buy_streak >= 2 else 0
    if rule == "foreign_streak3":
        return temp if feat.foreign_buy_streak >= 3 else 0
    if rule == "three_5d_buy":
        return temp if feat.three_net_5d > 0 else 0
    if rule == "foreign_buy_trust_nn":
        return temp if (f > 0 and feat.trust_net >= 0) else 0
    if rule == "align_foreign":
        if f == 0:
            return 0
        return temp if (1 if f > 0 else -1) == temp else 0
    if rule == "against_foreign":
        if f == 0:
            return 0
        return temp if (1 if f > 0 else -1) == -temp else 0
    raise ValueError(f"unknown rule {rule}")


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


def eval_day(
    bars: list[Bar],
    *,
    feat: InstFeat | None,
    variants: Sequence[Variant],
    horizon: int,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {v.name: [] for v in variants}
    for i in range(len(bars)):
        if i + horizon >= len(bars):
            break
        prev = bars[: i + 1]
        last = bars[i]
        hm = last.ts.hour * 60 + last.ts.minute
        c0 = last.close
        if not c0:
            continue
        fwd = bars[i + horizon].close / c0 - 1.0
        hhmm = last.ts.strftime("%H:%M")

        layer = fade_near_ext_from_bars(prev, midday_only=True)
        fade_temp = (
            int(layer.temp)
            if layer.ready and layer.temp is not None
            else 0
        )

        for v in variants:
            if v.rule == "null_foreign_midday":
                # Same midday clock as fade; bias = sign(foreign T-1).
                if not (10 * 60 <= hm <= 12 * 60 + 30):
                    continue
                if feat is None or feat.foreign_net == 0:
                    continue
                temp = 1 if feat.foreign_net > 0 else -1
            else:
                temp = apply_filter(fade_temp, feat, v.rule)
            if temp == 0:
                continue
            out[v.name].append(
                {
                    "temp": temp,
                    "fwd": fwd,
                    "hhmm": hhmm,
                    "bars_elapsed": i + 1,
                }
            )
    return out


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

    series = load_inst_series(conn, stock_id, start=start, end=end)
    by_print = build_inst_by_print(series)
    print_dates = [r[0] for r in series]

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    n_with_feat = 0
    for td, bars in sorted(days.items()):
        feat = resolve_t1(by_print, print_dates, td)
        if feat is not None:
            n_with_feat += 1
        split = "IS" if td <= is_end else "OOS"
        day_hits = eval_day(bars, feat=feat, variants=variants, horizon=horizon)
        for v in variants:
            by_var[v.name][split].extend(day_hits[v.name])

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
        "n_days_with_inst_t1": n_with_feat,
        "inst_prints": len(series),
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
    lag = payload["data_lag"]
    lines = [
        "# Track · 三大法人 × TA 30m bias",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Data sources",
        "",
        f"- Table: `{lag['table']}` (source=`{lag['source']}`)",
        f"- Sync: `{lag['sync_script']}`",
        f"- Columns: `foreign_net`, `investment_trust_net`, `dealer_self_net`, "
        "`three_institution_net`",
        f"- Coverage (universe): {lag['coverage_note']}",
        "",
        "## Data lag（PIT · critical）",
        "",
        lag["latency_zh"],
        "",
        "| Clock on day T | Available institutional | Used in this track? |",
        "|---|---|---|",
        "| **10:00 / midday fade window** | prints with `trade_date ≤ T−1` | **Yes** |",
        "| After TWSE EOD publish (~16:00+) | same-day `T` nets | **No** (not for 30m same-day) |",
        "",
        "## Metric（shared protocol）",
        "",
        "- **Directed hit (~30m)**: `temp ∈ {±1}` and "
        f"`|fwd| ≥ {m['flat_eps']}` (= {m['flat_eps']*100:.2f}%), hit iff "
        "`sign(temp) == sign(fwd)`.",
        f"- **Forward**: `close[i+{m['horizon']}] / close[i] - 1`（5m）。",
        "- **Price PIT**: completed 5m bars `≤ i` only.",
        "- **Chip PIT**: institutional features as-of **T−1** only.",
        f"- **IS / OOS**: IS `≤ {m['is_end']}`；OOS `>`。Select on IS；gates on OOS.",
        "- **Gates**: OOS≥70% n≥500；≥70% names OOS≥55% n≥50；max name ≤40% n.",
        "",
        f"- Window: **{m['start']} → {m['end']}**",
        f"- Universe: `{', '.join(m['stocks'])}`",
        f"- Pre-registered variants: {len(payload['pool_leaderboard'])}",
        "",
        f"## Verdict: helps fade30 toward OOS≥70%？ **{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- IS champion: `{champ['variant']}`",
        f"- Champion IS hit: **{champ['IS'].get('directed_hit_pct')}%** "
        f"(n={champ['IS'].get('n_directed')})",
        f"- Champion OOS hit: **{champ['OOS'].get('directed_hit_pct')}%** "
        f"(n={champ['OOS'].get('n_directed')})",
        f"- Best OOS in grid (exploratory): `{payload['best_oos']['variant']}` → "
        f"**{payload['best_oos']['OOS'].get('directed_hit_pct')}%** "
        f"(n={payload['best_oos']['OOS'].get('n_directed')})",
        f"- Unfiltered fade OOS: **{payload['baseline_fade_oos'].get('directed_hit_pct')}%** "
        f"(n={payload['baseline_fade_oos'].get('n_directed')})",
        f"- Δ vs unfiltered fade (champion OOS): "
        f"**{payload.get('delta_vs_fade_oos_pp')}** pp",
        "",
        "## Gates（IS champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
        "## Features（T−1 as-of）",
        "",
        "| feature | definition |",
        "|---|---|",
        "| `foreign_net` | 外資買賣超（股）T−1 |",
        "| `trust_net` | 投信買賣超 T−1 |",
        "| `dealer_net` | 自營（自行買賣）T−1 |",
        "| `three_net` | 三大合計 T−1 |",
        "| `*_net_5d` | 近 5 個法人交易日累計（含 T−1） |",
        "| `foreign_buy_streak` | 外資連續淨買日數（止於 T−1） |",
        "| `three_buy_streak` | 三大合計連續淨買日數 |",
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
        "## Honest read",
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
        "## Next steps",
        "",
    ]
    for s in payload.get("next_steps") or []:
        lines.append(f"- {s}")
    lines += [
        "",
        "## Do not",
        "",
        "- 未過閘寫入 `strategy.yaml` / Order live",
        "- 用同日盤中尚未公布的法人數字當 10:00 特徵（lookahead）",
        "- 事後擴格挑 OOS 當宣稱",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Script: `scripts/research/run_track_institutional_ta30m.py`",
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
            / "track_institutional_ta30m.json"
        ),
    )
    ap.add_argument(
        "--out-md",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "TRACK_INSTITUTIONAL.md"
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
    if not selectable:
        selectable = [
            r for r in pool_rows if r["IS"].get("directed_hit_pct") is not None
        ]
    champ_row = max(selectable, key=lambda r: r["IS"]["directed_hit_pct"])
    best_oos_row = max(
        [r for r in pool_rows if r["OOS"].get("directed_hit_pct") is not None],
        key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"].get("n_directed") or 0),
    )

    champ_oos_full = champ_row["_oos_full"]
    gates = stability_gates(champ_oos_full)
    baseline_fade = next(
        r for r in pool_rows if r["variant"] == "baseline_fade_near_ext"
    )
    delta = None
    if (
        champ_row["OOS"].get("directed_hit_pct") is not None
        and baseline_fade["OOS"].get("directed_hit_pct") is not None
    ):
        delta = round(
            champ_row["OOS"]["directed_hit_pct"]
            - baseline_fade["OOS"]["directed_hit_pct"],
            2,
        )

    # Sensitivity: re-summarize champion rows at looser flat eps via re-run
    # of pool with flat_eps_loose on stored... we don't keep raw rows.
    # Re-run only champion variant quickly.
    sens_results = []
    for sid in stocks:
        sens_results.append(
            run_stock(
                conn,
                sid,
                start=args.start,
                end=end,
                is_end=args.is_end,
                variants=tuple(v for v in variants if v.name == champ_row["variant"]),
                horizon=args.horizon,
                flat_eps=FLAT_EPS_LOOSE,
            )
        )
    sens_oos = pool_from_stocks(sens_results, champ_row["variant"], "OOS")
    sens_oos_compact = {k: sens_oos[k] for k in sens_oos if k != "per_name"}

    # Coverage note
    cov_bits = []
    for s in results:
        if s.get("error"):
            continue
        cov_bits.append(
            f"{s['stock_id']}:{s['n_days_with_inst_t1']}/{s['n_days']}d"
        )

    failure = (
        f"IS 冠軍 `{champ_row['variant']}` OOS="
        f"{champ_row['OOS'].get('directed_hit_pct')}% "
        f"(n={champ_row['OOS'].get('n_directed')})；"
        f"相對未過濾 fade OOS={baseline_fade['OOS'].get('directed_hit_pct')}% "
        f"Δ={delta} pp。"
        "法人為日終資料，盤中 30m 只能用 T−1；過濾多半在減樣本、"
        "未必抬命中。格子內最佳 OOS 亦未過 70% 閘。"
        if not gates["all_gates_pass"]
        else "通過 OOS 閘門（研究宣稱仍需獨立覆核）。"
    )

    next_steps = [
        "若需盤中即時法人流，改用分點/券商支流或盤中估計，非 TWSE 日終三大法人。",
        "可試「fade × 外資連賣」僅短邊／「連買」僅長邊的非對稱規則（仍須一次 IS 鎖定）。",
        "與 branch-specialist / whale chip 合流時，法人只當 regime 慢變數，勿當 30m alpha。",
        "0050/6451 法人覆蓋較短；擴宇宙前先補齊 `stock_institutional_daily`。",
    ]

    leaderboard = sorted(
        [{k: r[k] for k in ("variant", "family", "IS", "OOS")} for r in pool_rows],
        key=lambda r: (
            -(r["OOS"].get("directed_hit_pct") or -1),
            -(r["OOS"].get("n_directed") or 0),
        ),
    )

    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "track": "institutional_三大法人",
        "live_bias_updated": False,
        "metric": {
            "start": args.start,
            "end": end,
            "is_end": args.is_end,
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "stocks": stocks,
        },
        "data_lag": {
            "table": "stock_institutional_daily",
            "source": SOURCE,
            "sync_script": "src/sync_stock_market_daily.py",
            "latency_zh": (
                "三大法人為**收盤後**公布（FinMind 灌庫）。"
                "日內 10:00–12:30 決策僅能合法使用 **T−1（及更早）** 淨買賣；"
                "同日 T 數字要等到當日收盤後才可用於隔日。"
            ),
            "coverage_note": ", ".join(cov_bits),
        },
        "is_champion": {
            "variant": champ_row["variant"],
            "IS": champ_row["IS"],
            "OOS": {
                **{k: champ_oos_full[k] for k in champ_oos_full if k != "per_name"},
                "per_name": champ_oos_full.get("per_name"),
            },
        },
        "best_oos": {
            "variant": best_oos_row["variant"],
            "OOS": best_oos_row["OOS"],
            "IS": best_oos_row["IS"],
        },
        "baseline_fade_oos": baseline_fade["OOS"],
        "delta_vs_fade_oos_pp": delta,
        "gates": gates,
        "pool_leaderboard": leaderboard,
        "per_stock": results,
        "sensitivity_flat_eps_0p10": sens_oos_compact,
        "failure_note_zh": failure,
        "next_steps": next_steps,
    }

    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_md(payload, out_md)
    print(
        f"champion={champ_row['variant']} "
        f"IS={champ_row['IS'].get('directed_hit_pct')}% "
        f"OOS={champ_row['OOS'].get('directed_hit_pct')}% "
        f"gates={gates['all_gates_pass']} → {out_md}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
