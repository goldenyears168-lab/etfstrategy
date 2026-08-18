#!/usr/bin/env python3
"""錨點選擇診斷 — 「離開什麼東西 X 點」才是可交易的均值回歸？

M8/M10 已經證明台指盤中**確實**有均值回歸：VR(30m)=0.81（日盤）、
VR(60m)=0.85（夜盤），OU 半衰期日盤中位數 29 分、夜盤 96 分。
M12 卻證明策略實際在做的那個賭注是**輸的**：掛單價成交後，先賺 X 點的機率
只有 43–48%（X 越近越差），z 值 −36 到 −4.7。

兩件事都成立，唯一能同時解釋的假說是：**均值回歸存在於「相對於一個慢速中心」
的偏離上，而策略量的是「相對於 1 分鐘前的開盤價」的偏離**。後者幾乎就是隨機
漫步的增量本身——`hang_anchor="O"` 把錨點每分鐘重設一次，於是策略永遠在對
最高頻、資訊量最低的成分逆勢。

這正是 Avellaneda-Lee (2010) 統計套利框架的核心操作：訊號是
s-score = (價格 − 慢速均值)/殘差標準差，而且**半衰期太長的標的直接不交易**。
Bollinger 本人也反覆強調 band 的觸及本身不是訊號（"tags of the bands are not
in and of themselves signals"）——需要獨立指標確認偏離的性質。

本腳本對同一組資料、同一個 ±X 對稱首次通過檢定，只換錨點定義：
  open_1m   O[t]        ← live 現用（hang_anchor="O"）
  ema15 / sma30 / sma60 / vwap_session
並額外加上 Avellaneda-Lee 式的 s-score 濾網。
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
COST_PTS = 3.0  # 引擎現用常數；M1 實測 TMF 中位價差恰好也是 3.0 pts


def bars_db() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"


def load_sessions(start: str, end: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        rows = list(con.execute(
            "SELECT day, t, o, h, l, c, v, sess FROM bars WHERE source=? AND day BETWEEN ? AND ? "
            "ORDER BY day, t", (BAR_SOURCE, start, end)))
    finally:
        con.close()
    g: dict[tuple[str, str], dict[str, list]] = defaultdict(
        lambda: {"o": [], "h": [], "l": [], "c": [], "v": [], "hm": []})
    for day, t, o, h, low, c, v, sess in rows:
        d = g[(day, sess)]
        d["o"].append(float(o)); d["h"].append(float(h)); d["l"].append(float(low))
        d["c"].append(float(c)); d["v"].append(float(v or 0)); d["hm"].append(str(t))
    return [{"day": k[0], "sess": k[1], **v} for k, v in sorted(g.items()) if len(v["c"]) >= 120]


def anchors(s: dict[str, Any], kind: str) -> list[float]:
    o, c, v = s["o"], s["c"], s["v"]
    n = len(c)
    if kind == "open_1m":
        return list(o)
    if kind.startswith("sma"):
        w = int(kind[3:])
        out, run = [], 0.0
        for i in range(n):
            run += c[i]
            if i >= w:
                run -= c[i - w]
            out.append(run / min(i + 1, w))
        return out
    if kind.startswith("ema"):
        span = int(kind[3:])
        a = 2.0 / (span + 1.0)
        out, e = [], None
        for i in range(n):
            e = c[i] if e is None else e + a * (c[i] - e)
            out.append(e)
        return out
    if kind == "vwap_session":
        out, pv, vv = [], 0.0, 0.0
        for i in range(n):
            pv += c[i] * max(v[i], 1.0); vv += max(v[i], 1.0)
            out.append(pv / vv)
        return out
    raise ValueError(kind)


def resid_sigma(c: list[float], anc: list[float], w: int = 60) -> list[float]:
    """Rolling stdev of (price − anchor) — the denominator of the s-score."""
    out, buf = [], []
    for i in range(len(c)):
        buf.append(c[i] - anc[i])
        if len(buf) > w:
            buf.pop(0)
        out.append(st.pstdev(buf) if len(buf) >= 20 and st.pstdev(buf) > 0 else float("nan"))
    return out


def race(s: dict[str, Any], anc: list[float], X: float, *,
         min_abs_s: float = 0.0, sig: list[float] | None = None) -> dict[str, Any]:
    """Symmetric ±X first-passage from the rail price, anchored on ``anc``.

    Event fires when |price − anchor| crosses X from below (H/L based), which
    is exactly when a resting rail at anchor±X would be touched. Then race the
    take-profit (back toward the anchor by X) against the stop (a further X
    away). Non-overlapping: the next scan resumes after resolution.
    """
    h, low, c = s["h"], s["l"], s["c"]
    n = len(c)
    rev = ext = neither = 0
    holds: list[int] = []
    i = 1
    while i < n - 1:
        a = anc[i]
        # CROSSING FROM INSIDE, not "currently outside". Without this the test
        # is meaningless for slow anchors: after a 200-pt trend away from an
        # sma60, every subsequent bar still satisfies |price−anchor| ≥ X, so an
        # event would fire with a "fill" 175 pts below the live price and both
        # ±X targets already behind it — an instant, guaranteed "extend". That
        # artifact is what made the first run of this script report 5–16%
        # revert rates for the slow anchors (vs 43% for open_1m, whose anchor
        # is by construction always within a bar of the price).
        prev_inside = (h[i - 1] < anc[i - 1] + X) and (low[i - 1] > anc[i - 1] - X)
        if not prev_inside:
            i += 1
            continue
        side = 0
        if h[i] >= a + X:
            side = 1
        elif low[i] <= a - X:
            side = -1
        if not side:
            i += 1
            continue
        if min_abs_s > 0.0:
            sg = sig[i] if sig else float("nan")
            if not (sg == sg) or abs(X) / sg < min_abs_s:  # NaN-safe
                i += 1
                continue
        rail = a + X if side > 0 else a - X
        tp = rail - X if side > 0 else rail + X
        sl = rail + X if side > 0 else rail - X
        k, res = i + 1, "neither"
        while k < n:
            hit_tp = (low[k] <= tp) if side > 0 else (h[k] >= tp)
            hit_sl = (h[k] >= sl) if side > 0 else (low[k] <= sl)
            if hit_tp and hit_sl:
                break
            if hit_tp:
                res = "revert"
                break
            if hit_sl:
                res = "extend"
                break
            k += 1
        if res == "revert":
            rev += 1; holds.append(k - i)
        elif res == "extend":
            ext += 1; holds.append(k - i)
        else:
            neither += 1
        i = max(k, i + 1)
    return {"revert": rev, "extend": ext, "neither": neither, "holds": holds}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-02-01")
    ap.add_argument("--end", default="2026-08-07")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sessions = load_sessions(args.start, args.end)
    days = sorted({s["day"] for s in sessions})
    print(f"sessions={len(sessions)} days={len(days)} {days[0]} → {days[-1]}")
    print(f"每筆事件的期望值 = (2·p_revert − 1)·X − COST({COST_PTS} pts)\n")

    kinds = ["open_1m", "ema15", "sma30", "sma60", "vwap_session"]
    Xs = [10.0, 15.0, 25.0, 42.0]
    out: dict[str, Any] = {"schema": "tmf-anchor-choice-v1", "range": [days[0], days[-1]],
                           "cost_pts": COST_PTS, "results": {}}

    print("=== M14 錨點比較：對稱 ±X 首次通過的回歸率（null=50%）===")
    hdr = f"{'anchor':<14}" + "".join(f"{'X='+str(int(x)):>22}" for x in Xs)
    print(hdr)
    print(f"{'':<14}" + "".join(f"{'rev% / n / EV(pt)':>22}" for _ in Xs))
    print("-" * len(hdr))
    for kind in kinds:
        anc_by = {id(s): anchors(s, kind) for s in sessions}
        line = f"{kind:<14}"
        row: dict[str, Any] = {}
        for X in Xs:
            R = {"revert": 0, "extend": 0, "neither": 0, "holds": []}
            for s in sessions:
                r = race(s, anc_by[id(s)], X)
                R["revert"] += r["revert"]; R["extend"] += r["extend"]
                R["neither"] += r["neither"]; R["holds"] += r["holds"]
            dec = R["revert"] + R["extend"]
            if dec < 100:
                line += f"{'--':>22}"
                continue
            p = R["revert"] / dec
            ev = (2 * p - 1) * X - COST_PTS
            z = (p - 0.5) * math.sqrt(dec) / 0.5
            row[f"X{int(X)}"] = {
                "decided": dec, "revert_pct": round(100 * p, 1), "ev_pts": round(ev, 2),
                "z": round(z, 2), "median_hold_min": (sorted(R["holds"])[len(R["holds"]) // 2]
                                                      if R["holds"] else None),
            }
            line += f"{100*p:>7.1f}% {dec:>6} {ev:>+7.2f}"
        out["results"][kind] = row
        print(line)

    print("\n=== M15 Avellaneda-Lee 式 s-score 濾網（只在偏離 ≥ k·σ_resid 時才掛）===")
    print("   σ_resid = 過去 60 分 (price−anchor) 的標準差；k 為門檻")
    print(f"{'anchor':<14}{'X':>5}{'k':>6}{'decided':>9}{'revert%':>10}{'EV(pt)':>9}{'z':>8}")
    sfilt: dict[str, Any] = {}
    for kind in ("sma30", "sma60", "vwap_session"):
        for X in (25.0, 42.0):
            for k_thr in (0.0, 1.0, 1.5, 2.0):
                R = {"revert": 0, "extend": 0, "neither": 0, "holds": []}
                for s in sessions:
                    anc = anchors(s, kind)
                    sig = resid_sigma(s["c"], anc) if k_thr > 0 else None
                    r = race(s, anc, X, min_abs_s=k_thr, sig=sig)
                    R["revert"] += r["revert"]; R["extend"] += r["extend"]
                    R["holds"] += r["holds"]
                dec = R["revert"] + R["extend"]
                if dec < 200:
                    continue
                p = R["revert"] / dec
                ev = (2 * p - 1) * X - COST_PTS
                z = (p - 0.5) * math.sqrt(dec) / 0.5
                sfilt[f"{kind}_X{int(X)}_k{k_thr}"] = {
                    "decided": dec, "revert_pct": round(100 * p, 1),
                    "ev_pts": round(ev, 2), "z": round(z, 2)}
                print(f"{kind:<14}{X:>5.0f}{k_thr:>6.1f}{dec:>9}{100*p:>10.1f}{ev:>+9.2f}{z:>8.2f}")
    out["M15_sscore_filter"] = sfilt

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
