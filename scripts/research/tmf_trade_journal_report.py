#!/usr/bin/env python3
"""把 trade journal 還原成「一筆一筆的交易圖形」＋滾動符合度。

journal 是每輪一行的 append-only 流水（src/tmf_channel/trade_journal.py），
這支把它按 trade_id 收斂回交易本身：每筆的進場因子、逐分鐘軌跡、MFE/MAE、
最終結果，以及最重要的——**最近 N 筆整體還像不像自己**。

兩層符合度，檢定力差了一個數量級，別混用：
  單筆 z    只記錄不觸發。1 分鐘視野每筆標準差 ±47.4 點、期望 +2.76，訊噪比
            0.058；要 2σ 得在 1 分鐘內走 95 點，等它出現交易早就結束了。
  滾動 z    n=20 時 SE≈10.6 點，2σ 門檻是平均 −18.5 點。這一層才有檢定力，
            而且對應的動作是**停止開新倉**（策略不像自己了），不是砍手上的
            部位——後者是單筆決策，本來就沒有檢定力。

已知限制：目前 journal 只寫 hold 事件，所以「進場」取該 trade_id 的第一筆
觀測、「出場」取最後一筆。真正的成交價與出場原因仍在 live log 的 actions 裡；
要精確對齊需要再補 entry/exit 事件，這裡不假裝已經有。
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from tmf_channel.trade_journal import (
    BASELINE_MARKOUT,
    ROLLING_HORIZON_MIN,
    ROLLING_N,
    journal_path,
    rolling_conformance,
)


def load(days: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in days:
        p = journal_path(d.replace("-", ""))
        if not p.exists():
            print(f"  (無 {p.name})")
            continue
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: str(r.get("ts")))
    return rows


def episodes(rows: list[dict[str, Any]]) -> "OrderedDict[str, list[dict]]":
    eps: OrderedDict[str, list[dict]] = OrderedDict()
    for r in rows:
        if r.get("event") != "hold":
            continue
        tid = str(r.get("trade_id") or "")
        if not tid:
            continue
        eps.setdefault(tid, []).append(r)
    return eps


def markout_at(path: list[dict], minutes: float) -> float | None:
    """該筆在持有 ``minutes`` 分時的實際 markout（取最接近且不早於的那輪）。"""
    best = None
    for r in path:
        h = r.get("hold_min")
        if h is None or h < minutes:
            continue
        if best is None or h < best.get("hold_min", 1e9):
            best = r
    if best is None:
        return None
    c = best.get("conformance") or {}
    return c.get("actual_pts")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", nargs="+", required=True, help="YYYY-MM-DD ...")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    rows = load(args.days)
    eps = episodes(rows)
    print(f"journal 列數 {len(rows)} · 交易筆數 {len(eps)}\n")
    if not eps:
        print("沒有持倉紀錄——TMF 目前是 dry-run 且今日無成交時，這是預期結果。")
        return 0

    print("=== 每筆交易的圖形 ===")
    hdr = (f"{'#':<4}{'entry':<17}{'side':<5}{'ep':>9}  {'cell':<18}{'gate':<6}"
           f"{'hold':>7}{'MFE':>8}{'MAE':>8}{'last':>8}{'z@1m':>7}")
    print(hdr)
    print("-" * len(hdr))
    table: list[dict[str, Any]] = []
    for i, (tid, path) in enumerate(eps.items(), 1):
        first, last = path[0], path[-1]
        pos = first.get("position") or {}
        side, ep = str(pos.get("s") or "?"), pos.get("ep")
        outs = [(r.get("conformance") or {}).get("actual_pts") for r in path]
        outs = [x for x in outs if x is not None]
        f = first.get("factors") or {}
        cell = ((f.get("active_cell") or {}).get("cell")) or "?"
        rec = {
            "trade_id": tid, "side": side, "ep": ep, "cell": cell,
            "nq_gate": f.get("nq_gate"), "regime": f.get("regime"),
            "polls": len(path),
            "hold_min": last.get("hold_min"),
            "mfe": max(outs) if outs else None,
            "mae": min(outs) if outs else None,
            "last_markout": outs[-1] if outs else None,
            "m1": markout_at(path, 1), "m5": markout_at(path, 5),
            "m10": markout_at(path, 10), "m20": markout_at(path, 20),
            "z1": (last.get("conformance") or {}).get("z"),
        }
        table.append(rec)
        ts = str(first.get("ts") or "")[5:19]
        print(f"{i:<4}{ts:<17}{side:<5}{(ep if ep is not None else 0):>9.0f}  {cell:<18}"
              f"{str(rec['nq_gate']):<6}{(rec['hold_min'] or 0):>7.1f}"
              f"{(rec['mfe'] if rec['mfe'] is not None else 0):>8.1f}"
              f"{(rec['mae'] if rec['mae'] is not None else 0):>8.1f}"
              f"{(rec['last_markout'] if rec['last_markout'] is not None else 0):>8.1f}"
              f"{str(rec['z1']):>7}")

    print("\n=== 因子分組（每格的實際表現 vs 期望）===")
    print(f"{'cell':<22}{'n':>5}{'平均 last markout':>20}{'期望@持有中位':>16}")
    by_cell: dict[str, list[dict]] = {}
    for r in table:
        by_cell.setdefault(str(r["cell"]), []).append(r)
    for cell, rs in sorted(by_cell.items(), key=lambda kv: -len(kv[1])):
        vals = [r["last_markout"] for r in rs if r["last_markout"] is not None]
        holds = [r["hold_min"] for r in rs if r["hold_min"] is not None]
        if not vals:
            continue
        med_h = st.median(holds) if holds else 0
        exp = min(BASELINE_MARKOUT, key=lambda k: abs(k - med_h))
        print(f"{cell:<22}{len(rs):>5}{st.mean(vals):>20.2f}"
              f"{BASELINE_MARKOUT[exp][0]:>16.2f}")

    print(f"\n=== 滾動符合度（最近 {ROLLING_N} 筆 @ {ROLLING_HORIZON_MIN} 分視野）===")
    seq = [r[f"m{ROLLING_HORIZON_MIN}"] for r in table if r.get(f"m{ROLLING_HORIZON_MIN}") is not None]
    rc = rolling_conformance(seq)
    if not rc.get("ok"):
        print(f"  樣本不足：有 {rc.get('have')} 筆、需要 {rc.get('need')} 筆")
        print(f"  （這是誠實的狀態，不是錯誤——TMF 目前 dry-run，要累積到 "
              f"{ROLLING_N} 筆才有檢定力）")
    else:
        print(f"  最近 {rc['n']} 筆平均 {rc['mean_actual']:+.2f} pts · 期望 {rc['expected']:+.2f}"
              f" · SE {rc['se']} · z={rc['z']}")
        if rc["breached"]:
            print(f"  ⚠ 已破 {rc['z']}σ → 建議動作：{rc['action_if_breached']}"
                  f"（停止開新倉，不是砍現有部位）")
        else:
            print("  ✓ 未破門檻：策略行為與實測基準一致")

    if args.out:
        payload = {"schema": "tmf-trade-journal-report-v1",
                   "days": args.days, "n_trades": len(table),
                   "trades": table, "rolling_conformance": rc,
                   "generated_at": datetime.now().isoformat(timespec="seconds")}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
