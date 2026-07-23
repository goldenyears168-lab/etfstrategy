#!/usr/bin/env python3
"""G3 · Stage-2 leading 深跌加码 · Regime 分層 · -6% vs -7% · H3/H5。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_breadth_ma import (  # noqa: E402
    BREADTH_ZONE_DISPLAY,
    BREADTH_ZONE_ZH,
    BREADTH_ZONES_ORDER,
    build_breadth_panel,
    breadth_map_by_date,
)
from project_config import DEFAULT_ETF_CODES  # noqa: E402
from regime_snapshot import _load_ix_bars  # noqa: E402
from stage_analysis import classify_ix_trend_posture  # noqa: E402
from vcp_nse_port.bars import rows_to_ohlcv_df  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist  # noqa: E402

from run_leading_stage2_crash_entry_backtest import (  # noqa: E402
    BENCH,
    IX,
    Event,
    _pick,
    run_backtest,
)
from run_leading_stage2_dip_monthly_compare import (  # noqa: E402
    _filter_sessions,
    _stats,
)

REPORT_DIR = ROOT / "reports" / "research" / "leading-stage2-gap-fade"

BREADTH_BUCKETS: dict[str, tuple[str, ...]] = {
    "risk_on": ("strong", "overbought"),
    "mid": ("neutral",),
    "risk_off": ("weak", "oversold"),
}


def _trade_dates(conn, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source='finmind' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _prev_date_map(trade_dates: list[str]) -> dict[str, str]:
    return {trade_dates[i]: trade_dates[i - 1] for i in range(1, len(trade_dates))}


def _trend_posture_map(conn, dates: list[str]) -> dict[str, str]:
    if not dates:
        return {}
    end = max(dates)
    rows = conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE code = 'IX0001' AND source = 'tej' AND date <= ?
        ORDER BY date
        """,
        (end,),
    ).fetchall()
    if not rows:
        ix_df = _load_ix_bars(conn, end)
    else:
        ix_df = rows_to_ohlcv_df(rows)
    if ix_df.empty:
        return {}
    out: dict[str, str] = {}
    idx = ix_df.index.astype(str).str[:10]
    for d in sorted(set(dates)):
        mask = idx <= d
        if mask.sum() < 200:
            out[d] = "unknown"
            continue
        sub = ix_df.loc[mask]
        tp = classify_ix_trend_posture(sub)
        out[d] = str(tp.get("trend_posture") or "unknown")
    return out


def _tag_h3_events(
    events: list[Event],
    *,
    dip: float,
    hold: int,
    prev_map: dict[str, str],
    breadth: dict[str, str],
    trend: dict[str, str],
) -> list[dict[str, Any]]:
    h3 = _pick(
        events,
        dip=dip,
        hold=hold,
        leading=True,
        stage2=True,
        minervini7=True,
        gate_on=False,
    )
    rows: list[dict[str, Any]] = []
    for e in h3:
        prev = prev_map.get(e.session)
        zone = breadth.get(prev or "", "unknown")
        posture = trend.get(prev or "", "unknown")
        bucket = "unknown"
        for name, zones in BREADTH_BUCKETS.items():
            if zone in zones:
                bucket = name
                break
        rows.append(
            {
                "event": e,
                "regime_date": prev,
                "breadth_zone_200": zone,
                "breadth_bucket": bucket,
                "trend_posture": posture,
            }
        )
    return rows


def _stratum_stats(rows: list[dict[str, Any]], *, h5_only: bool = False) -> dict[str, Any]:
    if h5_only:
        rows = [r for r in rows if r["event"].gap_up_fade]
    rets = [r["event"].ret_pct for r in rows]
    return _stats(rets)


def _by_key(
    h3_tagged: list[dict[str, Any]],
    key: str,
) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in h3_tagged:
        by[str(r.get(key) or "unknown")].append(r)
    strata: dict[str, Any] = {}
    for label, subset in sorted(by.items()):
        h5_rows = [r for r in subset if r["event"].gap_up_fade]
        strata[label] = {
            "H3": _stratum_stats(subset),
            "H5": _stratum_stats(h5_rows, h5_only=False),
            "h5_share_pct": round(len(h5_rows) / len(subset) * 100, 1) if subset else None,
        }
    return strata


def _evaluate_g3(by_breadth: dict[str, Any], *, dip_key: str = "dip_6") -> dict[str, Any]:
    """Descriptive G3 · H5 在 risk_on 是否優於 risk_off。"""
    reasons: list[str] = []
    ro = by_breadth.get("risk_on", {}).get("H5", {})
    rf = by_breadth.get("risk_off", {}).get("H5", {})
    mid = by_breadth.get("mid", {}).get("H5", {})
    passed = True
    if (ro.get("n") or 0) >= 3 and (ro.get("med_pct") or 0) <= 0:
        passed = False
        reasons.append(f"risk_on H5 med={ro.get('med_pct')}% ≤ 0 (n={ro.get('n')})")
    if (
        (ro.get("n") or 0) >= 3
        and (rf.get("n") or 0) >= 3
        and (ro.get("med_pct") or -999) < (rf.get("med_pct") or -999)
    ):
        passed = False
        reasons.append(
            f"risk_on H5 med {ro.get('med_pct')}% < risk_off {rf.get('med_pct')}%"
        )
    if passed:
        reasons.append("risk_on H5 不劣於 risk_off · 或樣本不足描述")
    return {
        "dip": dip_key,
        "passed": passed,
        "reasons": reasons,
        "risk_on_H5": ro,
        "mid_H5": mid,
        "risk_off_H5": rf,
    }


def _md(payload: dict[str, Any]) -> str:
    p = payload
    lines = [
        "# Stage-2 leading 深跌加码 · Regime 分層（G3）",
        "",
        f"Window: `{p['start']}` .. `{p['end']}` · hold **{p['hold_days']}d**",
        f"Regime PIT: 進場 session 前一交易日（昨收環境）",
        "",
        "## G3 判定（H5 · breadth bucket）",
        "",
    ]
    for dip in ("dip_6", "dip_7"):
        g3 = p["g3"][dip]
        lines.append(f"- **{dip}**: {'PASS' if g3['passed'] else 'FAIL'} — " + "; ".join(g3["reasons"]))
    lines.extend(["", "## Market breadth（zone_200）· H5 开高后杀", ""])
    lines.append("| zone | dip | n | win% | med% | avg% |")
    lines.append("|------|-----|---|------|------|------|")
    for dip_key, dip_label in (("dip_6", "-6%"), ("dip_7", "-7%")):
        zones = p["strata"][dip_key]["by_breadth_zone"]
        for z in BREADTH_ZONES_ORDER + ("unknown",):
            if z not in zones:
                continue
            s = zones[z]["H5"]
            if not s["n"]:
                continue
            zh = BREADTH_ZONE_ZH.get(z, z) if z != "unknown" else "unknown"
            lines.append(
                f"| {zh} | {dip_label} | {s['n']} | {s['win_pct']} | {s['med_pct']} | {s['avg_pct']} |"
            )
    lines.extend(["", "## Breadth bucket · H3 vs H5", ""])
    lines.append("| bucket | dip | H3 n/med | H5 n/med |")
    lines.append("|--------|-----|----------|----------|")
    for dip_key, dip_label in (("dip_6", "-6%"), ("dip_7", "-7%")):
        buckets = p["strata"][dip_key]["by_breadth_bucket"]
        for b in ("risk_on", "mid", "risk_off", "unknown"):
            if b not in buckets:
                continue
            row = buckets[b]
            h3, h5 = row["H3"], row["H5"]
            lines.append(
                f"| {b} | {dip_label} | {h3['n']}/{h3['med_pct']}% | {h5['n']}/{h5['med_pct']}% |"
            )
    lines.extend(["", "## Trend posture · H5", ""])
    lines.append("| posture | dip | n | med% |")
    lines.append("|---------|-----|---|------|")
    for dip_key, dip_label in (("dip_6", "-6%"), ("dip_7", "-7%")):
        tp = p["strata"][dip_key]["by_trend_posture"]
        for posture, row in sorted(tp.items()):
            s = row["H5"]
            if s["n"]:
                lines.append(f"| {posture} | {dip_label} | {s['n']} | {s['med_pct']}% |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-29")
    parser.add_argument("--end", default="2026-06-26")
    parser.add_argument("--hold", type=int, default=5)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    args = parser.parse_args()

    conn = connect(DEFAULT_DB_PATH)
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    stock_ids = sorted({str(w["stock_id"]) for w in watch if w.get("stock_id")} - {BENCH, IX})
    trade_dates = _trade_dates(conn, args.start, args.end)
    prev_map = _prev_date_map(trade_dates)
    prev_dates = sorted(set(prev_map.values()))

    panel = build_breadth_panel(conn, date_start=min(prev_dates), date_end=max(prev_dates))
    breadth = breadth_map_by_date(panel, use="200")
    trend = _trend_posture_map(conn, prev_dates)

    print(f"Building events · {len(stock_ids)} names · {args.start}..{args.end} …")
    all_events = run_backtest(conn, stock_ids)
    events = _filter_sessions(all_events, args.start, args.end)
    conn.close()

    strata: dict[str, Any] = {}
    for dip_key, dip in (("dip_6", 0.06), ("dip_7", 0.07)):
        h3_tagged = _tag_h3_events(
            events,
            dip=dip,
            hold=args.hold,
            prev_map=prev_map,
            breadth=breadth,
            trend=trend,
        )
        strata[dip_key] = {
            "by_breadth_zone": _by_key(h3_tagged, "breadth_zone_200"),
            "by_breadth_bucket": _by_key(h3_tagged, "breadth_bucket"),
            "by_trend_posture": _by_key(h3_tagged, "trend_posture"),
        }

    g3 = {
        "dip_6": _evaluate_g3(strata["dip_6"]["by_breadth_bucket"], dip_key="dip_6"),
        "dip_7": _evaluate_g3(strata["dip_7"]["by_breadth_bucket"], dip_key="dip_7"),
    }

    payload: dict[str, Any] = {
        "schema": "leading_stage2_regime_stratify-v1",
        "generated_at": date.today().isoformat(),
        "start": args.start,
        "end": args.end,
        "hold_days": args.hold,
        "watchlist_n": len(stock_ids),
        "regime_pit": "prev_trade_date",
        "breadth_axis": "zone_200",
        "g3": g3,
        "strata": strata,
        "regime_context": {
            d: {"breadth_zone_200": breadth.get(d), "trend_posture": trend.get(d)}
            for d in prev_dates
            if args.start <= d <= args.end or d in prev_map.values()
        },
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = args.end.replace("-", "")
    json_path = args.out_json or (REPORT_DIR / f"regime_stratify_{stamp}.json")
    md_path = args.out_md or (REPORT_DIR / f"regime_stratify_{stamp}.md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_md(payload) + "\n", encoding="utf-8")

    print(f"Wrote {json_path}\nWrote {md_path}\n")
    for dip_key in ("dip_6", "dip_7"):
        print(f"=== G3 {dip_key} ===")
        print(json.dumps(g3[dip_key], ensure_ascii=False, indent=2))
        print("breadth bucket H5:")
        for b, row in strata[dip_key]["by_breadth_bucket"].items():
            print(f"  {b}: {row['H5']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
