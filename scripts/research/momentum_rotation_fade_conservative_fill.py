"""反向 edge 在「拿得到的成交價」下還剩多少 —— 樂觀(VWAP) vs 保守(該秒最不利價).

疑慮：先前量到「fade edge 隨爆量幅度單調放大」（>31bps 那組 1s +22.2bps t=5.3），
但進出場價都用該秒 VWAP。現實中買不到 VWAP：那一秒若價格一路噴，VWAP 落在區間中段，
下一秒的「回歸」有一部分只是**從區間中點量起**的算術效果。而且 move_bps 越大 =>
該秒區間越寬 => 這個效應越大，正好會製造出「edge 隨爆量放大」的假象。

這正是 2026-08-13 fill-price bug 的同一類錯（memory: score 用的資料跟 P&L 記帳用的
資料必須是同一個拿得到的價格）。

本腳本對同一批訊號算三種成交假設：
  A 樂觀 VWAP  ：進出場都用該秒 VWAP（先前的算法）
  B 保守極端價 ：fade 放空→進場取該秒**最低**價、出場取該秒**最高**價（fade 做多鏡像）
                 = 「你一定拿得到」的下界
  C 次秒進場   ：訊號成立後**下一秒**才進場（真實系統有偵測+送單延遲），用該秒 VWAP
"""
from __future__ import annotations

import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_horizon_decay import (  # noqa: E402
    COIL_SEC, CONTRACTION, COOLDOWN_SEC, EXCLUDE, MIN_ROWS,
    MOVE_BPS, TREND_SEC, VOL_MULT, ARCHIVE,
)

HZ = [1, 2, 3, 5, 8, 15, 30]


def build(times, prices, vols):
    sk = defaultdict(list)
    for i, t in enumerate(times):
        sk[int(t[11:13]) * 3600 + int(t[14:16]) * 60 + int(t[17:19])].append(i)
    secs = sorted(sk)
    n = len(secs)
    vwap = np.empty(n); lo = np.empty(n); hi = np.empty(n); sv = np.empty(n)
    for k, s in enumerate(secs):
        idx = sk[s]; p = prices[idx]; v = vols[idx]
        tot = v.sum()
        vwap[k] = float((p * v).sum() / tot) if tot > 0 else float(p.mean())
        lo[k] = float(p.min()); hi[k] = float(p.max()); sv[k] = float(tot)
    return np.array(secs), vwap, lo, hi, sv


def signals(secs, vwap, sv):
    out = []; last = -10**9; vh = []
    pos = {s: i for i, s in enumerate(secs)}
    for i in range(len(secs)):
        s = secs[i]; vh.append(sv[i])
        if i == 0 or s - last < COOLDOWN_SEC:
            continue
        base = max(statistics.median(vh[:-1]), 1e-9) if len(vh) > 1 else 1.0
        if sv[i] < VOL_MULT * base:
            continue
        mv = (vwap[i] - vwap[i - 1]) / vwap[i - 1] * 1e4
        if abs(mv) < MOVE_BPS:
            continue
        dr = 1 if mv > 0 else -1
        a0, b0 = pos.get(s - COIL_SEC), pos.get(s - 2 * COIL_SEC)
        if a0 is None or b0 is None or a0 <= b0:
            continue
        rec, pri = vwap[a0:i], vwap[b0:a0]
        if len(rec) < 2 or len(pri) < 2:
            continue
        if (rec.max() - rec.min()) > CONTRACTION * (pri.max() - pri.min()):
            continue
        t0 = pos.get(s - TREND_SEC)
        if t0 is None or np.sign(vwap[i] - vwap[t0]) != dr:
            continue
        out.append((i, dr, abs(mv))); last = s
    return out


def main() -> None:
    acc = {k: defaultdict(list) for k in ("A", "B", "C")}
    for path in sorted(ARCHIVE.glob("*.csv")):
        code = path.stem
        if code in EXCLUDE:
            continue
        by = defaultdict(lambda: ([], [], [])); tot = 0
        with path.open() as f:
            for r in csv.DictReader(f):
                if "/" in (r.get("contract_date") or ""):
                    continue
                d = (r.get("date") or "")[:10]
                if not d:
                    continue
                try:
                    px = float(r["price"]); vv = float(r["volume"])
                except (KeyError, ValueError, TypeError):
                    continue
                if px <= 0:
                    continue
                t, p, v = by[d]; t.append(r["date"]); p.append(px); v.append(vv); tot += 1
        if tot < MIN_ROWS:
            continue
        for d, (t, p, v) in by.items():
            o = sorted(range(len(t)), key=lambda k: t[k])
            t = [t[k] for k in o]
            p = np.array([p[k] for k in o]); v = np.array([v[k] for k in o])
            secs, vwap, lo, hi, sv = build(t, p, v)
            if len(secs) < 400:
                continue
            for i, dr, mv in signals(secs, vwap, sv):
                fd = -dr                      # fade = 反向
                for h in HZ:
                    j = int(np.searchsorted(secs, secs[i] + h))
                    if j >= len(secs):
                        continue
                    # A 樂觀：兩端都用 VWAP
                    acc["A"][(h, mv >= 31)].append((vwap[j] - vwap[i]) / vwap[i] * 1e4 * fd)
                    # B 保守：進場拿最不利、出場也拿最不利
                    ent = lo[i] if fd < 0 else hi[i]      # 放空進場取最低 / 做多進場取最高
                    ext = hi[j] if fd < 0 else lo[j]      # 放空回補取最高 / 做多賣出取最低
                    acc["B"][(h, mv >= 31)].append((ext - ent) / ent * 1e4 * fd)
                    # C 延遲一秒進場（用 VWAP），出場同樣延後
                    i2 = int(np.searchsorted(secs, secs[i] + 1))
                    j2 = int(np.searchsorted(secs, secs[i] + 1 + h))
                    if i2 < len(secs) and j2 < len(secs):
                        acc["C"][(h, mv >= 31)].append((vwap[j2] - vwap[i2]) / vwap[i2] * 1e4 * fd)
    names = {"A": "樂觀 VWAP（先前算法）", "B": "保守 該秒最不利價", "C": "延遲 1 秒進場(VWAP)"}
    for big in (True, False):
        print("=" * 84)
        print(f"=== 爆量 {'>=31bps（先前 edge 全部來自這組）' if big else '<31bps（先前接近 0）'} ===")
        print(f"{'持有':>6s}" + "".join(f"{names[k]:>26s}" for k in ("A", "B", "C")))
        for h in HZ:
            cells = []
            for k in ("A", "B", "C"):
                a = acc[k][(h, big)]
                if len(a) < 20:
                    cells.append("            n/a"); continue
                m = statistics.mean(a)
                t = m / (statistics.stdev(a) / math.sqrt(len(a))) if len(a) > 2 else float("nan")
                cells.append(f"{m:>+13.1f}bps (t{t:>+5.1f})")
            print(f"{str(h) + 's':>6s}" + "".join(f"{c:>26s}" for c in cells))
        print(f"   n = {len(acc['A'][(HZ[0], big)])}\n")
    print("成本線：小型緯穎 8.4 / 白名單 9.7~10.9 / QFF 小型台積電 21.3 / CCF 實測價差 41.7 bps")


if __name__ == "__main__":
    main()
