#!/usr/bin/env python3
"""籌碼評分：收縮式連續分數 vs 二元／三態 —— walk-forward 驗證。

**設計來源**（使用者提出）：v1 每個訊號只給 ±1，等於丟掉「變動多大」的資訊；
v2 的死區又開太大（27% 的分數被壓成零）。中間應該存在一個「保留連續梯度、
但只把很小的變動歸零」的設計。

**三種收縮方式**（z 為個股自適應標準化後的訊號值）
* ``tern``  三態：|z|>=k → ±1，否則 0                   （v2）
* ``soft``  軟收縮：sign(z)·max(0,|z|−k)                 （所有值都減 k）
* ``hard``  硬收縮：z if |z|>=k else 0                   （只砍小值，大值不動）

**In-sample 探索結果**（2011-2026，統一取每日前後 20%）：
hard k=0.15/0.2 t=16.52 > 純連續 k=0 的 16.45 > v1 二元 15.84 > v2 三態 13.12~14.74；
且每一組 hard 都優於 soft——soft 會把資訊最多的極端值也一起縮小。
最佳「分數=0」佔比約 5~6%，不是 v2 的 27%。

⚠️ 上述為 in-sample。本腳本用滾動窗**只以形成期資料挑 (方式, k)**，檢定該挑選
是否泛化。今天已兩次見到 in-sample 優勢在 walk-forward 消失（v2 的「+32%」實為
比較口徑造成的假象；9914 配對 t=5.03→0.18）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

Z_COLS = ["z1", "zp", "zu1", "zf"]


def prepare(panel: Path, start: str = "2011-01-01") -> pd.DataFrame:
    d = pd.read_pickle(panel)
    d = d[d.trade_date >= start].sort_values(["stock_id", "trade_date"]).copy()
    g = d.groupby("stock_id", group_keys=False)

    def zs(col, win=60):
        mu = g[col].transform(lambda x: x.rolling(win, min_periods=30).mean())
        sd = g[col].transform(lambda x: x.rolling(win, min_periods=30).std())
        return (d[col] - mu) / sd.replace(0, np.nan)

    d["z1"] = zs("d_sbl")
    d["zu1"] = zs("d_util")
    fee_pct = g.fee_rate_vw.transform(lambda x: x.rolling(60, min_periods=10).rank(pct=True))
    pct2 = g.sbl_pct.transform(lambda s: s.rolling(243, min_periods=60).rank(pct=True))
    d["zf"] = ((fee_pct - 0.5) * 4)
    d["zp"] = ((pct2 - 0.5) * 4)
    for c in Z_COLS:
        d[c] = d[c].fillna(0).clip(-2.5, 2.5)
    d["v1"] = d.S1 + d.S2 + d.S3 + d.S5
    return d.dropna(subset=["fwd_ex"])


def shrink(v: np.ndarray, k: float, how: str) -> np.ndarray:
    if how == "tern":
        return np.where(v >= k, 1.0, np.where(v <= -k, -1.0, 0.0))
    if how == "soft":
        return np.sign(v) * np.maximum(np.abs(v) - k, 0)
    return np.where(np.abs(v) >= k, v, 0.0)          # hard


def spread(x: pd.DataFrame, col: str) -> pd.Series:
    q = x.groupby("trade_date")[col].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    return x.assign(q=q).groupby("trade_date").apply(
        lambda g: g[g.q == 0].fwd_ex.mean() - g[g.q == 4].fwd_ex.mean(),
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

    d = prepare(args.panel)
    grid = [(how, k) for how in ("hard", "soft", "tern")
            for k in (0.0, 0.1, 0.15, 0.2, 0.3, 0.5) if not (how != "hard" and k == 0.0)]
    for i, (how, k) in enumerate(grid):
        d[f"c{i}"] = sum(shrink(d[c].to_numpy(), k, how) for c in Z_COLS)
    print(f"面板 {len(d):,} 個 stock-day · {d.trade_date.nunique():,} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()} · 網格 {len(grid)} 組\n")

    dates = np.sort(d.trade_date.unique())
    picks, new_leg, v1_leg = [], [], []
    i = args.form_days
    while i < len(dates):
        form = d[d.trade_date.isin(dates[max(0, i - args.form_days):i])]
        test = d[d.trade_date.isin(dates[i:i + args.step_days])]
        if test.empty:
            break
        best, best_t = None, -np.inf
        for j in range(len(grid)):
            s = spread(form, f"c{j}")
            t = tstat(s)
            if np.isfinite(t) and t > best_t:
                best, best_t = j, t
        how, k = grid[best]
        picks.append({"起": dates[i][:7], "方式": how, "k": k, "IS_t": round(best_t, 2)})
        new_leg.append(spread(test, f"c{best}"))
        v1_leg.append(spread(test, "v1"))
        i += args.step_days

    new = pd.concat(new_leg).sort_index()
    v1 = pd.concat(v1_leg).sort_index()
    pd.set_option("display.width", 200)
    print("=== 每期用形成期挑出的 (方式, k) ===")
    print(pd.DataFrame(picks).to_string(index=False))
    print(f"\n{'='*64}\n【OOS 累積】{len(new):,} 交易日 · {len(picks)} 期\n{'='*64}")
    for nm, s in (("v1 二元（基準）", v1), ("收縮式（滾動挑）", new)):
        print(f"  {nm:<18} 價差 {s.mean()*100:+.4f}%/日 · t = {tstat(s):+.2f} "
              f"· 年化 {s.mean()*252*100:+.2f}%")
    diff = (new - v1).dropna()
    print(f"\n  收縮式 − v1 逐日差 {diff.mean()*100:+.4f}%/日 · t = {tstat(diff):+.2f}")
    print(f"  判準（t 不低於 v1 且逐日差顯著為正）→ "
          f"{'通過' if tstat(new) >= tstat(v1) and tstat(diff) >= 2 else '未通過'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
