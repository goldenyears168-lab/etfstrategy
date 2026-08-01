#!/usr/bin/env python3
"""Backtest trend_3d thermometer · daily TA + bench + chips (PIT · research only).

Protocol
--------
- Signal at session T close (uses only date ≤ T).
- Forward return: close[T+h] / close[T] - 1  (h ∈ {1,3,5}).
- Excess: stock fwd − IX0001 fwd over same dates.
- Window: last ~2 years ending at DB max date.
- Directed accuracy: when temp≠0, sign(temp)==sign(fwd) (fwd flat |r|<0.1% skipped).

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_trend3d_thermometer_backtest.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.intraday_direction_thermometer import (  # noqa: E402
    ThermoConfig,
    load_bench_daily,
    load_chip_daily,
    load_stock_daily,
    trend_3d_from_daily,
)

DEFAULT_STOCKS = ("2327", "8046", "3189", "2492", "6451")
HORIZONS = (1, 3, 5)
FLAT_EPS = 0.001  # 0.1%


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _align_bench(
    bench: list[dict[str, Any]], asof: str
) -> list[dict[str, Any]]:
    return [r for r in bench if str(r["trade_date"]) <= asof]


def _fwd_ret(
    closes: list[float], i: int, h: int
) -> float | None:
    if i + h >= len(closes):
        return None
    c0 = closes[i]
    if not c0:
        return None
    return closes[i + h] / c0 - 1.0


def _bench_fwd_by_date(
    bench_close: dict[str, float], dates: list[str], i: int, h: int
) -> float | None:
    """Stock session dates[i] → dates[i+h] mapped onto IX0001 closes."""
    d0 = dates[i]
    d1 = dates[i + h]
    if d0 not in bench_close or d1 not in bench_close:
        return None
    c0 = bench_close[d0]
    c1 = bench_close[d1]
    if not c0:
        return None
    return c1 / c0 - 1.0


def eval_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    start: str,
    end: str,
    cfg: ThermoConfig,
) -> dict[str, Any]:
    daily = load_stock_daily(conn, stock_id, limit=800)
    chips = load_chip_daily(conn, stock_id, limit=800)
    bench = load_bench_daily(conn, limit=800)
    if len(daily) < 40:
        return {"stock_id": stock_id, "error": "insufficient_daily", "n": len(daily)}

    dates = [str(r["trade_date"]) for r in daily]
    closes = [float(r["close"]) for r in daily]
    bench_close = {str(r["trade_date"]): float(r["close"]) for r in bench}

    # first index with date >= start and enough history (20 for MA20)
    rows: list[dict[str, Any]] = []
    for i, d in enumerate(dates):
        if d < start or d > end:
            continue
        if i < 25:
            continue
        if i + max(HORIZONS) >= len(dates):
            break
        stock_hist = daily[: i + 1]
        asof = dates[i]
        layer = trend_3d_from_daily(
            stock_daily=stock_hist,
            bench_daily=_align_bench(bench, asof),
            chip_daily=[c for c in chips if str(c["trade_date"]) <= asof],
            cfg=cfg,
        )
        if not layer.ready or layer.temp is None:
            continue
        rec: dict[str, Any] = {
            "date": asof,
            "temp": layer.temp,
            "r3_pct": (layer.meta or {}).get("r3_pct"),
        }
        for h in HORIZONS:
            fr = _fwd_ret(closes, i, h)
            br = _bench_fwd_by_date(bench_close, dates, i, h)
            rec[f"fwd_{h}d"] = fr
            rec[f"ex_{h}d"] = (fr - br) if (fr is not None and br is not None) else None
        rows.append(rec)

    # metrics
    by_temp: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_temp[int(r["temp"])].append(r)

    summary: dict[str, Any] = {
        "stock_id": stock_id,
        "n": len(rows),
        "start": rows[0]["date"] if rows else None,
        "end": rows[-1]["date"] if rows else None,
        "by_temp": {},
        "directed": {},
        "naive_r3": {},
    }

    for temp, rs in sorted(by_temp.items()):
        bucket: dict[str, Any] = {"n": len(rs)}
        for h in HORIZONS:
            fvals = [r[f"fwd_{h}d"] for r in rs if r[f"fwd_{h}d"] is not None]
            evals = [r[f"ex_{h}d"] for r in rs if r[f"ex_{h}d"] is not None]
            bucket[f"fwd_{h}d_mean_pct"] = (
                round(100 * _mean(fvals), 3) if fvals else None
            )
            bucket[f"ex_{h}d_mean_pct"] = (
                round(100 * _mean(evals), 3) if evals else None
            )
            bucket[f"fwd_{h}d_hit_pct"] = (
                round(100 * sum(1 for x in fvals if x > 0) / len(fvals), 1)
                if fvals
                else None
            )
        summary["by_temp"][str(temp)] = bucket

    for h in HORIZONS:
        # directed: temp≠0 vs non-flat fwd
        hit = tot = 0
        for r in rows:
            t = int(r["temp"])
            fr = r[f"fwd_{h}d"]
            if t == 0 or fr is None or abs(fr) < FLAT_EPS:
                continue
            tot += 1
            if (t > 0 and fr > 0) or (t < 0 and fr < 0):
                hit += 1
        summary["directed"][f"{h}d"] = {
            "acc_pct": round(100 * hit / tot, 1) if tot else None,
            "n": tot,
        }
        # long-only when temp>=1
        longs = [r[f"fwd_{h}d"] for r in rows if r["temp"] >= 1 and r[f"fwd_{h}d"] is not None]
        shorts_proxy = [
            -r[f"fwd_{h}d"]
            for r in rows
            if r["temp"] <= -1 and r[f"fwd_{h}d"] is not None
        ]
        summary["directed"][f"{h}d_long_temp_ge1"] = {
            "n": len(longs),
            "mean_pct": round(100 * _mean(longs), 3) if longs else None,
            "hit_pct": round(100 * sum(1 for x in longs if x > 0) / len(longs), 1)
            if longs
            else None,
        }
        summary["directed"][f"{h}d_avoid_or_fade_temp_le_m1"] = {
            "n": len(shorts_proxy),
            "mean_fade_pct": round(100 * _mean(shorts_proxy), 3) if shorts_proxy else None,
            "note": "fade_pct = -fwd；>0 表示偏空後續續跌有利避開做多",
        }
        # naive: sign(r3) from meta
        nh = nt = 0
        for r in rows:
            r3 = r.get("r3_pct")
            fr = r[f"fwd_{h}d"]
            if r3 is None or fr is None or abs(fr) < FLAT_EPS or abs(r3) < 0.1:
                continue
            nt += 1
            if (r3 > 0 and fr > 0) or (r3 < 0 and fr < 0):
                nh += 1
        summary["naive_r3"][f"{h}d"] = {
            "acc_pct": round(100 * nh / nt, 1) if nt else None,
            "n": nt,
        }

    # baseline random ~50%; also report temp strength monotonicity on 3d fwd
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str((Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or ROOT) / "data" / "stocks.db")))
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument(
        "--years",
        type=float,
        default=2.0,
        help="lookback years from DB max date",
    )
    ap.add_argument(
        "--out",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "trend3d_backtest_2y.json"
        ),
    )
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    end = conn.execute(
        "SELECT MAX(trade_date) FROM stock_daily_bars WHERE stock_id='2327'"
    ).fetchone()[0]
    start_dt = datetime.strptime(end, "%Y-%m-%d") - timedelta(
        days=int(365 * args.years)
    )
    start = start_dt.strftime("%Y-%m-%d")
    cfg = ThermoConfig()

    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    results = []
    for sid in stocks:
        print(f"eval {sid} {start}→{end} ...", flush=True)
        results.append(eval_stock(conn, sid, start=start, end=end, cfg=cfg))

    # pool aggregate directed
    pool: dict[str, Any] = {"window": {"start": start, "end": end}, "stocks": results}
    # recompute pool directed by concatenating — quick pass via stored summaries only for display
    lines = [
        "# Trend-3d thermometer · 2y PIT backtest",
        "",
        "Research only · 未採納",
        "",
        f"- window: **{start} → {end}**",
        f"- signal: T close · `trend_3d_from_daily`（日K＋IX0001＋籌碼）",
        f"- forward: close[T+h]/close[T]-1 · h=1/3/5",
        f"- directed: temp≠0 且 |fwd|≥0.1% 時 sign(temp)==sign(fwd)",
        "",
        "## 總表（有方向命中率）",
        "",
        "|標的|N|1d命中%|3d命中%|5d命中%|naive r3→3d%|temp≥1 後3d均%|temp≤-1 後3d均%|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in results:
        if s.get("error"):
            lines.append(f"|{s['stock_id']}|err|{s.get('error')}|")
            continue
        d = s["directed"]
        n3 = s["by_temp"]
        # mean fwd 3d for + and -
        pos = n3.get("1") or n3.get("2")
        # aggregate temp>=1
        ge1 = []
        le_m1 = []
        # from by_temp buckets
        for k, b in s["by_temp"].items():
            t = int(k)
            mean3 = b.get("fwd_3d_mean_pct")
            if mean3 is None:
                continue
            if t >= 1:
                ge1.append((b["n"], mean3))
            if t <= -1:
                le_m1.append((b["n"], mean3))

        def wavg(pairs: list[tuple[int, float]]) -> float | None:
            if not pairs:
                return None
            num = sum(n * m for n, m in pairs)
            den = sum(n for n, _ in pairs)
            return round(num / den, 3) if den else None

        lines.append(
            "|{sid}|{n}|{a1}|{a3}|{a5}|{nv}|{g}|{b}|".format(
                sid=s["stock_id"],
                n=s["n"],
                a1=d.get("1d", {}).get("acc_pct"),
                a3=d.get("3d", {}).get("acc_pct"),
                a5=d.get("5d", {}).get("acc_pct"),
                nv=s.get("naive_r3", {}).get("3d", {}).get("acc_pct"),
                g=wavg(ge1),
                b=wavg(le_m1),
            )
        )

    lines.extend(
        [
            "",
            "## 解讀門檻（經驗）",
            "",
            "- directed ≈50% → 無預測力（擲硬幣）",
            "- 55–60% → 弱優勢；需配合風控",
            "- ≥60% 且 temp 分桶報酬單調 → 較有用",
            "- 對照 naive r3：若溫度計不明显贏過「三日漲跌延續」，則多因子增益有限",
            "",
            "## 分桶（3d 超額均%）",
            "",
        ]
    )
    for s in results:
        if s.get("error"):
            continue
        lines.append(f"### {s['stock_id']} · n={s['n']}")
        lines.append("")
        lines.append("|temp|n|fwd3d%|ex3d%|fwd5d%|")
        lines.append("|---:|---:|---:|---:|---:|")
        for k in sorted(s["by_temp"].keys(), key=lambda x: int(x)):
            b = s["by_temp"][k]
            lines.append(
                f"|{k}|{b['n']}|{b.get('fwd_3d_mean_pct')}|{b.get('ex_3d_mean_pct')}|{b.get('fwd_5d_mean_pct')}|"
            )
        lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    md = out.with_suffix(".md")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "protocol": {
            "start": start,
            "end": end,
            "horizons": list(HORIZONS),
            "flat_eps": FLAT_EPS,
            "pit": True,
            "layer": "research",
        },
        "results": results,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(md.read_text(encoding="utf-8"))
    print(f"wrote {out}\nwrote {md}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
