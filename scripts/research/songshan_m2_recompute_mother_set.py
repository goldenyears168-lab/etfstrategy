#!/usr/bin/env python3
"""songshan_m2 · Step 1：在 2026-08-17 補價後重算 9217 scan_5d_net95 母體（L1H7）.

不改協議、不改門檻，只是把 round10 的 pipeline 用最新 DB 重跑一次，
比對 whale_9217_round10_events.csv（n=48）有沒有因為 2026-08-17 凌晨補的
255 檔缺價股票而改變。

DB 唯讀。輸出：
  reports/research/branch-footprint-screen/songshan_m2/mother_set_events.csv
  reports/research/branch-footprint-screen/songshan_m2/mother_set_trades.csv
  reports/research/branch-footprint-screen/songshan_m2/mother_set_summary.json

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m2_recompute_mother_set.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from stock_db import DEFAULT_DB_PATH  # noqa: E402
from stock_db.connection import connect_ro  # noqa: E402

STUDY_START = "2024-07-01"
STUDY_END = "2026-08-17"  # 最新 bars 日
ROUND10_END = "2026-08-14"  # round10 用的尾端，做 apples-to-apples 對照

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen" / "songshan_m2"
SCRIPTS = ROOT / "scripts" / "research"
ROUND10_CSV = (
    ROOT / "reports" / "research" / "branch-footprint-screen" / "whale_9217_round10_events.csv"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    m9217 = _load_module("m9217", SCRIPTS / "study_whale_9217_5d_net95_live_signal_validation.py")
    mgen = _load_module("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")

    print(f"[INFO] DB(read-only) = {DEFAULT_DB_PATH}")
    conn = connect_ro(DEFAULT_DB_PATH)
    mega = mgen.load_mega(mgen.MEGA_PATH)
    print(f"[INFO] mega n={len(mega)}")

    results: dict[str, dict] = {}
    events_latest = None
    trades_latest = None

    for label, end in (("round10_end", ROUND10_END), ("latest", STUDY_END)):
        m9217.STUDY_START = STUDY_START
        m9217.STUDY_END = end
        mgen.STUDY_START = STUDY_START
        mgen.STUDY_END = end
        events, grid = m9217.build_5d_net95_events(conn, mega)
        trades, drop = mgen.build_trades(conn, events)
        stats = mgen.full_stats(trades["r_adj_pct"], f"{label}_n{len(trades)}") if len(trades) else {}
        results[label] = {
            "study_end": end,
            "n_events": int(len(events)),
            "n_trades": int(len(trades)),
            "drop_stats": drop,
            "stats": stats,
        }
        print(f"\n[{label}] end={end} n_events={len(events)} n_trades={len(trades)}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if label == "latest":
            events_latest, trades_latest = events, trades

    # --- 與 round10 CSV 逐筆比對 ---
    old = pd.read_csv(ROUND10_CSV, dtype={"stock_id": str})
    old_keys = set(zip(old["stock_id"], old["signal_date"]))
    new_keys = set(zip(events_latest["stock_id"], events_latest["signal_date"]))
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    print(f"\n[DIFF vs round10 csv n={len(old)}] new n={len(new_keys)} +{len(added)} -{len(removed)}")
    for k in added:
        print("  + ", k)
    for k in removed:
        print("  - ", k)

    events_latest.to_csv(OUT_DIR / "mother_set_events.csv", index=False)
    trades_latest.to_csv(OUT_DIR / "mother_set_trades.csv", index=False)
    summary = {
        "study_start": STUDY_START,
        "protocol": "L1H7 · cost 30bps · beta 1.15 · bench IX0001 · rising-edge dedupe",
        "runs": results,
        "diff_vs_round10_csv": {
            "round10_n": int(len(old)),
            "added": [{"stock_id": s, "signal_date": d} for s, d in added],
            "removed": [{"stock_id": s, "signal_date": d} for s, d in removed],
        },
    }
    (OUT_DIR / "mother_set_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n[OK] wrote {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
