#!/usr/bin/env python3
"""附錄：把「區塊層 ICpos 加權」× 「尾端放大程度（γ / clip）」交叉，
因為主表顯示 (a) 加權才把換手壓下來 (b) γ=2 一致優於 γ=1。全部格子都報。"""
import sys, time, itertools
sys.argv = ['x']
exec(open('/Users/jackm4/goldenstocks/scripts/research/chip_logodds_combine.py').read().split('if __name__')[0])
import numpy as np, pandas as pd

print(lab.HEADER, flush=True)
ALLB = ["A", "A2", "A3", "B", "B2", "C", "C2", "D", "E", "F"]
BIC = block_ic(ALLB)
BW = wf_weights(BIC, "icpos")

def wnorm(sub):
    W = BW[sub].div(BW[sub].sum(axis=1).replace(0, np.nan), axis=0).fillna(1.0/len(sub))
    return W

def comb_pow_w(names, gamma, W):
    S = pd.DataFrame({n: BS(n) for n in names})
    Z = np.sign(S) * S.abs() ** gamma
    Wd = pd.DataFrame(W[names].reindex(didx).values, index=d.index, columns=names)
    Z = Z * Wd
    den = Wd.where(Z.notna()).sum(axis=1)
    return Z.sum(axis=1, min_count=1) / den.replace(0, np.nan)

SETS = (["A3","E"], ["A3","B","E","F"], ["A3","B","C","E","F"], ["A2","B","C","E","F"], ["A3","C","E"])
print("\n== A1. 區塊 ICpos 加權 × γ 尾端放大 ==", flush=True)
for sub in SETS:
    W = wnorm(sub)
    print(f"  [權重 {'+'.join(sub)}] OOS 均值 = "
          f"{W.iloc[FORM:].mean().round(3).to_dict()}", flush=True)
    for gm in (0.5, 1.0, 2.0, 3.0, 5.0):
        run(f"W×gamma={gm:<4} {'+'.join(sub)}", comb_pow_w(sub, gm, W))

print("\n== A2. 區塊 ICpos 加權 × logodds clip ==", flush=True)
for sub in SETS:
    W = wnorm(sub)
    for cl in (0.001, 0.02, 0.05, 0.15):
        run(f"W×clip={cl:<5} {'+'.join(sub)}", combine(sub, "logodds", clip=cl, wblock=W))

pd.DataFrame(RES).to_csv(DIR / "logodds_combine_addendum.csv", index=False)
print(f"\n寫出 addendum 共 {len(RES)} 組態 · {time.time()-t0:.0f}s", flush=True)
