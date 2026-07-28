#!/usr/bin/env python3
"""把 44 檔體檢(retest_adopted44_new_data.py)四層分類寫回 EVAL_REGISTRY.json，供池 email 注記。

四層(依 verdict + new_n)：
  保留(keep_robust)  = HOLD 且 new_n>=12
  觀察(watch_weak)   = HOLD 且 new_n<12（薄樣本/無新證據）
  搁置(shelf_thin)   = THIN（new_n<6，無法判）
  退池(retire)       = WEAKEN 或 FAIL

每檔 adopted 加 `retest_20260728` 區塊（不覆蓋原 status/adoption 記錄）。可重跑。

用法：PYTHONPATH=src .venv/bin/python scripts/research/apply_retest_tiers_to_registry.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EP = ROOT / "reports/research/branch-footprint-screen/expert_pool"
REG = EP / "EVAL_REGISTRY.json"
CSV = EP / "RETEST_ADOPTED44_new_data.csv"
STAMP = "2026-07-28"


def tier_of(verdict: str, new_n) -> tuple[str, str]:
    v = str(verdict)
    n = int(new_n) if pd.notna(new_n) else 0
    if v.startswith("HOLD"):
        return ("keep_robust", "保留") if n >= 12 else ("watch_weak", "觀察·勿加碼")
    if v.startswith("THIN"):
        return ("shelf_thin", "搁置·待數據")
    if v.startswith("WEAKEN") or v.startswith("FAIL"):
        return ("retire", "退池")
    return ("unknown", "待查")


def main() -> int:
    reg = json.loads(REG.read_text("utf-8"))
    df = pd.read_csv(CSV, dtype={"sid": str})
    by_sid = {r["sid"]: r for _, r in df.iterrows()}

    tally = {}
    updated = 0
    for s in reg["stocks"]:
        if s.get("status") != "adopted":
            continue
        r = by_sid.get(s["stock_id"])
        if r is None:
            continue
        tier, action = tier_of(r["verdict"], r.get("new_n"))
        s["retest_20260728"] = {
            "tier": tier, "action": action,
            "verdict": str(r["verdict"]),
            "new_med_radj_pct": None if pd.isna(r.get("new_med")) else float(r["new_med"]),
            "new_n": int(r["new_n"]) if pd.notna(r.get("new_n")) else 0,
            "new_win_pct": None if pd.isna(r.get("new_win")) else float(r["new_win"]),
            "orig_med_radj_pct": None if pd.isna(r.get("orig_med")) else float(r["orig_med"]),
            "basis": "831-branch mini SSOT tape + adj_close, L1H7, data_max 2026-07-27, frozen core+floor (no re-grid)",
        }
        tally[action] = tally.get(action, 0) + 1
        updated += 1

    reg.setdefault("retest_history", []).append({"date": STAMP, "tally": tally, "n": updated})
    REG.write_text(json.dumps(reg, ensure_ascii=False, indent=2), "utf-8")
    print(f"[OK] EVAL_REGISTRY updated · {updated} adopted 加註 retest_{STAMP.replace('-','')} · tally={tally}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
