#!/usr/bin/env python3
"""符號穩定性矩陣 + 三因子等權組合（chip-orthogonal-rebuild 事後探索）.

⚠️ 本檔是 2026-08-27 預註記格子跑完後的追加分析（負號校正檢定與三因子公式），
非預註記假說；結論僅供 G2 前瞻驗證清單使用，不得當作已驗證宣稱。
用法： PYTHONPATH=src .venv/bin/python scripts/research/chip_rebuild_sign_combo.py
"""
import pandas as pd
import numpy as np

d = pd.read_pickle("reports/research/chip-orthogonal-rebuild/panel.pkl")
d = d[d.in_universe.astype(bool)].copy()
d["yr"] = d.trade_date.str[:4]
d["half"] = np.where(d.trade_date < "2025-09-01", "前半(24/07~25/08)", "後半(25/09~26/08)")

def spread(x, f, r):
    x = x.dropna(subset=[f, r])
    if len(x) < 3000: return None, None
    x = x.copy()
    x["q"] = x.groupby("trade_date")[f].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    day = x.groupby(["trade_date", "q"])[r].mean().unstack()
    if 4 not in day or 0 not in day: return None, None
    sp = (day[4] - day[0]).dropna()
    return sp.mean()*100, sp.mean()/sp.std()*np.sqrt(len(sp))

rows = []
for f in ("z1","zp","z6","retail","margin","inst"):
    for r,lab in (("r_oc","開→收(可執行)"),("r_cc","收→收(偷看)")):
        row = {"因子": f, "口徑": lab}
        m,t = spread(d, f, r); row["全期"] = f"{m:+.3f}({t:+.1f})" if m is not None else "—"
        for h,x in d.groupby("half"):
            m,t = spread(x, f, r)
            row[h] = f"{m:+.3f}({t:+.1f})" if m is not None else "—"
        rows.append(row)
pd.set_option("display.width", 200)
print(pd.DataFrame(rows).to_string(index=False))
print("\n格式：Q5−Q1 %/日（plain t）。同因子四格同號=方向穩定；異號=不准掛負號。")

print()
d = pd.read_pickle("reports/research/chip-orthogonal-rebuild/panel.pkl")
d = d[d.in_universe.astype(bool)].copy()

def xrank(s):  # 當日橫斷面 rank 0~1
    return s.groupby(d.trade_date).transform(lambda x: x.rank(pct=True))

# 偏空分數：z1 高=偏空、retail 高=偏空、margin 高=偏多(取負) —— 正號校正
d["rk_z1"], d["rk_rt"], d["rk_mg"] = xrank(d.z1), xrank(d.retail), xrank(-d.margin)

def spread(x, col, r="r_oc", label=""):
    x = x.dropna(subset=[col, r]).copy()
    x["q"] = x.groupby("trade_date")[col].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    day = x.groupby(["trade_date","q"])[r].mean().unstack()
    sp = (day[0] - day[4]).dropna()          # 偏多(Q1) − 偏空(Q5)，正=公式方向對
    n = x.groupby("trade_date").size().mean()
    # NW lag5
    e = sp - sp.mean(); T = len(sp)
    v = e.var(ddof=1) + 2*sum((1-l/6)*e.autocorr(l)*e.var(ddof=1) for l in range(1,6) if T>l)
    t = sp.mean()/np.sqrt(v/T)
    h1, h2 = sp[sp.index < "2025-09-01"], sp[sp.index >= "2025-09-01"]
    print(f"{label:34s} {sp.mean()*100:+.4f}%/日  NW t={t:+.2f}  days={T}  檔/日={n:.0f}  "
          f"前半{h1.mean()*100:+.3f} 後半{h2.mean()*100:+.3f}")
    return sp

print("=== 多空價差（偏多Q1 − 偏空Q5，開→收可執行口徑）===")
# 單因子基準
spread(d.assign(c=d.rk_z1), "c", label="z1 單獨")
spread(d.assign(c=d.rk_rt), "c", label="retail 單獨")
spread(d.assign(c=d.rk_mg), "c", label="margin 單獨(正號校正)")
# 兩因子（既有推薦）
d["c2"] = d[["rk_z1","rk_rt"]].mean(axis=1, skipna=False)
spread(d, "c2", label="等權 z1+retail（現行推薦）")
# 三因子 a: 全宇宙，>=2 個非NaN 就算（margin 缺值不排除該股）
d["c3a"] = d[["rk_z1","rk_rt","rk_mg"]].mean(axis=1)
d.loc[d[["rk_z1","rk_rt","rk_mg"]].notna().sum(axis=1) < 2, "c3a"] = np.nan
spread(d, "c3a", label="三因子等權（>=2因子可用，全宇宙）")
# 三因子 b: 三者齊備的交集（誠實面對 margin 覆蓋 ~197 檔）
m3 = d.dropna(subset=["rk_z1","rk_rt","rk_mg"])
spread(m3.assign(c=m3[["rk_z1","rk_rt","rk_mg"]].mean(axis=1)), "c", label="三因子等權（三者齊備交集）")
# 同交集上的兩因子對照（公平比較）
spread(m3.assign(c=m3[["rk_z1","rk_rt"]].mean(axis=1)), "c", label="  對照：同交集上 z1+retail")
