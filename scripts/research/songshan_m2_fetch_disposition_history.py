#!/usr/bin/env python3
"""songshan_m2 · 抓 2024-06 ~ 2026-08 完整處置股歷史（TWSE 上市 + TPEx 上櫃）.

既有 scripts/research/refresh_twse_disposition_cache.py 只抓 TWSE 且只留「第二次處置」、
只有 2026 年。本腳本擴成：
  - 兩個交易所（TWSE 上市 / TPEx 上櫃）—— 9217 母體有一半是上櫃股，只抓 TWSE 會漏掉
  - 全部處置類型（第一次／第二次／人工管制撮合）
  - 2024-06-01 ~ 今天

並解析「處置措施」文字，標記預收款券（全額）的適用範圍：
  prefund_blanket        = 任何委託都要預收全部買進價金（下單層會直接被券商退件）
  prefund_ge10lots       = 單筆達 10 交易單位或多筆累積達 30 交易單位才預收
  none                   = 只有人工管制撮合，無預收款券條款

輸出：reports/research/branch-footprint-screen/songshan_m2/disposition_history.json

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m2_fetch_disposition_history.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen" / "songshan_m2"
OUT = OUT_DIR / "disposition_history.json"

UA = {"User-Agent": "Mozilla/5.0"}
START = date(2024, 6, 1)
END = date.today()


def roc_to_iso(s: str) -> str:
    s = s.strip()
    y, m, d = s.split("/")
    return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"


def classify_measure(text: str) -> str:
    """由處置措施文字判定預收款券適用範圍。"""
    t = (text or "").replace("\n", "").replace(" ", "")
    has_prefund = "收取全部之買進價金" in t
    if not has_prefund:
        return "none"
    # 有門檻版：單筆達十/10交易單位 或 多筆累積達三十/30交易單位
    if re.search(r"單筆達(十|10)交易單位", t) or re.search(r"累積達(三十|30)交易單位", t):
        return "prefund_ge10lots"
    return "prefund_blanket"


def split_period(s: str) -> tuple[str, str] | None:
    s = (s or "").replace("～", "~").replace("﹏", "~").strip()
    if "~" not in s:
        return None
    a, b = s.split("~", 1)
    try:
        return roc_to_iso(a), roc_to_iso(b)
    except Exception:  # noqa: BLE001
        return None


def fetch_twse(start: date, end: date) -> list[dict]:
    url = (
        "https://www.twse.com.tw/rwd/zh/announcement/punish"
        f"?response=json&startDate={start:%Y%m%d}&endDate={end:%Y%m%d}"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    rows: list[dict] = []
    for r in payload.get("data") or []:
        sid = str(r[2]).strip()
        if len(sid) != 4 or not sid.isdigit():
            continue
        per = split_period(str(r[6]))
        if not per:
            continue
        rows.append(
            {
                "market": "TWSE",
                "announce": roc_to_iso(str(r[1])),
                "sid": sid,
                "name": str(r[3]),
                "p_start": per[0],
                "p_end": per[1],
                "disp_type": str(r[7]),
                "prefund": classify_measure(str(r[8])),
            }
        )
    return rows


def fetch_tpex(start: date, end: date) -> list[dict]:
    url = (
        "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
        f"?startDate={start:%Y/%m/%d}&endDate={end:%Y/%m/%d}&response=json"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    rows: list[dict] = []
    for tbl in payload.get("tables") or []:
        for r in tbl.get("data") or []:
            sid = str(r[2]).strip()
            if len(sid) != 4 or not sid.isdigit():
                continue
            per = split_period(str(r[5]))
            if not per:
                continue
            name = re.sub(r"\(.*\)$", "", str(r[3])).strip()
            rows.append(
                {
                    "market": "TPEx",
                    "announce": roc_to_iso(str(r[1])),
                    "sid": sid,
                    "name": name,
                    "p_start": per[0],
                    "p_end": per[1],
                    "disp_type": str(r[6])[:60],
                    "prefund": classify_measure(str(r[7])),
                }
            )
    return rows


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    # 分年抓，避免單次回傳被截斷
    for year in range(START.year, END.year + 1):
        s = max(START, date(year, 1, 1))
        e = min(END, date(year, 12, 31))
        if s > e:
            continue
        tw = fetch_twse(s, e)
        time.sleep(1.0)
        tp = fetch_tpex(s, e)
        time.sleep(1.0)
        print(f"[{year}] TWSE {len(tw)} · TPEx {len(tp)}")
        all_rows.extend(tw)
        all_rows.extend(tp)

    # 去重
    seen = set()
    dedup = []
    for r in all_rows:
        k = (r["market"], r["sid"], r["p_start"], r["p_end"])
        if k in seen:
            continue
        seen.add(k)
        dedup.append(r)
    dedup.sort(key=lambda x: (x["p_start"], x["sid"]))

    from collections import Counter

    print(f"[OK] episodes={len(dedup)}")
    print("  prefund:", Counter(r["prefund"] for r in dedup))
    print("  market :", Counter(r["market"] for r in dedup))
    print("  span   :", dedup[0]["p_start"], "~", dedup[-1]["p_start"])
    OUT.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "range": [str(START), str(END)],
                "sources": {
                    "TWSE": "https://www.twse.com.tw/rwd/zh/announcement/punish",
                    "TPEx": "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal",
                },
                "n": len(dedup),
                "episodes": dedup,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
