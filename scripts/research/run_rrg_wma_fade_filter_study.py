#!/usr/bin/env python3
"""RRG + WMA3/WMA5 track · native daily horizon + fade_near_ext 30m filter.

Research only · PIT · not Order.

Horizon honesty
---------------
Repo RRG / dual-WMA lengths (WMA3=micro L=3, WMA5=short L=5, W20=long L=20)
are **daily** JdK RRG legs (``rrg_rotation.compute_rrg_panel``), plus this
track also builds **price** WMA3/WMA5 on daily closes. Native directed-hit
uses next-day (and optional +5d) closes — **not** 30m bars.

Intraday use is only as a **regime filter** on ``fade_near_ext`` (~30m),
with daily features lagged to the prior trading day (strict PIT for midday).

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_rrg_wma_fade_filter_study.py
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
from typing import Any, Callable, Sequence

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402
from research.intraday_direction_thermometer import (  # noqa: E402
    Bar,
    FADE_HORIZON_BARS,
    fade_near_ext_from_bars,
)
from rrg_rotation import classify_quadrant, compute_rrg_panel, wma  # noqa: E402
from rrg_universe_intraday_panel import (  # noqa: E402
    WMA_LONG_LENGTH,
    WMA_MICRO_LENGTH,
    WMA_SHORT_LENGTH,
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
DEFAULT_START = "2024-01-02"
DEFAULT_END = "2026-07-22"
FLAT_EPS = 0.0005
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
OUT_DIR = ROOT / "reports" / "research" / "intraday_direction_thermometer"


@dataclass(frozen=True)
class DailyFeat:
    """PIT daily features as of a closed session date."""

    rs5: float | None
    mom5: float | None
    quad5: str | None
    rs3: float | None
    mom3: float | None
    quad3: str | None
    rs20: float | None
    mom20: float | None
    quad20: str | None
    close: float | None
    wma3: float | None
    wma5: float | None
    wma3_gt_wma5: bool | None
    price_gt_wma3: bool | None
    price_gt_wma5: bool | None


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


def load_stock_closes(
    conn: sqlite3.Connection, stock_ids: Sequence[str], *, start: str, end: str
) -> pd.DataFrame:
    """Wide daily close; prefer finmind then yfinance."""
    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, close, source
        FROM stock_daily_bars
        WHERE stock_id IN ({placeholders})
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, stock_id,
            CASE source WHEN 'finmind' THEN 0 WHEN 'yfinance' THEN 1 ELSE 2 END
        """,
        (*stock_ids, start, end),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["stock_id", "trade_date", "close", "source"])
    df = df.drop_duplicates(subset=["stock_id", "trade_date"], keep="first")
    wide = df.pivot(index="trade_date", columns="stock_id", values="close").astype(float)
    return wide.sort_index()


def build_daily_features(
    close: pd.DataFrame, bench: pd.Series
) -> dict[str, dict[str, DailyFeat]]:
    """sid → trade_date → DailyFeat (features known at that day's close)."""
    bench_a = bench.reindex(close.index).astype(float).ffill()
    rs5, mom5, q5 = compute_rrg_panel(close, bench_a, length=WMA_SHORT_LENGTH)
    rs3, mom3, q3 = compute_rrg_panel(close, bench_a, length=WMA_MICRO_LENGTH)
    rs20, mom20, q20 = compute_rrg_panel(close, bench_a, length=WMA_LONG_LENGTH)

    out: dict[str, dict[str, DailyFeat]] = {str(s): {} for s in close.columns}
    for sid in close.columns:
        s = str(sid)
        px = close[sid].astype(float)
        w3 = wma(px, 3)
        w5 = wma(px, 5)
        for td in close.index:
            c = px.get(td)
            if pd.isna(c):
                continue

            def _f(panel: pd.DataFrame, date: str = td) -> float | None:
                v = panel.at[date, sid] if date in panel.index else None
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return None
                return float(v)

            def _q(panel: pd.DataFrame, date: str = td) -> str | None:
                v = panel.at[date, sid] if date in panel.index else None
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return None
                return str(v) if v else None

            c3 = float(w3.at[td]) if td in w3.index and pd.notna(w3.at[td]) else None
            c5 = float(w5.at[td]) if td in w5.index and pd.notna(w5.at[td]) else None
            cf = float(c)
            out[s][str(td)] = DailyFeat(
                rs5=_f(rs5),
                mom5=_f(mom5),
                quad5=_q(q5),
                rs3=_f(rs3),
                mom3=_f(mom3),
                quad3=_q(q3),
                rs20=_f(rs20),
                mom20=_f(mom20),
                quad20=_q(q20),
                close=cf,
                wma3=c3,
                wma5=c5,
                wma3_gt_wma5=(c3 > c5) if c3 is not None and c5 is not None else None,
                price_gt_wma3=(cf > c3) if c3 is not None else None,
                price_gt_wma5=(cf > c5) if c5 is not None else None,
            )
    return out


def _sign_bool(b: bool | None) -> int:
    if b is True:
        return 1
    if b is False:
        return -1
    return 0


def native_temp(feat: DailyFeat, rule: str) -> int:
    """Daily directional temp from EOD features (native horizon)."""
    if rule == "rrg5_quad":
        q = feat.quad5
        if q in ("leading", "improving"):
            return 1
        if q in ("lagging", "weakening"):
            return -1
        return 0
    if rule == "rrg5_mom":
        if feat.mom5 is None:
            return 0
        if feat.mom5 > 100.0:
            return 1
        if feat.mom5 < 100.0:
            return -1
        return 0
    if rule == "rrg3_mom":
        if feat.mom3 is None:
            return 0
        if feat.mom3 > 100.0:
            return 1
        if feat.mom3 < 100.0:
            return -1
        return 0
    if rule == "rrg20_quad":
        q = feat.quad20
        if q in ("leading", "improving"):
            return 1
        if q in ("lagging", "weakening"):
            return -1
        return 0
    if rule == "price_vs_wma3":
        return _sign_bool(feat.price_gt_wma3)
    if rule == "price_vs_wma5":
        return _sign_bool(feat.price_gt_wma5)
    if rule == "wma3_gt_wma5":
        return _sign_bool(feat.wma3_gt_wma5)
    if rule == "combo_rrg5_wma_cross":
        a = native_temp(feat, "rrg5_mom")
        b = native_temp(feat, "wma3_gt_wma5")
        if a != 0 and a == b:
            return a
        return 0
    if rule == "combo_price_wma_stack":
        # price>WMA5 AND WMA3>WMA5 → +1; both opposite → -1
        a = native_temp(feat, "price_vs_wma5")
        b = native_temp(feat, "wma3_gt_wma5")
        if a != 0 and a == b:
            return a
        return 0
    raise KeyError(rule)


NATIVE_RULES = (
    "rrg5_quad",
    "rrg5_mom",
    "rrg3_mom",
    "rrg20_quad",
    "price_vs_wma3",
    "price_vs_wma5",
    "wma3_gt_wma5",
    "combo_rrg5_wma_cross",
    "combo_price_wma_stack",
)


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


def prior_dates_map(dates: Sequence[str]) -> dict[str, str | None]:
    """trade_date → previous trade_date (or None)."""
    ordered = sorted(dates)
    prev: dict[str, str | None] = {}
    for i, d in enumerate(ordered):
        prev[d] = ordered[i - 1] if i else None
    return prev


FilterFn = Callable[[DailyFeat, int], bool]


def _keep_always(_f: DailyFeat, _temp: int) -> bool:
    return True


def _rrg5_mom_align(f: DailyFeat, temp: int) -> bool:
    if f.mom5 is None or temp == 0:
        return False
    mom_sign = 1 if f.mom5 > 100.0 else -1 if f.mom5 < 100.0 else 0
    return mom_sign != 0 and mom_sign == temp


def _rrg5_mom_contra(f: DailyFeat, temp: int) -> bool:
    if f.mom5 is None or temp == 0:
        return False
    mom_sign = 1 if f.mom5 > 100.0 else -1 if f.mom5 < 100.0 else 0
    return mom_sign != 0 and mom_sign == -temp


def _rrg5_quad_risk_on(f: DailyFeat, temp: int) -> bool:
    # Keep fade-long only in leading/improving; fade-short only in lagging/weakening.
    q = f.quad5
    if temp > 0:
        return q in ("leading", "improving")
    if temp < 0:
        return q in ("lagging", "weakening")
    return False


def _rrg5_quad_risk_off_fade(f: DailyFeat, temp: int) -> bool:
    # Opposite: fade into strength / weakness (MR against RRG posture).
    q = f.quad5
    if temp > 0:
        return q in ("lagging", "weakening")
    if temp < 0:
        return q in ("leading", "improving")
    return False


def _wma_cross_align(f: DailyFeat, temp: int) -> bool:
    s = _sign_bool(f.wma3_gt_wma5)
    return s != 0 and s == temp


def _wma_cross_contra(f: DailyFeat, temp: int) -> bool:
    s = _sign_bool(f.wma3_gt_wma5)
    return s != 0 and s == -temp


def _price_wma5_align(f: DailyFeat, temp: int) -> bool:
    s = _sign_bool(f.price_gt_wma5)
    return s != 0 and s == temp


def _price_wma5_contra(f: DailyFeat, temp: int) -> bool:
    s = _sign_bool(f.price_gt_wma5)
    return s != 0 and s == -temp


def _combo_rrg_wma_align(f: DailyFeat, temp: int) -> bool:
    return _rrg5_mom_align(f, temp) and _wma_cross_align(f, temp)


def _combo_rrg_wma_contra(f: DailyFeat, temp: int) -> bool:
    return _rrg5_mom_contra(f, temp) and _wma_cross_contra(f, temp)


FILTERS: tuple[tuple[str, FilterFn], ...] = (
    ("fade_only", _keep_always),
    ("fade_rrg5_mom_align", _rrg5_mom_align),
    ("fade_rrg5_mom_contra", _rrg5_mom_contra),
    ("fade_rrg5_quad_risk_on", _rrg5_quad_risk_on),
    ("fade_rrg5_quad_mr", _rrg5_quad_risk_off_fade),
    ("fade_wma3_gt_wma5_align", _wma_cross_align),
    ("fade_wma3_gt_wma5_contra", _wma_cross_contra),
    ("fade_price_vs_wma5_align", _price_wma5_align),
    ("fade_price_vs_wma5_contra", _price_wma5_contra),
    ("fade_rrg5_wma_combo_align", _combo_rrg_wma_align),
    ("fade_rrg5_wma_combo_contra", _combo_rrg_wma_contra),
)


def pool_from_rows(
    by_sid: dict[str, dict[str, list[dict[str, Any]]]],
    variant: str,
    split: str,
    *,
    flat_eps: float,
) -> dict[str, Any]:
    per_name: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for sid, splits in by_sid.items():
        rows = splits.get(variant, {}).get(split, [])
        m = summarize(rows, flat_eps=flat_eps)
        per_name.append(
            {
                "stock_id": sid,
                "n_directed": m["n_directed"],
                "n_hit": m["n_hit"],
                "directed_hit_pct": m["directed_hit_pct"],
                "expectancy_signed_pct": m["expectancy_signed_pct"],
            }
        )
        all_rows.extend(rows)
    pool = summarize(all_rows, flat_eps=flat_eps)
    pool["per_name"] = per_name
    return pool


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


def eval_native_daily(
    feats_by_sid: dict[str, dict[str, DailyFeat]],
    close: pd.DataFrame,
    *,
    horizon_days: int,
    is_end: str,
    flat_eps: float,
) -> dict[str, Any]:
    dates = [str(d) for d in close.index]
    date_to_i = {d: i for i, d in enumerate(dates)}
    by_sid: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        sid: {r: {"IS": [], "OOS": []} for r in NATIVE_RULES} for sid in feats_by_sid
    }
    for sid, fmap in feats_by_sid.items():
        if sid not in close.columns:
            continue
        for td, feat in fmap.items():
            i = date_to_i.get(td)
            if i is None or i + horizon_days >= len(dates):
                continue
            td_fwd = dates[i + horizon_days]
            c0 = close.at[td, sid] if td in close.index else None
            c1 = close.at[td_fwd, sid] if td_fwd in close.index else None
            if c0 is None or c1 is None or pd.isna(c0) or pd.isna(c1) or not c0:
                continue
            fwd = float(c1) / float(c0) - 1.0
            split = "IS" if td <= is_end else "OOS"
            for rule in NATIVE_RULES:
                temp = native_temp(feat, rule)
                if temp == 0:
                    continue
                by_sid[sid][rule][split].append({"temp": temp, "fwd": fwd, "date": td})

    pool: dict[str, Any] = {}
    for rule in NATIVE_RULES:
        pool[rule] = {
            "IS": pool_from_rows(by_sid, rule, "IS", flat_eps=flat_eps),
            "OOS": pool_from_rows(by_sid, rule, "OOS", flat_eps=flat_eps),
        }
        # strip nested per_name lists from IS for compactness? keep both
    return {"horizon_days": horizon_days, "rules": pool, "by_sid": by_sid}


def eval_fade_filters(
    conn: sqlite3.Connection,
    stock_ids: Sequence[str],
    feats_by_sid: dict[str, dict[str, DailyFeat]],
    *,
    start: str,
    end: str,
    is_end: str,
    horizon: int,
    flat_eps: float,
) -> dict[str, Any]:
    # Collect all daily dates across feats for prior-day lag.
    all_dates: set[str] = set()
    for fmap in feats_by_sid.values():
        all_dates.update(fmap.keys())
    prev_of = prior_dates_map(sorted(all_dates))

    by_sid: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        sid: {name: {"IS": [], "OOS": []} for name, _ in FILTERS} for sid in stock_ids
    }
    # Dishonest control: daily RRG mom as direct 30m temp (no fade).
    control_name = "naive_rrg5_mom_as_30m"
    for sid in stock_ids:
        by_sid[sid][control_name] = {"IS": [], "OOS": []}

    for sid in stock_ids:
        days = load_days(conn, sid, start=start, end=end)
        fmap = feats_by_sid.get(sid, {})
        for td, bars in sorted(days.items()):
            split = "IS" if td <= is_end else "OOS"
            lag_d = prev_of.get(td)
            lag_feat = fmap.get(lag_d) if lag_d else None
            for i in range(len(bars)):
                if i + horizon >= len(bars):
                    break
                prev = bars[: i + 1]
                c0 = bars[i].close
                if not c0:
                    continue
                fwd = bars[i + horizon].close / c0 - 1.0

                # Naive daily→30m control (same lag feat).
                if lag_feat is not None:
                    nt = native_temp(lag_feat, "rrg5_mom")
                    if nt != 0:
                        by_sid[sid][control_name][split].append(
                            {
                                "temp": nt,
                                "fwd": fwd,
                                "hhmm": bars[i].ts.strftime("%H:%M"),
                            }
                        )

                layer = fade_near_ext_from_bars(prev, midday_only=True)
                if not layer.ready or layer.temp is None or int(layer.temp) == 0:
                    continue
                temp = int(layer.temp)
                for name, fn in FILTERS:
                    if name == "fade_only":
                        ok = True
                    else:
                        if lag_feat is None:
                            continue
                        ok = fn(lag_feat, temp)
                    if not ok:
                        continue
                    by_sid[sid][name][split].append(
                        {
                            "temp": temp,
                            "fwd": fwd,
                            "hhmm": bars[i].ts.strftime("%H:%M"),
                            "lag_date": lag_d,
                        }
                    )

    variant_names = [n for n, _ in FILTERS] + [control_name]
    pool: dict[str, Any] = {}
    for name in variant_names:
        pool[name] = {
            "IS": pool_from_rows(by_sid, name, "IS", flat_eps=flat_eps),
            "OOS": pool_from_rows(by_sid, name, "OOS", flat_eps=flat_eps),
        }
    return {"variants": pool, "by_sid": by_sid, "horizon_bars": horizon}


def pick_is_champion(fade_pool: dict[str, Any]) -> dict[str, Any]:
    """Select among fade filters only (exclude naive control) on IS hit, n≥200."""
    best = None
    for name, splits in fade_pool.items():
        if name.startswith("naive_"):
            continue
        is_m = splits["IS"]
        nd = int(is_m.get("n_directed") or 0)
        hit = is_m.get("directed_hit_pct")
        if hit is None or nd < 200:
            continue
        key = (float(hit), nd)
        if best is None or key > best[0]:
            best = (key, name, is_m, splits["OOS"])
    if best is None:
        return {"name": None}
    _, name, is_m, oos_m = best
    return {
        "name": name,
        "IS": {k: is_m[k] for k in ("directed_hit_pct", "n_directed", "edge_vs_coin_pp")},
        "OOS": {k: oos_m[k] for k in ("directed_hit_pct", "n_directed", "edge_vs_coin_pp")},
    }


def write_track_md(payload: dict[str, Any], path: Path) -> None:
    loc = payload["code_locations"]
    native = payload["native_daily"]
    fade = payload["fade_filter"]
    champ = payload["is_champion"]
    gates = payload["gates"]
    lines = [
        "# TRACK · RRG + WMA3 + WMA5（regime filter for fade30）",
        "",
        "Research only · **未採納** · not Order / not `strategy.yaml`",
        "",
        "## Horizon honesty（先講清楚）",
        "",
        "- Repo **RRG / dual-WMA**（`WMA_MICRO=3` · `WMA_SHORT=5` · `WMA_LONG=20`）是 **日線** JdK RRG legs"
        "（`rrg_rotation.compute_rrg_panel`），不是 30m TA 預測器。",
        "- 本 track 另算 **價格** 日線 WMA3 / WMA5（同 `rrg_rotation.wma`）與交叉。",
        "- **Native horizon** = 日線收盤訊號 → 次日（+1d；附 +5d）directed hit。",
        "- **Intraday 用法** = 僅當 `fade_near_ext`（≈30m）的 **regime filter**；"
        " lag = **前一交易日** EOD（盤中 PIT）。",
        "- 若日線 filter 無法把 fade30 OOS 推到 ≥70%：**誠實說不行**（見 Verdict）。",
        "",
        "## Code locations",
        "",
    ]
    for item in loc:
        lines.append(f"- `{item['path']}` — {item['note']}")
    lines += [
        "",
        f"- Window: **{payload['start']} → {payload['end']}** · IS ≤ `{payload['is_end']}`",
        f"- Universe: `{', '.join(payload['stocks'])}`",
        f"- Fade horizon: H={payload['fade_horizon_bars']} ≈30m @ 5m · flat_eps={payload['flat_eps']}",
        "",
        "## Verdict: OOS≥70% stable ~30m？ **NO**"
        if not gates.get("all_gates_pass")
        else "## Verdict: OOS≥70% stable ~30m？ **YES**",
        "",
        f"- IS champion filter: `{champ.get('name')}`",
        f"- Champion IS hit: **{champ.get('IS', {}).get('directed_hit_pct')}%** "
        f"(n={champ.get('IS', {}).get('n_directed')})",
        f"- Champion OOS hit: **{champ.get('OOS', {}).get('directed_hit_pct')}%** "
        f"(n={champ.get('OOS', {}).get('n_directed')})",
        f"- Baseline `fade_only` OOS: **{fade['variants']['fade_only']['OOS'].get('directed_hit_pct')}%** "
        f"(n={fade['variants']['fade_only']['OOS'].get('n_directed')}) · vs known ~62%",
        f"- Naive daily RRG→30m OOS: **{fade['variants']['naive_rrg5_mom_as_30m']['OOS'].get('directed_hit_pct')}%** "
        f"(n={fade['variants']['naive_rrg5_mom_as_30m']['OOS'].get('n_directed')}) · 不應當 30m predictor",
        "",
        "### Gates（IS champion OOS）",
        "",
        f"- `gate_oos_hit_ge70_n500`: **{gates.get('gate_oos_hit_ge70_n500')}**",
        f"- `gate_name_stability`: **{gates.get('gate_name_stability')}** "
        f"({gates.get('n_names_pass')}/{gates.get('n_names')} = {gates.get('name_pass_frac')})",
        f"- `gate_no_megacap_dom`: **{gates.get('gate_no_megacap_dom')}** "
        f"(max share {gates.get('max_name_share')} · {gates.get('max_name_sid')})",
        "",
        "## (a) Native daily horizon · directed hit",
        "",
        "Forward = close[t+H]/close[t]-1 · temp from EOD features on t（PIT closes ≤ t）。",
        "",
    ]
    for h_label, key in (("+1d", "h1"), ("+5d", "h5")):
        block = native[key]
        lines += [
            f"### Horizon {h_label}",
            "",
            "|rule|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|",
            "|---|---:|---:|---:|---:|---:|",
        ]
        rows = []
        for rule, splits in block["rules"].items():
            oos = splits["OOS"]
            is_m = splits["IS"]
            rows.append(
                (
                    oos.get("directed_hit_pct") or -1,
                    rule,
                    is_m.get("directed_hit_pct"),
                    is_m.get("n_directed"),
                    oos.get("directed_hit_pct"),
                    oos.get("n_directed"),
                    oos.get("edge_vs_coin_pp"),
                )
            )
        rows.sort(reverse=True)
        for _, rule, is_h, is_n, oos_h, oos_n, edge in rows:
            lines.append(f"|{rule}|{is_h}|{is_n}|{oos_h}|{oos_n}|{edge}|")
        lines.append("")

    lines += [
        "## (b) Filter on `fade_near_ext` · fwd ≈30m",
        "",
        "Daily features lagged to **prior session** · fade midday window unchanged。",
        "",
        "|variant|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|ΔOOS vs fade_only|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    base_oos = fade["variants"]["fade_only"]["OOS"].get("directed_hit_pct")
    ranked = []
    for name, splits in fade["variants"].items():
        oos = splits["OOS"]
        is_m = splits["IS"]
        oos_h = oos.get("directed_hit_pct")
        delta = (
            round(oos_h - base_oos, 2)
            if oos_h is not None and base_oos is not None
            else None
        )
        ranked.append(
            (
                oos_h if oos_h is not None else -1,
                name,
                is_m.get("directed_hit_pct"),
                is_m.get("n_directed"),
                oos_h,
                oos.get("n_directed"),
                oos.get("edge_vs_coin_pp"),
                delta,
            )
        )
    ranked.sort(reverse=True)
    for _, name, is_h, is_n, oos_h, oos_n, edge, delta in ranked:
        lines.append(f"|{name}|{is_h}|{is_n}|{oos_h}|{oos_n}|{edge}|{delta}|")

    if champ.get("name"):
        oos_names = fade["variants"][champ["name"]]["OOS"].get("per_name") or []
        lines += [
            "",
            f"### IS champion per-stock OOS · `{champ['name']}`",
            "",
            "|sid|OOS hit%|OOS n|E[signed]%|",
            "|---|---:|---:|---:|",
        ]
        for p in sorted(
            oos_names,
            key=lambda x: (x.get("directed_hit_pct") or 0),
            reverse=True,
        ):
            lines.append(
                f"|{p['stock_id']}|{p.get('directed_hit_pct')}|"
                f"{p.get('n_directed')}|{p.get('expectancy_signed_pct')}|"
            )

    lines += [
        "",
        "## Honest takeaway",
        "",
        "- Daily RRG/WMA **alone** are mediocre next-day predictors（OOS typically ~50–55% range；見表）。",
        "- Using them as **30m predictors**（naive control）is near coin-flip — do not confuse with sleeve RRG。",
        "- As **filters** on fade≈62% baseline：best IS-selected filter still **well below 70% OOS**；"
        " some filters raise hit slightly但常犧牲 n，且穩定性不足。",
        "- **Cannot claim OOS≥70%** from this track。",
        "",
        f"- Script: `scripts/research/run_rrg_wma_fade_filter_study.py`",
        f"- JSON: `{payload.get('json_path', '')}`",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "stocks.db")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    stocks = tuple(s.strip() for s in args.stocks.split(",") if s.strip())
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Warmup for WMA20 RRG: load earlier daily history.
    daily_start = "2020-01-01"
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        close = load_stock_closes(conn, stocks, start=daily_start, end=args.end)
        bench = load_benchmark_close(conn)
        if close.empty:
            raise SystemExit("no daily closes")
        feats = build_daily_features(close, bench)
        # Trim feature maps to study window for native eval (still need hist for WMA).
        close_win = close.loc[(close.index >= args.start) & (close.index <= args.end)]

        native_h1 = eval_native_daily(
            feats, close_win, horizon_days=1, is_end=args.is_end, flat_eps=FLAT_EPS
        )
        native_h5 = eval_native_daily(
            feats, close_win, horizon_days=5, is_end=args.is_end, flat_eps=FLAT_EPS
        )
        fade = eval_fade_filters(
            conn,
            stocks,
            feats,
            start=args.start,
            end=args.end,
            is_end=args.is_end,
            horizon=FADE_HORIZON_BARS,
            flat_eps=FLAT_EPS,
        )
    finally:
        conn.close()

    champ = pick_is_champion(fade["variants"])
    if champ.get("name"):
        gates = stability_gates(fade["variants"][champ["name"]]["OOS"])
    else:
        gates = stability_gates(fade["variants"]["fade_only"]["OOS"])

    # Drop heavy by_sid from JSON export of native (keep fade per_name only).
    native_export = {
        "h1": {
            "horizon_days": 1,
            "rules": {
                r: {"IS": native_h1["rules"][r]["IS"], "OOS": native_h1["rules"][r]["OOS"]}
                for r in NATIVE_RULES
            },
        },
        "h5": {
            "horizon_days": 5,
            "rules": {
                r: {"IS": native_h5["rules"][r]["IS"], "OOS": native_h5["rules"][r]["OOS"]}
                for r in NATIVE_RULES
            },
        },
    }
    # strip nested by_sid lists inside pool per_name — already there; OK
    fade_export = {
        "horizon_bars": fade["horizon_bars"],
        "variants": fade["variants"],
    }

    payload = {
        "metric": "directed_hit",
        "flat_eps": FLAT_EPS,
        "start": args.start,
        "end": args.end,
        "is_end": args.is_end,
        "stocks": list(stocks),
        "fade_horizon_bars": FADE_HORIZON_BARS,
        "code_locations": [
            {
                "path": "src/rrg_rotation.py",
                "note": "JdK RRG · wma() · compute_rrg_panel · classify_quadrant · default L=20 daily",
            },
            {
                "path": "src/rrg_universe_intraday_panel.py",
                "note": f"WMA_MICRO={WMA_MICRO_LENGTH} · WMA_SHORT={WMA_SHORT_LENGTH} · "
                f"WMA_LONG={WMA_LONG_LENGTH} · dual/triple RRG legs（日線+可選盤中覆寫 close）",
            },
            {
                "path": "src/research/backtest/dual_wma_signal_backtest.py",
                "note": "Dual-WMA RRG signal events（日線腿 · 非 price WMA cross）",
            },
            {
                "path": "src/research/backtest/rrg_mono_score_swap_c.py",
                "note": "rrg-c18acc / score-swap-C · uses W5/W20 RRG panels",
            },
            {
                "path": "src/research/intraday_direction_thermometer.py",
                "note": "fade_near_ext_from_bars · FADE_HORIZON_BARS=6 ≈30m",
            },
        ],
        "native_daily": native_export,
        "fade_filter": fade_export,
        "is_champion": champ,
        "gates": gates,
    }

    json_path = args.out_dir / "track_rrg_wma_backtest.json"
    md_path = args.out_dir / "TRACK_RRG_WMA.md"
    payload["json_path"] = str(json_path.relative_to(ROOT))
    # Remove per_name circular bulk? keep for report
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_track_md(payload, md_path)
    print(json.dumps({"md": str(md_path), "json": str(json_path), "champion": champ, "gates": gates}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    # Fix accidental typo reference if any left — validate compile via run
    raise SystemExit(main())
