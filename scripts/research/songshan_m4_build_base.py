#!/usr/bin/env python3
"""M4-Step0：9217（凱基-松山）scan_5d_net95 母體重算（2026-08-17 補價後）.

背景：2026-08-17 凌晨補了 255 檔缺價股票進 stock_daily_bars。第十輪母體
(reports/.../whale_9217_round10_events.csv, n=48) 是在補價前算的，
scan_5d_net95 的 buy_5d 需要 JOIN stock_daily_bars.close，缺價的 (stock,day)
會被整列丟掉 → 母體可能已變。本腳本重算並存檔給後續 H-D1/H-D2 使用。

協議完全沿用第十輪，不改：
  訊號 = scan_5d_net95（rolling 5 交易日 buy_5d>=0.5億 ∩ net_ratio>=0.95 ∩ !mega）
  去重 = rising-edge（per stock）
  L1H7 = T+1 開盤進 / 第7個交易日收盤出 / COST=30bps / BETA=1.15 / bench=IX0001

DB 唯讀。不寫 DB / config / .env。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m4_build_base.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"

STUDY_START = "2024-07-01"
END_R10 = "2026-08-14"   # 第十輪窗口尾端（直接可比）
END_NOW = "2026-08-17"   # 目前 tape / bars 最新日


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M9217 = _load_module("m9217", SCRIPTS / "study_whale_9217_5d_net95_live_signal_validation.py")
MGEN = _load_module("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")


def run(conn, mega, end: str):
    M9217.STUDY_START = STUDY_START
    M9217.STUDY_END = end
    MGEN.STUDY_START = STUDY_START
    MGEN.STUDY_END = end
    events, grid = M9217.build_5d_net95_events(conn, mega)
    trades, drop = MGEN.build_trades(conn, events)
    return events, grid, trades, drop


def main() -> int:
    print(f"[INFO] DB(read-only) = {DEFAULT_DB_PATH}")
    conn = connect_ro(DEFAULT_DB_PATH)
    mega = MGEN.load_mega(MGEN.MEGA_PATH)
    print(f"[INFO] mega blacklist n={len(mega)}")

    summary = {}
    for tag, end in (("r10_window_20260814", END_R10), ("extended_20260817", END_NOW)):
        print(f"\n{'='*88}\n[{tag}] scan window {STUDY_START} ~ {end}\n{'='*88}")
        events, grid, trades, drop = run(conn, mega, end)
        events.to_csv(OUT_DIR / f"songshan_m4_events_{tag}.csv", index=False)
        trades.to_csv(OUT_DIR / f"songshan_m4_trades_{tag}.csv", index=False)
        st = MGEN.full_stats(trades["r_adj_pct"], f"{tag}_n{len(trades)}")
        print(json.dumps(drop, ensure_ascii=False, indent=2))
        print(json.dumps(st, ensure_ascii=False, indent=2))
        summary[tag] = {"drop_stats": drop, "stats": st,
                        "n_events": int(len(events)),
                        "n_stocks": int(events["stock_id"].nunique())}

    # 與第十輪舊母體 diff
    old = pd.read_csv(OUT_DIR / "whale_9217_round10_events.csv", dtype={"stock_id": str})
    new = pd.read_csv(OUT_DIR / "songshan_m4_events_r10_window_20260814.csv", dtype={"stock_id": str})
    old_k = set(zip(old["stock_id"], old["signal_date"]))
    new_k = set(zip(new["stock_id"], new["signal_date"]))
    added = sorted(new_k - old_k)
    removed = sorted(old_k - new_k)
    print(f"\n[DIFF vs round10] old n={len(old_k)} new n={len(new_k)} "
          f"added={len(added)} removed={len(removed)}")
    print("  added:", added)
    print("  removed:", removed)
    summary["diff_vs_round10"] = {
        "old_n": len(old_k), "new_n": len(new_k),
        "added": [list(x) for x in added], "removed": [list(x) for x in removed],
    }

    (OUT_DIR / "songshan_m4_base_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
