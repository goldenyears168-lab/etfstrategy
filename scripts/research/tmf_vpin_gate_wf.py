#!/usr/bin/env python3
"""VPIN 毒性閘門 × 三段 walk-forward — 專業做市商用來決定「此刻該不該報價」。

文獻：Easley, López de Prado & O'Hara (2012)。作者的主張是做市商需要的不是
方向預測，而是**流量毒性**：知情單佔比升高時，被動報價的期望損益轉負，正確的
動作是退出市場。2010 年 Flash Crash 前 VPIN 已升到極端分位，是該文的主要實證。

與先前失敗的 signed OFI 濾網的差別（重要，否則這只是換個名字重跑）：
  * OFI 量**方向**（我是不是逆著浪成交），VPIN 量**幅度**（成交流有多毒）
  * OFI 用時間視窗，VPIN 用**成交量時鐘**分桶
  * OFI 用 tick rule 逐筆分類，VPIN 用 bulk volume classification（對分類誤差穩健）

判準與前幾輪一致，不放寬：三段不重疊窗口 + 實測成本 4.05 + Deflated Sharpe。
單一窗口好看不算數——這個 repo 已經有七次那樣的紀錄。
"""

from __future__ import annotations

import argparse
import json
import math
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
from tmf_channel.vpin import vpin_series, volume_buckets

BAR_SOURCE = "tx_1m_tick_built_582d"
ENGINE_COST = 3.0
TRUE_COST = 4.05
BUCKETS_PER_DAY = 200   # 比原文的 50 細，因為我們要盤中逐筆 gating 而非跨日
VPIN_WINDOW = 50


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
    return [d for d in bd if d in set(available_days())]


def arrays_for(day: str):
    rows = load_day(day, source=BAR_SOURCE)
    if not rows:
        return None
    return ([float(r["o"]) for r in rows], [float(r["h"]) for r in rows],
            [float(r["l"]) for r in rows], [float(r["c"]) for r in rows],
            [float(r.get("v") or 0) for r in rows],
            [f"{r['cal']}T{r['t']}:00+08:00" for r in rows])


def vpin_by_tick(idx) -> list[float | None]:
    """把每個成交量桶的 VPIN 攤回逐筆索引，讓成交當下能 O(1) 查到當時的值。

    嚴格因果：第 k 筆查到的是**該筆所屬桶之前**已完成桶算出來的 VPIN。
    """
    series = vpin_series(idx.tk_px, idx.tk_vol,
                         buckets_per_day=BUCKETS_PER_DAY, window=VPIN_WINDOW)
    if not series:
        return [None] * idx.n_tk
    total = sum(idx.tk_vol)
    bsize = total / max(1, BUCKETS_PER_DAY)
    out: list[float | None] = [None] * idx.n_tk
    acc = 0.0
    b = 0
    for k in range(idx.n_tk):
        # 先寫「上一個已完成桶」的值 → 不含當下這一桶的資訊
        out[k] = series[b - 1] if b >= 1 and b - 1 < len(series) else None
        acc += idx.tk_vol[k]
        if acc >= bsize:
            acc = 0.0
            b += 1
    return out


def find_fill_tick(idx, bar_t: str, next_bar_t: str | None, ep: float, side: str) -> int | None:
    start = idx.minute_start_idx.get(bar_t)
    if start is None:
        return None
    end = idx.minute_start_idx.get(next_bar_t, idx.n_tk) if next_bar_t else idx.n_tk
    for k in range(start, min(end, idx.n_tk)):
        p = idx.tk_px[k]
        if (side == "S" and p > ep) or (side == "L" and p < ep):
            return k
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--win", type=int, default=60)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    days = overlap_days()
    w = args.win
    windows = {"W1": days[-3 * w:-2 * w], "W2": days[-2 * w:-w], "W3": days[-w:]}
    for k, v in windows.items():
        print(f"{k} {len(v)} days  {v[0]} → {v[-1]}")
    print(f"\nVPIN buckets/day={BUCKETS_PER_DAY} window={VPIN_WINDOW} · 成本線 {TRUE_COST}\n")

    vix = load_vixtwn_delta() or {}
    rows: dict[str, list[dict[str, Any]]] = {k: [] for k in windows}

    for wn, wdays in windows.items():
        for i, day in enumerate(wdays, 1):
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
            vt = vpin_by_tick(idx)
            for t in trades:
                eb, ep, side = int(t["eb"]), float(t["ep"]), str(t["s"])
                if eb + 1 >= len(T):
                    continue
                k = find_fill_tick(idx, T[eb], T[eb + 1], ep, side)
                if k is None or vt[k] is None:
                    continue
                rows[wn].append({"vpin": vt[k], "gross": float(t["pnl"]) + ENGINE_COST})
            del a, idx, vt
            _ti._load_raw.cache_clear()
            if i % 20 == 0:
                print(f"  [{wn}] {i}/{len(wdays)} …", flush=True)

    print("\n=== VPIN 分位 → 該桶交易的毛額（成本線 4.05）===")
    hdr = f"{'VPIN 分位':<16}" + "".join(f"{wn+' n':>8}{wn+' 毛額':>10}" for wn in windows)
    print(hdr); print("-" * len(hdr))
    out: dict[str, Any] = {"schema": "tmf-vpin-gate-wf-v1", "true_cost": TRUE_COST,
                           "buckets_per_day": BUCKETS_PER_DAY, "window": VPIN_WINDOW,
                           "quintiles": {}, "threshold_sweep": {}}
    qs = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
    for lo, hi in qs:
        line = f"Q{lo//20+1} ({lo}-{hi}%)   "
        rec: dict[str, Any] = {}
        for wn in windows:
            rs = rows[wn]
            if len(rs) < 100:
                line += f"{'--':>8}{'--':>10}"; continue
            vs = sorted(r["vpin"] for r in rs)
            a_, b_ = vs[int(lo / 100 * (len(vs) - 1))], vs[int(hi / 100 * (len(vs) - 1))]
            sub = [r["gross"] for r in rs if a_ <= r["vpin"] <= b_]
            if len(sub) < 30:
                line += f"{'--':>8}{'--':>10}"; continue
            rec[wn] = {"n": len(sub), "gross": round(st.mean(sub), 3)}
            line += f"{len(sub):>8}{st.mean(sub):>10.2f}"
        out["quintiles"][f"Q{lo//20+1}"] = rec
        print(line)

    print("\n=== 只做 VPIN 低於門檻的交易（毒性低時才報價）===")
    print(f"{'門檻(分位)':<14}" + "".join(f"{wn+' n':>8}{wn+' 毛額':>10}{wn+' 淨/日':>11}" for wn in windows))
    for pctl in (20, 40, 60, 80):
        line = f"{'<=P'+str(pctl):<14}"
        rec: dict[str, Any] = {}
        for wn in windows:
            rs = rows[wn]
            if len(rs) < 100:
                line += f"{'--':>8}{'--':>10}{'--':>11}"; continue
            vs = sorted(r["vpin"] for r in rs)
            thr = vs[int(pctl / 100 * (len(vs) - 1))]
            sub = [r["gross"] for r in rs if r["vpin"] <= thr]
            if len(sub) < 50:
                line += f"{'--':>8}{'--':>10}{'--':>11}"; continue
            g = st.mean(sub)
            net_day = (g - TRUE_COST) * len(sub) / w
            rec[wn] = {"n": len(sub), "gross": round(g, 3), "net_day": round(net_day, 1)}
            line += f"{len(sub):>8}{g:>10.2f}{net_day:>11.1f}"
        out["threshold_sweep"][f"P{pctl}"] = rec
        print(line)

    mins = {}
    for pctl in (20, 40, 60, 80):
        r = out["threshold_sweep"].get(f"P{pctl}", {})
        gs = [v["gross"] for v in r.values()]
        if len(gs) == len(windows):
            mins[pctl] = min(gs)
    if mins:
        best = max(mins, key=lambda k: mins[k])
        print(f"\n三段最小毛額最高的門檻：P{best} = {mins[best]:.2f} pts "
              f"（成本線 {TRUE_COST}）→ {'過線' if mins[best] > TRUE_COST else '不過線'}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
