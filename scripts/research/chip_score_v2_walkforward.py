#!/usr/bin/env python3
"""籌碼評分 v2（個股自適應 + 死區）的 walk-forward 驗證。

對應 topic ``chip-signal-daily-horizon`` 的 ``H-CHIP-V2-ADAPTIVE``（跑數前已登錄）。

**v1 的兩個缺陷**（使用者指出）：
1. 未考慮個股相對尺度——借券餘額變動 500 張，對群創（餘額 79.9 萬張）是雜訊、
   對嘉基（餘額 404 張）是巨變，v1 一律給 ±1
2. S1／S3／S5 沒有中性區，強迫每天表態。實測 v1 的 Δ券源使用率只有 **8.9%**
   落在中性（連續變數的日變化幾乎不可能剛好為零）

**v2 的三個改動**
* S1／S3 改用「相對該股自身近 60 日」的 z 值，``|z| < k`` 判為中性
* S5 改用自身近 60 筆費率分位；**當日無借券成交 → 中性**（v1 錯算成偏多）
* 門檻 k 在 walk-forward 中**只用形成期資料挑**，不看未來

**為什麼要 walk-forward**：全期 in-sample 已跑過 10 組門檻，k=0.5 是比較後選的，
有挑選偏誤。本腳本讓每個評估期的 k 由前一段資料決定，檢定這個挑選會不會泛化。

⚠️ 中性帶的超額不會是精確 0——超額每日去均值，三帶加權和恆為 0，看空帶較
極端而看多帶較溫和，中性帶必須微正才配平。那是恆等式，判讀時視為零。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def add_adaptive(d: pd.DataFrame, win: int = 60, fee_win: int = 60) -> pd.DataFrame:
    d = d.sort_values(["stock_id", "trade_date"]).copy()
    g = d.groupby("stock_id", group_keys=False)
    for src, dst in (("d_sbl", "z_dsbl"), ("d_util", "z_dutil")):
        mu = g[src].transform(lambda x: x.rolling(win, min_periods=30).mean())
        sd = g[src].transform(lambda x: x.rolling(win, min_periods=30).std())
        d[dst] = (d[src] - mu) / sd.replace(0, np.nan)
    d["fee_pct"] = g.fee_rate_vw.transform(
        lambda x: x.rolling(fee_win, min_periods=10).rank(pct=True))
    d["pct_rank2"] = g.sbl_pct.transform(
        lambda s: s.rolling(243, min_periods=60).rank(pct=True))
    return d


def _band(v, lo, hi):
    return np.where(v >= hi, 1, np.where(v <= lo, -1, 0))


def score_v2(d: pd.DataFrame, k: float, pct: tuple[float, float],
             fee: tuple[float, float]) -> np.ndarray:
    s1 = _band(d.z_dsbl.fillna(0), -k, k)
    s2 = _band(d.pct_rank2.fillna(0.5), pct[0], pct[1])
    s3 = _band(d.z_dutil.fillna(0), -k, k)
    s5 = np.where(d.fee_pct.isna(), 0, _band(d.fee_pct.fillna(0.5), fee[0], fee[1]))
    return s1 + s2 + s3 + s5


def daily_spread(x: pd.DataFrame, col: str) -> pd.Series:
    return x.groupby("trade_date").apply(
        lambda g: g[g[col] <= -2].fwd_ex.mean() - g[g[col] >= 2].fwd_ex.mean(),
        include_groups=False).dropna()


def tstat(s: pd.Series) -> float:
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--form-days", type=int, default=500)
    ap.add_argument("--step-days", type=int, default=125)
    args = ap.parse_args()

    d = add_adaptive(pd.read_pickle(args.panel)).dropna(subset=["fwd_ex"])
    d["v1"] = d.S1 + d.S2 + d.S3 + d.S5
    grid = [(k, p, f) for k in (0.3, 0.5, 0.8, 1.0)
            for p in ((0.2, 0.8), (0.3, 0.7))
            for f in ((0.35, 0.65), (0.25, 0.75))]
    for i, (k, p, f) in enumerate(grid):
        d[f"g{i}"] = score_v2(d, k, p, f)
    print(f"面板 {len(d):,} 個 stock-day · {d.trade_date.nunique():,} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()} · 網格 {len(grid)} 組\n")

    dates = np.sort(d.trade_date.unique())
    picks, v2_leg, v1_leg = [], [], []
    i = args.form_days
    while i < len(dates):
        form = d[d.trade_date.isin(dates[max(0, i - args.form_days):i])]
        test = d[d.trade_date.isin(dates[i:i + args.step_days])]
        if test.empty:
            break
        best, best_t = None, -np.inf
        for j in range(len(grid)):
            s = daily_spread(form, f"g{j}")
            t = tstat(s) if len(s) > 30 else np.nan
            if np.isfinite(t) and t > best_t:
                best, best_t = j, t
        if best is None:
            i += args.step_days
            continue
        k, p, f = grid[best]
        picks.append({"起": dates[i][:7], "k": k, "pct": f"{p[0]}/{p[1]}",
                      "fee": f"{f[0]}/{f[1]}", "IS_t": round(best_t, 2)})
        v2_leg.append(daily_spread(test, f"g{best}"))
        v1_leg.append(daily_spread(test, "v1"))
        i += args.step_days

    v2 = pd.concat(v2_leg).sort_index()
    v1 = pd.concat(v1_leg).sort_index()
    pd.set_option("display.width", 200)
    print("=== 每期用形成期挑出的門檻 ===")
    print(pd.DataFrame(picks).to_string(index=False))
    print(f"\n{'='*66}\n【OOS 累積對照】{len(v2):,} 交易日 · {len(picks)} 期\n{'='*66}")
    for nm, s in (("v1 現況（二元）", v1), ("v2 自適應＋死區", v2)):
        print(f"  {nm:<18} 多空價差 {s.mean()*100:+.4f}%/日 · t = {tstat(s):+.2f} "
              f"· 年化 {s.mean()*252*100:+.2f}%")
    diff = (v2 - v1).dropna()
    print(f"\n  v2 − v1 逐日差 {diff.mean()*100:+.4f}%/日 · t = {tstat(diff):+.2f}")
    print(f"  判準（v2 的 t 不低於 v1）→ "
          f"{'通過' if tstat(v2) >= tstat(v1) else '未通過'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
