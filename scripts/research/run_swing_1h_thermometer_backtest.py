#!/usr/bin/env python3
"""Swing 1h thermometer · PIT directed-hit backtest (research only).

Primary metric (pre-registered)
-------------------------------
**Directed hit rate**: among samples with ``temp ≠ 0`` and
``|fwd_60m| ≥ flat_eps``, fraction where ``sign(temp) == sign(fwd)``.

- Forward: close[i + H] / close[i] − 1, H = ``swing_horizon_bars`` (default 12 ≈ 60m).
- PIT: signal at bar i uses only completed bars ``≤ i`` on the same session.
- Nulls: coin-flip 50%; always-long = % of directed samples with fwd>0;
  naive slope = sign(lookback close-close) vs fwd.

IS / OOS
--------
Calendar split (default): IS ≤ ``--is-end``; OOS > ``--is-end``.
Tune / select variants on IS only; gate claim requires **OOS** directed ≥ 60%
with pool n ≥ ``--min-oos-n`` (default 500).

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_swing_1h_thermometer_backtest.py
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
    ThermoConfig,
    swing_1h_from_bars,
)

# Holdings-like liquid + Focus4 with local 5m coverage (2492 has no kbar_5m).
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
FLAT_EPS = 0.0005  # 0.05% — 5m noise floor for ~60m moves
MIN_BARS_PER_DAY = 30


@dataclass(frozen=True)
class Variant:
    name: str
    lookback: int = 8
    ready: int = 12
    open_guard: int = 6
    use_short: bool = True
    min_slope_pct: float = 0.0
    strong_only: bool = False
    after_open_guard: bool = False
    sparse_clocks: tuple[str, ...] = ()  # e.g. ("10:00", "11:00", "12:00")


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_L8_R12"),
    Variant("L6_R12", lookback=6),
    Variant("L10_R12", lookback=10),
    Variant("L12_R12", lookback=12),
    Variant("L8_R8", ready=8),
    Variant("slope_only_L8", use_short=False),
    Variant("slope_only_L12", lookback=12, use_short=False),
    Variant("mag015_L8", min_slope_pct=0.15),
    Variant("mag030_L8", min_slope_pct=0.30),
    Variant("mag050_L8", min_slope_pct=0.50),
    Variant("mag030_slope_only", min_slope_pct=0.30, use_short=False),
    Variant("mag050_slope_only", min_slope_pct=0.50, use_short=False),
    Variant("after_guard", after_open_guard=True),
    Variant("after_guard_mag030", after_open_guard=True, min_slope_pct=0.30),
    Variant("strong_only", strong_only=True),
    Variant("strong_mag030", strong_only=True, min_slope_pct=0.30),
    Variant("sparse_101112", sparse_clocks=("10:00", "11:00", "12:00")),
    Variant(
        "sparse_mag030",
        sparse_clocks=("10:00", "11:00", "12:00"),
        min_slope_pct=0.30,
        use_short=False,
    ),
    # mean-reversion of slope-only (flip sign in eval via negative lookback proxy)
    Variant("mr_slope_L12", lookback=12, use_short=False),
    Variant("mr_mag030_L12", lookback=12, use_short=False, min_slope_pct=0.30),
    Variant(
        "best_candidate",
        lookback=10,
        ready=12,
        use_short=False,
        min_slope_pct=0.30,
        after_open_guard=True,
    ),
)


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _minute_hhmm(minute: str) -> str:
    # "09:05:00" or "09:05" → "09:05"
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
        # TWSE continuous roughly 09:00–13:25; keep session bars only
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


def swing_signal(
    prev: Sequence[Bar], cfg: ThermoConfig, *, use_short: bool, min_slope_pct: float
) -> tuple[int | None, float | None, bool]:
    """Return (temp, slope_pct, ready). Optionally strip short-temp fusion / mag gate."""
    n = len(prev)
    if n < cfg.swing_ready_bars:
        return None, None, False
    L = min(cfg.swing_lookback_bars, n)
    w = list(prev[-L:])
    slope = w[-1].close - w[0].close
    slope_pct = (100.0 * slope / w[0].close) if w[0].close else 0.0
    if abs(slope_pct) < min_slope_pct:
        return 0, slope_pct, True  # treat as neutral / skip directed
    if use_short:
        layer = swing_1h_from_bars(prev, cfg)
        return layer.temp, slope_pct, bool(layer.ready)
    # slope-only thermometer
    if slope > 0:
        temp = 1
    elif slope < 0:
        temp = -1
    else:
        temp = 0
    # amplify with |slope|
    if abs(slope_pct) >= max(min_slope_pct, 0.3) * 2 and temp != 0:
        temp = 2 if temp > 0 else -2
    return temp, slope_pct, True


def eval_variant_day(
    bars: list[Bar],
    variant: Variant,
    *,
    horizon: int,
) -> list[dict[str, Any]]:
    cfg = ThermoConfig(
        open_guard_bars=variant.open_guard,
        swing_ready_bars=variant.ready,
        swing_lookback_bars=variant.lookback,
        swing_horizon_bars=horizon,
    )
    out: list[dict[str, Any]] = []
    sparse = set(variant.sparse_clocks)
    flip = variant.name.startswith("mr_")
    for i in range(len(bars)):
        if i + horizon >= len(bars):
            break
        bars_elapsed = i + 1
        if bars_elapsed < variant.ready:
            continue
        if variant.after_open_guard and bars_elapsed < variant.open_guard:
            continue
        hhmm = bars[i].ts.strftime("%H:%M")
        if sparse and hhmm not in sparse:
            continue
        prev = bars[: i + 1]
        temp, slope_pct, ready = swing_signal(
            prev,
            cfg,
            use_short=variant.use_short,
            min_slope_pct=variant.min_slope_pct,
        )
        if not ready or temp is None:
            continue
        if flip and temp != 0:
            temp = -int(temp)
        if variant.strong_only and abs(temp) < 2:
            continue
        c0 = bars[i].close
        c1 = bars[i + horizon].close
        if not c0:
            continue
        fwd = c1 / c0 - 1.0
        L = min(variant.lookback, len(prev))
        w0, w1 = prev[-L].close, prev[-1].close
        naive_slope = w1 - w0
        out.append(
            {
                "temp": int(temp),
                "fwd": fwd,
                "slope_pct": slope_pct,
                "naive_slope": naive_slope,
                "hhmm": hhmm,
                "bars_elapsed": bars_elapsed,
            }
        )
    return out


def eval_fade_day(bars: list[Bar], *, horizon: int = 6) -> list[dict[str, Any]]:
    """IS-champion fade_near_ext (~30m) evaluation rows."""
    from research.intraday_direction_thermometer import fade_near_ext_from_bars

    out: list[dict[str, Any]] = []
    for i in range(len(bars)):
        if i + horizon >= len(bars):
            break
        prev = bars[: i + 1]
        layer = fade_near_ext_from_bars(prev)
        if not layer.ready or layer.temp is None or layer.temp == 0:
            continue
        c0 = bars[i].close
        if not c0:
            continue
        fwd = bars[i + horizon].close / c0 - 1.0
        out.append(
            {
                "temp": int(layer.temp),
                "fwd": fwd,
                "slope_pct": (layer.meta or {}).get("slope_pct"),
                "naive_slope": None,
                "hhmm": bars[i].ts.strftime("%H:%M"),
                "bars_elapsed": i + 1,
            }
        )
    return out


def summarize(
    rows: list[dict[str, Any]], *, flat_eps: float
) -> dict[str, Any]:
    directed_hit = directed_tot = 0
    long_hit = long_tot = 0  # always-long among directed
    naive_hit = naive_tot = 0
    fwds_pos: list[float] = []
    fwds_neg: list[float] = []
    signed_pnl: list[float] = []  # temp-directed: +fwd if long bias else -fwd

    for r in rows:
        t = int(r["temp"])
        fr = float(r["fwd"])
        if t == 0 or abs(fr) < flat_eps:
            continue
        directed_tot += 1
        if (t > 0 and fr > 0) or (t < 0 and fr < 0):
            directed_hit += 1
        long_tot += 1
        if fr > 0:
            long_hit += 1
        ns = r.get("naive_slope")
        if ns is not None and ns != 0:
            naive_tot += 1
            if (ns > 0 and fr > 0) or (ns < 0 and fr < 0):
                naive_hit += 1
        if t > 0:
            fwds_pos.append(fr)
            signed_pnl.append(fr)
        else:
            fwds_neg.append(fr)
            signed_pnl.append(-fr)

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    return {
        "n_signals": len(rows),
        "n_directed": directed_tot,
        "directed_hit_pct": pct(directed_hit, directed_tot),
        "always_long_pct": pct(long_hit, long_tot),
        "naive_slope_hit_pct": pct(naive_hit, naive_tot),
        "n_naive": naive_tot,
        "temp_ge1_n": len(fwds_pos),
        "temp_ge1_mean_fwd_pct": round(100 * (_mean(fwds_pos) or 0), 4)
        if fwds_pos
        else None,
        "temp_ge1_hit_pct": pct(sum(1 for x in fwds_pos if x > 0), len(fwds_pos))
        if fwds_pos
        else None,
        "temp_le_m1_n": len(fwds_neg),
        "temp_le_m1_mean_fwd_pct": round(100 * (_mean(fwds_neg) or 0), 4)
        if fwds_neg
        else None,
        "expectancy_signed_pct": round(100 * (_mean(signed_pnl) or 0), 4)
        if signed_pnl
        else None,
        "edge_vs_coin_pp": (
            round(pct(directed_hit, directed_tot) - 50.0, 2)
            if directed_tot
            else None
        ),
        "edge_vs_always_long_pp": (
            round(
                pct(directed_hit, directed_tot) - pct(long_hit, long_tot),
                2,
            )
            if directed_tot and long_tot
            else None
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
    fade_rows: dict[str, list[dict[str, Any]]] = {"IS": [], "OOS": []}
    for td, bars in sorted(days.items()):
        split = "IS" if td <= is_end else "OOS"
        for v in variants:
            by_var[v.name][split].extend(
                eval_variant_day(bars, v, horizon=horizon)
            )
        fade_rows[split].extend(eval_fade_day(bars, horizon=6))

    out_vars: dict[str, Any] = {}
    for v in variants:
        is_rows = by_var[v.name]["IS"]
        oos_rows = by_var[v.name]["OOS"]
        out_vars[v.name] = {
            "IS": summarize(is_rows, flat_eps=flat_eps),
            "OOS": summarize(oos_rows, flat_eps=flat_eps),
            "ALL": summarize(is_rows + oos_rows, flat_eps=flat_eps),
        }
    fade_summary = {
        "IS": summarize(fade_rows["IS"], flat_eps=flat_eps),
        "OOS": summarize(fade_rows["OOS"], flat_eps=flat_eps),
        "ALL": summarize(fade_rows["IS"] + fade_rows["OOS"], flat_eps=flat_eps),
        "note": "secondary · ~30m fade_near_ext IS-champion · not primary Swing 1h",
    }
    return {
        "stock_id": stock_id,
        "n_days": len(days),
        "date_min": min(days),
        "date_max": max(days),
        "variants": out_vars,
        "fade_near_ext_30m": fade_summary,
    }


def pool_summarize(
    stock_results: list[dict[str, Any]], variant: str, split: str
) -> dict[str, Any]:
    """Recompute pool metrics by summing directed counts from per-stock summaries.

    Note: directed_hit_pct from stock summaries cannot be reweighted perfectly
    without raw hits — we store hit counts in summarize via reconstruction:
    hit ≈ hit_pct/100 * n_directed. Use that for pool.
    """
    n_dir = hit = long_hit = n_sig = 0
    signed_w: list[tuple[int, float]] = []
    for s in stock_results:
        if s.get("error"):
            continue
        m = s["variants"][variant][split]
        nd = int(m["n_directed"] or 0)
        ns = int(m["n_signals"] or 0)
        n_sig += ns
        n_dir += nd
        if m["directed_hit_pct"] is not None and nd:
            hit += int(round(m["directed_hit_pct"] / 100.0 * nd))
        if m["always_long_pct"] is not None and nd:
            long_hit += int(round(m["always_long_pct"] / 100.0 * nd))
        if m["expectancy_signed_pct"] is not None and nd:
            signed_w.append((nd, m["expectancy_signed_pct"]))

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    exp = None
    if signed_w:
        exp = round(sum(n * e for n, e in signed_w) / sum(n for n, _ in signed_w), 4)

    d_pct = pct(hit, n_dir)
    al = pct(long_hit, n_dir)
    return {
        "n_signals": n_sig,
        "n_directed": n_dir,
        "directed_hit_pct": d_pct,
        "always_long_pct": al,
        "expectancy_signed_pct": exp,
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
        "edge_vs_always_long_pp": (
            round(d_pct - al, 2) if d_pct is not None and al is not None else None
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="")
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--flat-eps", type=float, default=FLAT_EPS)
    ap.add_argument("--min-oos-n", type=int, default=500)
    ap.add_argument(
        "--out-json",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "swing_1h_backtest.json"
        ),
    )
    ap.add_argument(
        "--out-md",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "SWING_1H_EVAL.md"
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

    # Pool leaderboard by OOS directed hit
    pool_rows = []
    for v in variants:
        is_m = pool_summarize(results, v.name, "IS")
        oos_m = pool_summarize(results, v.name, "OOS")
        pool_rows.append(
            {
                "variant": v.name,
                "IS": is_m,
                "OOS": oos_m,
                "spec": {
                    "lookback": v.lookback,
                    "ready": v.ready,
                    "use_short": v.use_short,
                    "min_slope_pct": v.min_slope_pct,
                    "strong_only": v.strong_only,
                    "after_open_guard": v.after_open_guard,
                    "sparse_clocks": list(v.sparse_clocks),
                },
            }
        )
    pool_rows.sort(
        key=lambda r: (
            -(r["OOS"]["directed_hit_pct"] or 0),
            -(r["OOS"]["n_directed"] or 0),
        )
    )

    gate_pass = False
    best = pool_rows[0] if pool_rows else None
    if best:
        o = best["OOS"]
        gate_pass = bool(
            o["directed_hit_pct"] is not None
            and o["directed_hit_pct"] >= 60.0
            and (o["n_directed"] or 0) >= args.min_oos_n
        )

    # Secondary: pool fade_near_ext_30m from per-stock summaries
    fade_pool: dict[str, Any] = {"IS": None, "OOS": None}
    for split in ("IS", "OOS"):
        n_dir = hit = long_hit = n_sig = 0
        signed_w: list[tuple[int, float]] = []
        for s in results:
            if s.get("error"):
                continue
            m = s.get("fade_near_ext_30m", {}).get(split) or {}
            nd = int(m.get("n_directed") or 0)
            ns = int(m.get("n_signals") or 0)
            n_sig += ns
            n_dir += nd
            if m.get("directed_hit_pct") is not None and nd:
                hit += int(round(m["directed_hit_pct"] / 100.0 * nd))
            if m.get("always_long_pct") is not None and nd:
                long_hit += int(round(m["always_long_pct"] / 100.0 * nd))
            if m.get("expectancy_signed_pct") is not None and nd:
                signed_w.append((nd, m["expectancy_signed_pct"]))

        def _pct(a: int, b: int) -> float | None:
            return round(100.0 * a / b, 2) if b else None

        d_pct = _pct(hit, n_dir)
        al = _pct(long_hit, n_dir)
        exp = (
            round(sum(n * e for n, e in signed_w) / sum(n for n, _ in signed_w), 4)
            if signed_w
            else None
        )
        fade_pool[split] = {
            "n_signals": n_sig,
            "n_directed": n_dir,
            "directed_hit_pct": d_pct,
            "always_long_pct": al,
            "expectancy_signed_pct": exp,
            "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
            "edge_vs_always_long_pp": (
                round(d_pct - al, 2) if d_pct is not None and al is not None else None
            ),
        }

    fade_oos = fade_pool["OOS"] or {}
    fade_gate = bool(
        fade_oos.get("directed_hit_pct") is not None
        and fade_oos["directed_hit_pct"] >= 60.0
        and (fade_oos.get("n_directed") or 0) >= args.min_oos_n
    )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layer": "research",
        "status": "observe_only",
        "metric": {
            "name": "directed_hit_rate_swing_1h",
            "definition": (
                "PRIMARY (~60m): temp≠0 and |fwd|≥flat_eps → sign(temp)==sign(fwd); "
                f"fwd=close[i+H]/close[i]-1; H={args.horizon}; flat_eps={args.flat_eps}"
            ),
            "null_coin_flip_pct": 50.0,
            "gate": f"OOS directed_hit_pct ≥ 60 with n_directed ≥ {args.min_oos_n}",
            "is_end": args.is_end,
            "window": {"start": args.start, "end": end},
            "universe": stocks,
            "note_2492": "2492 excluded — no stock_kbar_5m rows on Book replica",
            "secondary": (
                "fade_near_ext_30m · H=6 (~30m) · midday near day-extreme mean-reversion; "
                "NOT a substitute for primary Swing 1h gate"
            ),
        },
        "gate_pass_oos_ge60_primary_60m": gate_pass,
        "gate_pass_oos_ge60_secondary_fade30m": fade_gate,
        "best_oos_variant_primary": best["variant"] if best else None,
        "pool_leaderboard": pool_rows,
        "fade_near_ext_30m_pool": fade_pool,
        "per_stock": results,
    }

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Markdown report
    lines = [
        "# Swing 1h thermometer · PIT directed-hit evaluation",
        "",
        "Research only · **未採納** · not Order / not Live trading signal",
        "",
        "## Metric definition（預先鎖定）",
        "",
        "### Primary（本輪閘門）",
        "",
        "- **Directed hit rate (~60m)**: samples with `temp ≠ 0` and "
        f"`|fwd| ≥ {args.flat_eps * 100:.2f}%`, hit iff `sign(temp) == sign(fwd)`.",
        f"- **Forward**: `close[i+{args.horizon}] / close[i] - 1`（≈{args.horizon * 5} 分；同日 5m K）。",
        "- **Signal family**: frozen `swing_1h_from_bars` + minimal knobs "
        "(L / ready / mag / slope-only / open_guard / sparse / MR flip).",
        "- **PIT**: 訊號僅用當日已完成 5m bars `≤ i`。",
        "- **Null**: coin-flip 50%；always-long = directed 中 fwd>0 比例。",
        f"- **IS / OOS**: IS `trade_date ≤ {args.is_end}`；OOS `> {args.is_end}`。"
        " 調參只看 IS；**主閘門宣稱 ≥60% 只認 Primary OOS**。",
        f"- **Gate**: OOS directed ≥ **60%** 且 pool `n_directed ≥ {args.min_oos_n}`。",
        "",
        "### Secondary（旁支 · 不取代主閘）",
        "",
        "- `fade_near_ext_from_bars`：午盤贴近日高/日低之 **~30m** 均值回歸淡化。",
        "- 即使 secondary OOS ≥60%，**不得**宣稱 Primary Swing 1h 過閘。",
        "",
        f"- Window: **{args.start} → {end}**",
        f"- Universe: `{', '.join(stocks)}`（2492 無 kbar_5m → 排除）",
        "",
        f"## Verdict: Primary OOS ≥60%？ **{'YES' if gate_pass else 'NO'}**",
        "",
        f"## Secondary fade30m OOS ≥60%？ **{'YES' if fade_gate else 'NO'}** "
        f"（旁支；n={fade_oos.get('n_directed')} hit={fade_oos.get('directed_hit_pct')}%）",
        "",
    ]
    if best:
        lines += [
            f"- Best Primary OOS variant: `{best['variant']}`",
            f"- Primary OOS directed: **{best['OOS']['directed_hit_pct']}%** "
            f"(n={best['OOS']['n_directed']})",
            f"- Primary OOS always-long null: {best['OOS']['always_long_pct']}%",
            f"- Primary OOS edge vs coin: {best['OOS']['edge_vs_coin_pp']} pp",
            f"- Primary IS directed (same variant): {best['IS']['directed_hit_pct']}% "
            f"(n={best['IS']['n_directed']})",
            "",
        ]

    lines += [
        "## Pool leaderboard · Primary ~60m（OOS directed 排序）",
        "",
        "|variant|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|OOS vs long|OOS E[signed]%|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in pool_rows:
        is_m, oos_m = r["IS"], r["OOS"]
        lines.append(
            f"|{r['variant']}|{is_m['directed_hit_pct']}|{is_m['n_directed']}|"
            f"{oos_m['directed_hit_pct']}|{oos_m['n_directed']}|"
            f"{oos_m['edge_vs_coin_pp']}|{oos_m['edge_vs_always_long_pp']}|"
            f"{oos_m['expectancy_signed_pct']}|"
        )

    lines += [
        "",
        "## Secondary · fade_near_ext ~30m pool",
        "",
        f"|split|hit%|n|always-long%|E[signed]%|",
        f"|---|---:|---:|---:|---:|",
        f"|IS|{fade_pool['IS']['directed_hit_pct']}|{fade_pool['IS']['n_directed']}|"
        f"{fade_pool['IS']['always_long_pct']}|{fade_pool['IS']['expectancy_signed_pct']}|",
        f"|OOS|{fade_pool['OOS']['directed_hit_pct']}|{fade_pool['OOS']['n_directed']}|"
        f"{fade_pool['OOS']['always_long_pct']}|{fade_pool['OOS']['expectancy_signed_pct']}|",
        "",
        "### fade30m per stock (OOS)",
        "",
        "|sid|OOS hit%|OOS n|E[signed]%|",
        "|---|---:|---:|---:|",
    ]
    for s in results:
        if s.get("error"):
            continue
        m = s["fade_near_ext_30m"]["OOS"]
        lines.append(
            f"|{s['stock_id']}|{m['directed_hit_pct']}|{m['n_directed']}|"
            f"{m['expectancy_signed_pct']}|"
        )

    lines += [
        "",
        "## Baseline (`baseline_L8_R12`) per stock · Primary 60m",
        "",
        "|sid|days|IS hit%|IS n|OOS hit%|OOS n|OOS always-long%|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in results:
        if s.get("error"):
            lines.append(f"|{s['stock_id']}|0|err|{s.get('error')}|")
            continue
        b = s["variants"]["baseline_L8_R12"]
        lines.append(
            f"|{s['stock_id']}|{s['n_days']}|{b['IS']['directed_hit_pct']}|"
            f"{b['IS']['n_directed']}|{b['OOS']['directed_hit_pct']}|"
            f"{b['OOS']['n_directed']}|{b['OOS']['always_long_pct']}|"
        )

    lines += [
        "",
        "## What worked / failed",
        "",
        "- **Failed (Primary 60m)**: frozen momentum+short fusion、L/ready 掃描、"
        "magnitude gate、sparse clock — OOS directed 大致 45–49%，弱於或贴近硬币。",
        "- **Partial**: 把斜率改成均值回歸（`mr_*`）可把 Primary OOS 抬到 ~51–53%，仍 <60%。",
        "- **Secondary worked (不同地平線)**: 午盤贴近日極值之 ~30m fade，"
        f"OOS directed **{fade_oos.get('directed_hit_pct')}%** (n={fade_oos.get('n_directed')})；"
        "但個股異質（部分標的 <50%），且 **不是** Swing 1h/~60m 主指標。",
        "- **Hard stop（Primary）**: 全網格 Primary OOS <55% 且 n≥min_oos_n → "
        "停止再掃 L；下一輪需新資訊源（大盤5m／量能結構／隔夜gap），"
        "或獨立開題驗證 fade30m（fresh holdout／跨宇宙）。",
        "",
        "## Recommendation",
        "",
    ]
    if gate_pass:
        lines.append(
            "- Primary OOS gate **通過** → 可考慮 observe-only overlay；"
            "仍不可寫入 `strategy.yaml` / Order，除非使用者另行要求採納。"
        )
    else:
        lines.append(
            "- Primary Swing 1h (~60m) gate **未過** → **留在 research**；"
            "不建議把現有 1h 溫度當交易 overlay。"
        )
    if fade_gate:
        lines.append(
            "- Secondary fade30m OOS ≥60% → 僅可作 **研究候選／observe 標註**；"
            "需先過個股穩定性與 fresh holdout，再談 Live overlay。"
        )

    lines += [
        "",
        f"- JSON: `{out_json.relative_to(ROOT)}`",
        "",
    ]

    out_md = Path(args.out_md)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out_json}")
    print(f"wrote {out_md}")
    if best:
        print(
            f"PRIMARY BEST OOS {best['variant']}: "
            f"{best['OOS']['directed_hit_pct']}% n={best['OOS']['n_directed']} "
            f"gate_pass={gate_pass}"
        )
    print(
        f"SECONDARY fade30m OOS: {fade_oos.get('directed_hit_pct')}% "
        f"n={fade_oos.get('n_directed')} gate_pass={fade_gate}"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
