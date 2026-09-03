#!/usr/bin/env python3
"""live_biglot_flow_probe 的彙總器 · 每檔自適應大單門檻.

固定金額門檻（500 萬）對高價股失效：聯發科 4,315 元買 2 張就 863 萬，
幾乎每筆都被歸為大戶。改用**每檔自己的單量分布**：
    大單 = 單筆張數 >= 該股最近 20 個交易日逐筆單量的 P99
沒有歷史的（不在 PIT 宇宙）退回用當日窗口自身的 P99，並標記。

用法：PYTHONPATH=src .venv/bin/python scripts/research/live_biglot_flow_report.py [--minutes 30]
"""
from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from stock_db import DATA_DIR

_TZ = ZoneInfo("Asia/Taipei")
RAW = DATA_DIR.parent / "cache" / "live_biglot_flow"
DIST = DATA_DIR / "cache" / "pit_universe_tick" / "_lot_dist.pkl"
QUANT = "p99"
BIG_AMT = 5_000_000        # 可用 --amount 覆寫


def load(day: str):
    seen = set()
    out = []
    out_auction = []
    for line in (RAW / f"raw_{day}.jsonl").open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("kind") != "message":
            continue
        p = r.get("payload") or {}
        if p.get("channel") != "trades" or p.get("event") not in (None, "data"):
            continue
        d = p.get("data") or {}
        sid, px, sz = str(d.get("symbol", "")), d.get("price"), d.get("size")
        if not sid or px is None or sz is None:
            continue
        # 13:25-13:30 試撮不是成交，且回報的是「若現在撮合會成交多少」的累計量
        if d.get("isTrial"):
            continue
        # 13:30 收盤集合競價：單一價格撮合，Lee-Ready 內外盤無意義（clearing price
        # 幾乎等於 bid → 會被整批誤判為主動賣），單獨計不進連續盤統計
        if d.get("isContinuous") is not True:
            out_auction.append({"sid": sid, "sz": float(sz), "px": float(px)})
            continue
        key = (sid, d.get("serial"))
        if d.get("serial") is not None and key in seen:      # 殭屍重送
            continue
        seen.add(key)
        out.append({"ts": r["ts"], "sid": sid, "px": float(px), "sz": float(sz),
                    "bid": d.get("bid"), "ask": d.get("ask"),
                    "cumvol": d.get("volume"), "cont": d.get("isContinuous")})
    return out, out_auction


def side_of(t, prev):
    px, bid, ask = t["px"], t["bid"], t["ask"]
    if ask is not None and px >= float(ask):
        return 1
    if bid is not None and px <= float(bid):
        return -1
    if prev is not None:
        return 1 if px > prev else (-1 if px < prev else 0)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30, help="另外單獨看最後 N 分鐘")
    ap.add_argument("--amount", type=float, default=BIG_AMT, help="固定金額門檻（元），預設 500 萬")
    ap.add_argument("--sort", choices=("adaptive", "amount"), default="adaptive")
    a = ap.parse_args()
    amt_thr = a.amount
    day = datetime.now(_TZ).strftime("%Y-%m-%d")
    rows, auction = load(day)
    if not rows:
        print("尚無資料")
        return 1
    hist = pickle.load(DIST.open("rb")) if DIST.exists() else {}

    bysid: dict[str, list] = {}
    for t in rows:
        bysid.setdefault(t["sid"], []).append(t)
    t_end = max(t["ts"] for t in rows)
    cut = (datetime.fromisoformat(t_end).timestamp() - a.minutes * 60)

    res = []
    for sid, ts in bysid.items():
        sizes = np.array([t["sz"] for t in ts])
        if sid in hist:
            thr, src = hist[sid][QUANT], "hist"
        else:
            thr, src = float(np.quantile(sizes, 0.99)), "window"
        thr = max(thr, 1.0)
        prev = None
        bb = bs = fb = fs = lb = ls = 0.0
        vol = 0.0
        for t in ts:
            s = side_of(t, prev)
            prev = t["px"]
            vol += t["sz"]
            big_ad = t["sz"] >= thr
            big_fx = t["px"] * t["sz"] * 1000 >= amt_thr
            late = datetime.fromisoformat(t["ts"]).timestamp() >= cut
            if s > 0:
                if big_ad:
                    bb += t["sz"]
                    if late:
                        lb += t["sz"]
                if big_fx:
                    fb += t["sz"]
            elif s < 0:
                if big_ad:
                    bs += t["sz"]
                    if late:
                        ls += t["sz"]
                if big_fx:
                    fs += t["sz"]
        daily = ts[-1]["cumvol"] or 0
        res.append({"sid": sid, "thr": thr, "src": src, "net": bb - bs,
                    "norm": (bb - bs) / vol * 100 if vol else 0,
                    "fixnorm": (fb - fs) / vol * 100 if vol else 0,
                    "late": lb - ls, "vol": vol, "daily": daily, "n": len(ts),
                    "px0": ts[0]["px"], "px1": ts[-1]["px"]})
    res.sort(key=lambda x: -(x["norm"] if a.sort == "adaptive" else x["fixnorm"]))
    t0 = min(t["ts"] for t in rows)
    print(f"窗口 {t0[11:19]} → {t_end[11:19]}   {len(rows):,} 筆 / {len(bysid)} 檔")
    print(f"大單定義：自適應 = 單筆張數 >= 該股近 20 日逐筆單量 {QUANT.upper()}（無歷史者用窗口 P99，標 *）；"
          f"固定 = 單筆金額 >= {a.amount/1e4:,.0f} 萬")
    print(f"排序依據：{'自適應' if a.sort == 'adaptive' else '固定金額'}\n")
    hdr = (f"{'#':<3}{'代號':<6}{'門檻張':>7}{'大單淨買':>9}{'佔窗量%':>9}"
           f"{'末'+str(int(a.minutes))+'分淨':>10}{'窗內價%':>9}{'固定金額%':>11}")
    print("【大單主動買 — 自適應門檻】")
    print(hdr)
    for i, r in enumerate(res[:15], 1):
        m = "*" if r["src"] == "window" else " "
        print(f"{i:<3}{r['sid']+m:<6}{r['thr']:>7.0f}{r['net']:>9,.0f}{r['norm']:>+9.1f}"
              f"{r['late']:>+10,.0f}{(r['px1']/r['px0']-1)*100:>+8.2f}%{r['fixnorm']:>+11.1f}")
    print("\n【大單主動賣】")
    print(hdr)
    for i, r in enumerate(res[-8:][::-1], 1):
        m = "*" if r["src"] == "window" else " "
        print(f"{i:<3}{r['sid']+m:<6}{r['thr']:>7.0f}{r['net']:>9,.0f}{r['norm']:>+9.1f}"
              f"{r['late']:>+10,.0f}{(r['px1']/r['px0']-1)*100:>+8.2f}%{r['fixnorm']:>+11.1f}")
    if auction:
        av = sum(x["sz"] for x in auction)
        print(f"\n（另有收盤集合競價 {len(auction)} 筆 / {av:,.0f} 張，單一價格撮合，未計入上表）")
    tot = sum(r["net"] for r in res)
    late = sum(r["late"] for r in res)
    print(f"\n45 檔合計 大單淨買 {tot:+,.0f} 張；最後 {int(a.minutes)} 分鐘 {late:+,.0f} 張")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
