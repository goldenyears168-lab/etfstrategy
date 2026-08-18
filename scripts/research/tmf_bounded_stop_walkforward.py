#!/usr/bin/env python3
"""有界停損 · 三段 walk-forward（tick replay）+ 多重檢定校正。

動機（reports/research/channel_lab/tmf_markout_exit_futility.json）：
60 日 tick 回放的出場歸因顯示，除了 ``struct_break`` 以外每一條出場路徑都是
賺錢的，而 struct_break 一支 1637 筆、平均 −63.36 pts、合計 −103,717 pts，
把 +97,364 變成 −6,353。根因不是它「何時觸發」（那個方向已經試過約 120 個
變體全滅），而是它**觸發時虧損沒有上限**：live recipe 的 stop_pts=150 且
min_hold_before_stop=12，而 90% 的交易在 10 分鐘內就結束——絕大多數交易
從頭到尾都在沒有價格停損的狀態下運行，struct_break 就是事實上的唯一停損。

不需要改引擎：``stop_pts`` / ``min_hold_before_stop`` / ``struct_disabled``
都已經是 recipe 參數。本腳本只是把它們放進一個誠實的驗證框架。

框架設計（針對這個 repo 過去踩過的坑）：
  * 三段**時序不重疊**窗口 FIT → HOLDOUT → RECENT，全部 tick_native=True、
    fill_model="through"。2026-08-08 那次稽核就是敗在 1m-bar 成交假設 +
    同日 look-ahead。
  * 配對日差（paired daily delta vs 現行 live 設定）——同一天同一個市場，
    去掉市場本身的噪音，是檢定力最高的比較。
  * **Deflated Sharpe Ratio**（Bailey & López de Prado 2014）：把「我試了 N
    組參數」這件事代進去。單一 t 檢定在掃過 10 組之後已經沒有意義。
  * **PBO 式排名檢查**：FIT 最佳的那組，在 HOLDOUT / RECENT 的排名是否還在
    中位數以上。CELL_TUNE_V2 當年就是死在這一關。
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
from tmf_channel.cache_store import load_day
from tmf_channel.engine import load_vixtwn_delta, simulate
from tmf_channel import tick_index as _ti
from tmf_channel.tick_index import available_days, build_tick_index

BAR_SOURCE = "tx_1m_tick_built_582d"
EULER = 0.5772156649015329

# live 現行值：stop_pts=150 · min_hold_before_stop=12 · struct 開著
CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("live_baseline", {}),
    ("stop60_hold0", {"stop_pts": 60.0, "min_hold_before_stop": 0}),
    ("stop40_hold0", {"stop_pts": 40.0, "min_hold_before_stop": 0}),
    ("stop25_hold0", {"stop_pts": 25.0, "min_hold_before_stop": 0}),
    ("stop15_hold0", {"stop_pts": 15.0, "min_hold_before_stop": 0}),
    ("stop40_hold3", {"stop_pts": 40.0, "min_hold_before_stop": 3}),
    ("stop25_hold3", {"stop_pts": 25.0, "min_hold_before_stop": 3}),
    ("structoff", {"struct_disabled": True}),
    ("structoff_stop40", {"struct_disabled": True, "stop_pts": 40.0, "min_hold_before_stop": 0}),
    ("structoff_stop25", {"struct_disabled": True, "stop_pts": 25.0, "min_hold_before_stop": 0}),
]


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


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Acklam-style rational approximation; plenty accurate for DSR."""
    if not 0.0 < p < 1.0:
        return float("nan")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def sharpe_moments(xs: list[float]) -> tuple[float, float, float]:
    """(SR per observation, skew, kurtosis) — DSR needs the higher moments."""
    n = len(xs)
    m = st.mean(xs)
    s = st.stdev(xs) if n > 1 else 0.0
    if s <= 0:
        return 0.0, 0.0, 3.0
    z = [(x - m) / s for x in xs]
    g3 = sum(v ** 3 for v in z) / n
    g4 = sum(v ** 4 for v in z) / n
    return m / s, g3, g4


def deflated_sharpe(xs: list[float], all_trial_srs: list[float]) -> dict[str, Any]:
    """Bailey & López de Prado (2014). SR0 = expected max SR under the null
    given N independent trials with the observed cross-trial SR variance."""
    n = len(xs)
    sr, g3, g4 = sharpe_moments(xs)
    N = max(2, len(all_trial_srs))
    var_sr = st.variance(all_trial_srs) if len(all_trial_srs) > 1 else 0.0
    sr0 = math.sqrt(var_sr) * (
        (1 - EULER) * norm_ppf(1 - 1.0 / N) + EULER * norm_ppf(1 - 1.0 / (N * math.e))
    )
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr ** 2
    if denom <= 0 or n < 3:
        return {"sr": round(sr, 4), "sr0": round(sr0, 4), "dsr": None}
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return {"sr": round(sr, 4), "sr0_expected_max_under_null": round(sr0, 4),
            "n_trials": N, "dsr": round(norm_cdf(z), 4)}


def t_stat(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    s = st.stdev(xs)
    return round(st.mean(xs) / (s / math.sqrt(len(xs))), 2) if s > 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fit", type=int, default=120)
    ap.add_argument("--holdout", type=int, default=60)
    ap.add_argument("--recent", type=int, default=60)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    days = overlap_days()
    n_r, n_h, n_f = args.recent, args.holdout, args.fit
    windows = {
        "FIT": days[-(n_r + n_h + n_f):-(n_r + n_h)],
        "HOLDOUT": days[-(n_r + n_h):-n_r],
        "RECENT": days[-n_r:],
    }
    for k, v in windows.items():
        print(f"{k:<9}{len(v):>4} days  {v[0]} → {v[-1]}")
    print(f"\nconfigs={len(CONFIGS)} · tick_native=True · fill_model=through\n")

    vix = load_vixtwn_delta() or {}
    # per-window per-config per-day net points
    daily: dict[str, dict[str, dict[str, float]]] = {w: {c: {} for c, _ in CONFIGS} for w in windows}

    for wname, wdays in windows.items():
        for i, day in enumerate(wdays, 1):
            # Build per day and DROP immediately. Caching every day's tick
            # index kept ~160k floats/day alive; over a 120-day window that is
            # several GB of Python float objects and the first run of this
            # script died partway through FIT because of it.
            arr = arrays_for(day)
            if arr is None:
                continue
            idx = build_tick_index(arr[5])
            if idx is None:
                continue
            (O, H, L, C, V, T) = arr
            for cname, ov in CONFIGS:
                r = deepcopy(PAPER_RECIPE)
                r.update({"hang_anchor": "O", "eod_flatten": True,
                          "tick_native": True, "fill_model": "through"})
                r.update(ov)
                trades, *_ = simulate(O, H, L, C, V, T, r, vix_delta=vix, tick_index=idx)
                daily[wname][cname][day] = round(sum(float(t["pnl"]) for t in trades), 1)
            del arr, idx
            _ti._load_raw.cache_clear()
            if i % 20 == 0:
                print(f"  [{wname}] {i}/{len(wdays)} …", flush=True)

    # ---- per-window stats ----
    print("\n=== 三段 walk-forward · 每日淨點數（pts/day）===")
    hdr = f"{'config':<20}" + "".join(f"{w:>26}" for w in windows)
    print(hdr)
    print(f"{'':<20}" + "".join(f"{'mean (t)  Δvs base (t)':>26}" for _ in windows))
    print("-" * len(hdr))
    stats: dict[str, Any] = {}
    for cname, _ in CONFIGS:
        line = f"{cname:<20}"
        stats[cname] = {}
        for wname in windows:
            xs = [daily[wname][cname][d] for d in sorted(daily[wname][cname])]
            base = [daily[wname]["live_baseline"][d] for d in sorted(daily[wname][cname])]
            delta = [a - b for a, b in zip(xs, base)]
            row = {"days": len(xs), "mean": round(st.mean(xs), 1) if xs else None,
                   "t": t_stat(xs),
                   "delta_mean": round(st.mean(delta), 1) if delta else None,
                   "delta_t": t_stat(delta) if cname != "live_baseline" else None,
                   "win_days_pct": round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1) if xs else None}
            stats[cname][wname] = row
            dt = "" if cname == "live_baseline" else f" {row['delta_mean']:+7.1f}({row['delta_t']})"
            line += f"{row['mean']:>10.1f}({row['t']}){dt:>14}"
        print(line)

    # ---- Deflated Sharpe on each window ----
    print("\n=== Deflated Sharpe Ratio（Bailey & López de Prado）· N=%d 組試驗 ===" % len(CONFIGS))
    print("   DSR = 「這個 SR 不是 N 次搜尋的運氣」的機率。<0.95 = 不足以宣稱有效")
    print(f"{'config':<20}" + "".join(f"{w+' SR / DSR':>24}" for w in windows))
    for wname in windows:
        srs = []
        for cname, _ in CONFIGS:
            xs = [daily[wname][cname][d] for d in sorted(daily[wname][cname])]
            srs.append(sharpe_moments(xs)[0] if xs else 0.0)
        for cname, _ in CONFIGS:
            xs = [daily[wname][cname][d] for d in sorted(daily[wname][cname])]
            stats[cname][wname]["dsr"] = deflated_sharpe(xs, srs) if xs else None
    for cname, _ in CONFIGS:
        line = f"{cname:<20}"
        for wname in windows:
            d = stats[cname][wname].get("dsr") or {}
            line += f"{str(d.get('sr')):>12}/{str(d.get('dsr')):>11}"
        print(line)

    # ---- PBO-style rank consistency ----
    print("\n=== PBO 式排名一致性：FIT 最佳的組，在樣本外還排第幾？ ===")
    def ranks(wname: str) -> dict[str, int]:
        order = sorted(CONFIGS, key=lambda c: -st.mean(
            [daily[wname][c[0]][d] for d in sorted(daily[wname][c[0]])] or [0]))
        return {c[0]: i + 1 for i, c in enumerate(order)}
    r_fit, r_hold, r_rec = ranks("FIT"), ranks("HOLDOUT"), ranks("RECENT")
    fit_best = min(r_fit, key=lambda k: r_fit[k])
    med = (len(CONFIGS) + 1) / 2
    print(f"{'config':<20}{'FIT rank':>10}{'HOLDOUT':>10}{'RECENT':>10}{'OOS 中位數以上?':>18}")
    for cname, _ in CONFIGS:
        ok = "是" if (r_hold[cname] <= med and r_rec[cname] <= med) else "否"
        mark = "  ← FIT 最佳" if cname == fit_best else ""
        print(f"{cname:<20}{r_fit[cname]:>10}{r_hold[cname]:>10}{r_rec[cname]:>10}{ok:>18}{mark}")
    print(f"\n   FIT 最佳 = {fit_best}；OOS 排名 HOLDOUT={r_hold[fit_best]}, RECENT={r_rec[fit_best]} "
          f"(N={len(CONFIGS)}, 隨機期望={med:.1f})")

    if args.out:
        payload = {"schema": "tmf-bounded-stop-walkforward-v1",
                   "windows": {k: [v[0], v[-1], len(v)] for k, v in windows.items()},
                   "configs": {c: o for c, o in CONFIGS},
                   "stats": stats,
                   "ranks": {"FIT": r_fit, "HOLDOUT": r_hold, "RECENT": r_rec},
                   "fit_best": fit_best,
                   "daily": daily}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
