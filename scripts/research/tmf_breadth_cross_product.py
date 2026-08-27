#!/usr/bin/env python3
"""跨商品廣度檢定 — 這個 edge 是 TX 特有的，還是市場結構性的？

Grinold (1989) 主動管理基本定律：IR = IC × √N。本專案過去九次介入全部在提高
IC（單一策略每注的技術），只成功一次；N（**獨立**注數）這個乘性槓桿一次都沒
碰過。用自己的數字：每筆 SR 0.022 × √(29筆/日×250日) = 年化 1.87。要靠 IC 翻倍
很難，但三個彼此不相關、各自 1.87 的策略合起來是 3.24。

這支腳本問兩個問題，而且只有兩個：
  Q1  同一套（已用三段 WF 驗過的）通道邏輯，在別的 TAIFEX 期貨上也有毛額嗎？
      —— 若只有 TX 有，那它是資料探勘的產物；若普遍存在，它是結構性的。
  Q2  各商品的每日損益彼此相關嗎？
      —— Grinold 的 N 要的是**獨立**的注。高度相關的三個策略 N 還是 1。

單位一律換成 NT$：各商品每點價值差 400 倍（TMF NT$10 vs TE NT$4,000），
用點數比較毫無意義。

⚠️ 每點價值與費率是外部事實，請自行核對；成本公式與假設全部列在輸出裡。
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

ENGINE_COST = 3.0
FEE_TWD_PER_SIDE = 15.0      # 假設；請用你的對帳單取代
TAX_RATE = 0.00002           # 股價類期貨契約交易稅（單邊，法定）

#: product -> (bars source, 每點價值 NT$, 逐筆目錄用的 product code)
PRODUCTS: dict[str, tuple[str, float, str]] = {
    "TX":  ("tx_1m_tick_built_582d", 200.0, "TX"),
    "MTX": ("mtx_1m_tick_built",      50.0, "MTX"),
    "TE":  ("te_1m_tick_built",     4000.0, "TE"),
    "TF":  ("tf_1m_tick_built",     1000.0, "TF"),
}


def bars_db() -> Path:
    try:
        import stock_db
        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"


def days_for(source: str) -> list[str]:
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (source,))]
    finally:
        con.close()


def arrays_for(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows or len(rows) < 60:
        return None
    return ([float(r["o"]) for r in rows], [float(r["h"]) for r in rows],
            [float(r["l"]) for r in rows], [float(r["c"]) for r in rows],
            [float(r.get("v") or 0) for r in rows],
            [f"{r['cal']}T{r['t']}:00+08:00" for r in rows])


def scaled(mult: float) -> dict[str, Any]:
    r = deepcopy(PAPER_RECIPE)
    r.update({"hang_anchor": "O", "eod_flatten": True,
              "tick_native": True, "fill_model": "through"})
    for k in ("hang_lo", "hang_hi", "night_hang_lo", "night_hang_hi"):
        if r.get(k):
            r[k] = float(r[k]) * mult
    b = r.get("session_pv_book")
    if isinstance(b, dict):
        for s_ in b.values():
            for c in s_.values():
                for k in ("hang_lo", "hang_hi"):
                    if c.get(k):
                        c[k] = float(c[k]) * mult
    return r


def corr(a: list[float], b: list[float]) -> float | None:
    if len(a) < 10 or len(a) != len(b):
        return None
    try:
        return st.correlation(a, b)
    except (st.StatisticsError, ZeroDivisionError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mult", type=float, default=2.0, help="掛單距離倍率（三段 WF 的最適值）")
    ap.add_argument("--products", nargs="+", default=["TX", "MTX", "TE", "TF"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    vix = load_vixtwn_delta() or {}
    per_day: dict[str, dict[str, float]] = {}   # product -> {day: net_twd}
    summary: dict[str, Any] = {}

    for prod in args.products:
        if prod not in PRODUCTS:
            print(f"  跳過未知商品 {prod}")
            continue
        source, ptval, tickprod = PRODUCTS[prod]
        days = days_for(source)
        have = set(available_days(tickprod))
        days = [d for d in days if d in have]
        if len(days) < 20:
            print(f"{prod:<5} 資料不足（{len(days)} 日），跳過")
            continue
        print(f"{prod:<5} {len(days)} 日  {days[0]} → {days[-1]}  每點 NT${ptval:,.0f}", flush=True)

        net_by_day: dict[str, float] = {}
        n_tot = 0
        gross_pts_tot = 0.0
        cost_twd_tot = 0.0
        idx_levels: list[float] = []
        for i, day in enumerate(days, 1):
            a = arrays_for(day, source)
            if a is None:
                continue
            O, H, L, C, V, T = a
            idx = build_tick_index(T, tickprod)
            if idx is None:
                continue
            trades, *_ = simulate(O, H, L, C, V, T, scaled(args.mult),
                                  vix_delta=vix, tick_index=idx)
            n = len(trades)
            gross_pts = sum(float(t["pnl"]) for t in trades) + n * ENGINE_COST
            lvl = st.mean(C)
            idx_levels.append(lvl)
            # 成本（NT$）：手續費固定每口每邊；交易稅按契約金額
            cost_twd = n * 2 * (FEE_TWD_PER_SIDE + TAX_RATE * lvl * ptval)
            net_by_day[day] = gross_pts * ptval - cost_twd
            n_tot += n
            gross_pts_tot += gross_pts
            cost_twd_tot += cost_twd
            del a, idx
            _ti._load_raw.cache_clear()
            if i % 40 == 0:
                print(f"    {i}/{len(days)}…", flush=True)
        if not net_by_day:
            continue
        per_day[prod] = net_by_day
        nets = list(net_by_day.values())
        lvl = st.mean(idx_levels)
        summary[prod] = {
            "days": len(nets), "trades": n_tot,
            "trades_day": round(n_tot / len(nets), 1),
            "gross_pts_per_trade": round(gross_pts_tot / n_tot, 3) if n_tot else None,
            "gross_twd_per_trade": round(gross_pts_tot * ptval / n_tot, 1) if n_tot else None,
            "cost_twd_per_trade": round(cost_twd_tot / n_tot, 1) if n_tot else None,
            "net_twd_day": round(st.mean(nets), 0),
            "sd_twd_day": round(st.stdev(nets), 0) if len(nets) > 1 else None,
            "ann_sharpe": round(st.mean(nets) / st.stdev(nets) * math.sqrt(250), 2)
            if len(nets) > 1 and st.stdev(nets) > 0 else None,
            "avg_index": round(lvl, 1), "point_value_twd": ptval,
        }

    print(f"\n=== Q1 每個商品都有毛額嗎（掛單距離 ×{args.mult}）===")
    print(f"{'商品':<6}{'日數':>6}{'筆/日':>8}{'毛額(pt)':>11}{'毛額(NT$)':>12}"
          f"{'成本(NT$)':>12}{'淨/筆(NT$)':>13}{'淨/日(NT$)':>13}{'年化SR':>9}")
    for p, s in summary.items():
        netpt = (s["gross_twd_per_trade"] or 0) - (s["cost_twd_per_trade"] or 0)
        print(f"{p:<6}{s['days']:>6}{s['trades_day']:>8.1f}{s['gross_pts_per_trade']:>11.2f}"
              f"{s['gross_twd_per_trade']:>12,.0f}{s['cost_twd_per_trade']:>12,.0f}"
              f"{netpt:>+13,.0f}{s['net_twd_day']:>+13,.0f}{str(s['ann_sharpe']):>9}")

    print("\n=== Q2 每日損益相關性（Grinold 的 N 要的是獨立的注）===")
    prods = [p for p in summary if p in per_day]
    common = set.intersection(*[set(per_day[p]) for p in prods]) if len(prods) > 1 else set()
    print(f"   共同交易日 {len(common)} 天")
    cmat: dict[str, dict[str, float | None]] = {}
    if len(common) >= 20:
        cd = sorted(common)
        print(f"{'':<6}" + "".join(f"{p:>8}" for p in prods))
        for a in prods:
            row = f"{a:<6}"
            cmat[a] = {}
            for b in prods:
                c = corr([per_day[a][d] for d in cd], [per_day[b][d] for d in cd])
                cmat[a][b] = None if c is None else round(c, 3)
                row += f"{c:>8.3f}" if c is not None else f"{'--':>8}"
            print(row)
        # 等權組合
        port = [sum(per_day[p][d] for p in prods) for d in cd]
        ev, sd = st.mean(port), st.stdev(port)
        print(f"\n   等權組合：EV {ev:+,.0f} NT$/日 · SD {sd:,.0f} · "
              f"年化 SR {ev/sd*math.sqrt(250):+.2f}" if sd > 0 else "")
        best = max((summary[p]["ann_sharpe"] or -9) for p in prods)
        print(f"   單一最佳商品年化 SR = {best:+.2f}  →  組合 {'有' if sd>0 and ev/sd*math.sqrt(250)>best else '沒有'}提升")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"schema": "tmf-breadth-cross-product-v1", "mult": args.mult,
             "fee_twd_per_side": FEE_TWD_PER_SIDE, "tax_rate": TAX_RATE,
             "summary": summary, "corr": cmat}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
