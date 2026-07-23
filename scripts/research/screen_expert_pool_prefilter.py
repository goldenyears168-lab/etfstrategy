#!/usr/bin/env python3
"""Cheap expert-pool pre-filter (br8 + consensus density) before P4/P5.

  PYTHONPATH=src python3 scripts/research/screen_expert_pool_prefilter.py
  PYTHONPATH=src python3 scripts/research/screen_expert_pool_prefilter.py --lane soft

Writes:
  reports/research/branch-footprint-screen/expert_pool/SCALE80_PREFILTER_QUEUE.json
  (and a short .md)

Does NOT run the funnel. sqlite mode=ro.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen/expert_pool"
REG = OUT / "EVAL_REGISTRY.json"
SOURCE = "finmind"
MIN_AMT = 5e7
DEDUP = 5
FOREIGN_KW = (
    "美商", "摩根", "瑞銀", "美林", "港商", "野村", "瑞士", "花旗", "渣打",
    "香港", "新加坡", "大和", "法銀", "德意志", "巴克萊", "高盛", "美銀",
)


def connect_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=120.0)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-400000")
    return conn


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Expert-pool br8/consensus pre-filter")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default="2026-07-17")
    ap.add_argument(
        "--lane",
        choices=("primary", "soft", "both"),
        default="primary",
        help="primary=br8≥3+cons; soft=br8≥2+cons3 (after primary exhausted)",
    )
    ap.add_argument("--include-done", action="store_true", help="Do not skip EVAL_REGISTRY")
    return ap.parse_args()


def lane_ok(m: dict, lane: str) -> bool:
    if m["br8"] <= 1:
        return False  # hard ban U0 hard=0 oceans
    if lane == "primary":
        return (
            m["br8"] >= 3
            and m["episodes_stock"] >= 30
            and (m["cons3_days"] >= 3 or m["cons2_days"] >= 8)
        )
    if lane == "soft":
        return (
            m["br8"] >= 2
            and m["episodes_stock"] >= 18
            and m["cons3_days"] >= 5
        )
    return False


def main() -> int:
    args = parse_args()
    conn = connect_ro(args.db)
    traders = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT DISTINCT securities_trader_id, securities_trader "
            "FROM stock_broker_branch_daily WHERE source=?",
            (SOURCE,),
        )
    }
    foreign = {
        tid for tid, nm in traders.items() if any(k in (nm or "") for k in FOREIGN_KW)
    }
    rows = conn.execute(
        """
        SELECT b.stock_id, b.securities_trader_id, b.trade_date
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=? AND b.trade_date BETWEEN ? AND ?
          AND b.net > 0 AND p.close > 0 AND (b.net * p.close) >= ?
        """,
        (SOURCE, SOURCE, args.start, args.end, MIN_AMT),
    ).fetchall()

    eps: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    day_br: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for sid, tid, d in rows:
        if tid in foreign:
            continue
        lst = eps[sid][tid]
        if not lst or (
            datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(lst[-1], "%Y-%m-%d")
        ).days > DEDUP:
            lst.append(d)
        day_br[sid][d].add(tid)

    bar_sids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_daily_bars "
            "WHERE source=? AND trade_date BETWEEN ? AND ?",
            (SOURCE, args.start, args.end),
        )
    }
    names: dict[str, str] = {}
    try:
        for r in conn.execute("SELECT stock_id, name FROM stock_beta"):
            names[str(r[0])] = (r[1] or "").rstrip("*")
    except sqlite3.Error:
        pass

    done: set[str] = set()
    if REG.is_file() and not args.include_done:
        reg = json.loads(REG.read_text(encoding="utf-8"))
        done = {str(r["stock_id"]) for r in reg.get("stocks", [])}

    univ: list[dict] = []
    for sid, ep_by in eps.items():
        if sid not in bar_sids:
            continue
        stock_days = sorted({d for ds in ep_by.values() for d in ds})
        stock_ep: list[str] = []
        for d in stock_days:
            if not stock_ep or (
                datetime.strptime(d, "%Y-%m-%d")
                - datetime.strptime(stock_ep[-1], "%Y-%m-%d")
            ).days > DEDUP:
                stock_ep.append(d)
        br8 = sum(1 for ds in ep_by.values() if len(ds) >= 8)
        cons2 = sum(1 for s in day_br[sid].values() if len(s) >= 2)
        cons3 = sum(1 for s in day_br[sid].values() if len(s) >= 3)
        n_days = len(day_br[sid]) or 1
        univ.append(
            {
                "stock_id": sid,
                "name": names.get(sid, ""),
                "episodes_stock": len(stock_ep),
                "br8": br8,
                "cons2_days": cons2,
                "cons3_days": cons3,
                "cons2_share": round(cons2 / n_days, 4),
            }
        )

    lanes = ["primary", "soft"] if args.lane == "both" else [args.lane]
    queue: list[dict] = []
    seen: set[str] = set()
    for lane in lanes:
        for m in univ:
            sid = m["stock_id"]
            if sid in done or sid in seen:
                continue
            if not lane_ok(m, lane):
                continue
            row = {**m, "tier": "PRE_PRIMARY" if lane == "primary" else "PRE_SOFT"}
            queue.append(row)
            seen.add(sid)

    queue.sort(
        key=lambda x: (x["br8"], x["cons3_days"], x["cons2_days"], x["episodes_stock"]),
        reverse=True,
    )

    payload = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "window": [args.start, args.end],
        "lane": args.lane,
        "rules": {
            "hard_ban": "br8 <= 1 (no U0dense/U0broad)",
            "primary": "br8>=3 · ep>=30 · (cons3>=3 OR cons2>=8)",
            "soft": "br8>=2 · ep>=18 · cons3>=5",
        },
        "n_univ": len(univ),
        "n_queue": len(queue),
        "skipped_registry": len(done),
        "queue": queue,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "SCALE80_PREFILTER_QUEUE.json"
    jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# Pre-filter queue · {payload['built_at']}",
        "",
        f"- lane=`{args.lane}` · queue={len(queue)} · univ={len(univ)} · registry_skip={len(done)}",
        f"- hard ban: br8≤1 · primary: br8≥3·ep≥30·(cons3≥3|cons2≥8) · soft: br8≥2·ep≥18·cons3≥5",
        "",
        "| # | tier | sid | name | ep | br8 | cons2 | cons3 |",
        "|--:|---|---|---|---:|---:|---:|---:|",
    ]
    for i, x in enumerate(queue[:80], 1):
        lines.append(
            f"| {i} | {x['tier']} | {x['stock_id']} | {x['name']} | "
            f"{x['episodes_stock']} | {x['br8']} | {x['cons2_days']} | {x['cons3_days']} |"
        )
    if len(queue) > 80:
        lines.append(f"| … | | | | | | | ({len(queue) - 80} more) |")
    (OUT / "SCALE80_PREFILTER_QUEUE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"n_queue": len(queue), "out": str(jp)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
