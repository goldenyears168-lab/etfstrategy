#!/usr/bin/env python3
"""台指盤中「是否真的會回歸」的第一原理檢定 — 通道策略的前提測試。

通道逆勢策略的全部假設可以壓成一句話：**價格離開錨點 X 點之後，回頭的機率
大於續行**。這件事從來沒有在這個 repo 裡被獨立檢定過——所有驗證都是「跑策略、
看損益」，那是把假設、參數、成交模型、成本混在一起量。如果前提本身不成立，
16 格參數怎麼調都沒有意義。

方法（對應文獻）：
  M8   Variance Ratio VR(q)              Lo & MacKinlay (1988)
       VR<1 → 均值回歸；VR≈1 → 隨機漫步；VR>1 → 趨勢／動能
  M9   報酬自我相關 ACF(1..30 min)        Campbell-Lo-MacKinlay (1997) ch.2
  M10  OU 半衰期（AR(1) 迴歸）             Vasicek(1977) / Avellaneda-Lee(2010)
       Avellaneda-Lee 的關鍵操作：半衰期太長的標的**直接剔除**，不交易
  M11  條件續行：移動 X 點後的後續報酬      Bouchaud-Farmer-Lillo：order flow 長記憶
  M12  首次通過：碰到 +X 點後，先回錨點還是先再走 X 點
  M13  日內波動季節性 vs 固定掛單距離        Andersen-Bollerslev (1997) U 型
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any

BAR_SOURCE = "tx_1m_tick_built_582d"


def bars_db() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"


def load_sessions(start: str, end: str) -> list[dict[str, Any]]:
    """One entry per (day, session) with its own close series — never splice
    across a session break, a 15:00 or 08:45 gap is not a 1-minute return."""
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        rows = list(
            con.execute(
                "SELECT day, t, c, h, l, v, sess FROM bars WHERE source=? AND day BETWEEN ? AND ? "
                "ORDER BY day, t",
                (BAR_SOURCE, start, end),
            )
        )
    finally:
        con.close()
    grouped: dict[tuple[str, str], dict[str, list]] = defaultdict(
        lambda: {"c": [], "h": [], "l": [], "v": [], "hm": []}
    )
    for day, t, c, h, low, v, sess in rows:
        g = grouped[(day, sess)]
        g["c"].append(float(c))
        g["h"].append(float(h))
        g["l"].append(float(low))
        g["v"].append(float(v or 0))
        g["hm"].append(str(t))
    return [
        {"day": d, "sess": s, **g} for (d, s), g in sorted(grouped.items()) if len(g["c"]) >= 60
    ]


def variance_ratio(rets: list[float], q: int) -> float | None:
    """VR(q) = Var(q-period return)/(q · Var(1-period return)) — overlapping."""
    n = len(rets)
    if n < 5 * q:
        return None
    mu = st.mean(rets)
    var1 = sum((r - mu) ** 2 for r in rets) / (n - 1)
    if var1 <= 0:
        return None
    cum = [0.0]
    for r in rets:
        cum.append(cum[-1] + r)
    qrets = [cum[i + q] - cum[i] for i in range(n - q + 1)]
    muq = st.mean(qrets)
    varq = sum((r - muq) ** 2 for r in qrets) / (len(qrets) - 1)
    return varq / (q * var1)


def acf(xs: list[float], lag: int) -> float | None:
    n = len(xs)
    if n <= lag + 5:
        return None
    mu = st.mean(xs)
    den = sum((x - mu) ** 2 for x in xs)
    if den <= 0:
        return None
    num = sum((xs[i] - mu) * (xs[i + lag] - mu) for i in range(n - lag))
    return num / den


def ou_half_life(series: list[float]) -> float | None:
    """AR(1) on levels: Δp_t = a + b·p_{t-1}; half-life = -ln2/ln(1+b)."""
    n = len(series)
    if n < 60:
        return None
    x = series[:-1]
    y = [series[i + 1] - series[i] for i in range(n - 1)]
    mx, my = st.mean(x), st.mean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return None
    b = sum((x[i] - mx) * (y[i] - my) for i in range(len(x))) / sxx
    if b >= 0 or (1 + b) <= 0:
        return None  # not mean-reverting
    return -math.log(2) / math.log(1 + b)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-02-01")
    ap.add_argument("--end", default="2026-08-07")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sessions = load_sessions(args.start, args.end)
    if not sessions:
        print("no sessions")
        return 1
    days = sorted({s["day"] for s in sessions})
    print(f"sessions={len(sessions)} days={len(days)} {days[0]} → {days[-1]}\n")

    out: dict[str, Any] = {"schema": "tmf-mean-reversion-diag-v1",
                           "range": [days[0], days[-1]], "n_sessions": len(sessions)}

    # ---------- M8 Variance Ratio ----------
    print("=== M8 Variance Ratio（Lo-MacKinlay）· <1 回歸 / =1 隨機漫步 / >1 趨勢 ===")
    print(f"{'session':<10}{'n':>6}" + "".join(f"{'VR('+str(q)+'m)':>11}" for q in (2, 5, 10, 30, 60)))
    vr_out: dict[str, Any] = {}
    for label in ("day", "night", "ALL"):
        subs = [s for s in sessions if label == "ALL" or s["sess"] == label]
        if not subs:
            continue
        row = {}
        line = f"{label:<10}{len(subs):>6}"
        for q in (2, 5, 10, 30, 60):
            vals = []
            for s in subs:
                rets = [s["c"][i + 1] - s["c"][i] for i in range(len(s["c"]) - 1)]
                v = variance_ratio(rets, q)
                if v is not None:
                    vals.append(v)
            m = round(st.mean(vals), 4) if vals else None
            row[f"q{q}"] = m
            line += f"{str(m):>11}"
        vr_out[label] = row
        print(line)
    out["M8_variance_ratio"] = vr_out

    # ---------- M9 ACF ----------
    print("\n=== M9 1 分報酬自我相關 ACF ===")
    acf_out = {}
    line = f"{'lag(min)':<10}"
    vals_line = f"{'ACF':<10}"
    for lag in (1, 2, 3, 5, 10, 20, 30):
        vs = []
        for s in sessions:
            rets = [s["c"][i + 1] - s["c"][i] for i in range(len(s["c"]) - 1)]
            a = acf(rets, lag)
            if a is not None:
                vs.append(a)
        m = round(st.mean(vs), 4) if vs else None
        acf_out[f"lag{lag}"] = m
        line += f"{lag:>10}"
        vals_line += f"{str(m):>10}"
    print(line)
    print(vals_line)
    out["M9_acf"] = acf_out

    # ---------- M10 OU half-life ----------
    print("\n=== M10 OU 半衰期（每個 session 各自擬合）===")
    hl_out = {}
    for label in ("day", "night"):
        hls = [h for s in sessions if s["sess"] == label
               for h in [ou_half_life(s["c"])] if h is not None and h < 10_000]
        subs = [s for s in sessions if s["sess"] == label]
        share = 100.0 * len(hls) / len(subs) if subs else 0.0
        if hls:
            srt = sorted(hls)
            hl_out[label] = {"n_meanreverting": len(hls), "n_sessions": len(subs),
                             "share_pct": round(share, 1),
                             "p50_min": round(srt[len(srt) // 2], 1),
                             "p90_min": round(srt[int(.9 * (len(srt) - 1))], 1)}
            print(f"  {label:<6} 可測到均值回歸的 session: {len(hls)}/{len(subs)} ({share:.1f}%)"
                  f" · 半衰期中位數 {hl_out[label]['p50_min']:.0f} 分 · p90 {hl_out[label]['p90_min']:.0f} 分")
    out["M10_ou_half_life"] = hl_out

    # ---------- M11 條件續行 ----------
    print("\n=== M11 條件續行：過去 N 分移動 X 點後，未來 N 分的平均報酬（同向為正）===")
    print(f"{'lookback':<10}{'move≥':>8}{'n':>8}{'E[fwd|up]':>12}{'E[fwd|dn]':>12}{'續行率%':>10}")
    cont_out = {}
    for look in (5, 15):
        for thr in (10.0, 20.0, 40.0):
            fwd_up, fwd_dn, cont = [], [], 0
            for s in sessions:
                c = s["c"]
                for i in range(look, len(c) - look):
                    mv = c[i] - c[i - look]
                    if abs(mv) < thr:
                        continue
                    f = c[i + look] - c[i]
                    (fwd_up if mv > 0 else fwd_dn).append(f)
                    if (f > 0) == (mv > 0) and f != 0:
                        cont += 1
            n = len(fwd_up) + len(fwd_dn)
            if n < 100:
                continue
            row = {
                "n": n,
                "E_fwd_after_up": round(st.mean(fwd_up), 3) if fwd_up else None,
                "E_fwd_after_dn": round(st.mean(fwd_dn), 3) if fwd_dn else None,
                "continuation_pct": round(100.0 * cont / n, 1),
            }
            cont_out[f"look{look}_thr{int(thr)}"] = row
            print(f"{look:<10}{thr:>8.0f}{n:>8}{str(row['E_fwd_after_up']):>12}"
                  f"{str(row['E_fwd_after_dn']):>12}{row['continuation_pct']:>10}")
    out["M11_conditional_continuation"] = cont_out

    # ---------- M12 首次通過 ----------
    print("\n=== M12 首次通過（對稱、無偏）：成交在掛單價後，先賺 X 點 vs 先賠 X 點 ===")
    print("   起點＝掛單價本身（limit order 就是成交在那個價），不是穿越後那根 K 的收盤價；")
    print("   用 H/L 判斷觸及。null＝隨機漫步時應為 50/50。")
    print(f"{'X(pts)':<10}{'n':>8}{'先回歸(賺)%':>14}{'先續行(賠)%':>14}{'皆未到%':>10}{'z':>8}")
    fp_out = {}
    for X in (10.0, 15.0, 25.0, 30.0, 42.0):
        revert = extend = neither = 0
        for s in sessions:
            c, hi, lo = s["c"], s["h"], s["l"]
            i = 0
            n_bars = len(c)
            while i < n_bars - 1:
                anchor = c[i]
                j, side = i + 1, 0
                while j < n_bars:
                    if hi[j] >= anchor + X:
                        side = 1  # rail above → we are filled SHORT at anchor+X
                        break
                    if lo[j] <= anchor - X:
                        side = -1  # rail below → filled LONG at anchor-X
                        break
                    j += 1
                if not side:
                    break
                rail = anchor + X if side > 0 else anchor - X
                # symmetric ±X from the FILL price, so neither leg is favoured
                tp = rail - X if side > 0 else rail + X
                sl = rail + X if side > 0 else rail - X
                k, res = j + 1, "neither"
                while k < n_bars:
                    if side > 0:
                        hit_tp, hit_sl = lo[k] <= tp, hi[k] >= sl
                    else:
                        hit_tp, hit_sl = hi[k] >= tp, lo[k] <= sl
                    if hit_tp and hit_sl:
                        res = "neither"  # same bar → undecidable at 1m, drop it
                        break
                    if hit_tp:
                        res = "revert"
                        break
                    if hit_sl:
                        res = "extend"
                        break
                    k += 1
                if res == "revert":
                    revert += 1
                elif res == "extend":
                    extend += 1
                else:
                    neither += 1
                i = j
        decided = revert + extend
        n = decided + neither
        if decided < 30:
            continue
        p = revert / decided
        z = (p - 0.5) * math.sqrt(decided) / 0.5
        row = {"n": n, "decided": decided,
               "revert_pct": round(100.0 * revert / n, 1),
               "extend_pct": round(100.0 * extend / n, 1),
               "neither_pct": round(100.0 * neither / n, 1),
               "revert_share_of_decided_pct": round(100.0 * p, 1),
               "z_vs_coinflip": round(z, 2)}
        fp_out[f"X{int(X)}"] = row
        print(f"{X:<10.0f}{n:>8}{row['revert_pct']:>14}{row['extend_pct']:>14}"
              f"{row['neither_pct']:>10}{row['z_vs_coinflip']:>8}")
    out["M12_first_passage"] = fp_out

    # ---------- M13 日內波動季節性 ----------
    print("\n=== M13 日內波動季節性（每 30 分桶的 1 分絕對報酬中位數）===")
    buckets: dict[str, list[float]] = defaultdict(list)
    for s in sessions:
        for i in range(len(s["c"]) - 1):
            hm = s["hm"][i]
            key = f"{hm[:2]}:{'00' if hm[3:5] < '30' else '30'}"
            buckets[key].append(abs(s["c"][i + 1] - s["c"][i]))
    seas = {}
    for key in sorted(buckets):
        xs = buckets[key]
        if len(xs) < 200:
            continue
        seas[key] = round(st.median(xs), 2)
    med_all = st.median([v for v in seas.values()])
    print("   bucket  med|1m ret|   相對全日中位數")
    for k, v in seas.items():
        bar = "█" * int(round(20 * v / max(seas.values())))
        print(f"   {k}    {v:>6.2f}     {v/med_all:>5.2f}×  {bar}")
    out["M13_intraday_vol_seasonality"] = seas

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
