#!/usr/bin/env python3
"""掛單距離 × 真實成本的三段 walk-forward — 唯一同時攻擊勝率與週轉率的參數。

為什麼是這個參數
----------------
前幾輪把能攻擊的都試過了，全部樣本外失效（有界停損／關 struct_break／換錨點／
秒級 OFI 濾網，DSR 全部 ≈0）。它們的共同點是**都在調出場或加濾網**——而出場
不改變筆數，濾網砍掉的筆數連同它們的毛額一起消失。

筆數由**進場頻率**決定，而進場頻率由掛單距離決定。同時：
  * M12（244 個 session、無偏對稱首次通過）：X=10 的回歸率 43.2%，X=42 是
    48.5%——**掛得越遠，賭注越不糟**，單調。
  * 掛得越遠 → 成交次數越少 → 過路費越少。
兩個效果同向。而現行 cell book 的 hang_lo 密集落在 10–20，正是最差的那一段。

為什麼過去掃過卻沒發現
----------------------
歷史上的 cell tune 確實掃過 hang_lo/hi，但都是在 (a) 1 分 K 成交假設（實測
高估 55%）與 (b) COST=3.0（實測 4.05，且未含市價出場滑價）之下。在錯的成交
模型與低估的成本下，最適解會系統性偏向「掛近、多做」。這裡兩者都換成實測值
重掃。

判準
----
用**真實成本 4.05** 計算淨值，而不是引擎內建的 3.0；並且要求三段不重疊窗口
方向一致 + Deflated Sharpe（Bailey & López de Prado）校正試驗次數。
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

BAR_SOURCE = "tx_1m_tick_built_582d"
ENGINE_COST = 3.0     # 引擎內建，用來把 pnl 還原成毛額
TRUE_COST = 4.05      # 實測（tmf_true_cost.json）：手續費3.00+稅1.85−限價滑價0.80
EULER = 0.5772156649015329
#: 對整本 16 格 cell book 的 hang_lo/hi 同乘一個倍率——刻意只動一個自由度，
#: 而不是重新逐格調參（那正是製造出 DSR≈0 的做法）
MULTIPLIERS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


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


def scaled_recipe(mult: float) -> dict[str, Any]:
    r = deepcopy(PAPER_RECIPE)
    r.update({"hang_anchor": "O", "eod_flatten": True,
              "tick_native": True, "fill_model": "through"})
    for key in ("hang_lo", "hang_hi", "night_hang_lo", "night_hang_hi"):
        if r.get(key):
            r[key] = float(r[key]) * mult
    book = r.get("session_pv_book")
    if isinstance(book, dict):
        for sess in book.values():
            for cell in sess.values():
                for key in ("hang_lo", "hang_hi"):
                    if cell.get(key):
                        cell[key] = float(cell[key]) * mult
    return r


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def deflated_sharpe(xs: list[float], trial_srs: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    m, s = st.mean(xs), st.stdev(xs)
    if s <= 0:
        return None
    sr = m / s
    z3 = [(x - m) / s for x in xs]
    g3 = sum(v ** 3 for v in z3) / n
    g4 = sum(v ** 4 for v in z3) / n
    N = max(2, len(trial_srs))
    var_sr = st.variance(trial_srs) if len(trial_srs) > 1 else 0.0
    sr0 = math.sqrt(var_sr) * ((1 - EULER) * norm_ppf(1 - 1.0 / N)
                               + EULER * norm_ppf(1 - 1.0 / (N * math.e)))
    den = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr ** 2
    if den <= 0:
        return None
    return norm_cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--win", type=int, default=60, help="每個窗口天數")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    days = overlap_days()
    w = args.win
    windows = {"W1": days[-3 * w:-2 * w], "W2": days[-2 * w:-w], "W3": days[-w:]}
    for k, v in windows.items():
        print(f"{k} {len(v)} days  {v[0]} → {v[-1]}")
    print(f"\n倍率 {MULTIPLIERS} · fill_model=through · 淨值用實測成本 {TRUE_COST}\n")

    vix = load_vixtwn_delta() or {}
    daily: dict[str, dict[float, dict[str, list]]] = {
        wn: {m: {"net": [], "gross": [], "n": []} for m in MULTIPLIERS} for wn in windows}

    for wn, wdays in windows.items():
        for i, day in enumerate(wdays, 1):
            a = arrays_for(day)
            if a is None:
                continue
            O, H, L, C, V, T = a
            idx = build_tick_index(T)
            if idx is None:
                continue
            for m in MULTIPLIERS:
                trades, *_ = simulate(O, H, L, C, V, T, scaled_recipe(m),
                                      vix_delta=vix, tick_index=idx)
                n = len(trades)
                gross = sum(float(t["pnl"]) for t in trades) + n * ENGINE_COST
                daily[wn][m]["gross"].append(gross)
                daily[wn][m]["n"].append(n)
                daily[wn][m]["net"].append(gross - n * TRUE_COST)
            del a, idx
            _ti._load_raw.cache_clear()
            if i % 20 == 0:
                print(f"  [{wn}] {i}/{len(wdays)} …", flush=True)

    print("\n=== 三段結果（pts/day，淨值已用實測成本 4.05）===")
    hdr = f"{'倍率':<8}" + "".join(f"{wn+' 筆/日':>11}{wn+' 毛額':>10}{wn+' 淨值':>11}" for wn in windows)
    print(hdr); print("-" * len(hdr))
    out: dict[str, Any] = {"schema": "tmf-rail-width-cost-wf-v1", "true_cost": TRUE_COST,
                           "windows": {k: [v[0], v[-1], len(v)] for k, v in windows.items()},
                           "results": {}}
    for m in MULTIPLIERS:
        line = f"{m:<8.2f}"
        row: dict[str, Any] = {}
        for wn in windows:
            d = daily[wn][m]
            if not d["net"]:
                continue
            row[wn] = {"trades_day": round(st.mean(d["n"]), 1),
                       "gross_day": round(st.mean(d["gross"]), 1),
                       "net_day": round(st.mean(d["net"]), 1),
                       "gross_per_trade": round(sum(d["gross"]) / max(sum(d["n"]), 1), 3)}
            line += f"{row[wn]['trades_day']:>11.1f}{row[wn]['gross_day']:>10.1f}{row[wn]['net_day']:>11.1f}"
        out["results"][str(m)] = row
        print(line)

    print("\n=== 每筆毛額 vs 成本線 4.05 ===")
    print(f"{'倍率':<8}" + "".join(f"{wn:>12}" for wn in windows) + f"{'三段最小':>12}")
    for m in MULTIPLIERS:
        vals = [out["results"][str(m)][wn]["gross_per_trade"] for wn in windows
                if wn in out["results"][str(m)]]
        flag = "  ← 三段全過線" if vals and min(vals) > TRUE_COST else ""
        print(f"{m:<8.2f}" + "".join(f"{v:>12.2f}" for v in vals) + f"{min(vals):>12.2f}{flag}")

    print("\n=== Deflated Sharpe（N=%d 次試驗）===" % len(MULTIPLIERS))
    print(f"{'倍率':<8}" + "".join(f"{wn+' DSR':>12}" for wn in windows))
    for wn in windows:
        srs = []
        for m in MULTIPLIERS:
            xs = daily[wn][m]["net"]
            srs.append(st.mean(xs) / st.stdev(xs) if len(xs) > 1 and st.stdev(xs) > 0 else 0.0)
        for m in MULTIPLIERS:
            out["results"][str(m)].setdefault("dsr", {})[wn] = deflated_sharpe(daily[wn][m]["net"], srs)
    for m in MULTIPLIERS:
        print(f"{m:<8.2f}" + "".join(
            f"{str(round(out['results'][str(m)]['dsr'][wn], 4) if out['results'][str(m)]['dsr'][wn] is not None else None):>12}"
            for wn in windows))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
