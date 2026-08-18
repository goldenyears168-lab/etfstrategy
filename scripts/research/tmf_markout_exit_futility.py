#!/usr/bin/env python3
"""Markout（逆選擇）＋ 出場規則徒勞性檢定 — 策略真實成交點的資訊含量。

Markout 是造市／被動掛單業界的標準診斷（Hasbrouck 2007；Cartea-Jaimungal-
Penalva ch.10）：成交後 h 分鐘，價格往你**不利**方向走了多少。造市商靠它把
「賺到價差」和「被知情單掃到」分開量。這個 repo 從來沒算過。

三個檢定：
  M16 markout 曲線：真實成交點在 +1/2/5/10/20/30/60 分的平均毛損益
  M17 隨機對照組：同樣時段、同樣筆數、隨機時點隨機方向的 markout
       —— 若兩條曲線重合，進場訊號的資訊含量就是零
  M18 出場規則掃描：對**同一組進場**掃 (停利, 停損, 最長持有) 網格
       —— Doob 最佳停止定理：若價格在該尺度上是鞅，任何停止規則都無法
          創造正期望值，只會留下成本。這是 struct_break 一百多個變體全滅
          的理論解釋，不是運氣不好。

進場一律取 fill_model="through"（真 tick、需被穿越才成交），這是本 repo 目前
最保守也最可信的成交假設。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
from copy import deepcopy
from pathlib import Path
from typing import Any

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import load_day
from tmf_channel.engine import load_vixtwn_delta, simulate
from tmf_channel.tick_index import available_days, build_tick_index

BAR_SOURCE = "tx_1m_tick_built_582d"
HORIZONS = [1, 2, 5, 10, 20, 30, 60]
COST_PTS = 3.0


def bars_db() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"


def bar_days() -> list[str]:
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (BAR_SOURCE,))]
    finally:
        con.close()


def arrays_for(day: str):
    rows = load_day(day, source=BAR_SOURCE)
    if not rows:
        return None
    return (
        [float(r["o"]) for r in rows], [float(r["h"]) for r in rows],
        [float(r["l"]) for r in rows], [float(r["c"]) for r in rows],
        [float(r.get("v") or 0) for r in rows],
        [f"{r['cal']}T{r['t']}:00+08:00" for r in rows],
        [str(r.get("sess") or "") for r in rows],
    )


def markout(C: list[float], eb: int, ep: float, side: str, h: int) -> float | None:
    """Signed gross P&L h bars after entry (positive = trade moved our way)."""
    j = eb + h
    if j >= len(C):
        return None
    return (C[j] - ep) if side == "L" else (ep - C[j])


def summarize_curve(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    out = {}
    for h in HORIZONS:
        xs = [r[f"m{h}"] for r in rows if r.get(f"m{h}") is not None]
        if len(xs) < 30:
            continue
        mean = st.mean(xs)
        se = st.stdev(xs) / (len(xs) ** 0.5) if len(xs) > 1 else float("nan")
        out[f"h{h}"] = {"n": len(xs), "mean": round(mean, 3), "se": round(se, 3),
                        "t": round(mean / se, 2) if se else None,
                        "win_pct": round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1)}
    return {"label": label, "curve": out}


def print_curve(c: dict[str, Any]) -> None:
    print(f"  {c['label']:<22}" + "".join(
        f"{c['curve'].get('h'+str(h), {}).get('mean', float('nan')):>9.3f}" for h in HORIZONS))
    print(f"  {'  (t-stat)':<22}" + "".join(
        f"{str(c['curve'].get('h'+str(h), {}).get('t', '--')):>9}" for h in HORIZONS))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260817)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    have = set(available_days())
    days = [d for d in bar_days() if d in have][-args.days:]
    vix = load_vixtwn_delta() or {}

    real: list[dict[str, Any]] = []
    ctrl: list[dict[str, Any]] = []
    entry_ctx: list[dict[str, Any]] = []

    for day in days:
        a = arrays_for(day)
        if a is None:
            continue
        O, H, L, C, V, T, SESS = a
        idx = build_tick_index(T)
        if idx is None:
            continue
        recipe = deepcopy(PAPER_RECIPE)
        recipe.update({"hang_anchor": "O", "eod_flatten": True,
                       "tick_native": True, "fill_model": "through"})
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix, tick_index=idx)
        for t in trades:
            eb, ep, side = int(t["eb"]), float(t["ep"]), str(t["s"])
            row = {"day": day, "eb": eb, "ep": ep, "s": side,
                   "sess": SESS[eb] if eb < len(SESS) else "", "pnl": float(t["pnl"])}
            for h in HORIZONS:
                row[f"m{h}"] = markout(C, eb, ep, side, h)
            real.append(row)
            entry_ctx.append({"day": day, "eb": eb, "s": side})
        # matched random control: same day, same number of entries, random
        # bar and random side — the only thing removed is the entry rule
        for _ in range(len(trades)):
            eb = rng.randrange(30, max(31, len(C) - max(HORIZONS) - 1))
            side = rng.choice(["L", "S"])
            row = {"day": day, "eb": eb, "ep": C[eb], "s": side, "sess": SESS[eb]}
            for h in HORIZONS:
                row[f"m{h}"] = markout(C, eb, C[eb], side, h)
            ctrl.append(row)

    if not real:
        print("no trades")
        return 1
    print(f"days={len(days)} real_entries={len(real)} control_entries={len(ctrl)}\n")

    print("=== M16/M17 Markout 曲線（點數，毛額、未扣成本；正值＝往有利方向）===")
    print(f"  {'horizon (min)':<22}" + "".join(f"{h:>9}" for h in HORIZONS))
    curves = [summarize_curve(real, "策略真實進場"),
              summarize_curve(ctrl, "隨機對照組")]
    for lab, sub in (("  ├ 多單", [r for r in real if r["s"] == "L"]),
                     ("  └ 空單", [r for r in real if r["s"] == "S"])):
        curves.append(summarize_curve(sub, lab))
    for c in curves:
        print_curve(c)

    print("\n=== M18 出場規則掃描（同一組進場，掃 TP/SL/最長持有）===")
    print("   淨值 = 毛額 − COST(3.0)；每格顯示 平均點/筆 (n)")
    grid_tp = [10, 15, 25, 40, 60]
    grid_sl = [15, 25, 40, 60, 150]
    maxhold = 38
    print(f"{'TP\\SL':<8}" + "".join(f"{sl:>14}" for sl in grid_sl))
    sweep: dict[str, Any] = {}
    day_arrays: dict[str, Any] = {}
    for day in days:
        a = arrays_for(day)
        if a is not None:
            day_arrays[day] = a
    for tp in grid_tp:
        line = f"{tp:<8}"
        for sl in grid_sl:
            pnls = []
            for r in real:
                a = day_arrays.get(r["day"])
                if a is None:
                    continue
                _O, Hh, Ll, Cc, _V, _T, _S = a
                eb, ep, side = r["eb"], r["ep"], r["s"]
                out_pnl = None
                for k in range(eb + 1, min(eb + maxhold + 1, len(Cc))):
                    if side == "L":
                        hit_tp, hit_sl = Hh[k] >= ep + tp, Ll[k] <= ep - sl
                    else:
                        hit_tp, hit_sl = Ll[k] <= ep - tp, Hh[k] >= ep + sl
                    if hit_tp and hit_sl:
                        out_pnl = -sl  # same bar → assume the bad one
                        break
                    if hit_tp:
                        out_pnl = tp
                        break
                    if hit_sl:
                        out_pnl = -sl
                        break
                if out_pnl is None:
                    k = min(eb + maxhold, len(Cc) - 1)
                    out_pnl = (Cc[k] - ep) if side == "L" else (ep - Cc[k])
                pnls.append(out_pnl - COST_PTS)
            if len(pnls) < 50:
                line += f"{'--':>14}"
                continue
            m = st.mean(pnls)
            sweep[f"tp{tp}_sl{sl}"] = {"n": len(pnls), "mean_net": round(m, 3)}
            line += f"{m:>9.2f}({len(pnls):>3})"
        print(line)

    best = max(sweep.items(), key=lambda kv: kv[1]["mean_net"]) if sweep else None
    if best:
        print(f"\n   最佳格 {best[0]}: {best[1]['mean_net']:+.2f} pts/筆 "
              f"(n={best[1]['n']})  ← 這是在同一份資料上事後挑最好的一格，"
              f"不是樣本外結果")

    # --- M19 決定性對照：同一張出場網格套在隨機進場上 -------------------------
    # 網格最佳解落在角落（TP 最大、SL 最小）時，最可能的解釋不是「找到好的出場
    # 規則」，而是「這個 bracket 形狀在這段價格過程上本來就會這樣」。唯一能分辨
    # 的方法是把同一張網格套在沒有任何訊號的隨機進場上：若數字一樣，那面網格
    # 量到的是價格過程的性質，跟策略的進場點無關。
    print("\n=== M19 對照：同一張出場網格 × 隨機進場 ===")
    print(f"{'TP\\SL':<8}" + "".join(f"{sl:>14}" for sl in grid_sl))
    ctrl_sweep: dict[str, Any] = {}
    for tp in grid_tp:
        line = f"{tp:<8}"
        for sl in grid_sl:
            pnls = []
            for r in ctrl:
                a = day_arrays.get(r["day"])
                if a is None:
                    continue
                _O, Hh, Ll, Cc, _V, _T, _S = a
                eb, ep, side = r["eb"], r["ep"], r["s"]
                out_pnl = None
                for k in range(eb + 1, min(eb + maxhold + 1, len(Cc))):
                    if side == "L":
                        hit_tp, hit_sl = Hh[k] >= ep + tp, Ll[k] <= ep - sl
                    else:
                        hit_tp, hit_sl = Ll[k] <= ep - tp, Hh[k] >= ep + sl
                    if hit_tp and hit_sl:
                        out_pnl = -sl
                        break
                    if hit_tp:
                        out_pnl = tp
                        break
                    if hit_sl:
                        out_pnl = -sl
                        break
                if out_pnl is None:
                    k = min(eb + maxhold, len(Cc) - 1)
                    out_pnl = (Cc[k] - ep) if side == "L" else (ep - Cc[k])
                pnls.append(out_pnl - COST_PTS)
            if len(pnls) < 50:
                line += f"{'--':>14}"
                continue
            m = st.mean(pnls)
            ctrl_sweep[f"tp{tp}_sl{sl}"] = {"n": len(pnls), "mean_net": round(m, 3)}
            line += f"{m:>9.2f}({len(pnls):>3})"
        print(line)
    if best and best[0] in ctrl_sweep:
        d = best[1]["mean_net"] - ctrl_sweep[best[0]]["mean_net"]
        print(f"\n   同一格 {best[0]} 隨機進場 = {ctrl_sweep[best[0]]['mean_net']:+.2f} pts/筆")
        print(f"   → 進場訊號真正貢獻 = {d:+.2f} pts/筆"
              f"（其餘來自 bracket 形狀本身，跟訊號無關）")

    # --- M20 方向對照：保留真實進場「時點」，只隨機化「方向」 -----------------
    # M19 的隨機對照同時打亂了時點與方向，所以那 +6.01 可能來自「挑對時機」
    # （例如只在波動剛擴張時進場）而不是「挑對方向」。這一組把時點原封不動留著，
    # 只把 L/S 擲硬幣決定：剩下的差額就是**方向**的貢獻。兩者的政策意涵完全
    # 不同——時點有用代表該保留通道觸發、換出場；方向有用才代表逆勢本身對。
    print("\n=== M20 方向對照：真實時點 + 隨機方向 ===")
    print(f"  {'horizon (min)':<22}" + "".join(f"{h:>9}" for h in HORIZONS))
    side_ctrl: list[dict[str, Any]] = []
    for r in real:
        a = day_arrays.get(r["day"])
        if a is None:
            continue
        Cc = a[3]
        side = rng.choice(["L", "S"])
        row = {"day": r["day"], "eb": r["eb"], "ep": r["ep"], "s": side}
        for h in HORIZONS:
            row[f"m{h}"] = markout(Cc, r["eb"], r["ep"], side, h)
        side_ctrl.append(row)
    c_side = summarize_curve(side_ctrl, "真實時點+隨機方向")
    print_curve(summarize_curve(real, "策略真實進場"))
    print_curve(c_side)
    curves.append(c_side)

    tp, sl = 60, 15
    pnls = []
    for r in side_ctrl:
        a = day_arrays.get(r["day"])
        if a is None:
            continue
        _O, Hh, Ll, Cc, _V, _T, _S = a
        eb, ep, side = r["eb"], r["ep"], r["s"]
        out_pnl = None
        for k in range(eb + 1, min(eb + maxhold + 1, len(Cc))):
            if side == "L":
                hit_tp, hit_sl = Hh[k] >= ep + tp, Ll[k] <= ep - sl
            else:
                hit_tp, hit_sl = Ll[k] <= ep - tp, Hh[k] >= ep + sl
            if hit_tp and hit_sl:
                out_pnl = -sl
                break
            if hit_tp:
                out_pnl = tp
                break
            if hit_sl:
                out_pnl = -sl
                break
        if out_pnl is None:
            k = min(eb + maxhold, len(Cc) - 1)
            out_pnl = (Cc[k] - ep) if side == "L" else (ep - Cc[k])
        pnls.append(out_pnl - COST_PTS)
    m_side = st.mean(pnls) if pnls else float("nan")
    print(f"\n   tp60_sl15 · 真實時點+隨機方向 = {m_side:+.2f} pts/筆 (n={len(pnls)})")
    if best:
        print(f"   真實進場 {best[1]['mean_net']:+.2f} − 隨機方向 {m_side:+.2f} "
              f"= {best[1]['mean_net'] - m_side:+.2f} pts/筆 ← 純粹來自「方向」的貢獻")
        print(f"   隨機方向 {m_side:+.2f} − 全隨機 {ctrl_sweep.get('tp60_sl15', {}).get('mean_net', float('nan')):+.2f} "
              f"= 純粹來自「時點」的貢獻")

    if args.out:
        payload = {"schema": "tmf-markout-exit-futility-v1", "days": len(days),
                   "n_real": len(real), "n_control": len(ctrl),
                   "cost_pts": COST_PTS, "curves": curves,
                   "exit_sweep": sweep, "exit_sweep_random_control": ctrl_sweep}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
