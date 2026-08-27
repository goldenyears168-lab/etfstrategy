"""六個「先前沒看」的維度做成加權評分——先各自驗證，通過的才准進權重.

使用者 2026-08-21 要求把先前訊號沒看的六個維度一起納入評分。資料可用性先查過：
  ✅ 30 天可算：該秒真實 high/low、橫斷面（其他標的）、前一天或更早
  ❌ 只有 2 天：五檔／掛單量／買賣力道失衡、主動買 vs 主動賣（websocket 8/20 才開始收）
所以本腳本先做前三類，委託簿類等資料累積（約九月中）再加。

方法紀律（全部來自 2026-08-20/21 那輪的教訓）：
1. **C 檢定內建**：進場一律延後 1 秒。昨晚 +22.8bps 的「反向 edge」延後一秒就變 −6.8，
   整個效應只活在訊號那一秒內。任何不含延遲的數字都不要看。
2. **先個別驗證，再談加權**。在沒有個別預測力的維度上擬合權重＝製造過擬合。
   本腳本先報每個維度的 IC 與分位差，再組合。
3. **依股票切 train/holdout**（種子 42），權重只在 train 上決定。
4. 價格一律用順序無關的統計量（VWAP、該秒 high/low 的 max/min），
   不用「該秒第一筆/最後一筆」——archive 秒內是按價格排序的。
"""
from __future__ import annotations

import csv
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_horizon_decay import (  # noqa: E402
    ARCHIVE, COIL_SEC, CONTRACTION, COOLDOWN_SEC, EXCLUDE,
    MIN_ROWS, MOVE_BPS, TREND_SEC, VOL_MULT,
)

SEED = 42
ENTRY_DELAY = 1            # C 檢定：進場延後 1 秒
HZ = [5, 15, 30, 60]
FEATS = ["sec_range", "vwap_pos", "coil_true", "rel_strength", "prev_vol", "gap"]


def build_day(times, prices, vols):
    sk = defaultdict(list)
    for i, t in enumerate(times):
        sk[int(t[11:13]) * 3600 + int(t[14:16]) * 60 + int(t[17:19])].append(i)
    secs = sorted(sk)
    n = len(secs)
    vw = np.empty(n); hi = np.empty(n); lo = np.empty(n); sv = np.empty(n)
    for k, s in enumerate(secs):
        idx = sk[s]; p = prices[idx]; v = vols[idx]; tot = v.sum()
        vw[k] = float((p * v).sum() / tot) if tot > 0 else float(p.mean())
        hi[k] = float(p.max()); lo[k] = float(p.min()); sv[k] = float(tot)
    return np.array(secs), vw, hi, lo, sv


def main() -> None:
    print("載入 archive（30 天）...", flush=True)
    days: dict[str, dict[str, tuple]] = defaultdict(dict)
    daily: dict[str, dict[str, dict]] = defaultdict(dict)   # code -> date -> {ret, vol, open, close}
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
            secs, vw, hi, lo, sv = build_day(t, p, v)
            if len(secs) < 400:
                continue
            days[d][code] = (secs, vw, hi, lo, sv)
            rr = np.diff(vw) / vw[:-1]
            daily[code][d] = {"open": float(vw[0]), "close": float(vw[-1]),
                              "vol": float(np.std(rr) * 1e4) if len(rr) > 10 else float("nan")}
    print(f"  {len(days)} 個交易日 · {len({c for v in days.values() for c in v})} 檔")

    rows = []
    for d in sorted(days):
        book = days[d]
        # 市場因子：所有標的在 10 秒格點上的 60 秒報酬中位數（橫斷面基準）
        grid = {}
        for code, (secs, vw, *_r) in book.items():
            for k in range(len(secs)):
                if secs[k] % 10:
                    continue
                j = int(np.searchsorted(secs, secs[k] - 60))
                if j < k:
                    grid.setdefault(secs[k], []).append((vw[k] - vw[j]) / vw[j] * 1e4)
        mkt = {s: statistics.median(v) for s, v in grid.items() if len(v) >= 5}
        prev = sorted(x for x in days if x < d)
        pd_ = prev[-1] if prev else None
        for code, (secs, vw, hi, lo, sv) in book.items():
            pos = {s: i for i, s in enumerate(secs)}
            vh = []; last = -10**9
            pv = daily.get(code, {}).get(pd_) if pd_ else None
            gap = ((vw[0] - pv["close"]) / pv["close"] * 1e4) if pv else float("nan")
            for i in range(len(secs)):
                s = secs[i]; vh.append(sv[i])
                if i == 0 or s - last < COOLDOWN_SEC:
                    continue
                base = max(statistics.median(vh[:-1]), 1e-9) if len(vh) > 1 else 1.0
                if sv[i] < VOL_MULT * base:
                    continue
                mv = (vw[i] - vw[i - 1]) / vw[i - 1] * 1e4
                if abs(mv) < MOVE_BPS:
                    continue
                dr = 1 if mv > 0 else -1
                a0, b0 = pos.get(s - COIL_SEC), pos.get(s - 2 * COIL_SEC)
                t0 = pos.get(s - TREND_SEC)
                if a0 is None or b0 is None or t0 is None or a0 <= b0:
                    continue
                if np.sign(vw[i] - vw[t0]) != dr:
                    continue
                rec, pri = vw[a0:i], vw[b0:a0]
                if len(rec) < 2 or len(pri) < 2:
                    continue
                if (rec.max() - rec.min()) > CONTRACTION * (pri.max() - pri.min()):
                    continue
                last = s
                # ---- 六維度裡現在算得出來的三類 ----
                rng = hi[i] - lo[i]
                f = {
                    # 1) 該秒真實 high/low
                    "sec_range": rng / vw[i] * 1e4,
                    "vwap_pos": (((vw[i] - lo[i]) / rng) if rng > 0 else 0.5) * dr + (0.5 * (1 - dr)),
                    "coil_true": ((hi[a0:i].max() - lo[a0:i].min())
                                  / max(hi[b0:a0].max() - lo[b0:a0].min(), 1e-9)),
                    # 2) 橫斷面
                    "rel_strength": float("nan"),
                    # 3) 跨日
                    "prev_vol": pv["vol"] if pv else float("nan"),
                    "gap": gap,
                }
                g = s - s % 10
                if g in mkt:
                    j60 = int(np.searchsorted(secs, s - 60))
                    if j60 < i:
                        own = (vw[i] - vw[j60]) / vw[j60] * 1e4
                        f["rel_strength"] = (own - mkt[g]) * dr
                # ---- 目標：延後 1 秒進場的順勢報酬（負值代表反向才賺）----
                ie = int(np.searchsorted(secs, s + ENTRY_DELAY))
                if ie >= len(secs):
                    continue
                tgt = {}
                for h in HZ:
                    je = int(np.searchsorted(secs, secs[ie] + h))
                    tgt[h] = ((vw[je] - vw[ie]) / vw[ie] * 1e4 * dr) if je < len(secs) else None
                rows.append({"code": code, "date": d, "dir": dr, **f,
                             **{f"y{h}": tgt[h] for h in HZ}})
    print(f"  訊號 {len(rows)} 個（全部已延後 1 秒進場）\n")

    codes = sorted({r["code"] for r in rows})
    rng_ = random.Random(SEED); sh = codes[:]; rng_.shuffle(sh)
    train = set(sh[:len(sh) // 2]); hold = set(sh[len(sh) // 2:])
    tr = [r for r in rows if r["code"] in train]
    ho = [r for r in rows if r["code"] in hold]
    print(f"train {len(tr)} 訊號 / {len(train)} 檔   holdout {len(ho)} 訊號 / {len(hold)} 檔\n")

    def ic(rs, feat, h):
        pair = [(r[feat], r[f"y{h}"]) for r in rs
                if r.get(feat) is not None and r[feat] == r[feat] and r.get(f"y{h}") is not None]
        if len(pair) < 50:
            return float("nan"), 0
        a = [p[0] for p in pair]; b = [p[1] for p in pair]
        ra = {v: k for k, v in enumerate(sorted(range(len(a)), key=lambda i: a[i]))}
        rb = {v: k for k, v in enumerate(sorted(range(len(b)), key=lambda i: b[i]))}
        xa = [ra[i] for i in range(len(a))]; xb = [rb[i] for i in range(len(b))]
        ma, mb = statistics.mean(xa), statistics.mean(xb)
        num = sum((x - ma) * (y - mb) for x, y in zip(xa, xb))
        den = math.sqrt(sum((x - ma) ** 2 for x in xa) * sum((y - mb) ** 2 for y in xb))
        return (num / den if den else float("nan")), len(pair)

    print("=" * 88)
    print("=== 各維度個別預測力（train，Spearman IC；|IC|>0.1 才值得進權重）===")
    print(f"{'維度':>14s}" + "".join(f"{'y' + str(h) + 's':>12s}" for h in HZ) + f"{'n':>8s}")
    keep = []
    for f_ in FEATS:
        cells = []; nn = 0
        for h in HZ:
            v, n = ic(tr, f_, h); nn = max(nn, n)
            cells.append(f"{v:>+12.3f}" if v == v else f"{'n/a':>12s}")
        best = max((abs(ic(tr, f_, h)[0]) for h in HZ if ic(tr, f_, h)[0] == ic(tr, f_, h)[0]), default=0)
        if best >= 0.10:
            keep.append(f_)
        print(f"{f_:>14s}" + "".join(cells) + f"{nn:>8d}" + ("   ← 進權重" if best >= 0.10 else ""))
    print(f"\n通過 |IC|>=0.10 的維度：{keep if keep else '（無）'}")
    if not keep:
        print("→ 沒有任何維度有個別預測力，**不做加權組合**（在沒訊號的維度上擬合權重＝過擬合）")
        return

    # 加權：train 上以 IC 為權重（符號帶入），holdout 驗證分位差
    h0 = HZ[1]
    w = {f_: ic(tr, f_, h0)[0] for f_ in keep}
    print(f"\n權重（train 的 IC，目標 y{h0}s）：" + "  ".join(f"{k}={v:+.3f}" for k, v in w.items()))
    def score(r):
        z = [w[f_] * r[f_] for f_ in keep if r.get(f_) is not None and r[f_] == r[f_]]
        return sum(z) if len(z) == len(keep) else None
    for name, rs in (("train", tr), ("holdout", ho)):
        sc = [(score(r), r[f"y{h0}"]) for r in rs if score(r) is not None and r.get(f"y{h0}") is not None]
        if len(sc) < 60:
            print(f"{name}: 樣本不足"); continue
        sc.sort(key=lambda x: x[0])
        q = len(sc) // 5
        lo_, hi_ = [x[1] for x in sc[:q]], [x[1] for x in sc[-q:]]
        se = math.sqrt(statistics.variance(lo_) / len(lo_) + statistics.variance(hi_) / len(hi_))
        d_ = statistics.mean(hi_) - statistics.mean(lo_)
        print(f"{name:8s} n={len(sc):5d}  最低五分位 {statistics.mean(lo_):+7.2f}bps  "
              f"最高五分位 {statistics.mean(hi_):+7.2f}bps  差 {d_:+7.2f}bps  t={d_/se if se else float('nan'):+5.2f}")


if __name__ == "__main__":
    main()
