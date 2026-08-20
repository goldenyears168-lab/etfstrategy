#!/usr/bin/env python3
"""對抗性複核 B2：把限價出場的壓力階梯拉到策略真正的觸發尺度。

B2 的 stress 只做到「前 60 秒逆向 ≥10 點」。但 causal_engine 的 trail／struct_break
出場（佔市價出場的 ~76%）觸發時的逆向幅度是數十點（trail|80->16 這種），
差一個數量級。本腳本沿用 B2 的載入與成交判定，只把 k 拉到 20/30/50/80，
並加上 block bootstrap 看 +0.945 的離散度。
"""
from __future__ import annotations
import bisect, math, random, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "research"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "b2", Path(__file__).resolve().parents[2] / "scripts/research/wall_b2_exit_cost_curve.py")
b2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(b2)

T_SEC = 60.0
STRIDE = 10.0


def collect(day):
    rows, _ = b2.load_live_books(day)
    trades, _ = b2.load_live_trades(day)
    if len(rows) < 10 or len(trades) < 10:
        return []
    bt = [r["t"] for r in rows]; tt = [x[0] for x in trades]
    out = []
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    for k in range(int((t1 - t0) / STRIDE)):
        t = t0 + k * STRIDE
        i = bisect.bisect_right(bt, t) - 1
        if i < 0: continue
        r = rows[i]
        if t - r["t"] > b2.MAX_GAP_SEC: continue
        A, QA, B, QB = r["ap"][0], r["asz"][0], r["bp"][0], r["bs"][0]
        sess = r["sess"]; mid = (A + B) / 2
        i0 = bisect.bisect_right(bt, t - 60.0) - 1
        ret60 = math.nan
        if i0 >= 0:
            r0 = rows[i0]
            if (t - 60.0) - r0["t"] <= b2.MAX_GAP_SEC and r0["sess"] == sess:
                ret60 = mid - (r0["ap"][0] + r0["bp"][0]) / 2
        j = bisect.bisect_right(tt, t); ca = cb = 0
        fs = fb = None
        while j < len(trades) and trades[j][0] <= t + T_SEC:
            _, px, sz = trades[j]
            if fs is None:
                if px > A: fs = 1
                elif px >= A:
                    ca += sz
                    if ca > QA: fs = 1
            if fb is None:
                if px < B: fb = 1
                elif px <= B:
                    cb += sz
                    if cb > QB: fb = 1
            if fs and fb: break
            j += 1
        m = bisect.bisect_right(bt, t + T_SEC) - 1
        if m < 0: continue
        rr = rows[m]
        if (t + T_SEC) - rr["t"] > b2.MAX_GAP_SEC or rr["sess"] != sess: continue
        gs = (A if fs else rr["bp"][0]) - B
        gb = A - (B if fb else rr["ap"][0])
        out.append((t, sess, ret60, gs, gb, bool(fs), bool(fb)))
    return out


def main():
    days = sys.argv[1:] or ["2026-08-17", "2026-08-18", "2026-08-19"]
    allrows = []
    for d in days:
        rs = collect(d)
        print(f"  {d}: {len(rs):,} samples", flush=True)
        allrows += [(d,) + r for r in rs]
    for sess in ("day", "night"):
        sub = [r for r in allrows if r[2] == sess]
        if not sub: continue
        print(f"\n=== {sess}  n={len(sub):,} ===")
        for k in (0, 3, 10, 20, 30, 50, 80):
            sell = [r for r in sub if not math.isnan(r[3]) and r[3] <= -k]
            buy = [r for r in sub if not math.isnan(r[3]) and r[3] >= k]
            if len(sell) < 30 or len(buy) < 30:
                print(f"  k={k:<3} 樣本不足 (sell {len(sell)} / buy {len(buy)})"); continue
            fs = sum(1 for r in sell if r[6]) / len(sell)
            fbr = sum(1 for r in buy if r[7]) / len(buy)
            gs = sum(r[4] for r in sell) / len(sell)
            gb = sum(r[5] for r in buy) / len(buy)
            print(f"  k={k:<3} n={len(sell):>5,}/{len(buy):<5,} 成交率 賣{fs:.3f}/買{fbr:.3f}  "
                  f"淨益 賣{gs:+.3f}/買{gb:+.3f}  平均{(gs+gb)/2:+.3f}")
        # block bootstrap on the unconditional average gain
        g = [(r[4] + r[5]) / 2 for r in sub]
        ts = [r[1] for r in sub]
        blk = defaultdict(list)
        for t, v in zip(ts, g):
            blk[int(t // 600)].append(v)          # 10 分鐘不重疊 block
        keys = list(blk)
        random.seed(7)
        means = []
        for _ in range(2000):
            s = [x for kk in random.choices(keys, k=len(keys)) for x in blk[kk]]
            means.append(sum(s) / len(s))
        means.sort()
        pt = sum(g) / len(g)
        print(f"  無條件平均 {pt:+.3f}；10分鐘 block bootstrap 95% CI "
              f"[{means[50]:+.3f}, {means[1949]:+.3f}]  (blocks={len(keys)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
