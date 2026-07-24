#!/usr/bin/env python3
"""Expand HoldingSharesPer cache to expert-pool adopted (+ holdings).

Out: reports/research/chip-overlays/cache/holding_shares_per_expert.csv
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from finmind_client import fetch_finmind, finmind_token  # noqa: E402

OUT = ROOT / "reports" / "research" / "chip-overlays" / "cache"
REG = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "EVAL_REGISTRY.json"
)
HOLDINGS = ["2327", "3189", "3653", "6505", "8046", "2492"]
BIG = {
    "100,001-200,000",
    "200,001-400,000",
    "400,001-600,000",
    "600,001-800,000",
    "800,001-1,000,000",
    "more than 1,000,001",
}
START, END = date(2024, 7, 1), date(2026, 7, 20)
DELAY = 0.4


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    assert finmind_token()
    raw = json.loads(REG.read_text(encoding="utf-8"))
    ids = sorted(
        {
            r["stock_id"]
            for r in raw["stocks"]
            if r.get("status") == "adopted" and str(r["stock_id"]).isdigit()
        }
        | set(HOLDINGS)
    )
    path = OUT / "holding_shares_per_expert.csv"
    rows = []
    for i, sid in enumerate(ids, 1):
        print(f"HS {sid} ({i}/{len(ids)})")
        try:
            raw_rows = fetch_finmind(
                "TaiwanStockHoldingSharesPer", sid, START, END, timeout=120
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {exc}")
            time.sleep(DELAY)
            continue
        by_d: dict[str, list] = defaultdict(list)
        for item in raw_rows:
            d = str(item.get("date") or "")[:10]
            if d:
                by_d[d].append(item)
        prev = None
        for d in sorted(by_d):
            big = 0.0
            for item in by_d[d]:
                lvl = str(item.get("HoldingSharesLevel") or "")
                try:
                    pct = float(item.get("percent") or 0)
                except (TypeError, ValueError):
                    continue
                if lvl in BIG:
                    big += pct
            chg = None if prev is None else big - prev
            rows.append(
                dict(sid=sid, d=d, big_holder_pct=big, big_holder_pct_chg=chg)
            )
            prev = big
        time.sleep(DELAY)
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"wrote {path} n={len(df)} stocks={df.sid.nunique()}")


if __name__ == "__main__":
    main()
