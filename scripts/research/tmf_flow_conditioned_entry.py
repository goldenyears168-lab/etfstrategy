#!/usr/bin/env python3
"""秒級 order-flow 條件化進場 — 連續資料能不能把毛額拉過成本線？

判準（由前三輪調查釘死，不是猜的）：
  * 實測毛額 = +1.44 pts/筆（60 日 tick 回放、fill_model=through、4,073 筆）
  * 實測來回成本 = 4.05 pts（手續費 NT$15/邊 3.00 + 期交稅 1.85 − 限價滑價 0.80）
  → **任何優化都必須把毛額拉到 4 pts 以上，否則提高頻率只是虧得更快。**

為什麼用逐筆而不是五檔：真實五檔只有 2026-08-14 起 3.5 個交易日，做不了驗證。
逐筆有 1,010 天。Cont-Kukanov-Stoikov (2014) 的結果是 order flow imbalance
線性解釋價格變動，而成交方向的 tick-rule 分類是盤口 OFI 的標準代理
（Lee-Ready 1991 / Hasbrouck 2007）。概念在逐筆上過關，再用真五檔驗證。

檢定的是 Glosten-Milgrom (1985) 的核心預測：**被動掛單成交在「與成交流同向
的浪」裡時，就是被知情單掃到**——空單被買盤浪掃到後買盤還會繼續。若成立，
按進場當下的流向過濾應該能顯著提高存活交易的毛額。

輸出：把 4,073 筆真實進場依「進場當下的 OFI」分桶，看哪一桶的毛額過得了
4.05 的成本線，以及過線的那一桶還剩幾筆。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from copy import deepcopy
from pathlib import Path
from typing import Any

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel import tick_index as _ti
from tmf_channel.cache_store import load_day
from tmf_channel.engine import load_vixtwn_delta, simulate
from tmf_channel.tick_index import available_days, build_tick_index

BAR_SOURCE = "tx_1m_tick_built_582d"
ENGINE_COST = 3.0        # 引擎內建常數（毛額還原用）
TRUE_COST = 4.05         # 實測來回成本（見 tmf_true_cost.json）
WINDOWS_SEC = (10, 30, 60)


def bars_db() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"


def overlap_days() -> list[str]:
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        bd = [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (BAR_SOURCE,))]
    finally:
        con.close()
    have = set(available_days())
    return [d for d in bd if d in have]


def arrays_for(day: str):
    rows = load_day(day, source=BAR_SOURCE)
    if not rows:
        return None
    return ([float(r["o"]) for r in rows], [float(r["h"]) for r in rows],
            [float(r["l"]) for r in rows], [float(r["c"]) for r in rows],
            [float(r.get("v") or 0) for r in rows],
            [f"{r['cal']}T{r['t']}:00+08:00" for r in rows])


def tick_signs(px: list[float], lo: int, hi: int) -> list[int]:
    """Tick rule (Lee-Ready): uptick=+1, downtick=-1, zero-tick carries."""
    out: list[int] = []
    last = 0
    for k in range(lo, hi):
        if k == 0:
            out.append(0)
            continue
        d = px[k] - px[k - 1]
        if d > 0:
            last = 1
        elif d < 0:
            last = -1
        out.append(last)
    return out


def find_fill_tick(idx, bar_t: str, next_bar_t: str | None, ep: float, side: str) -> int | None:
    """First print inside the entry bar that a rail at ``ep`` would fill on.

    fill_model="through": a SHORT rail at ep needs a print strictly above it,
    a LONG rail strictly below — same rule the replay used to create the trade,
    so this reproduces the actual fill instant rather than approximating it.
    """
    start = idx.minute_start_idx.get(bar_t)
    if start is None:
        return None
    end = idx.minute_start_idx.get(next_bar_t, idx.n_tk) if next_bar_t else idx.n_tk
    for k in range(start, min(end, idx.n_tk)):
        p = idx.tk_px[k]
        if (side == "S" and p > ep) or (side == "L" and p < ep):
            return k
    return None


def flow_features(idx, k: int, win_sec: int) -> dict[str, float] | None:
    """Causal features from prints STRICTLY BEFORE the fill tick k."""
    if k <= 0:
        return None
    t_end = idx.tk_sec[k]
    lo = k - 1
    while lo > 0 and (t_end - idx.tk_sec[lo]) < win_sec and (k - lo) < 5000:
        lo -= 1
    if k - lo < 3:
        return None
    signs = tick_signs(idx.tk_px, lo, k)
    vols = idx.tk_vol[lo:k]
    sv = sum(s * v for s, v in zip(signs, vols))
    tv = sum(vols)
    if tv <= 0:
        return None
    pxs = idx.tk_px[lo:k]
    rng = max(pxs) - min(pxs)
    return {"ofi": sv / tv, "n_prints": float(k - lo), "vol": tv, "range": rng}


def bucket_report(rows: list[dict[str, Any]], key: str, edges: list[float],
                  labels: list[str]) -> list[dict[str, Any]]:
    out = []
    for i, lab in enumerate(labels):
        lo, hi = edges[i], edges[i + 1]
        sub = [r for r in rows if lo <= r[key] < hi]
        if len(sub) < 25:
            continue
        g = [r["gross"] for r in sub]
        se = st.stdev(g) / (len(g) ** 0.5) if len(g) > 1 else float("nan")
        out.append({"bucket": lab, "n": len(sub), "share_pct": round(100.0 * len(sub) / len(rows), 1),
                    "gross": round(st.mean(g), 3), "se": round(se, 3),
                    "t": round(st.mean(g) / se, 2) if se else None,
                    "net_vs_true_cost": round(st.mean(g) - TRUE_COST, 3)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--skip", type=int, default=0,
                    help="跳過最近 N 天（用來切出不重疊的 FIT/HOLDOUT/RECENT 窗口）")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    _all = overlap_days()
    days = _all[-(args.days + args.skip):-args.skip] if args.skip else _all[-args.days:]
    if args.label:
        print(f"### {args.label}  {days[0]} → {days[-1]}")
    vix = load_vixtwn_delta() or {}
    rows: list[dict[str, Any]] = []
    n_trades = 0

    for day in days:
        a = arrays_for(day)
        if a is None:
            continue
        O, H, L, C, V, T = a
        idx = build_tick_index(T)
        if idx is None:
            continue
        r = deepcopy(PAPER_RECIPE)
        r.update({"hang_anchor": "O", "eod_flatten": True,
                  "tick_native": True, "fill_model": "through"})
        trades, *_ = simulate(O, H, L, C, V, T, r, vix_delta=vix, tick_index=idx)
        n_trades += len(trades)
        for t in trades:
            eb, ep, side = int(t["eb"]), float(t["ep"]), str(t["s"])
            if eb + 1 >= len(T):
                continue
            k = find_fill_tick(idx, T[eb], T[eb + 1], ep, side)
            if k is None:
                continue
            rec: dict[str, Any] = {"day": day, "side": side,
                                   "gross": float(t["pnl"]) + ENGINE_COST,
                                   "hold": int(t["hold"])}
            ok = True
            for w in WINDOWS_SEC:
                f = flow_features(idx, k, w)
                if f is None:
                    ok = False
                    break
                # 以進場方向為準：+1 = 成交流與我同向（我做空、賣壓大）
                #                  −1 = 我逆著浪成交（我做空、買盤正在推）
                signed = f["ofi"] * (-1.0 if side == "S" else 1.0)
                rec[f"ofi{w}"] = signed
                rec[f"prints{w}"] = f["n_prints"]
                rec[f"range{w}"] = f["range"]
            rec["has_flow"] = ok
            rows.append(rec)
        _ti._load_raw.cache_clear()

    withf = [r for r in rows if r.get("has_flow")]
    nof = [r for r in rows if not r.get("has_flow")]
    print(f"days={len(days)} trades={n_trades} 納入={len(rows)} "
          f"(有流向特徵 {len(withf)} / 太安靜無特徵 {len(nof)})")
    if nof:
        g=st.mean([r["gross"] for r in nof])
        print(f"  ⚠ 「太安靜」那一桶 n={len(nof)} 毛額={g:+.3f} pts "
              f"net vs {TRUE_COST}={g-TRUE_COST:+.3f}  ← 先前版本把這桶靜默丟掉了")
    all_rows = rows
    rows = withf
    if len(rows) < 200:
        print("樣本不足")
        return 1

    base = st.mean([r["gross"] for r in rows])
    print(f"基準：全部進場的毛額 = {base:+.3f} pts/筆   成本線 = {TRUE_COST:.2f} pts")
    print("（gross 已還原 = pnl + 引擎 COST 3.0；net 欄為 gross − 實測成本 4.05）\n")

    out: dict[str, Any] = {"schema": "tmf-flow-conditioned-entry-v1",
                           "days": len(days), "n": len(rows),
                           "baseline_gross": round(base, 3),
                           "true_cost": TRUE_COST, "buckets": {}}

    edges = [-1.01, -0.5, -0.2, 0.2, 0.5, 1.01]
    labels = ["強逆浪 <-0.5", "逆浪 -0.5~-0.2", "中性 -0.2~0.2",
              "順浪 0.2~0.5", "強順浪 >0.5"]
    for w in WINDOWS_SEC:
        print(f"=== 進場前 {w} 秒的 signed OFI 分桶"
              f"（+ = 成交流與我同向 / − = 我逆著浪被掃到）===")
        print(f"   {'bucket':<18}{'n':>6}{'占比%':>8}{'毛額':>9}{'(t)':>8}{'net vs 4.05':>13}")
        b = bucket_report(rows, f"ofi{w}", edges, labels)
        for x in b:
            flag = "  ← 過線" if x["net_vs_true_cost"] > 0 else ""
            print(f"   {x['bucket']:<18}{x['n']:>6}{x['share_pct']:>8}{x['gross']:>+9.3f}"
                  f"{str(x['t']):>8}{x['net_vs_true_cost']:>+13.3f}{flag}")
        out["buckets"][f"ofi{w}"] = b
        print()

    # 活動量分桶（進場當下市場多熱）
    p = sorted(r["prints60"] for r in rows)
    q = [0.0, p[len(p)//4], p[len(p)//2], p[3*len(p)//4], 1e18]
    print("=== 進場前 60 秒成交筆數（活動量）分桶 ===")
    print(f"   {'bucket':<18}{'n':>6}{'占比%':>8}{'毛額':>9}{'(t)':>8}{'net vs 4.05':>13}")
    b = bucket_report(rows, "prints60", q, ["Q1 最冷", "Q2", "Q3", "Q4 最熱"])
    for x in b:
        flag = "  ← 過線" if x["net_vs_true_cost"] > 0 else ""
        print(f"   {x['bucket']:<18}{x['n']:>6}{x['share_pct']:>8}{x['gross']:>+9.3f}"
              f"{str(x['t']):>8}{x['net_vs_true_cost']:>+13.3f}{flag}")
    out["buckets"]["prints60"] = b

    # 最樂觀的組合：取單一最佳 OFI 門檻，看還剩多少筆、日均多少
    print("\n=== 只保留「逆浪夠強」的進場（越負＝被掃進去時對手方壓力越大）===")
    print("   逆浪強 = 大額暫時性失衡 → Grossman-Miller：吸收暫時性失衡才有報酬")
    print(f"   {'門檻(ofi<=)':<14}{'window':<9}{'保留n':>7}{'占比%':>8}{'毛額':>9}{'(t)':>7}{'net':>9}{'日均net':>10}")
    for w in WINDOWS_SEC:
        for thr in (-0.7, -0.6, -0.5, -0.4, -0.3):
            sub = [r for r in rows if r[f"ofi{w}"] <= thr]
            if len(sub) < 60:
                continue
            g = [r["gross"] for r in sub]
            m = st.mean(g); se = st.stdev(g)/(len(g)**0.5)
            net = m - TRUE_COST
            flag = "  ← 過線" if net > 0 else ""
            print(f"   {thr:<14.2f}{str(w)+'s':<9}{len(sub):>7}{100.0*len(sub)/len(rows):>8.1f}"
                  f"{m:>+9.3f}{m/se:>7.2f}{net:>+9.3f}{net*len(sub)/len(days):>10.1f}{flag}")
            out.setdefault("threshold_sweep", []).append(
                {"window": w, "thr": thr, "n": len(sub), "gross": round(m, 3),
                 "t": round(m/se, 2), "net": round(net, 3),
                 "daily_net_pts": round(net * len(sub) / len(days), 1)})

    print("\n=== 組合：安靜盤 AND 強逆浪（兩個方向都指向同一批交易嗎）===")
    med_p = sorted(r["prints60"] for r in rows)[len(rows)//2]
    print(f"   {'組合':<28}{'n':>7}{'毛額':>9}{'(t)':>7}{'net':>9}{'日均net':>10}")
    combos = [
        ("安靜(prints60<中位) 全部", lambda r: r["prints60"] < med_p),
        ("安靜 + ofi60<=-0.4", lambda r: r["prints60"] < med_p and r["ofi60"] <= -0.4),
        ("安靜 + ofi60<=-0.6", lambda r: r["prints60"] < med_p and r["ofi60"] <= -0.6),
        ("全部含無特徵的安靜桶", None),
    ]
    for lab, fn in combos:
        sub = all_rows if fn is None else [r for r in rows if fn(r)]
        if len(sub) < 40:
            continue
        g = [r["gross"] for r in sub]
        m = st.mean(g); se = st.stdev(g)/(len(g)**0.5)
        net = m - TRUE_COST
        flag = "  ← 過線" if net > 0 else ""
        print(f"   {lab:<28}{len(sub):>7}{m:>+9.3f}{m/se:>7.2f}{net:>+9.3f}"
              f"{net*len(sub)/len(days):>10.1f}{flag}")
        out.setdefault("combos", []).append(
            {"label": lab, "n": len(sub), "gross": round(m, 3), "t": round(m/se, 2),
             "net": round(net, 3), "daily_net_pts": round(net * len(sub) / len(days), 1)})

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
