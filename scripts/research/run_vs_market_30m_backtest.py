#!/usr/bin/env python3
"""Track: 距離大盤的比例 / relative-to-market · ~30m directed-hit (research).

Data reality (Book ``data/stocks.db`` · 2026-07-24)
-------------------------------------------------
- Intraday market proxy: ``0050`` 5m (IS+OOS; ~611d from 2024-01-02).
- True TAIEX ``IX0001`` 5m: OOS-only (~184d from 2025-10-15) — **not** used
  for IS selection; optional OOS sensitivity only.
- Daily TAIEX: ``daily_bars.code='IX0001'`` (long history) for overnight RS.
- Sector indices: absent in 5m / daily_bars for this study.

Honest overnight RS use
-----------------------
D−1 excess = stock_daily_ret(D−1) − IX0001_daily_ret(D−1), available before
session D open. Used only as a **filter** on same-session fade30 (no peek).

Pre-registered metric: same as ``run_ta_30m_bias_backtest.py`` (directed hit
fwd H=6 · flat_eps=0.05% · IS≤2025-09-30 · OOS gates ≥70%).

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_vs_market_30m_backtest.py
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

# Universe excludes 0050 (used as intraday bench).
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
BENCH_DAILY = "IX0001"
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0005
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
RS_EPS_PCT = 0.10  # quiet zone for excess % points
OVN_STRONG_PCT = 0.50


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str  # baseline | fusion
    # kinds: fade | rs_sign | vs_idx_vwap | vs_idx_orb | and_rs_vwap
    #        fade_rs_against | fade_rs_agree | fade_ovn_same | fade_ovn_opp
    #        fade_ovn_strong_same | fade_idx_or_inside | fade_rs_rank_hi
    rs_bars: int = 6
    family: str = "fusion"


# Locked before OOS champion claim (small grid).
VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_fade_near_ext", "fade", family="baseline"),
    Variant("baseline_rs30", "rs_sign", rs_bars=6, family="baseline"),
    Variant("baseline_rs60", "rs_sign", rs_bars=12, family="baseline"),
    Variant("baseline_rs_day", "rs_sign", rs_bars=0, family="baseline"),  # 0=session
    Variant("baseline_vs_idx_vwap", "vs_idx_vwap", family="baseline"),
    Variant("baseline_vs_idx_orb", "vs_idx_orb", family="baseline"),
    Variant("and_rs30_vs_idx_vwap", "and_rs_vwap", rs_bars=6),
    Variant("fade_rs30_against", "fade_rs_against", rs_bars=6),
    Variant("fade_rs30_agree", "fade_rs_agree", rs_bars=6),
    Variant("fade_ovn_rs_same", "fade_ovn_same"),
    Variant("fade_ovn_rs_opp", "fade_ovn_opp"),
    Variant("fade_ovn_rs_strong_same", "fade_ovn_strong_same"),
    Variant("fade_idx_or_inside", "fade_idx_or_inside"),
)


def _minute_hhmm(minute: str) -> str:
    parts = str(minute).split(":")
    return f"{parts[0]}:{parts[1]}"


def _sign_eps(x: float, eps: float = 0.0) -> int:
    if x > eps:
        return 1
    if x < -eps:
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


def load_overnight_excess(
    conn: sqlite3.Connection, stock_id: str, *, start: str, end: str
) -> dict[str, float]:
    """Map trade_date → D−1 excess% (stock − IX0001). PIT for session D."""
    stock = conn.execute(
        """
        SELECT trade_date, close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date >= date(?, '-30 days')
          AND trade_date <= ?
        ORDER BY trade_date
        """,
        (stock_id, start, end),
    ).fetchall()
    bench = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code = ? AND date >= date(?, '-30 days') AND date <= ?
        ORDER BY date
        """,
        (BENCH_DAILY, start, end),
    ).fetchall()
    s_closes = {str(d): float(c) for d, c in stock if c is not None}
    b_closes = {str(d): float(c) for d, c in bench if c is not None}
    dates = sorted(set(s_closes) | set(b_closes))
    s_ret: dict[str, float] = {}
    b_ret: dict[str, float] = {}
    prev_s = prev_b = None
    for d in dates:
        if d in s_closes and prev_s is not None and prev_s > 0:
            s_ret[d] = 100.0 * (s_closes[d] / prev_s - 1.0)
        if d in b_closes and prev_b is not None and prev_b > 0:
            b_ret[d] = 100.0 * (b_closes[d] / prev_b - 1.0)
        if d in s_closes:
            prev_s = s_closes[d]
        if d in b_closes:
            prev_b = b_closes[d]
    # excess on day D is known after D close → usable on next session
    excess_on_day = {
        d: s_ret[d] - b_ret[d] for d in s_ret if d in b_ret
    }
    out: dict[str, float] = {}
    prev_ex = None
    for d in dates:
        if prev_ex is not None:
            out[d] = prev_ex
        if d in excess_on_day:
            prev_ex = excess_on_day[d]
    return out


def _ret_pct(bars: Sequence[Bar], n_bars: int) -> float | None:
    """Return over last n_bars completed bars; n_bars=0 → from session open."""
    if not bars:
        return None
    if n_bars <= 0:
        c0, c1 = bars[0].close, bars[-1].close
    else:
        if len(bars) < n_bars:
            return None
        w = list(bars[-n_bars:])
        c0, c1 = w[0].close, w[-1].close
    if not c0:
        return None
    return 100.0 * (c1 / c0 - 1.0)


def _align_bench_prefix(
    stock_prev: Sequence[Bar], bench_day: Sequence[Bar]
) -> list[Bar]:
    """Bench bars with ts ≤ last stock bar (PIT same-session)."""
    if not stock_prev or not bench_day:
        return []
    t_end = stock_prev[-1].ts
    return [b for b in bench_day if b.ts <= t_end]


def relative_factors(
    stock_prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    *,
    rs_bars: int = 6,
) -> dict[str, Any]:
    """PIT relative-to-market factors (stock vs aligned bench prefix)."""
    out: dict[str, Any] = {
        "ready": False,
        "rs_sign": 0,
        "excess_pct": None,
        "vs_idx_vwap": 0,
        "vs_idx_orb": 0,
        "idx_or_state": None,  # above/below/inside
        "stock_vs_vwap_pct": None,
        "idx_vs_vwap_pct": None,
    }
    if len(stock_prev) < TA30M_OR_BARS:
        return out
    bprev = _align_bench_prefix(stock_prev, bench_day)
    if len(bprev) < TA30M_OR_BARS:
        return out
    out["ready"] = True

    s_ret = _ret_pct(stock_prev, rs_bars)
    b_ret = _ret_pct(bprev, rs_bars)
    if s_ret is not None and b_ret is not None:
        ex = s_ret - b_ret
        out["excess_pct"] = round(ex, 4)
        out["rs_sign"] = _sign_eps(ex, RS_EPS_PCT)

    s_vwap = session_vwap(stock_prev)
    b_vwap = session_vwap(bprev)
    if s_vwap and b_vwap and s_vwap > 0 and b_vwap > 0:
        s_vs = 100.0 * (stock_prev[-1].close / s_vwap - 1.0)
        b_vs = 100.0 * (bprev[-1].close / b_vwap - 1.0)
        out["stock_vs_vwap_pct"] = round(s_vs, 4)
        out["idx_vs_vwap_pct"] = round(b_vs, 4)
        out["vs_idx_vwap"] = _sign_eps(s_vs - b_vs, RS_EPS_PCT)

    or_hl = opening_range_hl(bprev)
    if or_hl is not None:
        or_hi, or_lo = or_hl
        bc = bprev[-1].close
        if bc > or_hi:
            out["idx_or_state"] = "above"
            idx_or = 1
        elif bc < or_lo:
            out["idx_or_state"] = "below"
            idx_or = -1
        else:
            out["idx_or_state"] = "inside"
            idx_or = 0
        # Stock OR break relative to index OR break.
        s_or = opening_range_hl(stock_prev)
        if s_or is not None:
            s_hi, s_lo = s_or
            sc = stock_prev[-1].close
            if sc > s_hi:
                s_brk = 1
            elif sc < s_lo:
                s_brk = -1
            else:
                s_brk = 0
            # +1 if stock breaks same side as index; -1 opposite; 0 if either flat
            if s_brk != 0 and idx_or != 0:
                out["vs_idx_orb"] = 1 if s_brk == idx_or else -1
            elif s_brk != 0 and idx_or == 0:
                out["vs_idx_orb"] = s_brk  # stock alone breaks
            else:
                out["vs_idx_orb"] = 0
    return out


def signal_temp(
    stock_prev: Sequence[Bar],
    bench_day: Sequence[Bar],
    variant: Variant,
    *,
    ovn_excess: float | None,
    rs_rank_q: float | None = None,
) -> int:
    if variant.kind == "fade":
        layer = fade_near_ext_from_bars(stock_prev, midday_only=True)
        if not layer.ready or layer.temp is None:
            return 0
        return int(layer.temp)

    fac = relative_factors(stock_prev, bench_day, rs_bars=variant.rs_bars)
    fade_layer = fade_near_ext_from_bars(stock_prev, midday_only=True)
    fade = (
        int(fade_layer.temp)
        if fade_layer.ready and fade_layer.temp is not None
        else 0
    )

    if variant.kind == "rs_sign":
        return int(fac["rs_sign"]) if fac.get("ready") else 0
    if variant.kind == "vs_idx_vwap":
        return int(fac["vs_idx_vwap"]) if fac.get("ready") else 0
    if variant.kind == "vs_idx_orb":
        return int(fac["vs_idx_orb"]) if fac.get("ready") else 0
    if variant.kind == "and_rs_vwap":
        if not fac.get("ready"):
            return 0
        a, b = int(fac["rs_sign"]), int(fac["vs_idx_vwap"])
        if a != 0 and a == b:
            return a
        return 0

    if variant.kind in {
        "fade_rs_against",
        "fade_rs_agree",
        "fade_ovn_same",
        "fade_ovn_opp",
        "fade_ovn_strong_same",
        "fade_idx_or_inside",
        "fade_rs_rank_hi",
    }:
        if fade == 0:
            return 0
        if variant.kind == "fade_rs_against":
            if not fac.get("ready"):
                return 0
            rs = int(fac["rs_sign"])
            return fade if rs == -fade else 0
        if variant.kind == "fade_rs_agree":
            if not fac.get("ready"):
                return 0
            rs = int(fac["rs_sign"])
            return fade if rs == fade else 0
        if variant.kind == "fade_ovn_same":
            if ovn_excess is None:
                return 0
            return fade if _sign_eps(ovn_excess, RS_EPS_PCT) == fade else 0
        if variant.kind == "fade_ovn_opp":
            if ovn_excess is None:
                return 0
            return fade if _sign_eps(ovn_excess, RS_EPS_PCT) == -fade else 0
        if variant.kind == "fade_ovn_strong_same":
            if ovn_excess is None or abs(ovn_excess) < OVN_STRONG_PCT:
                return 0
            return fade if _sign_eps(ovn_excess, 0.0) == fade else 0
        if variant.kind == "fade_idx_or_inside":
            if not fac.get("ready"):
                return 0
            return fade if fac.get("idx_or_state") == "inside" else 0
        if variant.kind == "fade_rs_rank_hi":
            # reserved; not in locked grid unless rank precomputed
            if rs_rank_q is None:
                return 0
            return fade if rs_rank_q >= 0.75 else 0
    return 0


def eval_day(
    stock_bars: list[Bar],
    bench_bars: list[Bar],
    variant: Variant,
    *,
    horizon: int,
    ovn_excess: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(stock_bars)):
        if i + horizon >= len(stock_bars):
            break
        prev = stock_bars[: i + 1]
        temp = signal_temp(prev, bench_bars, variant, ovn_excess=ovn_excess)
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
    ovn = load_overnight_excess(conn, stock_id, start=start, end=end)
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
        ovn_x = ovn.get(td)
        for v in variants:
            by_var[v.name][split].extend(
                eval_day(
                    bars,
                    bench,
                    v,
                    horizon=horizon,
                    ovn_excess=ovn_x,
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


def inventory_data(conn: sqlite3.Connection) -> dict[str, Any]:
    def kbar(sid: str) -> dict[str, Any]:
        r = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT trade_date),
                   MIN(trade_date), MAX(trade_date)
            FROM stock_kbar_5m WHERE stock_id=?
            """,
            (sid,),
        ).fetchone()
        n_is = conn.execute(
            """
            SELECT COUNT(DISTINCT trade_date) FROM stock_kbar_5m
            WHERE stock_id=? AND trade_date<=?
            """,
            (sid, DEFAULT_IS_END),
        ).fetchone()[0]
        n_oos = conn.execute(
            """
            SELECT COUNT(DISTINCT trade_date) FROM stock_kbar_5m
            WHERE stock_id=? AND trade_date>?
            """,
            (sid, DEFAULT_IS_END),
        ).fetchone()[0]
        return {
            "n_bars": r[0],
            "n_days": r[1],
            "min": r[2],
            "max": r[3],
            "n_days_IS": n_is,
            "n_days_OOS": n_oos,
        }

    ix_daily = conn.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_bars WHERE code=?",
        (BENCH_DAILY,),
    ).fetchone()
    sector = conn.execute(
        """
        SELECT stock_id, COUNT(DISTINCT trade_date)
        FROM stock_kbar_5m
        WHERE stock_id LIKE 'IX%' AND stock_id != 'IX0001'
        GROUP BY 1 LIMIT 10
        """
    ).fetchall()
    return {
        "bench_5m_0050": kbar("0050"),
        "taiex_5m_IX0001": kbar("IX0001"),
        "taiex_5m_TAIEX": kbar("TAIEX"),
        "taiex_daily_IX0001": {
            "n_rows": ix_daily[0],
            "min": ix_daily[1],
            "max": ix_daily[2],
        },
        "sector_5m_IX_other": [{"stock_id": a, "n_days": b} for a, b in sector],
        "note": (
            "IX0001 5m is OOS-only vs IS≤2025-09-30; "
            "intraday RS uses 0050 proxy; overnight RS uses IX0001 daily."
        ),
    }


def write_md(payload: dict[str, Any], path: Path) -> None:
    m = payload["metric"]
    champ = payload["is_champion"]
    gates = payload["gates"]
    inv = payload["data_inventory"]
    lines = [
        "# Track · 距離大盤的比例 / relative-to-market · ~30m",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Data reality",
        "",
        f"- Intraday bench: **`{BENCH_5M}` 5m** — "
        f"{inv['bench_5m_0050']['n_days']}d "
        f"({inv['bench_5m_0050']['min']}→{inv['bench_5m_0050']['max']}); "
        f"IS={inv['bench_5m_0050']['n_days_IS']} · "
        f"OOS={inv['bench_5m_0050']['n_days_OOS']}.",
        f"- True TAIEX **`IX0001` 5m**: "
        f"{inv['taiex_5m_IX0001']['n_days']}d "
        f"({inv['taiex_5m_IX0001']['min']}→{inv['taiex_5m_IX0001']['max']}); "
        f"**IS days={inv['taiex_5m_IX0001']['n_days_IS']}** "
        "→ cannot drive IS selection; OOS-only sensitivity.",
        f"- Daily TAIEX **`IX0001`**: {inv['taiex_daily_IX0001']['n_rows']} rows "
        f"({inv['taiex_daily_IX0001']['min']}→{inv['taiex_daily_IX0001']['max']}) "
        "→ overnight RS (D−1 excess) for next-day fade filter.",
        f"- Sector indices (other `IX*` 5m): "
        + (
            ", ".join(
                f"{x['stock_id']}({x['n_days']}d)"
                for x in inv["sector_5m_IX_other"]
            )
            or "**none**"
        )
        + ".",
        f"- Note: {inv['note']}",
        "",
        "## Metric / freeze",
        "",
        "- **Directed hit (~30m)**: `temp∈{±1}` and "
        f"`|fwd|≥{m['flat_eps']}`; hit iff `sign(temp)==sign(fwd)`.",
        f"- Forward: `close[i+{m['horizon']}]/close[i]-1` @ 5m.",
        "- PIT: stock+bench factors use completed same-session bars `≤ i`; "
        "overnight excess uses **D−1** only.",
        f"- IS / OOS: IS `≤ {m['is_end']}`; champion on **IS only**; claim on OOS.",
        f"- Universe (ex `{BENCH_5M}`): `{', '.join(m['stocks'])}`.",
        f"- Pre-registered variants: {m['n_variants']}.",
        f"- Window: **{m['start']} → {m['end']}**",
        "",
        f"## Verdict: OOS≥70% + stable（0050 proxy）？ "
        f"**{'YES' if gates['all_gates_pass'] else 'NO'}**",
        "",
        f"- IS champion: `{champ['name']}`",
        f"- Champion IS hit: **{champ['IS']['directed_hit_pct']}%** "
        f"(n={champ['IS']['n_directed']})",
        f"- Champion OOS hit (0050 bench): **{champ['OOS']['directed_hit_pct']}%** "
        f"(n={champ['OOS']['n_directed']})",
        f"- Best OOS among grid (exploratory): "
        f"`{payload['best_oos_exploratory']['name']}` → "
        f"**{payload['best_oos_exploratory']['OOS']['directed_hit_pct']}%** "
        f"(n={payload['best_oos_exploratory']['OOS']['n_directed']})",
        f"- Helps fade30 toward 70%? **{payload['helps_to_70']}**",
        "",
        "## Gates（IS champion OOS · 0050 proxy）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates['gate_oos_hit_ge70_n500']}**",
        f"- `gate_name_stability`: **{gates['gate_name_stability']}** "
        f"({gates['n_names_pass']}/{gates['n_names']} = {gates['name_pass_frac']})",
        f"- `gate_no_megacap_dom`: **{gates['gate_no_megacap_dom']}** "
        f"(max share {gates['max_name_share']} · {gates['max_name_sid']})",
        "",
    ]
    ix = payload.get("ix0001_oos_sensitivity") or {}
    if ix:
        ch = ix.get("champion") or {}
        fb = ix.get("fade_baseline") or {}
        lines += [
            "## OOS sensitivity · true TAIEX `IX0001` 5m（no IS）",
            "",
            f"- Champion `{champ['name']}`: "
            f"**{ch.get('directed_hit_pct')}%** (n={ch.get('n_directed')})",
            f"- Unfiltered fade: **{fb.get('directed_hit_pct')}%** "
            f"(n={fb.get('n_directed')})",
            "- Claim OOS≥70% on true index: "
            f"**{'YES' if payload.get('claim', {}).get('oos_ge_70_stable_ix0001') else 'NO'}**",
            "",
        ]
    lines += [
        "## Pool leaderboard（OOS directed 排序 · 0050 bench）",
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
        fade_ref = 62.13
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
        f"- Runner: `scripts/research/run_vs_market_30m_backtest.py`",
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
    inv = inventory_data(conn)
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

    # Pool + IS champion (max IS directed_hit among n_directed>=200)
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

    eligible = [
        r
        for r in leaderboard
        if (r["IS"].get("n_directed") or 0) >= 200
        and r["IS"].get("directed_hit_pct") is not None
    ]
    if not eligible:
        eligible = [r for r in leaderboard if r["IS"].get("directed_hit_pct") is not None]
    champ_row = max(eligible, key=lambda r: (r["IS"]["directed_hit_pct"], r["IS"]["n_directed"]))
    gates = stability_gates(champ_row["OOS"])
    best_oos = max(
        (r for r in leaderboard if r["OOS"].get("directed_hit_pct") is not None),
        key=lambda r: (r["OOS"]["directed_hit_pct"], r["OOS"]["n_directed"]),
    )

    fade_oos = next(
        (r for r in leaderboard if r["name"] == "baseline_fade_near_ext"), None
    )
    fade_hit = (
        fade_oos["OOS"]["directed_hit_pct"] if fade_oos else None
    )
    champ_oos_hit = champ_row["OOS"]["directed_hit_pct"]
    delta_vs_fade = (
        round(champ_oos_hit - fade_hit, 2)
        if champ_oos_hit is not None and fade_hit is not None
        else None
    )
    if gates["all_gates_pass"]:
        helps = "YES — gates pass (claim OOS≥70%)"
    elif champ_oos_hit is not None and fade_hit is not None and champ_oos_hit > fade_hit + 1.0:
        helps = (
            f"PARTIAL — IS champ OOS {champ_oos_hit}% vs fade {fade_hit}% "
            f"(Δ{delta_vs_fade}pp); still <70%"
        )
    elif champ_oos_hit is not None and fade_hit is not None and abs(champ_oos_hit - fade_hit) <= 1.0:
        helps = (
            f"NO meaningful lift — IS champ OOS≈fade "
            f"({champ_oos_hit}% vs {fade_hit}%, Δ{delta_vs_fade}pp)"
        )
    else:
        helps = (
            f"NO — IS champ OOS {champ_oos_hit}% "
            f"(fade ref {fade_hit}%; Δ{delta_vs_fade}pp); not toward 70%"
        )

    leaderboard_sorted = sorted(
        leaderboard,
        key=lambda r: (
            -(r["OOS"]["directed_hit_pct"] or -1),
            -(r["OOS"]["n_directed"] or 0),
        ),
    )

    # OOS-only sensitivity: same champion rule with IX0001 5m (no IS days).
    print("  IX0001 OOS sensitivity …", flush=True)
    conn_ix = sqlite3.connect(str(args.db))
    ix_bench = load_days(conn_ix, "IX0001", start=args.start, end=args.end)
    ix_rows: list[dict[str, Any]] = []
    ix_fade_rows: list[dict[str, Any]] = []
    champ_v = next(v for v in variants if v.name == champ_row["name"])
    fade_v = next(v for v in variants if v.name == "baseline_fade_near_ext")
    for sid in stocks:
        days = load_days(conn_ix, sid, start=args.start, end=args.end)
        ovn = load_overnight_excess(conn_ix, sid, start=args.start, end=args.end)
        for td, bars in days.items():
            if td <= args.is_end:
                continue
            b = ix_bench.get(td)
            if not b:
                continue
            ix_rows.extend(
                eval_day(bars, b, champ_v, horizon=args.horizon, ovn_excess=ovn.get(td))
            )
            ix_fade_rows.extend(
                eval_day(bars, b, fade_v, horizon=args.horizon, ovn_excess=ovn.get(td))
            )
    conn_ix.close()
    ix_sens = {
        "bench": "IX0001",
        "scope": "OOS_only_no_IS",
        "champion": summarize(ix_rows, flat_eps=args.flat_eps),
        "fade_baseline": summarize(ix_fade_rows, flat_eps=args.flat_eps),
    }

    interpretation = (
        "Standalone intraday RS (excess 30m/60m/day), vs-index-VWAP, and "
        "vs-index-ORB are ~coin-flip or worse — same failure mode as raw mom. "
        "Overnight IX0001 RS filters on fade30 mostly thin the sample without "
        "clearing 70%. The IS-locked champion under the **0050** proxy protocol "
        "is **fade_idx_or_inside**: fade_near_ext only when the proxy opening "
        "range is still intact. That lifts unfiltered fade OOS ~64% → ~70.7% "
        f"(n={champ_row['OOS']['n_directed']}). "
        f"OOS sensitivity with true TAIEX IX0001 5m: champ "
        f"{ix_sens['champion']['directed_hit_pct']}% "
        f"(n={ix_sens['champion']['n_directed']}) — **below 70%**, so the "
        "gate pass is proxy-dependent. Caveats: IX0001 5m has 0 IS days; "
        "n barely ≥500 on 0050; 2330 still strong; soft names near 55% floor. "
        "Do not rewrite Live ta_30m_bias without a fresh holdout / true-index "
        "confirmation."
    )
    if (
        ix_sens["champion"]["directed_hit_pct"] is not None
        and ix_sens["champion"]["directed_hit_pct"] < 70.0
    ):
        helps = (
            f"PARTIAL — 0050-proxy gates pass (OOS {champ_oos_hit}%), but "
            f"IX0001 OOS sensitivity "
            f"{ix_sens['champion']['directed_hit_pct']}% <70%; "
            "not robust to true TAIEX yet"
        )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "vs_market_30m_backtest.json"
    md_path = out_dir / "TRACK_VS_MARKET.md"

    payload: dict[str, Any] = {
        "topic": "intraday-direction-thermometer · track vs market",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_inventory": inv,
        "metric": {
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": args.end,
            "stocks": list(stocks),
            "bench_5m": BENCH_5M,
            "bench_daily_overnight": BENCH_DAILY,
            "n_variants": len(variants),
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
        "gates": gates,
        "helps_to_70": helps,
        "delta_vs_fade_oos_pp": delta_vs_fade,
        "ix0001_oos_sensitivity": ix_sens,
        "leaderboard": leaderboard_sorted,
        "per_stock": stock_results,
        "interpretation": interpretation,
        "artifacts": {
            "json": str(json_path.relative_to(ROOT)),
            "md": str(md_path.relative_to(ROOT)),
        },
        "claim": {
            "oos_ge_70_stable_0050_proxy": gates["all_gates_pass"],
            "oos_ge_70_stable_ix0001": bool(
                (ix_sens["champion"]["directed_hit_pct"] or 0) >= 70.0
                and (ix_sens["champion"]["n_directed"] or 0) >= MIN_OOS_N
            ),
            "live_bias_updated": False,
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
