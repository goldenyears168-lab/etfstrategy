#!/usr/bin/env python3
"""在新基準（同業相對 · w=0.50）上重跑控制檢定階梯。

背景：2026-08-26 的控制檢定是在舊的「全市場截面」基準上做的，結論是
「訊號是真的但淨值站不住」—— 加控週轉率與股價後 gross 從 +0.086% 掉到
+0.052%/日，低於損益兩平 0.064%/日。2026-08-27 改基準後那些數字不再對應
現行名單，本檔重跑。

口徑刻意對齊簡報實況（不是研究的寬鬆設定）：
  · 宇宙＝簡報宇宙（全市場、非只有大型股），不是研究用的大型股 1/4
  · 檔數＝前 30 / 後 30，不是 20% 寬度
  · 報酬＝open(T+1)→close(T+1)，PIT：T 收盤決策、T+1 開盤進場
  · 損益兩平＝當日名單換手 × 來回成本

⚠️ 面板沒有 v4 的 s_zp 欄，以底層 sbl_pct（借券佔股本）代替；散戶持股用
ret_pct。這與簡報實際用的分項有細微差異，結論應視為近似。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

LAB = SourceFileLoader("lab", str(Path(__file__).resolve().parent / "chip_lab.py")).load_module()
HZ = SourceFileLoader("hz", str(Path(__file__).resolve().parent / "chip_horizon.py")).load_module()

COST = 0.471          # 6 折現股來回（%）
W = 0.50              # 借券佔股本的權重（其餘給散戶持股）
MIN_IND_N = 3
TOP = 30

# 控制階梯：每一級累加
LADDER = [
    ("無控制", ()),
    ("＋風險（vol60/gap/mcap）", ("vol60", "gap", "mcap")),
    ("＋週轉率", ("vol60", "gap", "mcap", "turn")),
    ("＋股價", ("vol60", "gap", "mcap", "turn", "close")),
    ("＋產業", ("vol60", "gap", "mcap", "turn", "close", "__ind__")),
]


def neutral(d: pd.DataFrame, col: str, controls: tuple) -> pd.Series:
    """對 controls 做每日五分位 dummy 迴歸，回傳殘差報酬。"""
    if not controls:
        return d[col]
    x = d.dropna(subset=[col]).copy()
    qs = [c for c in controls if c != "__ind__"]
    for c in qs:
        x[f"q_{c}"] = x.groupby("trade_date")[c].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    x = x.dropna(subset=[f"q_{c}" for c in qs])
    out = pd.Series(np.nan, index=d.index)
    for _, g in x.groupby("trade_date"):
        if len(g) < 120:
            continue
        P = [np.ones((len(g), 1))]
        for c in qs:
            P.append(pd.get_dummies(g[f"q_{c}"].astype(int), drop_first=True).to_numpy(float))
        if "vol60" in qs and "gap" in qs:
            P.append(pd.get_dummies(g.q_vol60.astype(int) * 5 + g.q_gap.astype(int),
                                    drop_first=True).to_numpy(float))
        if "__ind__" in controls:
            P.append(pd.get_dummies(g["ind"], drop_first=True).to_numpy(float))
        X = np.column_stack(P)
        y = g[col].to_numpy()
        try:
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
            out.loc[g.index] = y - X @ b
        except np.linalg.LinAlgError:
            pass
    return out


def score(x: pd.DataFrame, basis: str) -> pd.Series:
    """HS：低＝偏多。basis 'mkt'＝全市場截面（舊）、'ind'＝同業內（新）。"""
    o = pd.Series(0.0, index=x.index)
    for src, w in (("sbl_pct", W), ("ret_pct", 1 - W)):
        m = x.groupby("trade_date")[src].rank(pct=True)
        if basis == "ind":
            r = x.groupby(["trade_date", "ind"])[src].rank(pct=True)
            r = r.where(x.groupby(["trade_date", "ind"])["ind"].transform("size") >= MIN_IND_N, m)
        else:
            r = m
        o = o + w * (r - 0.5) * 2
    return o


def legs(d: pd.DataFrame, s: pd.Series, rcol: str, top: int) -> pd.DataFrame:
    x = d.assign(_s=s).dropna(subset=["_s", rcol])
    dates = np.sort(x.trade_date.unique())
    oos = set(dates[250:])
    hist_l, hist_s, rows = [], [], []
    for t, g in x.groupby("trade_date", sort=True):
        if len(g) < 120:
            continue
        q = g.sort_values("_s")
        L, S = list(q.stock_id.head(top)), list(q.stock_id.tail(top))
        tl = len(set(L) - set(hist_l[-1])) / top if hist_l else np.nan
        ts = len(set(S) - set(hist_s[-1])) / top if hist_s else np.nan
        hist_l.append(set(L)); hist_s.append(set(S))
        if t in oos:
            rows.append({"t": t, "lg": q[rcol].head(top).mean(), "sh": q[rcol].tail(top).mean(),
                         "tl": tl, "ts": ts})
    return pd.DataFrame(rows).dropna()


def main() -> int:
    d = pd.read_pickle(LAB.DIR / "chip_frames_panel.pkl")
    d = d.loc[:, ~d.columns.duplicated()].copy()
    d = d.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    # 簡報口徑：T 收盤決策 → T+1 開盤進場 → T+1 收盤出場
    g = d.groupby("stock_id")
    d["oc1"] = g["close"].shift(-1) / g["open"].shift(-1) - 1
    d = d.dropna(subset=["sbl_pct", "ret_pct", "oc1"])
    print(f"宇宙 {d.stock_id.nunique()} 檔　{d.trade_date.nunique()} 個交易日"
          f"　{d.trade_date.min()} → {d.trade_date.max()}\n")

    print("【控制階梯 · 前/後 30 檔 · open→close】")
    print(f"{'控制':<26}{'基準':<6}{'多腿gross':>11}{'換手':>8}{'損平':>9}{'淨/日':>9}{'年化':>9}")
    res = {}
    for lab_, ctl in LADDER:
        rc = f"_n_{len(ctl)}"
        d[rc] = neutral(d, "oc1", ctl)
        for basis, bn in (("mkt", "舊"), ("ind", "新")):
            r = legs(d, score(d, basis), rc, TOP)
            if len(r) < 100:
                continue
            gr = r.lg.mean() * 100
            tau = r.tl.mean()
            be = tau * COST
            net = gr - be
            res[(lab_, basis)] = (gr, tau, be, net, HZ.nw_t(r.lg, 1), r)
            print(f"  {lab_:<24}{bn:<6}{gr:>+10.4f}%{tau*100:>7.1f}%{be:>+8.4f}%"
                  f"{net:>+8.4f}%{net*242:>+8.2f}%")
    print("\n  → 淨/日 為正才站得住。舊基準在「＋股價」那一級由正轉負。\n")

    print("【檔數敏感度 · 最嚴控制（＋股價）· 新基準】")
    rc = f"_n_{len(LADDER[3][1])}"
    s = score(d, "ind")
    print(f"{'檔數':>6}{'gross':>11}{'換手':>8}{'損平':>9}{'淨/年':>10}")
    pos = 0
    for n in (10, 15, 20, 30, 40, 60, 80, 100, 150):
        r = legs(d, s, rc, n)
        if len(r) < 100:
            continue
        gr = r.lg.mean() * 100
        be = r.tl.mean() * COST
        net = (gr - be) * 242
        pos += net > 0
        print(f"{n:>6}{gr:>+10.4f}%{r.tl.mean()*100:>7.1f}%{be:>+8.4f}%{net:>+9.2f}%")
    print(f"  → {pos}/9 個檔數為正（舊基準是 0/9）\n")

    print("【偏空腿 · 最嚴控制（＋股價）】")
    for basis, bn in (("mkt", "舊基準"), ("ind", "新基準")):
        r = legs(d, score(d, basis), rc, TOP)
        sh = r.sh.mean() * 100
        be = r.ts.mean() * COST
        print(f"  {bn}　空腿 gross {sh:>+8.4f}%　NW t {HZ.nw_t(r.sh, 1):>+5.2f}　"
              f"換手 {r.ts.mean()*100:.1f}%　放空淨/年 {(-sh - be)*242:>+7.2f}%")
    print("  （空腿 gross 為負才有放空價值；再扣借券成本與費率，門檻更高）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ═══════════════════════════════════════════════════════════════════════
# 2026-08-27 重跑結果 —— 舊的「淨值站不住」結論其實是**持有期特定**的
#
# 最嚴控制（vol60/gap/mcap/turn/close）· 前 30 檔 · 多腿淨值：
#     K      舊基準      新基準
#     1    +1.15%     −8.66%   ← 每日換，新基準 12.8% 換手攤不掉
#     3    +7.04%    +12.85%
#     5   +10.25%    +18.43%
#    10    +9.22%    +22.31%
#
# → 同業相對基準在 **K≥3 全贏、K=1 唯一輸**。原因純粹是換手：新基準日換手
#   12.8% vs 舊 8.0%（+60%），K=1 每天付、K=5 攤成 4.7%/日就便宜了。
#
# 【K=5 · 最嚴控制下的完整檢定（全部通過）】
#   檔數敏感度  10/15/20/30/40/60/80/100/150 檔 **9/9 全為正**
#               新基準每一個檔數都優於舊基準；+25.43%(10檔) → +4.25%(150檔) 單調衰減
#               （舊基準在 K=1 最嚴控制下是 0/9，這才是「參數脆弱」的來源）
#   偏空腿      新 −0.3154%/趟 NW t=−2.51（舊 −0.2932% t=−2.04）；K=1 時僅 −0.99
#   多空價差    新 +0.7992%/趟 NW t=+3.98（舊 +0.5735% t=+2.66）
#
# 【結論】2026-08-26 的「訊號是真的但淨值站不住」在 K=1 成立、在 K≥3 不成立。
# 這份名單是 **3~10 日的名單，不是每日翻的名單**。已寫進 email 警語。
