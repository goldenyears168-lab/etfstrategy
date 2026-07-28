#!/usr/bin/env python3
"""強制刷新 TWSE「第二次處置」清單快取（rolling endDate），供 OOS 凍結評分累積新處置窗。

既有 run_second_disp_top30_l1h7.fetch_twse_second_2026() 有快取即回傳、不刷新，且 endDate
寫死。本腳本每次都重抓（startDate 固定 20260101、endDate=今日），覆蓋
  reports/research/branch-footprint-screen/second_disp_top30_l1h7/twse_second_disp_2026.json
抓取失敗時**不動舊快取**（保留既有，exit 非 0 讓 launcher 通知）。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/refresh_twse_disposition_cache.py
  # 自訂區間：--start 20260101 --end 20261231
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports/research/branch-footprint-screen/second_disp_top30_l1h7/twse_second_disp_2026.json"
FREEZE = ROOT / "reports/research/branch-footprint-screen/expanded/oos_freeze/freeze_manifest.json"
DISP_TYPE = "第二次處置"


def roc_to_iso(s: str) -> str:
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911}-{m}-{d}"


def fetch(start: str, end: str) -> list[dict]:
    url = (
        "https://www.twse.com.tw/rwd/zh/announcement/punish"
        f"?response=json&startDate={start}&endDate={end}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode())
    rows = []
    for row in payload.get("data") or []:
        if row[7] != DISP_TYPE:
            continue
        try:
            a, b = row[6].replace("～", "~").split("~")
        except ValueError:
            continue
        sid = str(row[2])
        if len(sid) != 4 or not sid.isdigit():
            continue
        rows.append({
            "announce": roc_to_iso(row[1]),
            "sid": sid,
            "name": row[3],
            "p_start": roc_to_iso(a),
            "p_end": roc_to_iso(b),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    args = ap.parse_args()

    old = json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else []
    old_keys = {(e["sid"], e["p_start"]) for e in old}
    try:
        rows = fetch(args.start, args.end)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] TWSE fetch failed: {exc}. 保留舊快取（{len(old)} episodes），不覆蓋。", file=sys.stderr)
        return 2
    if not rows:
        print(f"[WARN] fetch 回傳 0 筆（{args.start}..{args.end}），保留舊快取不覆蓋。", file=sys.stderr)
        return 3

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
    new_keys = {(e["sid"], e["p_start"]) for e in rows}
    added = new_keys - old_keys

    freeze_date = None
    if FREEZE.exists():
        freeze_date = json.loads(FREEZE.read_text("utf-8")).get("freeze_date")
    oos_new = [e for e in rows if freeze_date and e["p_start"] > freeze_date]
    print(f"[OK] {args.start}..{args.end} · episodes={len(rows)} (was {len(old)}, +{len(added)} new)")
    if freeze_date:
        print(f"[OOS] freeze_date={freeze_date} · p_start>freeze 的處置窗={len(oos_new)}"
              + (f" → {sorted({e['sid'] for e in oos_new})}" if oos_new else "（尚無，OOS 待累積）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
