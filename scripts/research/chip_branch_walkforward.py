#!/usr/bin/env python3
"""籌碼四訊號 ＋ 分點「買賣家數差」的 walk-forward 增量驗證。

**為什麼加分點**：今天測過的所有評分改動（權重擬合、自適應死區、多日累積、
使用率交互、橫斷面排序、收縮）全部在 0.10~0.12%/日 的窄帶裡打轉，強烈暗示
限制在**資訊量**而非評分設計。分點資料完全不經過借券系統，是獨立資訊源。

**分點特徵**：``(買超家數 − 賣超家數) ÷ 有進出的分點家數``。
in-sample 測出**方向是反的**——買超家數越多、隔日超額越低（t=−7.11）。
經濟意義：買超家數多＝散戶分散進場；家數少但金額大＝主力集中進場。
「前 5 大買超÷量」「前 15 大買超÷量」「分點集中度」等常見的「主力買超」類
特徵**全部不顯著**（t 0.24~1.64），與本 repo 既有的「分點跟單全滅」一致。

**in-sample 結果**（2021-06~2026-08、652,535 個 stock-day）：
與四訊號相關僅 0.096；合併後價差 0.1164%→0.1266%、t 9.86→10.32；
控制籌碼分數後五個層級全部顯著（t 2.22~4.99）。增量檢定 t=1.98（壓線）。

⚠️ 樣本只有 5 年（分點資料 2021-06 起），是本輪所有測試中最短的。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def load(panel: Path, branch: Path) -> pd.DataFrame:
    b = pd.read_pickle(branch)
    d = pd.read_pickle(panel)
    d = d[d.trade_date >= "2021-06-01"]
    m = d.merge(b, on=["stock_id", "trade_date"], how="inner").dropna(subset=["fwd_ex"]).copy()
    m["brdiff"] = (m.n_buy_br - m.n_sell_br) / m.n_branches
    m["chip"] = m.S1 + m.S2 + m.S3 + m.S5
    # 分點三態：每日橫斷面五分位 → 最高分位（買超家數最多）判 +1 偏空
    m["S6"] = m.groupby("trade_date").brdiff.transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
    ).map({0: -1, 1: 0, 2: 0, 3: 0, 4: 1}).fillna(0)
    m["chip6"] = m.chip + m.S6
    return m


def spread(x: pd.DataFrame, col: str, thr: int) -> pd.Series:
    return x.groupby("trade_date").apply(
        lambda g: g[g[col] <= -thr].fwd_ex.mean() - g[g[col] >= thr].fwd_ex.mean(),
        include_groups=False).dropna()


def tstat(s: pd.Series) -> float:
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--branch", type=Path, default=Path("/tmp/branch_feat.pkl"))
    ap.add_argument("--form-days", type=int, default=250)
    ap.add_argument("--step-days", type=int, default=60)
    args = ap.parse_args()

    m = load(args.panel, args.branch)
    print(f"面板 {len(m):,} 個 stock-day · {m.stock_id.nunique():,} 檔 · "
          f"{m.trade_date.nunique():,} 日 · {m.trade_date.min()}~{m.trade_date.max()}")
    print(f"形成窗 {args.form_days} 日 · 每次前進 {args.step_days} 日\n")

    dates = np.sort(m.trade_date.unique())
    grid = [("chip", 2), ("chip6", 2), ("chip6", 3)]
    picks, new_leg, base_leg = [], [], []
    i = args.form_days
    while i < len(dates):
        form = m[m.trade_date.isin(dates[max(0, i - args.form_days):i])]
        test = m[m.trade_date.isin(dates[i:i + args.step_days])]
        if test.empty:
            break
        best, best_t = None, -np.inf
        for col, thr in grid:
            s = spread(form, col, thr)
            t = tstat(s)
            if np.isfinite(t) and t > best_t:
                best, best_t = (col, thr), t
        picks.append({"起": dates[i][:7], "選中": f"{best[0]}±{best[1]}",
                      "IS_t": round(best_t, 2)})
        new_leg.append(spread(test, best[0], best[1]))
        base_leg.append(spread(test, "chip", 2))
        i += args.step_days

    new = pd.concat(new_leg).sort_index()
    base = pd.concat(base_leg).sort_index()
    fixed = pd.concat([spread(m[m.trade_date.isin(dates[i:i + args.step_days])], "chip6", 2)
                       for i in range(args.form_days, len(dates), args.step_days)]).sort_index()
    pd.set_option("display.width", 200)
    print("=== 每期用形成期挑出的設定 ===")
    print(pd.DataFrame(picks).to_string(index=False))
    print(f"\n{'='*66}\n【OOS 累積】{len(base):,} 交易日 · {len(picks)} 期\n{'='*66}")
    for nm, s in (("四訊號（基準）", base), ("四訊號+分點 固定±2", fixed),
                  ("滾動挑設定", new)):
        print(f"  {nm:<20} 價差 {s.mean()*100:+.4f}%/日 · t = {tstat(s):+.2f} "
              f"· 年化 {s.mean()*252*100:+.2f}%")
    for nm, s in (("固定±2", fixed), ("滾動挑", new)):
        d_ = (s - base).dropna()
        print(f"\n  {nm} − 基準 逐日差 {d_.mean()*100:+.4f}%/日 · t = {tstat(d_):+.2f}")
    ok = tstat((fixed - base).dropna()) >= 2
    print(f"\n  判準（增量逐日差 t >= 2.0）→ {'通過' if ok else '未通過'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
