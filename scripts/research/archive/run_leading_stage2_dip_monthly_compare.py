#!/usr/bin/env python3
"""近 N 日 · 監控清單 149 檔 · H3/H5 首次觸線 · 盤中 -6% vs -7% 比較。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_config import DEFAULT_ETF_CODES  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist  # noqa: E402

from run_leading_stage2_crash_entry_backtest import (  # noqa: E402
    BENCH,
    IX,
    Event,
    _pick,
    run_backtest,
)

REPORT_DIR = ROOT / "reports" / "research" / "leading-stage2-gap-fade"
_TZ = ZoneInfo("Asia/Taipei")


def _stats(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0, "win_pct": None, "med_pct": None, "avg_pct": None}
    return {
        "n": len(rets),
        "win_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "med_pct": round(median(rets), 2),
        "avg_pct": round(sum(rets) / len(rets), 2),
    }


def _filter_sessions(events: list[Event], start: str, end: str) -> list[Event]:
    return [e for e in events if start <= e.session <= end]


def _h5_from_h3(h3: list[Event]) -> list[Event]:
    return [e for e in h3 if e.gap_up_fade]


def _compare_block(events: list[Event], *, dip: float, hold: int) -> dict[str, Any]:
    h3 = _pick(events, dip=dip, hold=hold, leading=True, stage2=True, minervini7=True, gate_on=False)
    h5 = _h5_from_h3(h3)
    s3 = _stats([e.ret_pct for e in h3])
    s5 = _stats([e.ret_pct for e in h5])
    by_day: dict[str, int] = defaultdict(int)
    for e in h3:
        by_day[e.session] += 1
    return {
        "dip_pct": dip,
        "hold_days": hold,
        "H3_baseline": s3,
        "H5_gap_up_fade": s5,
        "lift_med_pct": round((s5["med_pct"] or 0) - (s3["med_pct"] or 0), 2)
        if s3["med_pct"] is not None and s5["med_pct"] is not None
        else None,
        "active_days": len(by_day),
        "events_by_session": dict(sorted(by_day.items())),
        "events": [
            {
                "stock_id": e.stock_id,
                "session": e.session,
                "ret_pct": round(e.ret_pct, 2),
                "gap_up_fade": e.gap_up_fade,
            }
            for e in sorted(h3, key=lambda x: (x.session, x.stock_id))
        ],
    }


def _md(payload: dict[str, Any]) -> str:
    lines = [
        "# Stage-2 leading · 近月 -6% vs -7% 首次觸線比較",
        "",
        f"Generated: {payload['generated_at']}",
        f"Window: `{payload['start']}` .. `{payload['end']}` · watchlist **{payload['watchlist_n']}** 檔",
        f"Kbar 覆蓋（有資料檔數/日）: {payload['kbar_coverage']}",
        "",
        "規則：昨收 RRG leading · Stage 2 · Minervini≥7 · gate OFF · 09:05 後**首次**觸及 dip · hold 5d",
        "",
        "## 摘要",
        "",
        "| dip | 假说 | n | win% | med% | avg% |",
        "|-----|------|---|------|------|------|",
    ]
    for dip_key in ("dip_6", "dip_7"):
        block = payload["compare"][dip_key]
        d = block["dip_pct"]
        for label, key in (("H3", "H3_baseline"), ("H5 开高后杀", "H5_gap_up_fade")):
            s = block[key]
            lines.append(
                f"| -{d*100:.0f}% | {label} | {s['n']} | {s['win_pct']} | {s['med_pct']} | {s['avg_pct']} |"
            )
    lines.extend(["", "## -6% vs -7%（H3）", ""])
    h3_6 = payload["compare"]["dip_6"]["H3_baseline"]
    h3_7 = payload["compare"]["dip_7"]["H3_baseline"]
    lines.append(
        f"- H3 -6%: n={h3_6['n']} med={h3_6['med_pct']}% · "
        f"H3 -7%: n={h3_7['n']} med={h3_7['med_pct']}%"
    )
    if h3_6["med_pct"] is not None and h3_7["med_pct"] is not None:
        lines.append(f"- 更深 -7% 進場 med lift: **{h3_7['med_pct'] - h3_6['med_pct']:+.2f}pp**")
    lines.extend(["", "## -6% vs -7%（H5 开高后杀）", ""])
    h5_6 = payload["compare"]["dip_6"]["H5_gap_up_fade"]
    h5_7 = payload["compare"]["dip_7"]["H5_gap_up_fade"]
    lines.append(
        f"- H5 -6%: n={h5_6['n']} med={h5_6['med_pct']}% · "
        f"H5 -7%: n={h5_7['n']} med={h5_7['med_pct']}%"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="起始 session（含）")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="結束 session（含）")
    parser.add_argument("--days", type=int, default=30, help="未指定 start 時回看交易日約 N 日")
    parser.add_argument("--hold", type=int, default=5)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    args = parser.parse_args()

    conn = connect(DEFAULT_DB_PATH)
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    stock_ids = sorted({str(w["stock_id"]) for w in watch if w.get("stock_id")} - {BENCH, IX})

    if args.end:
        end = args.end
    else:
        row = conn.execute("SELECT MAX(trade_date) FROM stock_kbar_1m").fetchone()
        end = str(row[0] or date.today().isoformat())[:10]

    if args.start:
        start = args.start
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily_bars
            WHERE source='finmind' AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT ?
            """,
            (end, max(args.days, 5)),
        ).fetchall()
        start = str(rows[-1][0]) if rows else end

    # kbar coverage in window
    trade_days = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily_bars
            WHERE source='finmind' AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (start, end),
        ).fetchall()
    ]
    cov: dict[str, int] = {}
    for d in trade_days:
        cov[d] = sum(
            1
            for sid in stock_ids
            if conn.execute(
                "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id=? AND trade_date=?",
                (sid, d),
            ).fetchone()[0]
            >= 4
        )

    print(f"Building events · watchlist {len(stock_ids)} · window {start}..{end} …")
    all_events = run_backtest(conn, stock_ids)
    events = _filter_sessions(all_events, start, end)
    conn.close()

    payload: dict[str, Any] = {
        "generated_at": date.today().isoformat(),
        "start": start,
        "end": end,
        "watchlist_n": len(stock_ids),
        "hold_days": args.hold,
        "kbar_coverage": {d: f"{n}/{len(stock_ids)}" for d, n in cov.items()},
        "compare": {
            "dip_6": _compare_block(events, dip=0.06, hold=args.hold),
            "dip_7": _compare_block(events, dip=0.07, hold=args.hold),
        },
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = end.replace("-", "")
    json_path = args.out_json or (REPORT_DIR / f"dip6_vs_dip7_monthly_{stamp}.json")
    md_path = args.out_md or (REPORT_DIR / f"dip6_vs_dip7_monthly_{stamp}.md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_md(payload) + "\n", encoding="utf-8")

    print(f"Wrote {json_path}\nWrote {md_path}\n")
    for key, label in (("dip_6", "-6%"), ("dip_7", "-7%")):
        b = payload["compare"][key]
        print(f"=== {label} hold {args.hold}d ===")
        print(f"  H3: {b['H3_baseline']}")
        print(f"  H5: {b['H5_gap_up_fade']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
