#!/usr/bin/env python3
"""同業相對基準的 +18.43%/年：是選股還是押科技？

背景：改用同業相對後名單科技權重 94.6%（宇宙 75.3%），+19.3pp。先前只驗到
「對各檔自己的產業為基準仍有 +31.28% t=+1.81」，但那是把兩群混在一起算的。
本檔把科技與非科技**分開各自成名單、各自對自己那群的基準**，直接回答：

  · 非科技組還在  → 是選股（機制普遍適用）
  · 只剩科技組    → 是產業押注（換個環境就死）

⚠️ 非科技只佔宇宙約 25%，樣本較小、檢定力較低，t 值天生會比科技組小。
   要看的是**點估計的方向與量級**，不是只看有沒有過 1.96。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

CR = SourceFileLoader("cr", str(Path(__file__).resolve().parent / "chip_brief_control_rerun.py")).load_module()
LAB, HZ, COST = CR.LAB, CR.HZ, CR.COST
K = 5
CTL = ("vol60", "gap", "mcap", "turn", "close")
TECH = {"半導體業", "電子工業", "光電業", "電子零組件業", "其他電子業",
        "電腦及週邊設備業", "通信網路業", "電子通路業", "資訊服務業"}
FRAC = 0.064          # 對齊簡報：前 30/467 ≈ 6.4%


def legs(d: pd.DataFrame, s: pd.Series, rcol: str, frac: float, min_n: int) -> pd.DataFrame:
    """名單在傳入的宇宙內形成，基準＝同一宇宙的等權。"""
    x = d.assign(_s=s).dropna(subset=["_s", rcol])
    dates = np.sort(x.trade_date.unique())
    oos = set(dates[250:])
    hist, rows = [], []
    for t, g in x.groupby("trade_date", sort=True):
        if len(g) < min_n:
            continue
        n = max(3, int(round(len(g) * frac)))
        q = g.sort_values("_s")
        L = set(q.stock_id.head(n))
        tk = len(L - hist[-K]) / n if len(hist) >= K else np.nan
        hist.append(L)
        if t in oos:
            rows.append({"t": t, "lg": q[rcol].head(n).mean(),
                         "bm": g[rcol].mean(), "tk": tk, "n": n})
    return pd.DataFrame(rows).dropna()


def report(r: pd.DataFrame, tag: str) -> None:
    if len(r) < 100:
        print(f"  {tag:<16}樣本不足（{len(r)} 日）")
        return
    ex = r.lg - r.bm
    gr = ex.mean() * 100
    tau = r.tk.mean() / K
    net = (gr / K - tau * COST) * 242
    t_nw = HZ.nw_t(ex, K)
    r = r.assign(y=pd.to_datetime(r.t).dt.year)
    yr = "  ".join(f"{y}:{g.lg.sub(g.bm).mean()*100/K*242:+.1f}%" for y, g in r.groupby("y"))
    print(f"  {tag:<16}平均 {r.n.mean():>4.0f} 檔　超額 {gr:>+8.4f}%/趟　NW t {t_nw:>+5.2f}"
          f"　淨/年 {net:>+7.2f}%　{yr}")


def main() -> int:
    d = pd.read_pickle(LAB.DIR / "chip_frames_panel.pkl")
    d = d.loc[:, ~d.columns.duplicated()].copy()
    d = d.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    g = d.groupby("stock_id")
    d["oc5"] = g["close"].shift(-K) / g["open"].shift(-1) - 1
    d = d.dropna(subset=["sbl_pct", "ret_pct", "oc5"])
    d["_n"] = CR.neutral(d, "oc5", CTL)
    d["tech"] = d["ind"].isin(TECH)
    print(f"宇宙 {d.stock_id.nunique()} 檔　科技 {d.tech.mean()*100:.1f}%"
          f"　{d.trade_date.min()} → {d.trade_date.max()}\n")

    print("【分組各自成名單、各自對自己那群的基準 · K=5 · 最嚴控制】")
    for basis, bn in (("mkt", "舊基準"), ("ind", "新基準")):
        print(f"  ── {bn} ──")
        report(legs(d, CR.score(d, basis), "_n", FRAC, 120), "全宇宙")
        for flag, tag in ((True, "科技組"), (False, "非科技組")):
            sub = d[d.tech == flag].copy()
            report(legs(sub, CR.score(sub, basis), "_n", FRAC, 40), tag)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
